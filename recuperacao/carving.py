# -*- coding: utf-8 -*-
"""Motor de carving: procura cabeçalhos de arquivo numa origem (só leitura) e confere cada achado com o validador do tipo.

    catalogo = carregar()
    for pos, achados, setores_ruins in Varredor(leitor, catalogo, 'rapida').blocos(0):
        ...

O motor não conhece jobs nem banco: `blocos()` entrega, a cada bloco lido, a posição já varrida e os achados desse
bloco. Quem chama (o tipo `varredura`) grava os achados e o checkpoint e decide pausar. Retomar é chamar `blocos(pos)`
com a posição do último checkpoint.

Modos: `rapida` só considera cabeçalhos no começo de um setor (é onde arquivos de sistema de arquivos começam);
`profunda` considera todo byte. Arquivos contíguos: um achado bom ou parcial faz a varredura pular para depois dele
(sem achar o que está embutido, como miniaturas dentro de outro arquivo). A heurística de arquivo fragmentado
(PhotoRec) não está feita: arquivo fragmentado sai `parcial` ou `ruim`.
"""
import json
import os
import struct
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

from . import leitor as leitor_mod
from . import validar
from .achados import GRUPOS, nome_gerado

BLOCO_PADRAO = 4 * 1024 * 1024
MODOS = ('rapida', 'profunda')
CAMINHO_PADRAO = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assinaturas.json')
MAX_CABECALHO = 32


class ErroAssinaturas(ValueError):
    pass


def _hex(valor, campo, tipo):
    try:
        b = bytes.fromhex(valor)
    except (TypeError, ValueError):
        raise ErroAssinaturas('Assinatura %s: "%s" precisa ser hexadecimal.' % (tipo, campo)) from None
    if not b or len(b) > MAX_CABECALHO:
        raise ErroAssinaturas('Assinatura %s: "%s" precisa ter de 1 a %d bytes.' % (tipo, campo, MAX_CABECALHO))
    return b


class Catalogo:
    """A tabela de assinaturas conferida e agrupada por (cabeçalho, posição do cabeçalho no arquivo)."""

    def __init__(self, linhas):
        self.assinaturas = []
        self.grupos = {}
        self.caminho = None          # de onde veio a tabela (os trabalhadores em segundo plano recarregam por ele)
        for l in linhas:
            if not isinstance(l, dict):
                raise ErroAssinaturas('Cada assinatura precisa ser um objeto.')
            tipo = l.get('tipo')
            if not isinstance(tipo, str) or not tipo:
                raise ErroAssinaturas('Assinatura sem "tipo".')
            if l.get('grupo') not in GRUPOS:
                raise ErroAssinaturas('Assinatura %s: grupo desconhecido (%s).' % (tipo, ', '.join(GRUPOS)))
            ext = l.get('extensao')
            if not isinstance(ext, str) or not ext.isalnum() or len(ext) > 10:
                raise ErroAssinaturas('Assinatura %s: extensão inválida.' % tipo)
            cab = _hex(l.get('cabecalho'), 'cabecalho', tipo)
            desl = l.get('cabecalho_offset', 0)
            if type(desl) is not int or not 0 <= desl <= 512:
                raise ErroAssinaturas('Assinatura %s: "cabecalho_offset" precisa ser um inteiro de 0 a 512.' % tipo)
            tmax = l.get('tamanho_max')
            if type(tmax) is not int or tmax <= 0:
                raise ErroAssinaturas('Assinatura %s: "tamanho_max" precisa ser um inteiro positivo.' % tipo)
            if l.get('rodape') is not None:
                _hex(l['rodape'], 'rodape', tipo)
            nome = l.get('validar') or ('generico' if l.get('rodape') else None)
            if nome not in validar.VALIDADORES:
                raise ErroAssinaturas('Assinatura %s: precisa de "validar" (%s) ou de "rodape".'
                                      % (tipo, ', '.join(sorted(validar.VALIDADORES))))
            a = dict(l, validar=nome, _cab=cab, _desl=desl, _funcao=validar.VALIDADORES[nome])
            a['_i'] = len(self.assinaturas)
            self.assinaturas.append(a)
            self.grupos.setdefault((cab, desl), []).append(a)
        self.sobra = max([len(c) + d for c, d in self.grupos] or [0])


def carregar(caminho=None):
    try:
        with open(caminho or CAMINHO_PADRAO, 'r', encoding='utf-8') as f:
            dados = json.load(f)
    except (OSError, ValueError) as e:
        raise ErroAssinaturas('Não consegui ler a tabela de assinaturas: %s' % e) from None
    linhas = dados.get('assinaturas') if isinstance(dados, dict) else None
    if not isinstance(linhas, list):
        raise ErroAssinaturas('A tabela de assinaturas precisa ter a lista "assinaturas".')
    cat = Catalogo(linhas)
    cat.caminho = caminho or CAMINHO_PADRAO
    return cat


def ler_com_zeros(leit, setor, pos, n, extra=0):
    """Lê [pos, pos+n+extra). Setor ilegível vira zeros. Devolve (bytes, setores_ruins), contando só os de
    [pos, pos+n): o `extra` é a sobra que o próximo bloco vai ler de novo."""
    try:
        return leit.ler(pos, n + extra), 0
    except OSError:
        pass
    partes, ruins = [], 0
    x = pos
    fim = min(pos + n + extra, leit.tamanho)
    while x < fim:
        y = min(fim, (x // setor + 1) * setor)
        try:
            partes.append(leit.ler(x, y - x))
        except OSError:
            partes.append(bytes(y - x))
            if x < pos + n:
                ruins += 1
        x = y
    return b''.join(partes), ruins


def candidatos(dados, pos, n, grupos, rapida, setor):
    """{início_do_arquivo: [assinaturas]} para cabeçalhos cujo primeiro byte cai em [pos, pos+n).

    Rápida (pos alinhado ao setor): só olha o primeiro byte de cada setor, pegando-os de uma vez com fatia de passo
    `setor` (feito em C), então custa uma fração da leitura. Profunda: um bytes.find por cabeçalho."""
    achados = {}
    for (cab, desl), sigs in grupos.items():
        if rapida and pos % setor == 0:
            i0 = desl % setor
            fatia = dados[i0:n:setor]
            k = fatia.find(cab[:1])
            while k >= 0:
                i = i0 + k * setor
                if dados.startswith(cab, i) and pos + i - desl >= 0:
                    achados.setdefault(pos + i - desl, []).extend(sigs)
                k = fatia.find(cab[:1], k + 1)
            continue
        lim = min(len(dados), n + len(cab) - 1)
        i = dados.find(cab, 0, lim)
        while i >= 0:
            inicio = pos + i - desl
            if inicio >= 0 and (not rapida or inicio % setor == 0):
                achados.setdefault(inicio, []).extend(sigs)
            i = dados.find(cab, i + 1, lim)
    return achados


# --- trabalho em segundo plano (leitura antecipada e busca de cabeçalhos) --------------------------------------------
# Cada trabalhador (thread ou processo) abre a origem de novo, só para leitura, com o seu próprio descritor: o
# Leitor usa lseek+read e não pode ser dividido entre threads. Só a busca de candidatos vai para lá; a validação
# (que lê para a frente, com cache) continua na thread de quem chamou, em ordem.
_W = threading.local()
MINIMO_SEGUNDO_PLANO = 64 * 1024 * 1024
MAX_PROCESSOS = 3


def _iniciar_trabalhador(caminho, cat_caminho, modo, setor, desl, tam, registro):
    _W.leitor = leitor_mod.abrir(caminho)
    if registro is not None:
        registro.append(_W.leitor)
    if desl or tam is not None:
        _W.leitor = Fatia(_W.leitor, desl, tam)
    _W.cat = carregar(cat_caminho)
    _W.rapida = modo == 'rapida'
    _W.setor = setor


def _tarefa(pos, n, extra):
    dados, ruins = ler_com_zeros(_W.leitor, _W.setor, pos, n, extra)
    c = candidatos(dados, pos, n, _W.cat.grupos, _W.rapida, _W.setor)
    return {i: [s['_i'] for s in sigs] for i, sigs in c.items()}, ruins


class Fatia:
    """Uma janela [deslocamento, deslocamento+tamanho) da origem, vista como se fosse a origem inteira (para varrer só uma
    partição). Só leitura: delega ao leitor de verdade. Os achados saem com o offset absoluto na origem."""

    def __init__(self, base, deslocamento, tamanho):
        self.base = base
        self.deslocamento = deslocamento
        self.tamanho = tamanho
        self.setor = base.setor
        self.caminho = getattr(base, 'caminho', None)

    def ler(self, off, n):
        if off < 0 or n < 0:
            raise ValueError('offset e tamanho não podem ser negativos')
        n = min(n, max(0, self.tamanho - off))
        return self.base.ler(self.deslocamento + off, n) if n else b''


class Varredor:
    def __init__(self, leitor, catalogo, modo='rapida', bloco=BLOCO_PADRAO, trabalhadores=None):
        """trabalhadores: None = decide sozinho (origem grande e com caminho: rápida usa threads de leitura antecipada,
        profunda usa processos); 0 = tudo nesta thread; N = N trabalhadores."""
        if modo not in MODOS:
            raise ValueError('modo desconhecido: %r' % (modo,))
        self.leitor = leitor
        self.cat = catalogo
        self.modo = modo
        self.bloco = bloco
        self.setor = max(1, getattr(leitor, 'setor', 512))
        self.janela = validar.Janela(leitor)
        self.deslocamento = getattr(leitor, 'deslocamento', 0)     # Fatia: onde a janela começa na origem
        self.livre = 0              # nada abaixo desta posição pode começar um achado (já está dentro de um)
        self.erros_validador = 0    # validador que levantou exceção (bug ou dado maluco): conta e segue
        self.trabalhadores = self._escolher(trabalhadores)
        self.segundo_plano = 'nenhum'      # 'threads' | 'processos' | 'nenhum' (o que ficou em uso)

    def _escolher(self, pedido):
        if not getattr(self.leitor, 'caminho', None) or not self.cat.caminho:
            return 0                                          # leitor falso ou tabela só na memória: nada a reabrir
        if pedido is not None:
            return max(0, int(pedido))
        if self.leitor.tamanho < MINIMO_SEGUNDO_PLANO:
            return 0
        if self.modo == 'rapida':
            return 2
        return max(1, min(MAX_PROCESSOS, (os.cpu_count() or 1) - 1))

    # --- leitura -----------------------------------------------------------------------------------------------
    def _ler(self, pos, n, extra=0):
        return ler_com_zeros(self.leitor, self.setor, pos, n, extra)

    # --- busca ---------------------------------------------------------------------------------------------------
    def _candidatos(self, dados, pos, n):
        return candidatos(dados, pos, n, self.cat.grupos, self.modo == 'rapida', self.setor)

    def _avaliar(self, inicio, a):
        limite = min(a['tamanho_max'], self.leitor.tamanho - inicio)
        if limite <= 0:
            return None
        try:
            return a['_funcao'](self.janela, inicio, limite, a)
        except (ValueError, IndexError, OverflowError, MemoryError, TypeError, KeyError, struct.error):
            self.erros_validador += 1
            return None

    def _aparar(self, inicio, r):
        """Um achado `parcial` (começo válido, resto desconhecido) não pode engolir um arquivo inteiro que começa dentro
        dele: se um cabeçalho dentro do trecho valida como `bom`, o parcial termina ali."""
        fim = inicio + r['tamanho']
        pos = inicio + 1
        while pos < fim:
            n = min(self.bloco, fim - pos)
            dados, _ = self._ler(pos, n, self.cat.sobra)
            cands = self._candidatos(dados, pos, n)
            for cand in sorted(cands):
                if cand <= inicio:
                    continue
                for a in cands[cand]:
                    x = self._avaliar(cand, a)
                    if x is not None and x['estado'] == 'bom':
                        return dict(r, tamanho=cand - inicio)
            pos += n
        return r

    def _achado(self, inicio, a, r):
        ext = r.get('extensao') or a['extensao']
        return {'nome': nome_gerado(inicio + self.deslocamento, ext), 'tipo': r.get('tipo') or a['tipo'], 'grupo': r.get('grupo') or a['grupo'],
                'extensao': ext, 'tamanho': r['tamanho'], 'offset': inicio + self.deslocamento, 'estado': r['estado'],
                'pontuacao': r['pontuacao'], 'data': r.get('data'), 'largura': r.get('largura'),
                'altura': r.get('altura'), 'apagado': None}

    # --- segundo plano -------------------------------------------------------------------------------------------
    def _abrir_pool(self, abertos):
        if not self.trabalhadores:
            return None
        args = (self.leitor.caminho, self.cat.caminho, self.modo, self.setor, self.deslocamento,
                self.leitor.tamanho if self.deslocamento or hasattr(self.leitor, 'base') else None)
        try:
            if self.modo == 'profunda':
                pool = ProcessPoolExecutor(max_workers=self.trabalhadores, initializer=_iniciar_trabalhador,
                                           initargs=args + (None,))
                self.segundo_plano = 'processos'
            else:
                pool = ThreadPoolExecutor(max_workers=self.trabalhadores, initializer=_iniciar_trabalhador,
                                          initargs=args + (abertos,))
                self.segundo_plano = 'threads'
            return pool
        except (OSError, ValueError, RuntimeError, ImportError):
            return None

    def _do_pool(self, pool, futs, pos, tam):
        """Resultado do bloco em `pos`, mantendo `2 x trabalhadores` blocos à frente em andamento (memória limitada).
        Bloco adiantado que ficou para trás (a varredura pulou um arquivo grande) é descartado."""
        n_blocos = 2 * self.trabalhadores
        esperados = [p for p in (pos + k * self.bloco for k in range(n_blocos)) if p < tam]
        for p in [p for p in futs if p not in esperados]:
            futs.pop(p).cancel()
        for p in esperados:
            if p not in futs:
                futs[p] = pool.submit(_tarefa, p, min(self.bloco, tam - p), self.cat.sobra)
        mapa, ruins = futs.pop(pos).result()
        return {i: [self.cat.assinaturas[k] for k in idx] for i, idx in mapa.items()}, ruins

    def blocos(self, inicio=0):
        """Gera (posição_já_varrida, achados_do_bloco, setores_ruins_do_bloco). A posição é onde a retomada recomeça:
        todo achado cujo início fica antes dela já foi entregue."""
        tam = self.leitor.tamanho
        pos = max(0, inicio)
        if self.modo == 'rapida':
            pos = pos // self.setor * self.setor
        self.livre = pos
        abertos, futs = [], {}
        pool = self._abrir_pool(abertos)
        try:
            while pos < tam:
                n = min(self.bloco, tam - pos)
                cands = None
                if pool is not None:
                    try:
                        cands, ruins = self._do_pool(pool, futs, pos, tam)
                    except Exception:                      # trabalhador morreu, não abriu a origem...: segue sozinho
                        pool.shutdown(wait=False, cancel_futures=True)
                        pool, futs, self.segundo_plano = None, {}, 'nenhum'
                if cands is None:
                    dados, ruins = self._ler(pos, n, self.cat.sobra)
                    cands = self._candidatos(dados, pos, n)
                achados = []
                for inicio_arq, sigs in sorted(cands.items()):
                    if inicio_arq < self.livre:
                        continue
                    for a in sigs:
                        r = self._avaliar(inicio_arq, a)
                        if r is None or r['tamanho'] <= 0:
                            continue
                        if r['estado'] == 'parcial' and r['tamanho'] > 1:
                            r = self._aparar(inicio_arq, r)
                        achados.append(self._achado(inicio_arq, a, r))
                        if r['estado'] == 'ruim':
                            self.livre = inicio_arq + 1
                        else:
                            self.livre = inicio_arq + r['tamanho']
                            if self.modo == 'rapida':
                                self.livre = -(-self.livre // self.setor) * self.setor
                        break
                pos = max(pos + n, self.livre)
                if self.modo == 'rapida':
                    pos = -(-pos // self.setor) * self.setor
                yield min(pos, tam), achados, ruins
        finally:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
            for l in abertos:
                try:
                    l.fechar()
                except OSError:
                    pass
