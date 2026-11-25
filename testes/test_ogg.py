# -*- coding: utf-8 -*-
"""OGG (Vorbis, Opus, FLAC, Theora): página a página, com o CRC do Ogg; o fim é a página EOS."""
import struct
import unittest

import amostras as A
from test_carving import Memoria, com_folga, varrer

from recuperacao import validar

ASSIN = {'tipo': 'OGG', 'tamanho_max': 1 << 29}


def _tabela():
    t = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if r & 0x80000000 else (r << 1) & 0xFFFFFFFF
        t.append(r)
    return t


TAB = _tabela()


def crc_ref(dados):
    """CRC do Ogg, byte a byte, escrito do jeito da especificação (independente do código testado)."""
    r = 0
    for b in dados:
        r = ((r << 8) & 0xFFFFFFFF) ^ TAB[((r >> 24) ^ b) & 0xFF]
    return r


def pagina(corpo, seq, serie=0x1234, tipo=0, granule=0):
    segs, resto = [], len(corpo)
    while resto >= 255:
        segs.append(255)
        resto -= 255
    segs.append(resto)
    cab = b'OggS\x00' + bytes([tipo]) + struct.pack('<QIII', granule, serie, seq, 0) + bytes([len(segs)]) + bytes(segs)
    pag = cab + corpo
    return pag[:22] + struct.pack('<I', crc_ref(pag)) + pag[26:]


def ogg(id_corpo=b'\x01vorbis' + bytes(23), paginas=8, serie=0x1234, eos=True):
    p = [pagina(id_corpo, 0, serie, 2), pagina(b'\x03vorbis' + A.aleatorio(60, 1), 1, serie)]
    for i in range(2, paginas):
        p.append(pagina(A.aleatorio(900 + i * 40, i), i, serie, 4 if eos and i == paginas - 1 else 0, i * 1000))
    return p


OPUS = b'OpusHead\x01\x02' + bytes(10)


def validar_ogg(dados, off=0):
    return validar.ogg(validar.Janela(Memoria(dados)), off, len(dados) - off, ASSIN)


class Ogg(unittest.TestCase):
    def test_o_crc_rapido_bate_com_o_de_referencia(self):
        for n in (0, 1, 27, 1000, 65000):
            d = A.aleatorio(n, n)
            self.assertEqual(validar._crc_ogg(d), crc_ref(d), n)

    def test_inteiro_com_eos_e_bom_com_o_tamanho_certo(self):
        for corpo, tipo, ext, grupo in ((b'\x01vorbis' + bytes(23), 'OGG', 'ogg', 'audio'), (OPUS, 'OPUS', 'opus', 'audio'),
                                        (b'\x7fFLAC\x01\x00', 'OGA', 'oga', 'audio'),
                                        (b'\x80theora\x03\x02', 'OGV', 'ogv', 'video')):
            arq = b''.join(ogg(corpo))
            r = validar_ogg(bytes(100) + arq + bytes(3000), 100)
            self.assertEqual((r['estado'], r['tamanho'], r['tipo'], r['extensao'], r['grupo']),
                             ('bom', len(arq), tipo, ext, grupo), tipo)

    def test_cortado_e_parcial_ate_a_ultima_pagina_inteira(self):
        p = ogg()
        arq = b''.join(p)
        r = validar_ogg(arq[:len(arq) - 300])
        self.assertEqual((r['estado'], r['tamanho']), ('parcial', len(b''.join(p[:-1]))))

    def test_sem_eos_e_parcial(self):
        arq = b''.join(ogg(eos=False))
        r = validar_ogg(arq + bytes(2000))
        self.assertEqual((r['estado'], r['tamanho']), ('parcial', len(arq)))

    def test_pagina_com_crc_errado_no_meio_e_parcial_ate_ali(self):
        p = ogg()
        ruim = bytearray(p[4])
        ruim[100] ^= 0xFF
        arq = b''.join(p[:4]) + bytes(ruim) + b''.join(p[5:])
        r = validar_ogg(arq)
        self.assertEqual((r['estado'], r['tamanho']), ('parcial', len(b''.join(p[:4]))))

    def test_pagina_perdida_no_meio_quebra_a_sequencia(self):
        p = ogg()
        r = validar_ogg(b''.join(p[:3] + p[4:]))
        self.assertEqual((r['estado'], r['tamanho']), ('parcial', len(b''.join(p[:3]))))

    def test_dois_fluxos_multiplexados_e_encadeado(self):
        a, b = ogg(serie=1), ogg(OPUS, serie=2)
        mux = [a[0], b[0]] + a[1:-1] + b[1:-1] + [a[-1], b[-1]]
        arq = b''.join(mux)
        self.assertEqual((validar_ogg(arq)['estado'], validar_ogg(arq)['tamanho']), ('bom', len(arq)))
        cad = b''.join(ogg(serie=1) + ogg(serie=7))
        self.assertEqual((validar_ogg(cad)['estado'], validar_ogg(cad)['tamanho']), ('bom', len(cad)))

    def test_lixo_com_oggs_nao_vira_achado(self):
        for semente in range(30):
            self.assertIsNone(validar_ogg(b'OggS\x00\x02' + A.aleatorio(4000, semente)), semente)
        self.assertIsNone(validar_ogg(ogg(b'XXXXXXXX' + bytes(20))[0]))                 # codec desconhecido
        self.assertIsNone(validar_ogg(ogg()[0] + bytes(500)))                          # uma página só, sem EOS
        self.assertIsNone(validar_ogg(ogg()[1]))                                       # sem BOS

    def test_varredura_acha_com_inicio_e_tamanho_certos(self):
        arq = b''.join(ogg(OPUS))
        achados = [a for a in varrer(com_folga(arq))[0] if a['tipo'] == 'OPUS']
        self.assertEqual([(a['estado'], a['tamanho'], a['offset'], a['grupo']) for a in achados],
                         [('bom', len(arq), 1024, 'audio')])


if __name__ == '__main__':
    unittest.main()
