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
    return Catalogo(linhas)


class Varredor:
    def __init__(self, leitor, catalogo, modo='rapida', bloco=BLOCO_PADRAO):
        if modo not in MODOS:
            raise ValueError('modo desconhecido: %r' % (modo,))
        self.leitor = leitor
        self.cat = catalogo
        self.modo = modo
        self.bloco = bloco
        self.setor = max(1, getattr(leitor, 'setor', 512))
        self.janela = validar.Janela(leitor)
        self.livre = 0              # nada abaixo desta posição pode começar um achado (já está dentro de um)
        self.erros_validador = 0    # validador que levantou exceção (bug ou dado maluco): conta e segue

    # --- leitura -----------------------------------------------------------------------------------------------
    def _ler(self, pos, n, extra=0):
        """Lê [pos, pos+n+extra). Setor ilegível vira zeros. Devolve (bytes, setores_ruins), contando só os de
        [pos, pos+n): o `extra` é a sobra que o próximo bloco vai ler de novo."""
        try:
            return self.leitor.ler(pos, n + extra), 0
        except OSError:
            pass
        partes, ruins = [], 0
        s = self.setor
        x = pos
        fim = min(pos + n + extra, self.leitor.tamanho)
        while x < fim:
            y = min(fim, (x // s + 1) * s)
            try:
                partes.append(self.leitor.ler(x, y - x))
            except OSError:
                partes.append(bytes(y - x))
                if x < pos + n:
                    ruins += 1
            x = y
        return b''.join(partes), ruins

    # --- busca ---------------------------------------------------------------------------------------------------
    def _candidatos(self, dados, pos, n):
        """{início_do_arquivo: [assinaturas]} para cabeçalhos cujo primeiro byte cai em [pos, pos+n)."""
        achados = {}
        rapida = self.modo == 'rapida'
        for (cab, desl), sigs in self.cat.grupos.items():
            lim = min(len(dados), n + len(cab) - 1)
            i = dados.find(cab, 0, lim)
            while i >= 0:
                inicio = pos + i - desl
                if inicio >= 0 and (not rapida or inicio % self.setor == 0):
                    achados.setdefault(inicio, []).extend(sigs)
                i = dados.find(cab, i + 1, lim)
        return achados

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
            candidatos = self._candidatos(dados, pos, n)
            for cand in sorted(candidatos):
                if cand <= inicio:
                    continue
                for a in candidatos[cand]:
                    x = self._avaliar(cand, a)
                    if x is not None and x['estado'] == 'bom':
                        return dict(r, tamanho=cand - inicio)
            pos += n
        return r

    def _achado(self, inicio, a, r):
        ext = r.get('extensao') or a['extensao']
        return {'nome': nome_gerado(inicio, ext), 'tipo': r.get('tipo') or a['tipo'], 'grupo': r.get('grupo') or a['grupo'],
                'extensao': ext, 'tamanho': r['tamanho'], 'offset': inicio, 'estado': r['estado'],
                'pontuacao': r['pontuacao'], 'data': r.get('data'), 'largura': r.get('largura'),
                'altura': r.get('altura'), 'apagado': None}

    def blocos(self, inicio=0):
        """Gera (posição_já_varrida, achados_do_bloco, setores_ruins_do_bloco). A posição é onde a retomada recomeça:
        todo achado cujo início fica antes dela já foi entregue."""
        tam = self.leitor.tamanho
        pos = max(0, inicio)
        if self.modo == 'rapida':
            pos = pos // self.setor * self.setor
        self.livre = pos
        while pos < tam:
            n = min(self.bloco, tam - pos)
            dados, ruins = self._ler(pos, n, self.cat.sobra)
            achados = []
            for ini in sorted(self._candidatos(dados, pos, n).items()):
                inicio_arq, sigs = ini
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
