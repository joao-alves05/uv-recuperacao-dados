# -*- coding: utf-8 -*-
"""GZ: o fim vem do deflate descompactado em streaming até o rodapé (CRC-32 e ISIZE); membros colados valem como um."""
import gzip
import io
import time
import tracemalloc
import unittest

import amostras as A
from test_carving import Memoria, com_folga, varrer

from recuperacao import validar

ASSIN = {'tipo': 'GZ', 'tamanho_max': 1 << 32}
MIB = 1 << 20


def gz(dados, nome=None, **kw):
    b = io.BytesIO()
    with gzip.GzipFile(filename=nome or '', fileobj=b, mode='wb', mtime=1700000000, **kw) as f:
        f.write(dados)
    return b.getvalue()


def validar_gz(dados, off=0):
    return validar.gz(validar.Janela(Memoria(dados)), off, len(dados) - off, ASSIN)


TEXTO = b'linha de texto repetida para comprimir bem. ' * 400 + A.aleatorio(3000, 5)


class Gz(unittest.TestCase):
    def test_inteiro_e_bom_com_o_tamanho_certo(self):
        for nome in (None, 'relatorio.txt'):
            z = gz(TEXTO, nome)
            r = validar_gz(bytes(100) + z + bytes(3000), 100)
            self.assertEqual((r['estado'], r['tamanho'], r['tipo'], r['extensao'], r['grupo']),
                             ('bom', len(z), 'GZ', 'gz', 'compactado'), nome)

    def test_extra_e_comentario_e_crc_do_cabecalho(self):
        # monta à mão: FLG = FEXTRA|FCOMMENT|FHCRC
        corpo = zlib_raw(TEXTO)
        cab = bytearray(b'\x1f\x8b\x08\x16\x00\x00\x00\x00\x00\x03') + b'\x03\x00abc' + b'um comentario\x00'
        import zlib
        cab += (zlib.crc32(bytes(cab)) & 0xFFFF).to_bytes(2, 'little')
        arq = bytes(cab) + corpo + zlib.crc32(TEXTO).to_bytes(4, 'little') + (len(TEXTO) & 0xFFFFFFFF).to_bytes(4, 'little')
        r = validar_gz(arq + bytes(500))
        self.assertEqual((r['estado'], r['tamanho']), ('bom', len(arq)))
        ruim = bytearray(arq)
        ruim[12] ^= 0xFF                                       # o FHCRC deixa de bater
        self.assertIsNone(validar_gz(bytes(ruim)))

    def test_dois_membros_concatenados_valem_como_um(self):
        z = gz(TEXTO) + gz(b'segundo membro ' * 500)
        r = validar_gz(bytes(10) + z + bytes(2000) + b'lixo', 10)
        self.assertEqual((r['estado'], r['tamanho']), ('bom', len(z)))

    def test_cortado_e_parcial(self):
        z = gz(TEXTO)
        r = validar_gz(z[:len(z) - 500])
        self.assertEqual(r['estado'], 'parcial')
        self.assertEqual(r['tamanho'], len(z) - 500)

    def test_so_o_rodape_cortado_e_parcial(self):
        z = gz(TEXTO)
        self.assertEqual(validar_gz(z[:-3])['estado'], 'parcial')

    def test_segundo_membro_cortado_e_parcial(self):
        z = gz(TEXTO) + gz(b'segundo membro ' * 500)
        self.assertEqual(validar_gz(z[:-6])['estado'], 'parcial')

    def test_crc_errado_e_parcial(self):
        z = bytearray(gz(TEXTO))
        z[-8] ^= 0xFF
        r = validar_gz(bytes(z) + bytes(900))
        self.assertEqual((r['estado'], r['tamanho']), ('parcial', len(z)))
        z = bytearray(gz(TEXTO))
        z[-1] ^= 0x01                                          # ISIZE
        self.assertEqual(validar_gz(bytes(z))['estado'], 'parcial')

    def test_lixo_com_1f8b08_nao_vira_achado(self):
        for semente in range(40):
            lixo = b'\x1f\x8b\x08\x00' + bytes(4) + b'\x00\x03' + A.aleatorio(4000, semente)
            self.assertIsNone(validar_gz(lixo), semente)
        self.assertIsNone(validar_gz(b'\x1f\x8b\x08\x00' + bytes(4) + b'\x00\x03'))       # só o cabeçalho
        self.assertIsNone(validar_gz(b'\x1f\x8b\x08\xe0' + A.aleatorio(500, 1)))          # bits reservados do FLG

    def test_varredura_acha_com_inicio_e_tamanho_certos(self):
        z = gz(TEXTO)
        achados = [a for a in varrer(com_folga(z))[0] if a['tipo'] == 'GZ']
        self.assertEqual([(a['estado'], a['tamanho'], a['offset'], a['grupo']) for a in achados],
                         [('bom', len(z), 1024, 'compactado')])

    def test_bomba_de_zeros_200_mib_com_pouca_memoria(self):
        b = io.BytesIO()
        with gzip.GzipFile(fileobj=b, mode='wb', mtime=1) as f:
            for _ in range(200):
                f.write(bytes(MIB))
        z = b.getvalue()
        self.assertLess(len(z), 2 * MIB)
        tracemalloc.start()
        try:
            t = time.time()
            r = validar_gz(z)
            pico = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual((r['estado'], r['tamanho']), ('bom', len(z)))
        self.assertLess(pico, 64 * MIB, 'pico de %d MiB' % (pico // MIB))
        self.assertLess(time.time() - t, 30)


def zlib_raw(dados):
    import zlib
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(dados) + c.flush()


if __name__ == '__main__':
    unittest.main()
