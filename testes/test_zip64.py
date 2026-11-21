# -*- coding: utf-8 -*-
"""ZIP64: inteiro é bom (CRC conferido); cortado sem diretório central é parcial até a última entrada inteira."""
import io
import unittest
import zipfile

import amostras as A
from test_carving import Memoria, com_folga, varrer

from recuperacao import validar

ASSIN = {'tipo': 'ZIP', 'tamanho_max': 1 << 32}


def zip64(n=3, metodo=zipfile.ZIP_STORED):
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w', metodo) as z:
        for i in range(n):
            with z.open('arq%d.bin' % i, 'w', force_zip64=True) as f:
                f.write(A.aleatorio(5000 + i * 700, i + 1))
    return b.getvalue()


def validar_zip(dados, off=0):
    return validar.zip_(validar.Janela(Memoria(dados)), off, len(dados) - off, ASSIN)


def fim_da_entrada(z, k):
    """Offset logo depois da entrada k (0-based) de um ZIP armazenado: o próximo cabeçalho local, ou o central."""
    p = 0
    for _ in range(k + 1):
        nlen, xlen = int.from_bytes(z[p + 26:p + 28], 'little'), int.from_bytes(z[p + 28:p + 30], 'little')
        csize = int.from_bytes(z[p + 42 + nlen:p + 50 + nlen], 'little')       # extra: id, tam, usize, csize
        p += 30 + nlen + xlen + csize
    return p


class Zip64(unittest.TestCase):
    def test_o_local_realmente_e_zip64(self):
        z = zip64()
        self.assertEqual(z[18:26], b'\xff' * 8)

    def test_inteiro_e_bom_com_o_tamanho_certo(self):
        for m in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            z = zip64(metodo=m)
            r = validar_zip(bytes(100) + z, 100)
            self.assertEqual((r['estado'], r['tamanho'], r['tipo']), ('bom', len(z), 'ZIP'), m)

    def test_inteiro_com_dado_adulterado_vira_parcial(self):
        z = bytearray(zip64())
        z[fim_da_entrada(bytes(z), 1) - 100] ^= 0xFF
        self.assertEqual(validar_zip(bytes(z))['estado'], 'parcial')

    def test_cortado_sem_diretorio_central_e_parcial_ate_a_ultima_entrada_inteira(self):
        z = zip64(4)
        for k, corte in ((1, 300), (2, 30)):
            fim_k = fim_da_entrada(z, k)
            r = validar_zip(z[:fim_k + corte])
            self.assertEqual((r['estado'], r['tamanho']), ('parcial', fim_k), k)

    def test_cortado_e_achado_pela_varredura(self):
        z = zip64(4)
        fim_k = fim_da_entrada(z, 1)
        achados = [a for a in varrer(com_folga(z[:fim_k + 300]))[0] if a['tipo'] == 'ZIP']
        self.assertEqual([(a['estado'], a['tamanho'], a['offset']) for a in achados], [('parcial', fim_k, 1024)])

    def test_lixo_com_pk0304_nao_vira_achado(self):
        lixo = b'PK\x03\x04' + A.aleatorio(2000, 7)
        self.assertIsNone(validar_zip(lixo))
        z64 = b'PK\x03\x04\x2d\x00\x00\x00\x00\x00\x00\x00\x21\x00' + bytes(4) + b'\xff' * 8 + b'\x05\x00\x08\x00abc.b' \
              + b'\x01\x00\x04\x00' + bytes(20)           # campo extra 0x0001 curto demais
        self.assertIsNone(validar_zip(z64 + bytes(500)))


if __name__ == '__main__':
    unittest.main()
