# -*- coding: utf-8 -*-
"""Tipo `imagem`: cópia bit a bit de uma origem (arquivo, nesta fase) para um arquivo de imagem, com mapfile.

A origem só é aberta com os.O_RDONLY. O mapfile `<destino>.mapa.json` é uma lista de faixas
`[inicio, fim, estado]` (fim exclusivo; estado "ok", "ruim" ou "pendente"), cobrindo o arquivo todo
sem buracos, no espírito do ddrescue. O arquivo do mapa é `{"versao", "origem", "tamanho", "faixas"}`: a
origem (caminho resolvido) e o tamanho são conferidos na retomada. Retomar lê o mapfile e só lê as faixas pendentes.
"""
import hashlib
import json
import os
import re
import secrets
import time

from .. import config, leitor, unidade
from ..executor import ErroJob
from ..leitor import ErroLeitor

O_BINARY = getattr(os, 'O_BINARY', 0)
BLOCO_PADRAO = 1024 * 1024
BLOCO_MIN = 4096
BLOCO_MAX = 64 * 1024 * 1024
TENTATIVAS_OPTICO = 3          # padrão de `tentativas` quando a origem é CD/DVD/Blu-ray
TENTATIVAS_PADRAO = 1          # opção `tentativas`: leituras de cada setor ruim na segunda passada
MAX_TENTATIVAS = 20
SEGUIDOS_PARA_PULAR = 3        # blocos com erro em sequência até começar a pular áreas
TETO_PULO = 1 << 30
SETOR = 512
FASES = ('copia', 'retorno', 'bissecao', 'repasse', 'fim')
MAX_EVENTOS_DE_FAIXA = 50
BLOCO_HASH = 4 * 1024 * 1024
ESTADOS_FAIXA = ('ok', 'ruim', 'pendente', 'suspeito', 'pulado')     # suspeito = bloco que falhou, a bisseccionar; pulado = área pulada, a voltar
DONO = 'recdados'            # marca no mapa: este programa criou esta recuperação
MODO_PRIVADO = 0o600          # imagem e mapa só do dono (no Windows o arquivo herda as permissões da pasta)
NOMES_RESERVADOS = frozenset(['CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$']
                             + ['COM%d' % i for i in range(1, 10)] + ['LPT%d' % i for i in range(1, 10)])


def caminho_mapa(destino):
    return destino + '.mapa.json'


def _normal(caminho):
    if leitor.eh_dispositivo(caminho):
        return caminho.upper().replace('/', '\\') if caminho.startswith('\\\\') else caminho    # disco: sem realpath (abriria o dispositivo)
    return os.path.normcase(os.path.realpath(caminho))


def bloco_de(opcoes):
    return (opcoes or {}).get('bloco', BLOCO_PADRAO)


def opcoes_de(opcoes):
    o = opcoes or {}
    return {'bloco': bloco_de(o), 'tentativas': o.get('tentativas', TENTATIVAS_PADRAO),
            'limite_velocidade': o.get('limite_velocidade', 0), 'pular': o.get('pular', True)}


def caminho_proibido(caminho):
    """Frase de erro para caminho de rede, disco bruto ou nome de dispositivo do Windows; None se está ok."""
    if caminho.startswith(('\\\\.\\', '\\\\?\\', '//./', '//?/')):
        return 'Disco bruto e caminhos de dispositivo não são aceitos nesta versão.'
    if caminho.startswith(('\\\\', '//')):
        return 'Caminhos de rede (\\\\servidor\\...) não são aceitos nesta versão. Use uma unidade do computador.'
    for parte in re.split(r'[\\/]+', caminho):
        nome = parte.rstrip(' .').split('.')[0].upper()
        if nome in NOMES_RESERVADOS:
            return 'O nome "%s" é reservado pelo Windows (dispositivo) e não pode ser usado.' % parte
    return None


def validar(origem, destino, opcoes, retomada=False):
    """Devolve a frase de erro (português) ou None se está tudo certo."""
    if not isinstance(origem, str) or not origem.strip() or '\x00' in origem:
        return 'Informe a origem: o caminho do arquivo a copiar.'
    if not isinstance(destino, str) or not destino.strip() or '\x00' in destino:
        return 'Informe o destino: o caminho do arquivo de imagem a criar.'
    if opcoes is not None and not isinstance(opcoes, dict):
        return 'As opções precisam ser um objeto.'
    bloco = bloco_de(opcoes)
    if type(bloco) is not int or not BLOCO_MIN <= bloco <= BLOCO_MAX:
        return 'O tamanho do bloco precisa ser um número entre %d e %d bytes.' % (BLOCO_MIN, BLOCO_MAX)
    o = opcoes_de(opcoes)
    if type(o['tentativas']) is not int or not 0 <= o['tentativas'] <= MAX_TENTATIVAS:
        return 'As tentativas precisam ser um número inteiro de 0 a %d.' % MAX_TENTATIVAS
    if isinstance(o['limite_velocidade'], bool) or not isinstance(o['limite_velocidade'], (int, float)) \
            or not 0 <= o['limite_velocidade'] <= 1e12:
        return 'O limite de velocidade precisa ser um número de bytes por segundo (0 = sem limite).'
    if type(o['pular']) is not bool:
        return 'A opção "pular" precisa ser verdadeiro ou falso.'
    erro = caminho_proibido(destino)
    if erro:
        return erro
    if leitor.eh_dispositivo(origem):
        return _validar_destino_de_disco(origem, destino, retomada)
    erro = caminho_proibido(origem)
    if erro:
        return erro
    if not os.path.isfile(origem):
        return 'A origem não existe ou não é um arquivo nem um disco.'
    if _normal(origem) == _normal(destino):
        return 'O destino não pode ser a própria origem.'
    if os.path.exists(destino):
        try:
            if os.path.samefile(origem, destino):
                return 'O destino não pode ser a própria origem.'
        except OSError:
            pass
    o, d = _normal(origem), _normal(destino)
    try:
        if os.path.commonpath([o, d]) == o:
            return 'O destino não pode ficar dentro da origem.'
    except ValueError:
        pass                                   # unidades diferentes
    pasta = os.path.dirname(os.path.abspath(destino))
    if not os.path.isdir(pasta):
        return 'A pasta do destino não existe.'
    if not retomada:
        if os.path.exists(destino):
            return 'Já existe um arquivo no destino. Escolha outro nome para não sobrescrever nada.'
        if os.path.exists(caminho_mapa(destino)):
            return 'Já existe um mapa de recuperação para esse destino. Escolha outro nome.'
    return None


def _validar_destino_de_disco(origem, destino, retomada):
    """Origem é disco ou volume: não toca no caminho do disco (nem stat), só confere o destino."""
    pasta = os.path.dirname(os.path.abspath(destino))
    if not os.path.isdir(pasta):
        return 'A pasta do destino não existe.'
    if not leitor.eh_optico(origem):                  # CD/DVD é só leitura: o destino nunca cai nele
        erro = unidade.conferir(origem, destino)      # nunca gravar no disco que está sendo lido
        if erro:
            return erro
    if not retomada:
        if os.path.exists(destino):
            return 'Já existe um arquivo no destino. Escolha outro nome para não sobrescrever nada.'
        if os.path.exists(caminho_mapa(destino)):
            return 'Já existe um mapa de recuperação para esse destino. Escolha outro nome.'
    return None


# --- mapfile -------------------------------------------------------------
def marcar(faixas, ini, fim, estado):
    """Marca [ini, fim) com `estado`, cortando as faixas que atravessa, e junta vizinhas iguais."""
    novo, posto = [], False
    for a, b, e in faixas:
        if b <= ini or a >= fim:
            if a >= fim and not posto:
                novo.append([ini, fim, estado])
                posto = True
            novo.append([a, b, e])
            continue
        if a < ini:
            novo.append([a, ini, e])
        if not posto:
            novo.append([ini, fim, estado])
            posto = True
        if b > fim:
            novo.append([fim, b, e])
    if not posto:
        novo.append([ini, fim, estado])
    juntas = []
    for f in novo:
        if juntas and juntas[-1][2] == f[2] and juntas[-1][1] == f[0]:
            juntas[-1][1] = f[1]
        else:
            juntas.append(f)
    faixas[:] = juntas


def somar(faixas, estado):
    return sum(b - a for a, b, e in faixas if e == estado)


def setores_ruins(faixas, setor=SETOR):
    """Setores (não blocos) que falharam: os já confirmados ruins e os suspeitos ainda por bisseccionar."""
    return sum(-(-(b - a) // setor) for a, b, e in faixas if e in ('ruim', 'suspeito'))


def primeira(faixas, estado):
    for a, b, e in faixas:
        if e == estado:
            return a, b
    return None


def primeira_pendente(faixas):
    return primeira(faixas, 'pendente')


def resumir_mapa(destino, maximo=2000):
    """Lê o mapa da imagem e devolve as faixas resumidas para o painel desenhar a barra de setores.

    Estados do painel: ok, ruim, pendente (suspeito conta como ruim; pulado, como pendente). Vizinhas do mesmo estado
    são juntadas; se ainda passar de `maximo`, o arquivo é dividido em `maximo` pedaços iguais e cada pedaço fica com o
    pior estado que contém (ruim > pendente > ok), então setor ruim nunca some da barra. None se ainda não há mapa."""
    try:
        with open(caminho_mapa(destino), 'r', encoding='utf-8') as f:
            conteudo = json.load(f)
    except (OSError, ValueError):
        return None
    faixas = conteudo.get('faixas') if isinstance(conteudo, dict) else None
    tamanho = conteudo.get('tamanho') if isinstance(conteudo, dict) else None
    if not isinstance(faixas, list) or type(tamanho) is not int:
        return None
    traducao = {'ok': 'ok', 'ruim': 'ruim', 'suspeito': 'ruim', 'pendente': 'pendente', 'pulado': 'pendente'}
    simples, totais = [], {'ok': 0, 'ruim': 0, 'pendente': 0}
    for a, b, e in faixas:
        e = traducao.get(e, 'pendente')
        totais[e] += b - a
        if simples and simples[-1][2] == e:
            simples[-1][1] = b
        else:
            simples.append([a, b, e])
    resumido = len(simples) > maximo
    if resumido:
        passo = -(-tamanho // maximo)
        peso = {'ok': 0, 'pendente': 1, 'ruim': 2}
        baldes, i = [], 0
        for k in range(maximo):
            ini, fim = k * passo, min(tamanho, (k + 1) * passo)
            if ini >= fim:
                break
            pior = 'ok'
            while i < len(simples) and simples[i][1] <= ini:
                i += 1
            j = i
            while j < len(simples) and simples[j][0] < fim:
                if peso[simples[j][2]] > peso[pior]:
                    pior = simples[j][2]
                j += 1
            if baldes and baldes[-1][2] == pior:
                baldes[-1][1] = fim
            else:
                baldes.append([ini, fim, pior])
        simples = baldes
    return {'tamanho': tamanho, 'totais': totais, 'resumido': resumido,
            'faixas': [{'inicio': a, 'fim': b, 'estado': e, 'fracao': (b - a) / tamanho if tamanho else 0.0} for a, b, e in simples]}


def fase_do_mapa(caminho):
    try:
        with open(caminho, 'r', encoding='utf-8') as f:
            fase = json.load(f).get('fase', 'copia')
    except (OSError, ValueError, AttributeError):
        return 'copia'
    return fase if fase in FASES else 'copia'


def salvar_mapa(caminho, faixas, origem=None, tamanho=None, etapa='pronto', fase='copia'):
    """Grava o mapa de forma atômica. O temporário tem nome aleatório e é criado com O_EXCL: nunca sobrescreve
    um arquivo que já exista e não seja nosso."""
    tmp = '%s.%s.tmp' % (caminho, secrets.token_hex(4))
    conteudo = {'versao': 1, 'dono': DONO, 'etapa': etapa, 'fase': fase, 'origem': origem, 'tamanho': tamanho, 'faixas': faixas}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, MODO_PRIVADO)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(conteudo, f)
            f.flush()
            os.fsync(f.fileno())
        for tentativa in range(100):                  # no Windows o painel lendo o mapa pode segurar o arquivo por um instante
            try:
                os.replace(tmp, caminho)
                break
            except PermissionError:
                if tentativa == 99:
                    raise
                time.sleep(0.02)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def carregar_mapa(caminho, tamanho, origem=None, completo=False):
    """Lê e confere o mapa. Se `origem` vier, tem de ser a mesma da primeira vez (senão: ErroJob, sem tocar em nada)."""
    try:
        with open(caminho, 'r', encoding='utf-8') as f:
            conteudo = json.load(f)
    except (OSError, ValueError):
        raise ErroJob('Não consegui ler o mapa desta imagem. Ele pode estar danificado.') from None
    if not isinstance(conteudo, dict):
        raise ErroJob('O mapa desta imagem está danificado.')
    faixas = conteudo.get('faixas')
    if conteudo.get('dono') != DONO:
        raise ErroJob('Este mapa não foi criado por este programa. Por segurança, nada foi alterado.')
    if origem is not None and conteudo.get('origem') != origem:
        raise ErroJob('Esta imagem foi começada com outra origem. Por segurança, nada foi alterado.')
    if conteudo.get('tamanho') != tamanho:
        raise ErroJob('A origem mudou de tamanho desde que a imagem foi começada. Por segurança, nada foi alterado.')
    pos = 0
    ok = isinstance(faixas, list)
    for f in faixas if ok else ():
        if not (isinstance(f, list) and len(f) == 3 and type(f[0]) is int and type(f[1]) is int
                and f[2] in ESTADOS_FAIXA and f[0] == pos and f[1] > f[0]):
            ok = False
            break
        pos = f[1]
    if not ok or pos != tamanho:
        raise ErroJob('O mapa desta imagem não confere com a origem (o arquivo mudou de tamanho ou o mapa está danificado).')
    if completo:
        return faixas, conteudo.get('etapa', 'pronto')
    return faixas


# --- leitura e escrita ----------------------------------------------------
def _ler_bloco(origem_aberta, pos, n):
    """Lê n bytes a partir de pos (menos só no fim). Ponto único de leitura da origem (arquivo, disco ou volume)."""
    return origem_aberta.ler(pos, n)


def _escrever(fd, pos, dados):
    os.lseek(fd, pos, os.SEEK_SET)
    visto = memoryview(dados)
    while visto:
        visto = visto[os.write(fd, visto):]


def _sha256(aberto, ctrl):
    """SHA-256 do que o leitor lê (do começo ao fim), ou 'pausado'/'cancelado' se pediram para parar."""
    h = hashlib.sha256()
    pos = 0
    while True:
        if ctrl.cancelamento_pedido():
            return 'cancelado'
        if ctrl.pausa_pedida():
            return 'pausado'
        b = _ler_bloco(aberto, pos, BLOCO_HASH)
        if not b:
            return h.hexdigest()
        h.update(b)
        pos += len(b)


def _criar_destino(destino, mapa, tamanho, remover_mapa):
    """Cria a imagem (O_EXCL: nunca toca em arquivo que já exista) e dá o tamanho. Se um arquivo alheio apareceu
    no lugar, apaga o mapa que acabamos de criar (senão ele faria o arquivo alheio parecer nosso)."""
    try:
        fd = os.open(destino, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, MODO_PRIVADO)
    except FileExistsError:
        if remover_mapa:
            try:
                os.remove(mapa)
            except OSError:
                pass
        raise ErroJob('Apareceu um arquivo no destino enquanto a imagem era preparada. '
                      'Por segurança, nada foi sobrescrito. Escolha outro nome.') from None
    os.ftruncate(fd, tamanho)
    return fd


def executar(job, ctrl):
    origem, destino = job['origem'], job['destino']
    bloco = bloco_de(job.get('opcoes'))
    mapa = caminho_mapa(destino)
    tem_dest, tem_mapa = os.path.exists(destino), os.path.exists(mapa)
    if tem_dest and not tem_mapa:
        raise ErroJob('Já existe um arquivo no destino que não é de uma recuperação deste programa. '
                      'Por segurança, nada foi sobrescrito. Escolha outro nome.')
    retomada = tem_mapa                         # mapa sem destino = queda antes de criar a imagem (ver abaixo)
    erro = validar(origem, destino, job.get('opcoes'), retomada=retomada)
    if erro:
        raise ErroJob(erro)

    try:
        fd_o = leitor.abrir(origem)                         # única abertura da origem para cópia: só leitura
    except ErroLeitor as e:
        raise ErroJob(str(e)) from None
    fd_d = None
    registro = None
    try:
        tamanho = fd_o.tamanho
        origem_real = _normal(origem)
        if retomada:
            faixas, etapa = carregar_mapa(mapa, tamanho, origem_real, completo=True)
            if etapa == 'criando':
                # Caiu antes de a imagem ficar pronta: nada foi copiado. Só aceita destino ausente ou vazio (criado por nós).
                if not tem_dest:
                    fd_d = _criar_destino(destino, mapa, tamanho, remover_mapa=False)
                elif os.path.getsize(destino) == 0:
                    fd_d = os.open(destino, os.O_WRONLY | O_BINARY)
                    os.ftruncate(fd_d, tamanho)
                else:
                    raise ErroJob('O destino já existe e não dá para confirmar que foi criado por este programa. '
                                  'Por segurança, nada foi alterado. Escolha outro nome.')
                os.fsync(fd_d)
                salvar_mapa(mapa, faixas, origem_real, tamanho)
                ctrl.evento('Imagem preparada de novo depois de uma queda: %d bytes.' % tamanho)
            else:
                if not tem_dest:
                    raise ErroJob('A imagem desta recuperação não está mais no destino (foi apagada?). '
                                  'Nada foi recriado, para não perder o que já tinha sido copiado.')
                if os.path.getsize(destino) != tamanho:
                    raise ErroJob('A imagem parcial não tem o tamanho esperado. Nada foi alterado.')
                fd_d = os.open(destino, os.O_WRONLY | O_BINARY)
            ctrl.evento('Retomando: %d bytes já prontos, %d ainda por ler.'
                        % (somar(faixas, 'ok') + somar(faixas, 'ruim'), somar(faixas, 'pendente')))
        else:
            faixas = [[0, tamanho, 'pendente']] if tamanho else []
            salvar_mapa(mapa, faixas, origem_real, tamanho, etapa='criando')      # o mapa nasce ANTES da imagem
            fd_d = _criar_destino(destino, mapa, tamanho, remover_mapa=True)
            os.fsync(fd_d)
            salvar_mapa(mapa, faixas, origem_real, tamanho)
            ctrl.evento('Imagem criada: %d bytes, blocos de %d.' % (tamanho, bloco))

        op = opcoes_de(job.get('opcoes'))
        tentativas, limite, pular = op['tentativas'], float(op['limite_velocidade']), op['pular']
        if leitor.eh_optico(origem):
            if 'tentativas' not in (job.get('opcoes') or {}):
                tentativas = TENTATIVAS_OPTICO              # mídia riscada: vale insistir mais nos setores ruins
            if not retomada:
                ctrl.evento('CD/DVD/Blu-ray: a unidade tenta de novo sozinha nos trechos riscados, então a leitura '
                            'pode demorar bastante (horas, em disco muito danificado). Pode pausar e retomar.')
        setor = max(1, getattr(fd_o, 'setor', SETOR))
        est = {'fase': fase_do_mapa(mapa) if retomada else 'copia', 'lidos': 0}
        t0 = time.monotonic()
        teto = min(64 * bloco, TETO_PULO)
        atraso = config.atraso_bloco()
        registro = open(config.log_leituras(), 'a', encoding='ascii', buffering=1) if config.log_leituras() else None

        def salvar():
            os.fsync(fd_d)                  # os dados vão para o disco antes do mapa que diz que estão prontos
            salvar_mapa(mapa, faixas, origem_real, tamanho, fase=est['fase'])

        def andamento(texto=None):
            ctrl.progresso(somar(faixas, 'ok') + somar(faixas, 'ruim'), tamanho, texto, setores_ruins(faixas, setor))

        def parar():
            return 'cancelado' if ctrl.cancelamento_pedido() else 'pausado' if ctrl.pausa_pedida() else None

        def esperar_limite():
            if limite > 0:
                while not parar():
                    falta = est['lidos'] / limite - (time.monotonic() - t0)
                    if falta <= 0:
                        break
                    time.sleep(min(falta, 0.1))
            if atraso:
                time.sleep(atraso)

        def tentar(ini, fim, vezes=1):
            """Lê [ini, fim) até `vezes` vezes. Devolve os bytes, ou None se todas falharem."""
            for _ in range(max(1, vezes)):
                if registro:
                    registro.write('%d %d\n' % (ini, fim - ini))      # só para os testes provarem o que foi lido
                est['lidos'] += fim - ini
                try:
                    dados = _ler_bloco(fd_o, ini, fim - ini)
                    if len(dados) == fim - ini:
                        return dados
                except OSError:
                    pass
            return None

        def gravar(ini, fim, dados):
            _escrever(fd_d, ini, dados)
            marcar(faixas, ini, fim, 'ok')

        def passo(n):
            andamento()
            ctrl.checkpoint(salvar, bytes_novos=n)
            esperar_limite()

        def passada_de_copia(pode_pular):
            """Blocos grandes, do começo ao fim. Bloco com erro vira `suspeito` (sem zerar nada); depois de erros seguidos
            pula áreas cada vez maiores (marcadas `pulado`) e volta a elas depois."""
            seguidos, salto = 0, bloco
            while True:
                p = parar()
                if p:
                    return p
                prox = primeira(faixas, 'pendente')
                if prox is None:
                    return None
                ini = prox[0]
                fim = min(prox[1], ini + bloco)
                dados = tentar(ini, fim)
                if dados is not None:
                    gravar(ini, fim, dados)
                    seguidos, salto = 0, bloco
                else:
                    marcar(faixas, ini, fim, 'suspeito')
                    seguidos += 1
                    if pode_pular and seguidos >= SEGUIDOS_PARA_PULAR:
                        for a, b, e in faixas:
                            if a == fim and e == 'pendente':
                                marcar(faixas, fim, min(b, fim + salto), 'pulado')
                                salto = min(salto * 2, teto)
                                break
                passo(fim - ini)

        def passada_de_retorno():
            for a, b, e in [f for f in faixas if f[2] == 'pulado']:
                marcar(faixas, a, b, 'pendente')
            return passada_de_copia(False)

        def passada_de_bisseccao():
            """Dentro de cada bloco suspeito, lê em metades até o tamanho do setor: só o que falha vira `ruim`.
            O que ainda não foi resolvido continua `suspeito` no mapa, então pausar, cair e retomar só repete leituras
            de trecho que falhou, nunca de trecho `ok`."""
            while True:
                prox = primeira(faixas, 'suspeito')
                if prox is None:
                    return None
                pilha = [prox]
                while pilha:
                    p = parar()
                    if p:
                        return p
                    a, b = pilha.pop()
                    if b - a <= setor:
                        # todo trecho da pilha já falhou inteiro: com um setor só, é um setor ruim (sem reler)
                        _escrever(fd_d, a, bytes(b - a))
                        marcar(faixas, a, b, 'ruim')
                    else:
                        meio = a + max(setor, (b - a) // 2 // setor * setor)
                        falhou = []
                        for x, y in ((a, meio), (meio, b)):
                            dados = tentar(x, y)
                            if dados is None:
                                falhou.append((x, y))
                            else:
                                gravar(x, y, dados)
                        pilha.extend(reversed(falhou))            # a metade da frente sai primeiro
                    passo(b - a)

        def passada_de_repasse():
            """Segunda passada: os setores ruins, do fim para o começo, com `tentativas` leituras cada um."""
            recuperados = 0
            for a, b, e in [f for f in reversed(faixas) if f[2] == 'ruim']:
                x = a + (b - 1 - a) // setor * setor
                while x >= a:
                    p = parar()
                    if p:
                        return p
                    y = min(x + setor, b)
                    dados = tentar(x, y, tentativas)
                    if dados is not None:
                        gravar(x, y, dados)
                        recuperados += y - x
                    passo(y - x)
                    x -= setor
            if recuperados:
                ctrl.evento('Ao reler, recuperei %d bytes que tinham falhado.' % recuperados)
            return None

        def relatar_faixas_ruins():
            ruins = [(a, b) for a, b, e in faixas if e == 'ruim']
            for a, b in ruins[:MAX_EVENTOS_DE_FAIXA]:
                ctrl.evento('Setores ilegíveis: bytes %d a %d (%d setores).' % (a, b, -(-(b - a) // setor)))
            if len(ruins) > MAX_EVENTOS_DE_FAIXA:
                ctrl.evento('... e mais %d faixas ilegíveis (veja o mapa).' % (len(ruins) - MAX_EVENTOS_DE_FAIXA))

        passadas = {'copia': ('Copiando', lambda: passada_de_copia(pular)), 'retorno': ('Voltando às áreas puladas', passada_de_retorno),
                    'bissecao': ('Procurando os setores ruins', passada_de_bisseccao),
                    'repasse': ('Relendo os setores ruins', passada_de_repasse)}
        for fase in FASES[FASES.index(est['fase']):-1]:
            texto, rodar = passadas[fase]
            if fase == 'repasse' and (tentativas == 0 or not somar(faixas, 'ruim')):
                resultado = None
            else:
                est['fase'] = fase
                andamento(texto)
                resultado = rodar()
            if resultado:
                andamento()
                ctrl.checkpoint(salvar, forcar=True)
                return resultado
            if fase == 'bissecao':
                relatar_faixas_ruins()
            est['fase'] = FASES[FASES.index(fase) + 1]
            ctrl.checkpoint(salvar, forcar=True)

        andamento('Conferindo a cópia')
        ctrl.checkpoint(salvar, forcar=True)
        if somar(faixas, 'ruim'):
            ctrl.evento('Terminou com %d setores ilegíveis; a conferência por SHA-256 não se aplica.' % setores_ruins(faixas, setor))
            return 'concluido'
        h_origem = _sha256(fd_o, ctrl)
        if h_origem in ('pausado', 'cancelado'):
            return h_origem
        os.fsync(fd_d)
        with leitor.abrir(destino) as conferencia:
            h_destino = _sha256(conferencia, ctrl)
        if h_destino in ('pausado', 'cancelado'):
            return h_destino
        if h_origem != h_destino:
            raise ErroJob('A cópia não confere com a origem (SHA-256 diferente). Não use esta imagem.')
        ctrl.evento('Conferido: SHA-256 igual ao da origem (%s).' % h_destino)
        return 'concluido'
    finally:
        if registro:
            registro.close()
        fd_o.fechar()
        if fd_d is not None:
            os.close(fd_d)
