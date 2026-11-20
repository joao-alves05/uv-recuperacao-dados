# -*- coding: utf-8 -*-
"""Item #32: UDF com partição esparsa (CD-RW em pacotes, UDF 1.50+). Pacote defeituoso é lido da reposição (docs/FASE6.md)."""
import hashlib
import os
import unittest

import auxiliar  # noqa: F401  (ajusta o sys.path)
import imagens_udf as iu
from recuperacao import sa
from recuperacao.sa import udf

S = iu.SETOR
P = 4                                                    # setores por pacote

REMAPEADO = os.urandom(6 * S + 33)                      # ocupa 2 pacotes
OUTRO = os.urandom(2 * S)
ARQ = {'docs/remapeado.bin': REMAPEADO, 'outro.bin': OUTRO, 'pequeno.txt': b'dentro do file entry'}


class Mem:
    def __init__(self, d):
        self.d, self.tamanho = d, len(d)
        self.ruins = []

    def ler(self, off, n):
        return bytes(self.d[off:off + n])


def sha(b):
    return hashlib.sha256(b).hexdigest()


class TestEsparsa(unittest.TestCase):
    def abrir(self, copias):
        img = iu.disco_udf([iu.Sessao(ARQ, 0, esparsa={'pacote': P, 'remapa': 'docs/remapeado.bin', 'copias': copias})], 1000)
        v = sa.detectar(Mem(img))
        self.assertIsInstance(v, udf.Udf)
        return v, {e.caminho: e for e in v.varrer()}

    def test_pacote_remapeado_so_le_certo_pela_reposicao(self):
        v, ents = self.abrir(['ok'])
        self.assertTrue(v.spar)
        self.assertEqual(len(v.spar), 2)
        self.assertEqual(sha(b''.join(v.ler(ents['/docs/remapeado.bin']))), sha(REMAPEADO))
        self.assertEqual(b''.join(v.ler(ents['/outro.bin'])), OUTRO)
        self.assertNotIn('AVISO', v.limitacao)

    def test_primeira_copia_corrompida_cai_na_segunda(self):
        v, ents = self.abrir(['ruim', 'ok'])
        self.assertTrue(v.spar)
        self.assertEqual(sha(b''.join(v.ler(ents['/docs/remapeado.bin']))), sha(REMAPEADO))
        self.assertNotIn('AVISO', v.limitacao)

    def test_nenhuma_copia_valida_avisa_e_le_direto_sem_travar(self):
        v, ents = self.abrir(['ruim', 'ruim'])
        self.assertFalse(v.spar)
        self.assertIn('AVISO', v.limitacao)
        self.assertIn('Sparing Table', v.limitacao)
        self.assertIn('/outro.bin', ents)
        self.assertEqual(b''.join(v.ler(ents['/outro.bin'])), OUTRO)                    # fora dos pacotes remapeados lê normal
        self.assertNotEqual(sha(b''.join(v.ler(ents['/docs/remapeado.bin']))), sha(REMAPEADO))

    def test_sem_copia_nenhuma_tambem_avisa(self):
        v, ents = self.abrir([])
        self.assertIn('AVISO', v.limitacao)
        self.assertIn('/outro.bin', ents)


if __name__ == '__main__':
    unittest.main()
