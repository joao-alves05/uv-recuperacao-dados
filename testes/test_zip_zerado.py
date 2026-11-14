# -*- coding: utf-8 -*-
"""ZIP com os dados danificados (resto zerado, byte trocado) mas a estrutura coerente: a validação confere o CRC-32."""
import io
import unittest
import zipfile

import amostras as A
from test_carving import Memoria

from recuperacao import validar

ASSIN = {'tipo': 'ZIP'}


def zipar(metodo):
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w', metodo) as z:
        z.writestr('a.bin', A.aleatorio(6000, 3))
        z.writestr('b.txt', b'ola mundo ' * 3000)
        z.writestr('c.bin', A.aleatorio(9000, 4))
    return b.getvalue()


def validar_zip(dados, off=0):
    return validar.zip_(validar.Janela(Memoria(dados)), off, len(dados) - off, ASSIN)


class ZipZerado(unittest.TestCase):
    def test_inteiro_continua_bom(self):
        for m in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            z = zipar(m)
            r = validar_zip(bytes(100) + z, 100)
            self.assertEqual(r['estado'], 'bom', m)
            self.assertEqual(r['tamanho'], len(z))

    def test_dados_zerados_com_diretorio_central_intacto_vira_parcial(self):
        for m in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            z = zipar(m)
            i = z.find(b'PK\x01\x02')
            ruim = z[:i - 5000] + bytes(5000) + z[i:]       # fim dos dados da última entrada virou zeros
            self.assertEqual(len(ruim), len(z))
            self.assertEqual(validar_zip(ruim)['estado'], 'parcial', m)

    def test_truncado_com_resto_zerado_vira_parcial(self):
        z = zipar(zipfile.ZIP_STORED)
        ruim = z[:len(z) // 2] + bytes(len(z) - len(z) // 2)
        self.assertEqual(validar_zip(ruim)['estado'], 'parcial')

    def test_um_byte_trocado_no_meio_vira_parcial(self):
        for m in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            z = bytearray(zipar(m))
            meio = z.find(b'PK\x01\x02') // 2
            z[meio] ^= 0xFF
            self.assertEqual(validar_zip(bytes(z))['estado'], 'parcial', m)


if __name__ == '__main__':
    unittest.main()
