# -*- coding: utf-8 -*-
"""A busca de partição perdida acha FAT12/16 sem o rótulo no setor de boot, pela estrutura do BPB, e não inventa
FAT em setor qualquer com 55AA."""
import os
import random
import struct
import unittest

from auxiliar import CasoComPasta
import imagens_particoes as ip
from test_fat16_sem_rotulo import boot_fat16

from recuperacao import leitor, particoes

MIB = ip.MIB
INICIO = 5 * MIB
TOTAL = 16384                       # setores: 8 MiB, FAT16 com 1 setor por cluster


class BuscaFatSemRotulo(CasoComPasta):
    def abrir(self, img):
        caminho = os.path.join(self.trabalho, 'disco.img')
        with open(caminho, 'wb') as f:
            f.write(img)
        l = leitor.abrir(caminho)
        self.addCleanup(l.fechar)
        return l

    def disco(self, rotulo=b'        '):
        img = bytearray(16 * MIB)
        boot = boot_fat16(rotulo, total=TOTAL, fat=64, spc=1)
        struct.pack_into('<I', boot, 28, INICIO // 512)
        ip.poe(img, INICIO, boot)
        return img

    def test_fat16_sem_rotulo_e_sem_tabela_aparece_na_busca(self):
        for rotulo in (b'        ', b'\x00' * 8, b'\xf6\x01xyz\xff\x07\x00'):
            r = particoes.buscar(self.abrir(self.disco(rotulo)))
            achados = [(c['inicio'], c['tamanho'], c['sistema']) for c in r['candidatos']]
            self.assertEqual(achados, [(INICIO, TOTAL * 512, 'fat16')], rotulo)

    def test_com_rotulo_continua_um_hit_so(self):
        r = particoes.buscar(self.abrir(self.disco(b'FAT16   ')))
        self.assertEqual([(c['inicio'], c['sistema']) for c in r['candidatos']], [(INICIO, 'fat16')])
        hits, _ = particoes.hits_do_bloco(self.abrir(self.disco(b'FAT16   ')), 4 * MIB)
        self.assertEqual(len(hits), 1)

    def test_lixo_com_55aa_nao_gera_hit(self):
        rnd = random.Random(7)
        img = bytearray(16 * MIB)
        for k in range(3000):
            s = bytearray(rnd.randbytes(512))
            s[0] = rnd.choice((0xEB, 0xE9))
            if s[0] == 0xEB:
                s[2] = 0x90
            s[510:512] = b'\x55\xaa'
            ip.poe(img, (100 + k) * 512, s)
        hits, _ = particoes.hits_do_bloco(self.abrir(img), 0)
        self.assertEqual(hits, [])
        self.assertEqual(particoes.buscar(self.abrir(img))['candidatos'], [])


if __name__ == '__main__':
    unittest.main()
