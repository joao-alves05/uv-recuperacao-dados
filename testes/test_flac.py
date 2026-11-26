# -*- coding: utf-8 -*-
"""FLAC nativo: STREAMINFO obrigatório; quadros verificados pelo CRC-8 do cabeçalho e pelo CRC-16 do quadro."""
import struct
import unittest

import amostras as A
from test_carving import Memoria, com_folga, varrer

from recuperacao import validar

ASSIN = {'tipo': 'FLAC', 'tamanho_max': 1 << 29}


def crc8_ref(dados):
    r = 0
    for b in dados:
        r ^= b
        for _ in range(8):
            r = ((r << 1) ^ 0x07) & 0xFF if r & 0x80 else (r << 1) & 0xFF
    return r


def crc16_ref(dados):
    r = 0
    for b in dados:
        r ^= b << 8
        for _ in range(8):
            r = ((r << 1) ^ 0x8005) & 0xFFFF if r & 0x8000 else (r << 1) & 0xFFFF
    return r


def streaminfo(total):
    pacote = (44100 << 44) | (1 << 41) | (15 << 36) | total
    return struct.pack('>HH', 4096, 4096) + bytes(6) + pacote.to_bytes(8, 'big') + bytes(range(16))


def quadro(n, semente, tamanho=1500, bs_code=12, extra=b''):
    """Quadro sintético: cabeçalho válido (CRC-8), corpo aleatório (com um sync falso dentro) e CRC-16 no fim."""
    cab = bytes([0xFF, 0xF8, bs_code << 4 | 9, 1 << 4 | 4 << 1, n]) + extra
    cab += bytes([crc8_ref(cab)])
    corpo = bytearray(A.aleatorio(tamanho, semente))
    corpo[10:12] = b'\xff\xf8'
    q = cab + bytes(corpo)
    return q + struct.pack('>H', crc16_ref(q))


def flac(n=6, total=True, vorbis=True):
    """(cabeçalho com metadados, lista de quadros). O último quadro tem 200 amostras (código de bloco 6)."""
    quadros = [quadro(i, i + 1, 1200 + 90 * i) for i in range(n - 1)] + [quadro(n - 1, 99, 300, 6, bytes([199]))]
    si = streaminfo(4096 * (n - 1) + 200 if total else 0)
    if vorbis:
        vc = struct.pack('<I', 6) + b'UVtest' + struct.pack('<I', 0)
        meta = bytes([0x00, 0, 0, 34]) + si + bytes([0x84]) + len(vc).to_bytes(3, 'big') + vc
    else:
        meta = bytes([0x80, 0, 0, 34]) + si
    return b'fLaC' + meta, quadros


def montado(**kw):
    cab, q = flac(**kw)
    return cab + b''.join(q), cab, q


def validar_flac(dados, off=0):
    return validar.flac(validar.Janela(Memoria(dados)), off, len(dados) - off, ASSIN)


class Flac(unittest.TestCase):
    def test_crcs_rapidos_batem_com_a_referencia(self):
        for n in (0, 1, 2, 7, 1501, 20000):
            d = A.aleatorio(n, n + 3)
            self.assertEqual(validar._crc16_flac(d), crc16_ref(d), n)
        self.assertEqual(validar._crc8_flac(b'\xff\xf8\xc9\x18\x00'), crc8_ref(b'\xff\xf8\xc9\x18\x00'))

    def test_inteiro_e_bom_com_o_tamanho_certo(self):
        for vorbis in (True, False):
            arq, _, _ = montado(vorbis=vorbis)
            r = validar_flac(bytes(100) + arq + bytes(3000), 100)
            self.assertEqual((r['estado'], r['tamanho'], r['tipo'], r['extensao']), ('bom', len(arq), 'FLAC', 'flac'))

    def test_sem_total_de_amostras_so_fecha_se_acabar_no_fim(self):
        arq, _, _ = montado(total=False)
        r = validar_flac(arq)
        self.assertEqual((r['estado'], r['tamanho']), ('bom', len(arq)))
        r = validar_flac(arq + bytes(2000))                    # zeros de enchimento depois: o CRC-16 continua em 0
        self.assertEqual((r['estado'], r['tamanho']), ('bom', len(arq)))
        self.assertEqual(validar_flac(arq + A.aleatorio(2000, 6))['estado'], 'parcial')   # outro dado depois: não fecha

    def test_cortado_e_parcial_ate_o_ultimo_quadro_inteiro(self):
        arq, cab, q = montado()
        r = validar_flac(arq[:len(arq) - 150])
        self.assertEqual((r['estado'], r['tamanho']), ('parcial', len(cab) + sum(map(len, q[:-1]))))
        r = validar_flac(arq[:len(cab) + sum(map(len, q[:3])) + 400])
        self.assertEqual((r['estado'], r['tamanho']), ('parcial', len(cab) + sum(map(len, q[:3]))))   # o 4o quadro começa

    def test_crc16_errado_num_quadro_do_meio_e_parcial_ate_ali(self):
        _, cab, q = montado()
        ruim = bytearray(q[3])
        ruim[200] ^= 0xFF
        arq = cab + b''.join(q[:3]) + bytes(ruim) + b''.join(q[4:])
        r = validar_flac(arq)
        self.assertEqual((r['estado'], r['tamanho']), ('parcial', len(cab) + sum(map(len, q[:3]))))

    def test_lixo_com_flac_sem_streaminfo_valido_nao_vira_achado(self):
        _, cab, q = montado()
        self.assertIsNone(validar_flac(b'fLaC' + A.aleatorio(4000, 1)))
        self.assertIsNone(validar_flac(b'fLaC\x80\x00\x00\x10' + bytes(16) + q[0]))           # STREAMINFO curto
        self.assertIsNone(validar_flac(b'fLaC\x80\x00\x00\x22' + bytes(34) + q[0]))           # taxa 0, blocos 0
        self.assertIsNone(validar_flac(b'fLaC\x01\x00\x00\x22' + cab[8:42] + q[0]))           # o primeiro não é STREAMINFO
        self.assertIsNone(validar_flac(cab))                                                   # metadados e nenhum quadro
        self.assertIsNone(validar_flac(cab + A.aleatorio(3000, 4)))                           # nenhum quadro de verdade

    def test_varredura_acha_com_inicio_e_tamanho_certos(self):
        arq, _, _ = montado()
        achados = [a for a in varrer(com_folga(arq))[0] if a['tipo'] == 'FLAC']
        self.assertEqual([(a['estado'], a['tamanho'], a['offset'], a['grupo']) for a in achados],
                         [('bom', len(arq), 1024, 'audio')])


if __name__ == '__main__':
    unittest.main()
