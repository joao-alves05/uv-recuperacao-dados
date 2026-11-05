# -*- coding: utf-8 -*-
"""Base dos leitores de sistema de arquivos: só leitura, pelo `Leitor`, Python puro.

Cada sistema de arquivos vira um `Volume` com `varrer()` (todas as entradas, em uso e apagadas, com caminho
completo) e `ler(entrada)` (os bytes, que saem das faixas `runs` da própria entrada). Nada aqui escreve em lugar
nenhum: o único acesso à origem é `leitor.ler(offset, n)`.

Datas: os sistemas de arquivos guardam hora local sem fuso (FAT, exFAT) ou UTC (NTFS, ext, ISO). Aqui tudo vira
segundos desde 1970 (float), lendo a hora local do FAT/exFAT como se fosse UTC; `None` quando a data é inválida.
"""
import calendar
from dataclasses import dataclass, field

BLOCO = 1 << 20
MAX_PROFUNDIDADE = 64


class ErroSA(Exception):
    """Estrutura do sistema de arquivos ilegível ou fora do que sabemos ler; a mensagem já vem em português."""


@dataclass
class Entrada:
    nome: str
    caminho: str                    # '/pasta/arquivo.txt'; arquivo sem pai conhecido fica em '/[perdidos]'
    eh_dir: bool = False
    tamanho: int = 0
    apagado: bool = False
    criado: float = None
    modificado: float = None
    acessado: float = None
    estado: str = 'bom'             # bom | parcial | ruim (parcial: pode ter dados de outro arquivo ou faltar parte)
    runs: list = field(default_factory=list)    # [(offset absoluto no leitor, bytes)]; offset None = zeros (arquivo esparso)
    inline: bytes = None            # conteúdo guardado dentro do próprio registro (NTFS residente, ext4 inline)
    ident: int = 0                  # primeiro cluster / registro MFT / inode: identifica a entrada dentro do volume
    nota: str = ''

    def como_dict(self):
        return {'nome': self.nome, 'caminho': self.caminho, 'eh_dir': self.eh_dir, 'tamanho': self.tamanho,
                'apagado': self.apagado, 'criado': self.criado, 'modificado': self.modificado,
                'acessado': self.acessado, 'estado': self.estado, 'ident': self.ident, 'nota': self.nota}


def juntar(pai, nome):
    return (pai.rstrip('/') + '/' + nome) if pai != '/' else '/' + nome


def limpar_nome(nome):
    """Nome vindo do disco é dado de terceiros: tira separadores e controles para não virar caminho."""
    nome = ''.join('_' if (ord(c) < 32 or c in '/\\') else c for c in nome)
    return nome or '_'


def epoch(ano, mes, dia, h=0, mi=0, s=0, frac=0.0):
    try:
        return calendar.timegm((ano, mes, dia, h, mi, s, 0, 0, 0)) + frac
    except (ValueError, OverflowError):
        return None


def data_dos(data, hora=0, decimos=0):
    """Data e hora DOS de 16 bits cada (FAT). `decimos` em 10 ms (0..199)."""
    mes, dia = (data >> 5) & 15, data & 31
    if not (1 <= mes <= 12 and 1 <= dia <= 31):
        return None
    h, mi, s = hora >> 11, (hora >> 5) & 63, (hora & 31) * 2
    if h > 23 or mi > 59 or s > 59:
        return None
    return epoch(1980 + (data >> 9), mes, dia, h, mi, s, min(decimos, 199) / 100.0)


class Volume:
    tipo = ''
    rotulo = ''
    limitacao = ''
    ilegiveis = 0           # setores que não deu para ler ao montar a lista (o resto da lista segue sem eles)

    def __init__(self, leitor, inicio=0, tamanho=None):
        self.leitor = leitor
        self.inicio = inicio
        self.tamanho = (leitor.tamanho - inicio) if tamanho is None else min(tamanho, leitor.tamanho - inicio)

    def _ler(self, off, n):
        """Lê `n` bytes a partir de `off` relativo ao início do volume; falha se passar do fim."""
        if off < 0 or n < 0 or off + n > self.tamanho:
            raise ErroSA('leitura fora do volume (%d+%d)' % (off, n))
        return self.leitor.ler(self.inicio + off, n)

    def varrer(self):
        raise NotImplementedError

    def listar(self, caminho='/'):
        """Filhos diretos de uma pasta (varre tudo; as rotas usam a tabela de achados, não isto)."""
        pai = '/' + caminho.strip('/') if caminho.strip('/') else '/'
        for e in self.varrer():
            if (e.caminho.rsplit('/', 1)[0] or '/') == pai and e.caminho != pai:
                yield e

    def ler(self, entrada, bloco=BLOCO):
        """Bytes do arquivo em pedaços, até `tamanho`. Faixa sem offset (esparsa) sai como zeros."""
        falta = entrada.tamanho
        if entrada.inline is not None:
            yield entrada.inline[:falta]
            return
        for off, n in entrada.runs:
            while n > 0 and falta > 0:
                q = min(n, falta, bloco)
                yield (b'\0' * q) if off is None else self.leitor.ler(off, q)
                if off is not None:
                    off += q
                n -= q
                falta -= q
