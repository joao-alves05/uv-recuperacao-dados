# -*- coding: utf-8 -*-
"""CR3 (RAW da Canon): ISO-BMFF com ftyp 'crx '; sai como CR3/foto, nunca como MP4."""
import unittest

import amostras as A
from test_carving import Memoria, com_folga, varrer

from recuperacao import validar

ASSIN = {'tipo': 'MP4', 'tamanho_max': 1 << 33}


def caixa(tp, corpo=b''):
    return (8 + len(corpo)).to_bytes(4, 'big') + tp + corpo


def iso_arq(marca, mdat=6000):
    ftyp = caixa(b'ftyp', marca + b'\x00\x00\x00\x01' + marca + b'isom')
    uuid = caixa(b'uuid', bytes.fromhex('85c0b687820f11e08111f4ce462b6a48') + A.aleatorio(300, 1))
    moov = caixa(b'moov', uuid)
    return ftyp + moov + caixa(b'mdat', A.aleatorio(mdat, 2))


def cr3(): return iso_arq(b'crx ')
def mp4(): return iso_arq(b'isom')


def validar_iso(dados, off=0):
    return validar.iso(validar.Janela(Memoria(dados)), off, len(dados) - off, ASSIN)


class Cr3(unittest.TestCase):
    def test_inteiro_e_cr3_bom_com_o_tamanho_certo(self):
        arq = cr3()
        r = validar_iso(bytes(100) + arq + bytes(3000), 100)
        self.assertEqual((r['estado'], r['tamanho'], r['tipo'], r['extensao'], r['grupo']),
                         ('bom', len(arq), 'CR3', 'cr3', 'foto'))

    def test_cortado_e_parcial(self):
        arq = cr3()
        r = validar_iso(arq[:len(arq) - 2500])
        self.assertEqual((r['estado'], r['tipo']), ('parcial', 'CR3'))

    def test_mp4_comum_continua_mp4(self):
        r = validar_iso(mp4())
        self.assertEqual((r['estado'], r['tipo'], r['extensao'], r['grupo']), ('bom', 'MP4', 'mp4', 'video'))

    def test_varredura_acha_o_cr3_pela_tabela(self):
        arq = cr3()
        achados = varrer(com_folga(arq))[0]
        cr = [a for a in achados if a['tipo'] == 'CR3']
        self.assertEqual(len(cr), 1)
        self.assertEqual(cr[0]['tamanho'], len(arq))


if __name__ == '__main__':
    unittest.main()
