# -*- coding: utf-8 -*-
"""ext2 com arquivo de mais de 1 Mi blocos (> 1 GiB com blocos de 1 KiB): o mapa é de runs, sem teto de blocos.

A imagem é falsa: o leitor devolve zeros para os blocos de dados e só os metadados (superbloco, inode, blocos de
ponteiros) são reais. Nada de 1 GiB em disco.
"""
import os
import struct
import unittest
import unittest.mock as m

from auxiliar import CasoComPasta

import imagens_ext as ie
from recuperacao import sa
from recuperacao.sa import ext as mod_ext

BS = ie.BS
NBLOCOS = 2000000                 # blocos do volume falso
DADOS = 10000                     # primeiro bloco físico dos dados do arquivo
META = 1900000                    # onde ficam os blocos de ponteiros
N = (1 << 20) + 5000              # blocos lógicos do arquivo grande: 1 GiB + 5000 KiB
PPB = BS // 4


class LeitorFalso:
    """Zeros em tudo, menos nos bytes de `base` (imagem pequena real) e nos blocos de `extra`."""
    def __init__(self, base, extra):
        self.base, self.extra, self.tamanho, self.setor = base, extra, NBLOCOS * BS, 512

    def ler(self, off, n):
        out = bytearray()
        while n > 0:
            b, d = divmod(off, BS)
            q = min(n, BS - d)
            if b in self.extra:
                out += self.extra[b][d:d + q]
            else:
                out += self.base[off:off + q].ljust(q, b'\0')
            off += q
            n -= q
        return bytes(out)


class Montagem:
    """Monta o ext2 falso com um arquivo `grande.bin` de N blocos; `fis(l)` dá o bloco físico do bloco lógico l."""
    def __init__(self, caminho, fis, n=N):
        f = ie.ExtImg('ext2')
        ino = f.add_file(2, 'grande.bin', b'x' * BS)
        f.gravar(caminho)
        with open(caminho, 'rb') as g:
            self.base = bytearray(g.read())
        struct.pack_into('<I', self.base, 1024 + 4, NBLOCOS)
        self.extra, self.prox, self.n, self.fis = {}, META, n, fis
        ib = f.tabela * BS + (ino - 1) * f.isize
        struct.pack_into('<I', self.base, ib + 4, n * BS)
        ptrs = [fis(l) for l in range(12)] + [self.nivel(12, 1), self.nivel(12 + PPB, 2),
                                              self.nivel(12 + PPB + PPB * PPB, 3), ]
        struct.pack_into('<15I', self.base, ib + 40, *ptrs)
        self.ib = ib

    def novo(self, ptrs):
        b = self.prox
        self.prox += 1
        self.extra[b] = struct.pack('<%dI' % PPB, *(list(ptrs) + [0] * (PPB - len(ptrs))))
        return b

    def nivel(self, ini, nivel):
        """Bloco indireto de `nivel` que começa no bloco lógico `ini` (0 se o arquivo acaba antes)."""
        if ini >= self.n:
            return 0
        if nivel == 1:
            return self.novo([self.fis(l) for l in range(ini, min(ini + PPB, self.n))])
        passo = PPB ** (nivel - 1)
        return self.novo([self.nivel(ini + k * passo, nivel - 1) for k in range(PPB) if ini + k * passo < self.n])

    def varrer(self):
        vol = sa.ext.reconhecer(LeitorFalso(self.base, self.extra))
        assert vol is not None
        return {e.nome: e for e in vol.varrer()}


def contiguo(l):
    return DADOS + l


def com_buraco_de_fragmento(l):
    return DADOS + l + (7 if l >= 500000 else 0)        # um salto: dois runs


class TestExtGrande(CasoComPasta):
    def montar(self, fis, n=N):
        return Montagem(os.path.join(self.pasta, 'g.img'), fis, n)

    def test_arquivo_de_mais_de_1_gib_sai_bom_com_um_run(self):
        g = self.montar(contiguo).varrer()['grande.bin']
        self.assertEqual(g.estado, 'bom', g.nota)
        self.assertEqual(g.tamanho, N * BS)
        self.assertEqual(g.runs, [(DADOS * BS, N * BS)])

    def test_fragmentos_viram_poucos_runs_com_os_extents_certos(self):
        g = self.montar(com_buraco_de_fragmento).varrer()['grande.bin']
        self.assertEqual(g.estado, 'bom', g.nota)
        self.assertEqual(g.runs, [(DADOS * BS, 500000 * BS), ((DADOS + 500007) * BS, (N - 500000) * BS)])

    def test_ponteiro_em_loop_nao_trava_e_sai_parcial_com_aviso(self):
        mt = self.montar(contiguo)
        tind = struct.unpack_from('<15I', mt.base, mt.ib + 40)[14]
        mt.extra[tind] = struct.pack('<%dI' % PPB, *([tind] * PPB))          # o triplo aponta para si mesmo
        g = mt.varrer()['grande.bin']
        self.assertEqual(g.estado, 'parcial')
        self.assertIn('loop', g.nota)
        self.assertEqual(g.runs, [(DADOS * BS, (12 + PPB + PPB * PPB) * BS)])    # o que veio antes do loop é mantido

    def test_bloco_indireto_fora_do_volume_sai_parcial(self):
        mt = self.montar(contiguo)
        struct.pack_into('<I', mt.base, mt.ib + 40 + 4 * 13, NBLOCOS + 5)         # duplo indireto fora do volume
        g = mt.varrer()['grande.bin']
        self.assertEqual(g.estado, 'parcial')
        self.assertIn('fora do volume', g.nota)

    def test_limite_de_blocos_indiretos_lidos(self):
        with m.patch.object(mod_ext, 'MAX_INDIRETOS', 100):
            g = self.montar(contiguo).varrer()['grande.bin']
        self.assertEqual(g.estado, 'parcial')
        self.assertIn('indiretos demais', g.nota)
        self.assertLess(sum(q for _, q in g.runs), N * BS)

    def test_limite_de_runs_por_arquivo(self):
        fragmentado = lambda l: DADOS + 2 * l                                     # cada bloco é um run
        with m.patch.object(mod_ext, 'MAX_RUNS_MAPA', 1000):
            g = self.montar(fragmentado, n=20000).varrer()['grande.bin']
        self.assertEqual(g.estado, 'parcial')
        self.assertIn('fragmentos demais', g.nota)
        self.assertLessEqual(len(g.runs), 1001)


if __name__ == '__main__':
    unittest.main()
