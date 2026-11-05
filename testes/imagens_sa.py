# -*- coding: utf-8 -*-
"""Monta, só em Python, imagens pequenas de sistemas de arquivos para os testes da fase 4.

Nada aqui usa ferramenta externa nem disco real: cada construtor junta o modelo na memória e `gravar(caminho)`
escreve um arquivo comum numa pasta temporária.
"""
import struct

DATA = (2024, 3, 15, 10, 20, 30)


def dos_data(a=DATA):
    return ((a[0] - 1980) << 9) | (a[1] << 5) | a[2]


def dos_hora(a=DATA):
    return (a[3] << 11) | (a[4] << 5) | (a[5] // 2)


def _soma_curta(raw11):
    s = 0
    for b in raw11:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def curto_de(nome):
    base, _, ext = nome.upper().rpartition('.') if '.' in nome else (nome.upper(), '', '')
    base = ''.join(c for c in base if c.isascii() and c.isalnum())[:6]
    return (base + '~1').ljust(8).encode('cp437') + ext[:3].ljust(3).encode('cp437')


class Pasta:
    def __init__(self, primeiro, fixa=False):
        self.primeiro = primeiro
        self.fixa = fixa
        self.entradas = []                  # bytearrays de 32 bytes


class FatImg:
    """FAT12/16/32 com 1 setor por cluster (FAT32) ou 2 (FAT12/16)."""

    def __init__(self, tipo='FAT32', rotulo='TESTE'):
        self.tipo = tipo
        self.rotulo = rotulo
        self.bps = 512
        self.spc = 1 if tipo == 'FAT32' else 2
        self.nclusters = {'FAT32': 66000, 'FAT16': 5000, 'FAT12': 1000}[tipo]
        self.reservados = 32 if tipo == 'FAT32' else 1
        self.raiz_ent = 0 if tipo == 'FAT32' else 512
        bits = {'FAT32': 32, 'FAT16': 16, 'FAT12': 12}[tipo]
        self.fatsz = -(-(self.nclusters + 2) * bits // 8 // 512)
        self.raiz_setores = self.raiz_ent * 32 // 512
        self.dados_ini = self.reservados + 2 * self.fatsz + self.raiz_setores
        self.total = self.dados_ini + self.nclusters * self.spc
        self.fat = [0] * (self.nclusters + 2)
        self.fat[0], self.fat[1] = (0x0FFFFFF8 if tipo == 'FAT32' else 0xFFF8 if tipo == 'FAT16' else 0xFF8), self.eoc
        self.livre = 2
        self.escritas = []                  # (offset, bytes) em ordem
        self.pastas = {}
        if tipo == 'FAT32':
            self.raiz = Pasta(self._alocar(1)[0])
        else:
            self.raiz = Pasta(0, fixa=True)
        self.pastas['/'] = self.raiz
        self.rotulo_gravado = False

    @property
    def eoc(self):
        return {'FAT32': 0x0FFFFFFF, 'FAT16': 0xFFFF, 'FAT12': 0xFFF}[self.tipo]

    @property
    def csize(self):
        return self.bps * self.spc

    def _alocar(self, n):
        cs = list(range(self.livre, self.livre + n))
        self.livre += n
        assert self.livre <= self.nclusters + 2
        for a, b in zip(cs, cs[1:]):
            self.fat[a] = b
        self.fat[cs[-1]] = self.eoc
        return cs

    def _off_cluster(self, c):
        return (self.dados_ini + (c - 2) * self.spc) * self.bps

    def _gravar_cluster(self, c, dados):
        self.escritas.append((self._off_cluster(c), dados))

    def _pasta(self, caminho):
        return self.pastas[caminho]

    def _entrada(self, nome, attr, primeiro, tam, curto=None, lfn=True, data=DATA):
        curto = curto or curto_de(nome)
        e = bytearray(32)
        e[0:11] = curto
        e[11] = attr
        e[13] = 0
        struct.pack_into('<H', e, 14, dos_hora(data))
        struct.pack_into('<H', e, 16, dos_data(data))
        struct.pack_into('<H', e, 18, dos_data(data))
        struct.pack_into('<H', e, 20, primeiro >> 16)
        struct.pack_into('<H', e, 22, dos_hora(data))
        struct.pack_into('<H', e, 24, dos_data(data))
        struct.pack_into('<H', e, 26, primeiro & 0xFFFF)
        struct.pack_into('<I', e, 28, tam)
        partes = []
        if lfn:
            u = nome.encode('utf-16-le') + b'\0\0'
            u += b'\xff' * ((-len(u)) % 26)
            n = len(u) // 26
            chk = _soma_curta(curto)
            for k in range(n, 0, -1):
                p = bytearray(32)
                p[0] = k | (0x40 if k == n else 0)
                p[11], p[13] = 0x0F, chk
                bloco = u[(k - 1) * 26: k * 26]
                p[1:11], p[14:26], p[28:32] = bloco[0:10], bloco[10:22], bloco[22:26]
                partes.append(p)
        partes.append(e)
        return partes

    def mkdir(self, pai, nome, **kw):
        c = self._alocar(1)[0]
        caminho = pai.rstrip('/') + '/' + nome
        self.pastas[caminho] = Pasta(c)
        ents = self._entrada(nome, 0x10, c, 0, **kw)
        self.pastas[pai].entradas.append(ents)
        return caminho

    def add(self, pai, nome, dados, fragmentar=False, cluster=None, **kw):
        """Grava um arquivo. `cluster`: força o primeiro cluster (para simular reuso), sem tocar na FAT."""
        n = max(1, -(-len(dados) // self.csize)) if dados else 0
        if cluster is not None:
            cs = list(range(cluster, cluster + n))
            for a, b in zip(cs, cs[1:]):
                self.fat[a] = b
            if cs:
                self.fat[cs[-1]] = self.eoc
        elif fragmentar and n >= 2:
            a = self._alocar(n // 2)
            self.livre += 3                                    # buraco: outro arquivo já ocupou
            b = self._alocar(n - n // 2)
            self.fat[a[-1]] = b[0]
            cs = a + b
        else:
            cs = self._alocar(n) if n else []
        for i, c in enumerate(cs):
            self._gravar_cluster(c, dados[i * self.csize:(i + 1) * self.csize].ljust(self.csize, b'\0'))
        ents = self._entrada(nome, 0x20, cs[0] if cs else 0, len(dados), **kw)
        self.pastas[pai].entradas.append(ents)
        return ents, cs

    def apagar(self, ents, cs=()):
        """Apaga como o Windows: 0xE5 em cada entrada e a cadeia da FAT zerada; os dados ficam."""
        for e in ents:
            e[0] = 0xE5
        for c in cs:
            self.fat[c] = 0

    def apagar_pasta(self, caminho, ents):
        self.apagar(ents, [self.pastas[caminho].primeiro])

    def montar(self):
        out = []
        # pastas: grava o conteúdo, encadeando clusters extras se passar de um cluster
        for caminho, p in self.pastas.items():
            plano = b''.join(bytes(x) for lote in p.entradas for x in lote)
            if caminho == '/' and self.tipo != 'FAT32':
                rot = bytearray(32)
                rot[0:11] = self.rotulo.ljust(11).encode('cp437')
                rot[11] = 0x08
                plano = bytes(rot) + plano
                out.append(((self.reservados + 2 * self.fatsz) * 512, plano.ljust(self.raiz_ent * 32, b'\0')))
                continue
            if caminho == '/':
                rot = bytearray(32)
                rot[0:11] = self.rotulo.ljust(11).encode('cp437')
                rot[11] = 0x08
                plano = bytes(rot) + plano
            n = max(1, -(-len(plano) // self.csize))
            cs = [p.primeiro] + (self._alocar(n - 1) if n > 1 else [])
            if n > 1:
                for a, b in zip(cs, cs[1:]):
                    self.fat[a] = b
                self.fat[cs[-1]] = self.eoc
            for i, c in enumerate(cs):
                out.append((self._off_cluster(c), plano[i * self.csize:(i + 1) * self.csize].ljust(self.csize, b'\0')))
        # FAT
        if self.tipo == 'FAT32':
            fat = b''.join(struct.pack('<I', v) for v in self.fat)
        elif self.tipo == 'FAT16':
            fat = b''.join(struct.pack('<H', v) for v in self.fat)
        else:
            f = bytearray((len(self.fat) * 3 + 1) // 2 + 2)
            for c, v in enumerate(self.fat):
                o = c + c // 2
                w = f[o] | (f[o + 1] << 8)
                w = (w & 0x000F) | (v << 4) if c & 1 else (w & 0xF000) | v
                f[o], f[o + 1] = w & 0xFF, w >> 8
            fat = bytes(f)
        fat = fat.ljust(self.fatsz * 512, b'\0')
        boot = bytearray(512)
        boot[0:3] = b'\xeb\x58\x90'
        boot[3:11] = b'MSDOS5.0'
        struct.pack_into('<HBHBH', boot, 11, 512, self.spc, self.reservados, 2, self.raiz_ent)
        if self.tipo != 'FAT32' and self.total < 65536:
            struct.pack_into('<H', boot, 19, self.total)
        boot[21] = 0xF8
        if self.tipo != 'FAT32':
            struct.pack_into('<H', boot, 22, self.fatsz)
        struct.pack_into('<I', boot, 32, self.total if (self.tipo == 'FAT32' or self.total >= 65536) else 0)
        if self.tipo == 'FAT32':
            struct.pack_into('<II', boot, 36, self.fatsz, 0)
            struct.pack_into('<I', boot, 44, self.raiz.primeiro)
            boot[71:82] = self.rotulo.ljust(11).encode('cp437')
            boot[82:90] = b'FAT32   '
        else:
            boot[43:54] = self.rotulo.ljust(11).encode('cp437')
            boot[54:62] = (self.tipo + '   ').encode()
        boot[510:512] = b'\x55\xaa'
        base = [(0, bytes(boot)), (self.reservados * 512, fat), ((self.reservados + self.fatsz) * 512, fat)]
        return base + self.escritas + out

    def gravar(self, caminho):
        # a ordem importa: dados primeiro, depois FAT/pastas; a FAT é montada por último
        itens = self.montar()
        with open(caminho, 'wb') as f:
            f.truncate(self.total * 512)
            for off, dados in itens:
                f.seek(off)
                f.write(dados)
        return caminho


# ---------------------------------------------------------------------------------------------------------------
class ExFatImg:
    """exFAT com setor de 512 B e 2 setores por cluster; bitmap no cluster 2, tabela de maiúsculas no 3, raiz no 4."""

    def __init__(self, rotulo='TESTE'):
        self.bps, self.spc = 512, 2
        self.nclusters = 4000
        self.rotulo = rotulo
        self.fat_off = 24
        self.fat_len = 32
        self.heap_off = 64
        self.total = self.heap_off + self.nclusters * self.spc
        self.livre = 5
        self.bitmap = bytearray((self.nclusters + 7) // 8)
        for c in (2, 3, 4):
            self._bit(c, True)
        self.fat = {0: 0xFFFFFFF8, 1: 0xFFFFFFFF, 2: 0xFFFFFFFF, 3: 0xFFFFFFFF, 4: 0xFFFFFFFF}
        self.escritas = []
        self.entradas = {'/': []}
        self.fats = {}

    @property
    def csize(self):
        return self.bps * self.spc

    def _bit(self, c, v):
        i = c - 2
        if v:
            self.bitmap[i // 8] |= 1 << (i % 8)
        else:
            self.bitmap[i // 8] &= ~(1 << (i % 8))

    def _off(self, c):
        return (self.heap_off + (c - 2) * self.spc) * self.bps

    def _conjunto(self, nome, attr, primeiro, tam, nofat, data=DATA):
        u = nome.encode('utf-16-le')
        nomes = [u[i:i + 30].ljust(30, b'\0') for i in range(0, len(u), 30)]
        prim = bytearray(32)
        prim[0], prim[1] = 0x85, 1 + len(nomes)
        struct.pack_into('<H', prim, 4, attr)
        ts = ((data[0] - 1980) << 25) | (data[1] << 21) | (data[2] << 16) | (data[3] << 11) | (data[4] << 5) | (data[5] // 2)
        struct.pack_into('<III', prim, 8, ts, ts, ts)
        fl = bytearray(32)
        fl[0], fl[1] = 0xC0, 0x01 | (0x02 if nofat else 0)
        fl[3] = len(u) // 2
        struct.pack_into('<Q', fl, 8, tam)
        struct.pack_into('<I', fl, 20, primeiro)
        struct.pack_into('<Q', fl, 24, tam)
        ents = [prim, fl]
        for n in nomes:
            e = bytearray(32)
            e[0] = 0xC1
            e[2:32] = n
            ents.append(e)
        return ents

    def mkdir(self, pai, nome):
        c = self.livre
        self.livre += 1
        self._bit(c, True)
        self.fat[c] = 0xFFFFFFFF
        caminho = pai.rstrip('/') + '/' + nome
        self.entradas[caminho] = []
        self.entradas[pai].append(self._conjunto(nome, 0x10, c, self.csize, True))
        self.fats[caminho] = c
        return caminho

    def add(self, pai, nome, dados, cluster=None, nofat=True):
        n = -(-len(dados) // self.csize) if dados else 0
        if cluster is None:
            cluster = self.livre
            self.livre += n
        cs = list(range(cluster, cluster + n))
        for i, c in enumerate(cs):
            self._bit(c, True)
            self.escritas.append((self._off(c), dados[i * self.csize:(i + 1) * self.csize].ljust(self.csize, b'\0')))
            if not nofat:
                self.fat[c] = cs[i + 1] if i + 1 < n else 0xFFFFFFFF
        ents = self._conjunto(nome, 0x20, cluster if n else 0, len(dados), nofat)
        self.entradas[pai].append(ents)
        return ents, cs

    def apagar(self, ents, cs=()):
        for e in ents:
            e[0] &= 0x7F
        for c in cs:
            self._bit(c, False)
            self.fat.pop(c, None)

    def gravar(self, caminho):
        itens = list(self.escritas)
        for cam, lotes in self.entradas.items():
            plano = b''.join(bytes(x) for lote in lotes for x in lote)
            if cam == '/':
                rot = bytearray(32)
                rot[0] = 0x83
                rot[1] = len(self.rotulo)
                rot[2:2 + 2 * len(self.rotulo)] = self.rotulo.encode('utf-16-le')
                bm = bytearray(32)
                bm[0] = 0x81
                struct.pack_into('<IQ', bm, 20, 2, len(self.bitmap))
                up = bytearray(32)
                up[0] = 0x82
                struct.pack_into('<IQ', up, 20, 3, 128)
                plano = bytes(rot + bm + up) + plano
                c = 4
            else:
                c = self.fats[cam]
            assert len(plano) <= self.csize
            itens.append((self._off(c), plano.ljust(self.csize, b'\0')))
        itens.append((self._off(2), bytes(self.bitmap)))
        itens.append((self._off(3), b'\0' * 128))
        fat = b''.join(struct.pack('<I', self.fat.get(c, 0)) for c in range(self.nclusters + 2))
        itens.append((self.fat_off * self.bps, fat.ljust(self.fat_len * self.bps, b'\0')))
        boot = bytearray(512)
        boot[0:3] = b'\xeb\x76\x90'
        boot[3:11] = b'EXFAT   '
        struct.pack_into('<QIIIII', boot, 72, self.total, self.fat_off, self.fat_len, self.heap_off, self.nclusters, 4)
        boot[108], boot[109], boot[110] = 9, 1, 1
        boot[510:512] = b'\x55\xaa'
        itens.append((0, bytes(boot)))
        with open(caminho, 'wb') as f:
            f.truncate(self.total * self.bps)
            for off, dados in itens:
                f.seek(off)
                f.write(dados)
        return caminho
