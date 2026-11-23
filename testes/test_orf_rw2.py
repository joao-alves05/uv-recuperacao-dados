# -*- coding: utf-8 -*-
"""ORF (Olympus) e RW2 (Panasonic): variantes de TIFF; o fim vem das tiras, do JPEG de prévia e do RawDataOffset."""
import struct
import unittest

import amostras as A
from test_carving import Memoria, com_folga, varrer

from recuperacao import validar

ASSIN = {'tipo': 'ORF', 'tamanho_max': 1 << 28}


def monta(cab, campos, dados, prox=0, ifd=8):
    """cab de 4 bytes; campos [(tag, tipo, cnt, valor)] com offsets absolutos; `dados` vem logo depois do IFD."""
    le = cab[:2] == b'II'
    e = '<' if le else '>'
    campos = sorted(campos)
    b = struct.pack(e + 'H', len(campos))
    for tag, tp, cnt, val in campos:
        v = struct.pack(e + 'H', val) + b'\0\0' if tp == 3 and cnt == 1 else struct.pack(e + 'I', val)
        b += struct.pack(e + 'HHI', tag, tp, cnt) + v
    b += struct.pack(e + 'I', prox)
    return cab + struct.pack(e + 'I', ifd) + b + dados


def orf(cab=b'IIRO', jpg=1500, strip=9000):
    n = 6
    base = 8 + 2 + n * 12 + 4
    campos = [(256, 3, 1, 16), (257, 3, 1, 12), (273, 4, 1, base + jpg), (279, 4, 1, strip),
              (513, 4, 1, base), (514, 4, 1, jpg)]
    return monta(cab, campos, b'\xff\xd8' + A.aleatorio(jpg - 4, 1) + b'\xff\xd9' + A.aleatorio(strip, 2))


def rw2(jpg=1500, strip=9000):
    n = 5
    base = 8 + 2 + n * 12 + 4
    campos = [(0x2E, 7, jpg, base), (273, 4, 1, base + jpg), (279, 4, 1, strip), (280, 4, 1, base + jpg),
              (256, 3, 1, 16)]
    return monta(b'IIU\x00', campos, b'\xff\xd8' + A.aleatorio(jpg - 4, 3) + b'\xff\xd9' + A.aleatorio(strip, 4))


def validar_tiff(dados, off=0, assin=ASSIN):
    return validar.tiff(validar.Janela(Memoria(dados)), off, len(dados) - off, assin)


class OrfRw2(unittest.TestCase):
    def test_orf_inteiro_e_bom_nos_dois_enderecamentos(self):
        for cab in (b'IIRO', b'IIRS', b'MMOR'):
            arq = orf(cab)
            r = validar_tiff(bytes(100) + arq + bytes(3000), 100)
            self.assertEqual((r['estado'], r['tamanho'], r['tipo'], r['extensao']),
                             ('bom', len(arq), 'ORF', 'orf'), cab)

    def test_rw2_inteiro_e_bom(self):
        arq = rw2()
        r = validar_tiff(bytes(50) + arq + bytes(3000), 50)
        self.assertEqual((r['estado'], r['tamanho'], r['tipo'], r['extensao']),
                         ('bom', len(arq), 'RW2', 'rw2'))

    def test_jpeg_de_previa_depois_das_tiras_conta_para_o_fim(self):
        campos = [(256, 3, 1, 16), (257, 3, 1, 12), (273, 4, 1, 8 + 2 + 6 * 12 + 4), (279, 4, 1, 500),
                  (513, 4, 1, 8 + 2 + 6 * 12 + 4 + 500), (514, 4, 1, 1200)]
        arq = monta(b'IIRO', campos, A.aleatorio(500, 5) + b'\xff\xd8' + A.aleatorio(1196, 6) + b'\xff\xd9')
        r = validar_tiff(arq + bytes(2000))
        self.assertEqual((r['estado'], r['tamanho']), ('bom', len(arq)))

    def test_cortado_e_parcial(self):
        for arq, tipo in ((orf(), 'ORF'), (rw2(), 'RW2')):
            r = validar_tiff(arq[:len(arq) - 3000])
            self.assertEqual((r['estado'], r['tipo']), ('parcial', tipo))

    def test_rw2_cortado_antes_dos_dados_raw_e_parcial(self):
        arq = rw2()
        r = validar_tiff(arq[:len(arq) - 9000 + 10])
        self.assertEqual((r['estado'], r['tipo']), ('parcial', 'RW2'))

    def test_ifd_em_laco_nao_vira_achado(self):
        arq = bytearray(orf())
        n = struct.unpack_from('<H', arq, 8)[0]
        struct.pack_into('<I', arq, 8 + 2 + n * 12, 8)              # o IFD seguinte é ele mesmo
        self.assertIsNone(validar_tiff(bytes(arq)))

    def test_fim_acima_do_teto_nao_vira_achado(self):
        self.assertIsNone(validar_tiff(orf(), assin={'tipo': 'ORF', 'tamanho_max': 5000}))

    def test_lixo_com_o_cabecalho_nao_vira_achado(self):
        for cab in (b'IIRO', b'IIU\x00', b'MMOR'):
            self.assertIsNone(validar_tiff(cab + A.aleatorio(3000, 8)))

    def test_varredura_acha_cada_tipo(self):
        for arq, tipo in ((orf(), 'ORF'), (orf(b'MMOR'), 'ORF'), (rw2(), 'RW2')):
            achados = [a for a in varrer(com_folga(arq))[0] if a['tipo'] == tipo]
            self.assertEqual([(a['estado'], a['tamanho'], a['offset'], a['grupo']) for a in achados],
                             [('bom', len(arq), 1024, 'foto')], tipo)


if __name__ == '__main__':
    unittest.main()
