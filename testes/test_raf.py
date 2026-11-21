# -*- coding: utf-8 -*-
"""RAF (RAW da Fujifilm): fim = maior offset + tamanho entre JPEG, cabeçalho CFA e dados CFA (big-endian)."""
import struct
import unittest

import amostras as A
from test_carving import Memoria, com_folga, varrer

from recuperacao import validar

ASSIN = {'tipo': 'RAF', 'tamanho_max': 1 << 28}


def raf(jpg=3000, cfa_h=500, cfa=20000, ofs=None):
    jo = 160
    ho = jo + jpg
    co = ho + cfa_h
    if ofs:
        jo, ho, co = ofs
    cab = b'FUJIFILMCCD-RAW ' + b'0201' + b'FF383501' + b'X-T30'.ljust(32, b'\0') + b'0100' + bytes(20)
    cab += struct.pack('>6I', jo, jpg, ho, cfa_h, co, cfa)
    cab = cab.ljust(160, b'\0')
    corpo = b'\xff\xd8' + A.aleatorio(jpg - 4, 1) + b'\xff\xd9' + A.aleatorio(cfa_h, 2) + A.aleatorio(cfa, 3)
    return cab + corpo


def validar_raf(dados, off=0):
    return validar.raf(validar.Janela(Memoria(dados)), off, len(dados) - off, ASSIN)


class Raf(unittest.TestCase):
    def test_inteiro_e_bom_com_o_tamanho_certo(self):
        arq = raf()
        r = validar_raf(bytes(100) + arq + bytes(4000), 100)
        self.assertEqual((r['estado'], r['tamanho'], r['tipo'], r['extensao'], r['grupo']),
                         ('bom', len(arq), 'RAF', 'raf', 'foto'))

    def test_varredura_acha_com_inicio_e_tamanho_certos(self):
        arq = raf()
        achados = [a for a in varrer(com_folga(arq))[0] if a['tipo'] == 'RAF']
        self.assertEqual([(a['estado'], a['tamanho'], a['offset']) for a in achados], [('bom', len(arq), 1024)])

    def test_cortado_e_parcial(self):
        arq = raf()
        r = validar_raf(arq[:len(arq) - 5000])
        self.assertEqual((r['estado'], r['tamanho'], r['tipo']), ('parcial', len(arq) - 5000, 'RAF'))

    def test_offsets_incoerentes_nao_viram_achado(self):
        self.assertIsNone(validar_raf(raf(ofs=(160, 3160, 3000))))             # blocos sobrepostos
        self.assertIsNone(validar_raf(raf(ofs=(10, 3160, 3660))))              # JPEG dentro do cabeçalho
        arq = bytearray(raf())
        arq[104:108] = struct.pack('>I', 1 << 29)                              # CFA declarado além do teto
        self.assertIsNone(validar_raf(bytes(arq)))
        arq = bytearray(raf())
        arq[88:92] = bytes(4)                                                  # JPEG com tamanho zero
        self.assertIsNone(validar_raf(bytes(arq)))
        arq = bytearray(raf())
        arq[160:162] = b'\x00\x00'                                             # JPEG embutido sem FFD8
        self.assertIsNone(validar_raf(bytes(arq)))

    def test_so_o_texto_do_cabecalho_nao_basta(self):
        self.assertIsNone(validar_raf(b'FUJIFILMCCD-RAW ' + A.aleatorio(2000, 4)))


if __name__ == '__main__':
    unittest.main()
