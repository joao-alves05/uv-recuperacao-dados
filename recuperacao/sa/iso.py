# -*- coding: utf-8 -*-
"""ISO 9660 com extensão Joliet (nomes longos em UCS-2): descritores de volume, pastas e arquivos.

Se existir descritor suplementar Joliet, ele manda (nomes longos e acentos); senão vale o descritor primário, com
os nomes 8.3 sem o ";1" final. Arquivos em várias extensões (flag "multi-extent") são juntados. Não há arquivo
apagado em ISO. Limites: Rock Ridge e El Torito não são lidos; só um volume por imagem; UDF puro é só reconhecido.
"""
import struct

from .base import ErroSA, Entrada, Volume, MAX_PROFUNDIDADE, epoch, juntar, limpar_nome

SETOR = 2048
MAX_PASTA = 64 << 20


def _descritores(leitor, inicio):
    """[(tipo, bytes)] dos descritores de volume a partir do setor 16, até o terminador."""
    out = []
    for k in range(16, 16 + 32):
        d = leitor.ler(inicio + k * SETOR, SETOR)
        if len(d) < SETOR or d[1:6] != b'CD001':
            break
        if d[0] == 255:
            break
        out.append((d[0], d))
    return out


def reconhecer(leitor, inicio=0, tamanho=None):
    try:
        if not any(t == 1 for t, _ in _descritores(leitor, inicio)):
            return None
        return Iso(leitor, inicio, tamanho)
    except (ErroSA, OSError, struct.error):
        return None


def _data7(b):
    ano, mes, dia, h, mi, s = b[0] + 1900, b[1], b[2], b[3], b[4], b[5]
    if not (1 <= mes <= 12 and 1 <= dia <= 31 and h < 24 and mi < 60 and s < 61):
        return None
    t = epoch(ano, mes, dia, h, mi, min(s, 59))
    return None if t is None else t - struct.unpack('b', b[6:7])[0] * 900


class Iso(Volume):
    tipo = 'ISO9660'

    def __init__(self, leitor, inicio=0, tamanho=None):
        super().__init__(leitor, inicio, tamanho)
        try:
            desc = _descritores(leitor, inicio)
        except OSError as e:
            raise ErroSA('Não consegui ler os descritores: %s' % e) from None
        prim = next((d for t, d in desc if t == 1), None)
        if prim is None:
            raise ErroSA('Sem descritor primário ISO 9660.')
        jol = next((d for t, d in desc if t == 2 and d[88:91] in (b'%/@', b'%/C', b'%/E')), None)
        self.joliet = jol is not None
        usado = jol if self.joliet else prim
        self.bloco = struct.unpack_from('<H', usado, 128)[0]
        if self.bloco not in (512, 1024, 2048, 4096):
            raise ErroSA('Tamanho de bloco ISO inválido.')
        if self.joliet:
            self.tipo = 'ISO9660/Joliet'
        self.rotulo = (usado[40:72].decode('utf-16-be', 'replace') if self.joliet
                       else usado[40:72].decode('latin-1')).strip().rstrip('\0').strip()
        self._raiz = usado[156:190]

    def _nome(self, b):
        if self.joliet:
            n = b.decode('utf-16-be', 'replace')
        else:
            n = b.decode('latin-1')
            if ';' in n:
                n = n.rsplit(';', 1)[0]
            if n.endswith('.'):
                n = n[:-1]
        return limpar_nome(n)

    def _registros(self, dados):
        """Gera os registros de uma pasta (bytes); registros não cruzam o limite de setor."""
        off = 0
        while off < len(dados):
            n = dados[off]
            if n == 0:
                off = (off // SETOR + 1) * SETOR
                continue
            if n < 34 or off + n > len(dados):
                return
            yield dados[off: off + n]
            off += n

    def _ler_extent(self, lba, tam):
        if tam > MAX_PASTA:
            raise ErroSA('pasta grande demais')
        return self._ler(lba * self.bloco, tam)

    def _pasta(self, dados, pai, nivel, vistos):
        pendente = None                                      # arquivo em várias extensões
        for r in self._registros(dados):
            lba = struct.unpack_from('<I', r, 2)[0]
            tam = struct.unpack_from('<I', r, 10)[0]
            flags = r[25]
            nl = r[32]
            nb = r[33:33 + nl]
            if nl == 1 and nb in (b'\0', b'\1'):
                continue
            nome = self._nome(nb)
            eh_dir = bool(flags & 2)
            data = _data7(r[18:25])
            if pendente is not None and not eh_dir and pendente.nome == nome:
                ent = pendente
                ent.tamanho += tam
                ent.runs.append((self.inicio + lba * self.bloco, tam))
            else:
                ent = Entrada(nome=nome, caminho=juntar(pai, nome), eh_dir=eh_dir, tamanho=0 if eh_dir else tam,
                              modificado=data, criado=data, ident=lba)
                if not eh_dir:
                    ent.runs = [(self.inicio + lba * self.bloco, tam)]
                if self.inicio + lba * self.bloco + (0 if eh_dir else tam) > self.inicio + self.tamanho:
                    ent.estado, ent.nota = 'parcial', 'a imagem termina antes do fim do arquivo'
            if flags & 0x80 and not eh_dir:
                pendente = ent
                continue
            pendente = None
            yield ent
            if eh_dir and lba not in vistos and nivel < MAX_PROFUNDIDADE:
                vistos.add(lba)
                try:
                    sub = self._ler_extent(lba, tam)
                except (OSError, ErroSA):
                    continue
                yield from self._pasta(sub, ent.caminho, nivel + 1, vistos)

    def varrer(self):
        lba = struct.unpack_from('<I', self._raiz, 2)[0]
        tam = struct.unpack_from('<I', self._raiz, 10)[0]
        try:
            dados = self._ler_extent(lba, tam)
        except OSError as e:
            raise ErroSA('Não consegui ler a pasta raiz: %s' % e) from None
        yield from self._pasta(dados, '/', 0, {lba})
