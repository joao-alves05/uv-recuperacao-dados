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

from .. import config
from ..executor import ErroJob

O_BINARY = getattr(os, 'O_BINARY', 0)
BLOCO_PADRAO = 1024 * 1024
BLOCO_MIN = 4096
BLOCO_MAX = 64 * 1024 * 1024
TENTATIVAS = 2
SETOR = 512
BLOCO_HASH = 4 * 1024 * 1024
ESTADOS_FAIXA = ('ok', 'ruim', 'pendente')
MODO_PRIVADO = 0o600          # imagem e mapa só do dono (no Windows o arquivo herda as permissões da pasta)
NOMES_RESERVADOS = frozenset(['CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$']
                             + ['COM%d' % i for i in range(1, 10)] + ['LPT%d' % i for i in range(1, 10)])


def caminho_mapa(destino):
    return destino + '.mapa.json'


def _normal(caminho):
    return os.path.normcase(os.path.realpath(caminho))


def bloco_de(opcoes):
    return (opcoes or {}).get('bloco', BLOCO_PADRAO)


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
    for c in (origem, destino):
        erro = caminho_proibido(c)
        if erro:
            return erro
    if not os.path.isfile(origem):
        return 'A origem não existe ou não é um arquivo. Nesta versão só dá para copiar arquivos.'
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


def setores_ruins(faixas):
    return sum(-(-(b - a) // SETOR) for a, b, e in faixas if e == 'ruim')


def primeira_pendente(faixas):
    for a, b, e in faixas:
        if e == 'pendente':
            return a, b
    return None


def salvar_mapa(caminho, faixas, origem=None, tamanho=None):
    """Grava o mapa de forma atômica. O temporário tem nome aleatório e é criado com O_EXCL: nunca sobrescreve
    um arquivo que já exista e não seja nosso."""
    tmp = '%s.%s.tmp' % (caminho, secrets.token_hex(4))
    conteudo = {'versao': 1, 'origem': origem, 'tamanho': tamanho, 'faixas': faixas}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, MODO_PRIVADO)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(conteudo, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, caminho)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def carregar_mapa(caminho, tamanho, origem=None):
    """Lê e confere o mapa. Se `origem` vier, tem de ser a mesma da primeira vez (senão: ErroJob, sem tocar em nada)."""
    try:
        with open(caminho, 'r', encoding='utf-8') as f:
            conteudo = json.load(f)
    except (OSError, ValueError):
        raise ErroJob('Não consegui ler o mapa desta imagem. Ele pode estar danificado.') from None
    if not isinstance(conteudo, dict):
        raise ErroJob('O mapa desta imagem está danificado.')
    faixas = conteudo.get('faixas')
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
    return faixas


# --- leitura e escrita ----------------------------------------------------
def _ler_bloco(fd, pos, n):
    """Lê n bytes a partir de pos (menos só no fim do arquivo). Ponto único de leitura da origem."""
    os.lseek(fd, pos, os.SEEK_SET)
    partes, falta = [], n
    while falta:
        b = os.read(fd, falta)
        if not b:
            break
        partes.append(b)
        falta -= len(b)
    return b''.join(partes)


def _escrever(fd, pos, dados):
    os.lseek(fd, pos, os.SEEK_SET)
    visto = memoryview(dados)
    while visto:
        visto = visto[os.write(fd, visto):]


def _sha256(fd, ctrl):
    """SHA-256 do arquivo aberto em fd, ou 'pausado'/'cancelado' se pediram para parar."""
    h = hashlib.sha256()
    pos = 0
    while True:
        if ctrl.cancelamento_pedido():
            return 'cancelado'
        if ctrl.pausa_pedida():
            return 'pausado'
        b = _ler_bloco(fd, pos, BLOCO_HASH)
        if not b:
            return h.hexdigest()
        h.update(b)
        pos += len(b)


def executar(job, ctrl):
    origem, destino = job['origem'], job['destino']
    bloco = bloco_de(job.get('opcoes'))
    mapa = caminho_mapa(destino)
    tem_dest, tem_mapa = os.path.exists(destino), os.path.exists(mapa)
    if tem_dest != tem_mapa:
        raise ErroJob('O destino e o mapa desta imagem não estão juntos (falta um dos dois). '
                      'Por segurança, nada foi sobrescrito.')
    retomada = tem_dest
    erro = validar(origem, destino, job.get('opcoes'), retomada=retomada)
    if erro:
        raise ErroJob(erro)

    fd_o = os.open(origem, os.O_RDONLY | O_BINARY)          # única abertura da origem para cópia: só leitura
    fd_d = None
    registro = None
    try:
        tamanho = os.fstat(fd_o).st_size
        origem_real = _normal(origem)
        if retomada:
            faixas = carregar_mapa(mapa, tamanho, origem_real)
            if os.path.getsize(destino) != tamanho:
                raise ErroJob('A imagem parcial não tem o tamanho esperado. Nada foi alterado.')
            fd_d = os.open(destino, os.O_WRONLY | O_BINARY)
            ctrl.evento('Retomando: %d bytes já prontos, %d ainda por ler.'
                        % (somar(faixas, 'ok') + somar(faixas, 'ruim'), somar(faixas, 'pendente')))
        else:
            faixas = [[0, tamanho, 'pendente']] if tamanho else []
            fd_d = os.open(destino, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, MODO_PRIVADO)
            os.ftruncate(fd_d, tamanho)
            salvar_mapa(mapa, faixas, origem_real, tamanho)
            ctrl.evento('Imagem criada: %d bytes, blocos de %d.' % (tamanho, bloco))

        def salvar():
            os.fsync(fd_d)                  # os dados vão para o disco antes do mapa que diz que estão prontos
            salvar_mapa(mapa, faixas, origem_real, tamanho)

        def andamento(texto=None):
            ctrl.progresso(somar(faixas, 'ok') + somar(faixas, 'ruim'), tamanho, texto, setores_ruins(faixas))

        atraso = config.atraso_bloco()
        registro = open(config.log_leituras(), 'a', encoding='ascii', buffering=1) if config.log_leituras() else None
        andamento('Copiando')
        while True:
            parar = 'cancelado' if ctrl.cancelamento_pedido() else 'pausado' if ctrl.pausa_pedida() else None
            if parar:
                andamento()
                ctrl.checkpoint(salvar, forcar=True)
                return parar
            prox = primeira_pendente(faixas)
            if prox is None:
                break
            ini = prox[0]
            fim = min(prox[1], ini + bloco)
            dados = None
            for tentativa in range(TENTATIVAS):
                try:
                    dados = _ler_bloco(fd_o, ini, fim - ini)
                    if len(dados) != fim - ini:
                        raise OSError('leitura curta')
                    break
                except OSError:
                    dados = None
            if registro:
                registro.write('%d %d\n' % (ini, fim - ini))      # só para os testes provarem o que foi lido
            if dados is None:
                _escrever(fd_d, ini, bytes(fim - ini))
                marcar(faixas, ini, fim, 'ruim')
                ctrl.evento('Não consegui ler %d bytes a partir da posição %d; marquei como ruim e segui.' % (fim - ini, ini))
            else:
                _escrever(fd_d, ini, dados)
                marcar(faixas, ini, fim, 'ok')
            andamento()
            ctrl.checkpoint(salvar, bytes_novos=fim - ini)
            if atraso:
                time.sleep(atraso)

        andamento('Conferindo a cópia')
        ctrl.checkpoint(salvar, forcar=True)
        if somar(faixas, 'ruim'):
            ctrl.evento('Terminou com %d setores ruins; a conferência por SHA-256 não se aplica.' % setores_ruins(faixas))
            return 'concluido'
        h_origem = _sha256(fd_o, ctrl)
        if h_origem in ('pausado', 'cancelado'):
            return h_origem
        os.fsync(fd_d)
        fd_conf = os.open(destino, os.O_RDONLY | O_BINARY)
        try:
            h_destino = _sha256(fd_conf, ctrl)
        finally:
            os.close(fd_conf)
        if h_destino in ('pausado', 'cancelado'):
            return h_destino
        if h_origem != h_destino:
            raise ErroJob('A cópia não confere com a origem (SHA-256 diferente). Não use esta imagem.')
        ctrl.evento('Conferido: SHA-256 igual ao da origem (%s).' % h_destino)
        return 'concluido'
    finally:
        if registro:
            registro.close()
        os.close(fd_o)
        if fd_d is not None:
            os.close(fd_d)
