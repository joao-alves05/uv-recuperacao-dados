# -*- coding: utf-8 -*-
"""Item #30: UDF com ICB indireto (ECMA-167 4/14.6.2 e 4/14.7). A cadeia de Indirect Entries é seguida até o File Entry."""
import hashlib
import os
import unittest

import auxiliar  # noqa: F401  (ajusta o sys.path)
import imagens_udf as iu
from recuperacao import sa
from recuperacao.sa import udf

S = iu.SETOR


class Mem:
    def __init__(self, d):
        self.d, self.tamanho = d, len(d)
        self.ruins = []

    def ler(self, off, n):
        return bytes(self.d[off:off + n])


UM = os.urandom(3 * S + 50)
DOIS = os.urandom(2 * S + 7)
PEQ = b'dentro do file entry'
ARQ = {'um.bin': UM, 'docs/dois.bin': DOIS, 'normal.txt': PEQ, 'laco.bin': os.urandom(500)}
IND = {'um.bin': 1, 'docs/dois.bin': 2, 'laco.bin': 'laco'}


def sha(b):
    return hashlib.sha256(b).hexdigest()


class TestIcbIndireto(unittest.TestCase):
    def varrer(self, **kw):
        img = iu.disco_udf([iu.Sessao(ARQ, 0, alt_fim=999, **kw)], 1000)
        v = sa.detectar(Mem(img))
        self.assertIsInstance(v, udf.Udf)
        return v, {e.caminho: e for e in v.varrer()}

    def test_um_e_dois_saltos_leem_o_arquivo_e_o_sha_bate(self):
        v, ents = self.varrer(indireto=IND)
        for caminho, dados in (('/um.bin', UM), ('/docs/dois.bin', DOIS)):
            self.assertEqual(ents[caminho].estado, 'bom', caminho)
            self.assertEqual(ents[caminho].tamanho, len(dados))
            self.assertEqual(sha(b''.join(v.ler(ents[caminho]))), sha(dados), caminho)
        self.assertEqual(b''.join(v.ler(ents['/normal.txt'])), PEQ)

    def test_laco_nao_trava_e_sai_ruim_com_nota_sem_derrubar_o_resto(self):
        v, ents = self.varrer(indireto=IND)
        self.assertEqual(ents['/laco.bin'].estado, 'ruim')
        self.assertIn('laço', ents['/laco.bin'].nota)
        self.assertEqual(ents['/laco.bin'].runs, [])
        self.assertIn('/um.bin', ents)

    def test_cadeia_longa_demais_vira_ruim(self):
        v, ents = self.varrer(indireto={'um.bin': udf.MAX_SALTOS_ICB + 2})
        self.assertEqual(ents['/um.bin'].estado, 'ruim')
        self.assertIn('saltos', ents['/um.bin'].nota)

    def test_no_limite_de_saltos_ainda_le(self):
        v, ents = self.varrer(indireto={'um.bin': udf.MAX_SALTOS_ICB})
        self.assertEqual(sha(b''.join(v.ler(ents['/um.bin']))), sha(UM))


if __name__ == '__main__':
    unittest.main()
