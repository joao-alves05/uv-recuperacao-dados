# -*- coding: utf-8 -*-
"""Item #33: UDF 2.50 com partição de metadados. ICBs e diretórios estão no Metadata File (ou no espelho); dados na física."""
import hashlib
import os
import unittest

import auxiliar  # noqa: F401  (ajusta o sys.path)
import imagens_udf as iu
from recuperacao import sa
from recuperacao.sa import udf

S = iu.SETOR

GRANDE = os.urandom(3 * S + 77)
OUTRO = os.urandom(2 * S)
ARQ = {'docs/grande.bin': GRANDE, 'docs/sub/outro.bin': OUTRO, 'pequeno.txt': b'dentro do file entry'}
CAMINHOS = {'/docs', '/docs/sub', '/docs/grande.bin', '/docs/sub/outro.bin', '/pequeno.txt'}


class Mem:
    def __init__(self, d):
        self.d, self.tamanho = d, len(d)
        self.ruins = []

    def ler(self, off, n):
        return bytes(self.d[off:off + n])


def sha(b):
    return hashlib.sha256(b).hexdigest()


class TestMetadados(unittest.TestCase):
    def abrir(self, **meta):
        img = iu.disco_udf([iu.Sessao(ARQ, 0, metadados=meta)], 1000)
        v = sa.detectar(Mem(img))
        self.assertIsInstance(v, udf.Udf)
        return v, {e.caminho: e for e in v.varrer()}

    def test_pastas_e_arquivos_achados_pelo_metadata_file_e_o_sha_bate(self):
        v, ents = self.abrir(arquivo='ok', espelho='ok')
        self.assertEqual(set(ents), CAMINHOS)
        self.assertEqual(sha(b''.join(v.ler(ents['/docs/grande.bin']))), sha(GRANDE))
        self.assertEqual(sha(b''.join(v.ler(ents['/docs/sub/outro.bin']))), sha(OUTRO))
        self.assertEqual(b''.join(v.ler(ents['/pequeno.txt'])), b'dentro do file entry')
        self.assertNotIn('AVISO', v.limitacao)
        self.assertEqual(len(v.meta), 2)                                   # 2 extents que não se tocam
        self.assertTrue(all(e.estado == 'bom' for e in ents.values()))

    def test_bloco_de_metadados_nao_e_o_fisico(self):
        v, _ = self.abrir(arquivo='ok', espelho='ok')
        raiz_fisica = v._meta_fisico(v._raiz)
        self.assertNotEqual(raiz_fisica, v._raiz)

    def test_metadata_file_corrompido_le_pelo_espelho_e_avisa(self):
        for defeito in ('ruim', 'dados'):
            v, ents = self.abrir(arquivo=defeito, espelho='ok')
            self.assertEqual(set(ents), CAMINHOS, defeito)
            self.assertEqual(sha(b''.join(v.ler(ents['/docs/grande.bin']))), sha(GRANDE), defeito)
            self.assertIn('AVISO', v.limitacao)
            self.assertIn('Metadata Mirror', v.limitacao)

    def test_os_dois_ruins_avisa_sem_travar(self):
        v, ents = self.abrir(arquivo='ruim', espelho='ruim')
        self.assertEqual(ents, {})
        self.assertIn('AVISO', v.limitacao)
        self.assertIn('Metadata File ilegível', v.limitacao)
        self.assertIn('Metadata Mirror File ilegível', v.limitacao)

    def test_espelho_ruim_com_arquivo_bom_nao_avisa(self):
        v, ents = self.abrir(arquivo='ok', espelho='ruim')
        self.assertEqual(set(ents), CAMINHOS)
        self.assertNotIn('AVISO', v.limitacao)


if __name__ == '__main__':
    unittest.main()
