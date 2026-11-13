# -*- coding: utf-8 -*-
"""FAT12/FAT16 cujo setor de boot não tem o rótulo "FAT12   "/"FAT16   " (campo só informativo): reconhecidos pela
estrutura do BPB. Imagens sintéticas em pasta temporária; setor qualquer com 55AA e lixo não pode virar FAT."""
import os
import random
import struct
import unittest

from auxiliar import CasoComPasta
import imagens_particoes as ip

from recuperacao import leitor, particoes

MIB = ip.MIB


def boot_fat16(rotulo=b'        ', total=32768, fat=32, spc=4, raiz=512):
    b = bytearray(512)
    b[0:3] = b'\xeb\x3c\x90'
    b[3:11] = b'MSDOS5.0'
    struct.pack_into('<HBHBHHBHHHII', b, 11, 512, spc, 1, 2, raiz, total if total < 65536 else 0, 0xF8, fat, 63, 255, 0,
                     total if total >= 65536 else 0)
    b[38] = 0x29
    b[43:54] = b'SEM ROTULO '
    b[54:62] = rotulo
    b[510:512] = b'\x55\xaa'
    return b


class FatSemRotulo(CasoComPasta):
    def abrir(self, img):
        caminho = os.path.join(self.trabalho, 'disco.img')
        with open(caminho, 'wb') as f:
            f.write(img)
        l = leitor.abrir(caminho)
        self.addCleanup(l.fechar)
        return l

    def test_fat16_sem_rotulo_e_achada_no_volume(self):
        for rotulo in (b'        ', b'\x00' * 8, b'\xf6\x01xyz\xff\x07\x00'):
            img = bytearray(16 * MIB)
            ip.poe(img, 0, boot_fat16(rotulo))
            info = particoes.det_fat(bytes(img[:512]))
            self.assertIsNotNone(info, rotulo)
            self.assertEqual(info['sistema'], 'fat16')
            tabela = particoes.analisar(self.abrir(img))
            self.assertEqual(tabela['esquema'], 'volume')
            self.assertEqual(tabela['particoes'][0]['sistema'], 'fat16')

    def test_fat16_sem_rotulo_dentro_de_particao_mbr(self):
        img = bytearray(20 * MIB)
        ini = 2048 * 512
        b = boot_fat16()
        struct.pack_into('<I', b, 28, 2048)
        ip.poe(img, ini, b)
        mbr = bytearray(512)
        struct.pack_into('<B3sB3sII', mbr, 446, 0, b'\0\0\0', 0x0E, b'\0\0\0', 2048, 32768)
        mbr[510:512] = b'\x55\xaa'
        ip.poe(img, 0, mbr)
        tabela = particoes.analisar(self.abrir(img))
        self.assertEqual([p['sistema'] for p in tabela['particoes']], ['fat16'])

    def test_fat12_sem_rotulo(self):
        info = particoes.det_fat(bytes(boot_fat16(total=2880, fat=9, spc=1, raiz=224)))
        self.assertIsNotNone(info)
        self.assertEqual(info['sistema'], 'fat12')

    def test_com_rotulo_continua_igual(self):
        info = particoes.det_fat(bytes(boot_fat16(rotulo=b'FAT16   ')))
        self.assertEqual(info['sistema'], 'fat16')

    def test_setor_com_55aa_e_lixo_nao_vira_fat(self):
        rng = random.Random(7)
        for _ in range(3000):
            b = bytearray(rng.randbytes(512))
            b[0] = rng.choice((0xEB, 0xE9))
            b[510:512] = b'\x55\xaa'
            self.assertIsNone(particoes.det_fat(bytes(b)))
        lixo = bytearray(rng.randbytes(512))
        lixo[0], lixo[510:512] = 0xEB, b'\x55\xaa'
        img = bytearray(4 * MIB)
        ip.poe(img, 0, lixo)
        self.assertNotEqual(particoes.analisar(self.abrir(img))['esquema'], 'volume')

    def test_bpb_incoerente_sem_rotulo_e_recusado(self):
        casos = {
            'fat pequena demais para os clusters': boot_fat16(fat=2),
            'raiz que não ocupa setores inteiros': boot_fat16(raiz=500),
            'salto fora do padrão': boot_fat16(),
            'clusters demais para FAT16': boot_fat16(total=600000, fat=256, spc=8),
        }
        casos['salto fora do padrão'][2] = 0x00
        for nome, b in casos.items():
            self.assertIsNone(particoes.det_fat(bytes(b)), nome)


if __name__ == '__main__':
    unittest.main()
