# -*- coding: utf-8 -*-
"""ZIP com bomba de zeros: o CRC é conferido em pedaços de 1 MiB, sem guardar a entrada descompactada na memória."""
import io
import struct
import time
import tracemalloc
import unittest
import zipfile

from test_carving import Memoria

from recuperacao import validar

ASSIN = {'tipo': 'ZIP', 'tamanho_max': 1 << 32}
MIB = 1 << 20
ZEROS = bytes(MIB)


def zip_de_zeros(mib, force_zip64=False):
    """ZIP com uma entrada deflate de `mib` MiB de zeros, escrita em streaming (o ZIP em si tem ~200 KB)."""
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w', zipfile.ZIP_DEFLATED) as z:
        with z.open('zeros.bin', 'w', force_zip64=force_zip64) as f:
            for _ in range(mib):
                f.write(ZEROS)
    return b.getvalue()


def validar_zip(dados):
    return validar.zip_(validar.Janela(Memoria(dados)), 0, len(dados), ASSIN)


def medir(dados):
    tracemalloc.start()
    try:
        t = time.time()
        r = validar_zip(dados)
        return r, tracemalloc.get_traced_memory()[1], time.time() - t
    finally:
        tracemalloc.stop()


class Bomba(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.z = zip_de_zeros(200)
        cls.z64 = zip_de_zeros(200, force_zip64=True)

    def test_a_amostra_e_pequena_e_descomprime_muito(self):
        self.assertLess(len(self.z), 2 * MIB)

    def test_200_mib_de_zeros_sai_bom_com_pouca_memoria(self):
        for nome, z in (('zip', self.z), ('zip64', self.z64)):
            r, pico, seg = medir(z)
            self.assertEqual((r['estado'], r['tamanho']), ('bom', len(z)), nome)
            self.assertLess(pico, 64 * MIB, '%s: pico de %d MiB' % (nome, pico // MIB))
            self.assertLess(seg, 30, nome)

    def test_entrada_que_descomprime_mais_que_o_declarado_sai_parcial(self):
        for nome, z in (('zip', self.z), ('zip64', self.z64)):
            ruim = bytearray(z)
            i = ruim.find(b'PK\x01\x02')
            if nome == 'zip':
                struct.pack_into('<I', ruim, i + 24, 10 * MIB)           # usize declarado no diretório central: 10 MiB
            else:                                                        # zip64: o usize fica no extra 0x0001 do local
                j = ruim.find(b'\x01\x00\x10\x00')
                struct.pack_into('<Q', ruim, j + 4, 10 * MIB)
                struct.pack_into('<I', ruim, i + 24, 10 * MIB)
            r, pico, _ = medir(bytes(ruim))
            self.assertEqual(r['estado'], 'parcial', nome)
            self.assertLess(pico, 64 * MIB, nome)


if __name__ == '__main__':
    unittest.main()
