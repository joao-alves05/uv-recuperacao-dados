# -*- coding: utf-8 -*-
"""3GP e 3G2: ISO-BMFF com brand 3gp*/3g2*; saem como 3GP/3G2 (video), MP4 e CR3 não mudam."""
import unittest

from test_carving import Memoria
from test_cr3 import iso_arq

from recuperacao import validar

ASSIN = {'tipo': 'MP4', 'tamanho_max': 1 << 33}


def validar_iso(dados, off=0):
    return validar.iso(validar.Janela(Memoria(dados)), off, len(dados) - off, ASSIN)


class Gp(unittest.TestCase):
    def test_inteiro_e_bom_com_o_tamanho_certo(self):
        for marca, tipo, ext in ((b'3gp4', '3GP', '3gp'), (b'3gp5', '3GP', '3gp'), (b'3gp6', '3GP', '3gp'),
                                 (b'3ge6', '3GP', '3gp'), (b'3gg6', '3GP', '3gp'),
                                 (b'3g2a', '3G2', '3g2'), (b'3g2b', '3G2', '3g2'), (b'3g2c', '3G2', '3g2')):
            arq = iso_arq(marca)
            r = validar_iso(bytes(100) + arq + bytes(3000), 100)
            self.assertEqual((r['estado'], r['tamanho'], r['tipo'], r['extensao'], r['grupo']),
                             ('bom', len(arq), tipo, ext, 'video'), marca)

    def test_cortado_e_parcial(self):
        for marca, tipo in ((b'3gp4', '3GP'), (b'3g2a', '3G2')):
            arq = iso_arq(marca)
            r = validar_iso(arq[:len(arq) - 2500])
            self.assertEqual((r['estado'], r['tipo']), ('parcial', tipo))

    def test_mp4_e_cr3_continuam_os_mesmos(self):
        self.assertEqual(validar_iso(iso_arq(b'isom'))['tipo'], 'MP4')
        self.assertEqual(validar_iso(iso_arq(b'mp42'))['tipo'], 'MP4')
        self.assertEqual(validar_iso(iso_arq(b'crx '))['tipo'], 'CR3')


if __name__ == '__main__':
    unittest.main()
