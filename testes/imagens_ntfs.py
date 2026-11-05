# -*- coding: utf-8 -*-
"""Monta um NTFS mínimo só em Python (clusters e registros de 1024 B, MFT em 2 pedaços) para os testes da fase 4."""
import calendar
import struct

from imagens_sa import DATA


def _alinha8(n):
    return (n + 7) & ~7


def _ft(a=DATA):
    return int(calendar.timegm((a[0], a[1], a[2], a[3], a[4], a[5], 0, 0, 0)) * 10_000_000) + 116444736000000000


def codificar_runs(runs):
    """[(lcn ou None, clusters)] -> bytes de data runs."""
    out, ant = bytearray(), 0
    for lcn, n in runs:
        nb = max(1, (n.bit_length() + 7) // 8)
        if lcn is None:
            out.append(nb)
            out += n.to_bytes(nb, 'little')
            continue
        d = lcn - ant
        ob = max(1, (d.bit_length() + 8) // 8)
        out.append((ob << 4) | nb)
        out += n.to_bytes(nb, 'little') + d.to_bytes(ob, 'little', signed=True)
        ant = lcn
    return bytes(out) + b'\0'


class NtfsImg:
    CS = 1024
    REG = 1024

    def __init__(self, rotulo='NTFSTESTE'):
        self.rotulo = rotulo
        self.nclusters = 2000
        self.mft_runs = [(4, 32), (100, 32)]            # registros 0..31 e 32..63
        self.nreg = 64
        self.bitmap_cluster = 40
        self.livre_cluster = 140
        self.prox_reg = 24
        self.escritas = []
        self.bits = set(list(range(0, 36)) + list(range(100, 132)) + [40])
        self.runs_de = {}
        self.registros = {}
        self.registros[5] = self._registro(5, 3, [self._si(), self._fn(5, '.', 0x10000000, 1)])
        self.registros[3] = self._registro(3, 1, [self._attr_res(0x60, rotulo.encode('utf-16-le'))])

    def _attr_res(self, tipo, conteudo, nome='', id_=0):
        nb = nome.encode('utf-16-le')
        coff = _alinha8(24 + len(nb))
        tam = _alinha8(coff + len(conteudo))
        a = bytearray(tam)
        struct.pack_into('<IIBBHHH', a, 0, tipo, tam, 0, len(nome), 24, 0, id_)
        struct.pack_into('<IH', a, 16, len(conteudo), coff)
        a[24:24 + len(nb)] = nb
        a[coff:coff + len(conteudo)] = conteudo
        return bytes(a)

    def _attr_nres(self, tipo, runs, real, flags=0, id_=0):
        rb = codificar_runs(runs)
        tam = _alinha8(64 + len(rb))
        a = bytearray(tam)
        total = sum(n for _, n in runs)
        struct.pack_into('<IIBBHHH', a, 0, tipo, tam, 1, 0, 64, flags, id_)
        struct.pack_into('<QQHH', a, 16, 0, max(total - 1, 0), 64, 0)
        struct.pack_into('<QQQ', a, 40, total * self.CS, real, real)
        a[64:64 + len(rb)] = rb
        return bytes(a)

    def _si(self, data=DATA):
        t = _ft(data)
        return self._attr_res(0x10, struct.pack('<QQQQ', t, t, t, t) + b'\0' * 16)

    def _fn(self, pai, nome, flags=0, ns=1, tam=0, data=DATA):
        t = _ft(data)
        c = struct.pack('<QQQQQQQIIBB', pai | (1 << 48), t, t, t, t, tam, tam, flags, 0, len(nome), ns)
        return self._attr_res(0x30, c + nome.encode('utf-16-le'))

    def _registro(self, n, flags, attrs):
        r = bytearray(self.REG)
        r[0:4] = b'FILE'
        struct.pack_into('<HH', r, 4, 48, 3)
        struct.pack_into('<HHHH', r, 16, 1, 1, 56, flags)
        pos = 56
        for a in attrs:
            r[pos:pos + len(a)] = a
            pos += len(a)
        struct.pack_into('<I', r, pos, 0xFFFFFFFF)
        struct.pack_into('<II', r, 24, pos + 8, self.REG)
        struct.pack_into('<I', r, 44, n)
        return r

    def _novo_reg(self):
        n = self.prox_reg
        self.prox_reg += 1
        assert n < self.nreg
        return n

    def add_dir(self, pai, nome):
        n = self._novo_reg()
        self.registros[n] = self._registro(n, 3, [self._si(), self._fn(pai, nome, 0x10000000, 1)])
        return n

    def add_file(self, pai, nome, dados, fragmentos=1, lcn=None, nome_dos=None, flags_dados=0):
        n = self._novo_reg()
        attrs = [self._si(), self._fn(pai, nome, 0, 1, len(dados))]
        if nome_dos:                                      # nome 8.3 (namespace DOS) vem DEPOIS no registro e não deve ganhar
            attrs.insert(1, self._fn(pai, nome_dos, 0, 2, len(dados)))
        runs = []
        if len(dados) <= 600 and fragmentos == 1 and lcn is None and not flags_dados:
            attrs.append(self._attr_res(0x80, dados))
        else:
            total = max(1, -(-len(dados) // self.CS))
            partes = [total] if fragmentos == 1 else [total // 2, total - total // 2]
            pos = 0
            for q in partes:
                if lcn is not None and pos == 0:
                    c = lcn
                else:
                    c = self.livre_cluster
                    self.livre_cluster += q + (3 if fragmentos > 1 else 0)       # buraco entre os pedaços
                runs.append((c, q))
                self.escritas.append((c * self.CS, dados[pos * self.CS:(pos + q) * self.CS].ljust(q * self.CS, b'\0')))
                self.bits.update(range(c, c + q))
                pos += q
            attrs.append(self._attr_nres(0x80, runs, len(dados), flags=flags_dados))
        self.registros[n] = self._registro(n, 1, attrs)
        self.runs_de[n] = runs
        return n

    def apagar(self, n):
        r = self.registros[n]
        struct.pack_into('<H', r, 22, struct.unpack_from('<H', r, 22)[0] & ~1)
        for lcn, q in self.runs_de.get(n, []):
            self.bits.difference_update(range(lcn, lcn + q))

    def gravar(self, caminho):
        itens = []
        bm = bytearray((self.nclusters + 7) // 8)
        for c in self.bits:
            bm[c // 8] |= 1 << (c % 8)
        self.registros[6] = self._registro(6, 1, [self._attr_nres(0x80, [(self.bitmap_cluster, 1)], len(bm))])
        itens.append((self.bitmap_cluster * self.CS, bytes(bm).ljust(self.CS, b'\0')))
        self.registros[0] = self._registro(0, 1, [self._attr_nres(0x80, self.mft_runs, self.nreg * self.REG)])
        pos = 0
        for lcn, q in self.mft_runs:
            for k in range(q):
                r = self.registros.get(pos + k)
                if r is not None:
                    b = bytearray(r)
                    chk = b'\x55\xaa'
                    b[48:50] = chk
                    for s in (1, 2):
                        b[48 + 2 * s:50 + 2 * s] = b[s * 512 - 2:s * 512]
                        b[s * 512 - 2:s * 512] = chk
                    itens.append(((lcn + k) * self.CS, bytes(b)))
            pos += q
        boot = bytearray(512)
        boot[0:3] = b'\xeb\x52\x90'
        boot[3:11] = b'NTFS    '
        struct.pack_into('<HB', boot, 11, 512, 2)
        struct.pack_into('<QQQ', boot, 40, self.nclusters * 2, self.mft_runs[0][0], 2)
        boot[64] = 0xF6                                      # registro de 2^10 bytes
        boot[68] = 1
        boot[510:512] = b'\x55\xaa'
        itens.append((0, bytes(boot)))
        with open(caminho, 'wb') as f:
            f.truncate(self.nclusters * self.CS)
            for off, d in self.escritas + itens:
                f.seek(off)
                f.write(d)
        return caminho
