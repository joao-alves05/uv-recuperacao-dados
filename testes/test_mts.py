# -*- coding: utf-8 -*-
"""MPEG-TS (188) e AVCHD MTS/M2TS (192): 5 sync seguidos no passo certo; o fim é onde a cadência quebra."""
import time
import unittest

import amostras as A
from test_carving import com_folga, varrer

PAT = bytes([0x47, 0x40, 0x00, 0x10])


def pacote(i, passo):
    corpo = bytearray(A.aleatorio(184, i + 1))
    corpo = bytes(b if b != 0x47 else 0x46 for b in corpo)
    cab = PAT if i == 0 else bytes([0x47, 0x01, 0x00, 0x10 | (i & 15)])
    p = cab + corpo
    return (bytes([0, 0, 0, i & 255]) + p) if passo == 192 else p


def arquivo(passo, n=40):
    return b''.join(pacote(i, passo) for i in range(n))


def achar(dados, tipo):
    achados = varrer(dados)[0]
    return [a for a in achados if a['tipo'] == tipo]


class Ts(unittest.TestCase):
    def test_inteiro_e_bom_com_o_tamanho_certo(self):
        for passo, tipo, ext in ((188, 'TS', 'ts'), (192, 'MTS', 'mts')):
            arq = arquivo(passo)
            r = achar(com_folga(arq, 1024, 3000), tipo)
            self.assertEqual(len(r), 1, tipo)
            self.assertEqual((r[0]['estado'], r[0]['tamanho'], r[0]['extensao'], r[0]['grupo'], r[0]['offset']),
                             ('bom', len(arq), ext, 'video', 1024), tipo)

    def test_cortado_no_meio_de_pacote_e_parcial_alinhado_ao_ultimo_inteiro(self):
        for passo, tipo in ((188, 'TS'), (192, 'MTS')):
            arq = arquivo(passo)[:passo * 30 + 100]
            r = achar(arq, tipo)
            self.assertEqual(len(r), 1, tipo)
            self.assertEqual((r[0]['estado'], r[0]['tamanho']), ('parcial', passo * 30), tipo)

    def test_lixo_com_47_esparso_nao_acha(self):
        lixo = bytearray(A.aleatorio(200000, 9))
        for i in range(0, len(lixo) - 3, 97):
            lixo[i:i + 3] = b'\x47\x40\x00'          # PAT a cada 97 bytes: nunca 5 sync no passo de 188/192
        lixo[188 * 3:188 * 4] = b'\x00' * 188
        self.assertEqual([a for a in varrer(bytes(lixo))[0] if a['tipo'] in ('TS', 'MTS')], [])

    def test_varredura_de_4mib_de_lixo_com_muitos_47_e_rapida(self):
        lixo = bytearray(A.aleatorio(4 << 20, 5))
        for i in range(0, len(lixo) - 8, 97):
            lixo[i:i + 3] = b'\x47\x40\x00'
        t = time.time()
        for modo in ('rapida', 'profunda'):
            achados = varrer(bytes(lixo), modo=modo)[0]
            self.assertEqual([a for a in achados if a['tipo'] in ('TS', 'MTS')], [], modo)
        self.assertLess(time.time() - t, 5)


if __name__ == '__main__':
    unittest.main()
