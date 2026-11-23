# -*- coding: utf-8 -*-
"""Item #31: UDF com partição virtual (VAT, CD-R/RW incremental). Bloco virtual != físico: a VAT traduz (docs/FASE6.md)."""
import hashlib
import os
import unittest
from array import array
from unittest import mock

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


GRANDE = os.urandom(3 * S + 77)
PEQ = b'dentro do file entry'
ARQ = {'docs/grande.bin': GRANDE, 'raiz.bin': os.urandom(2 * S), 'pequeno.txt': PEQ}


def sha(b):
    return hashlib.sha256(b).hexdigest()


class TestVat(unittest.TestCase):
    def abrir(self, vat, arq=None, **kw):
        img = iu.disco_udf([iu.Sessao(arq or ARQ, 0, vat=vat, **kw)], 1000)
        v = sa.detectar(Mem(img))
        self.assertIsInstance(v, udf.Udf)
        return v

    def test_vat_200_e_150_traduzem_blocos_virtuais_e_o_sha_bate(self):
        for versao in ('2.00', '1.50'):
            v = self.abrir(versao)
            self.assertIsNotNone(v.vat, versao)
            ents = {e.caminho: e for e in v.varrer()}
            self.assertEqual(set(ents), {'/docs', '/docs/grande.bin', '/raiz.bin', '/pequeno.txt'}, versao)
            self.assertEqual(sha(b''.join(v.ler(ents['/docs/grande.bin']))), sha(GRANDE), versao)
            self.assertEqual(b''.join(v.ler(ents['/raiz.bin'])), ARQ['raiz.bin'], versao)
            self.assertEqual(b''.join(v.ler(ents['/pequeno.txt'])), PEQ, versao)
            self.assertNotIn('virtual', v.limitacao)
            self.assertTrue(all(e.estado == 'bom' for e in ents.values()), versao)

    def test_blocos_virtuais_nao_batem_com_os_fisicos(self):
        v = self.abrir('2.00')
        self.assertEqual(v.vat[iu.DESLOCA_VIRTUAL + 3], 3)
        self.assertNotEqual(v._raiz, 1)                                    # raiz virtual 101, física 1
        self.assertEqual(v._fisico(v._raiz), 1)

    def test_tabela_fica_compacta_e_limitada_a_particao(self):
        v = self.abrir('2.00')
        self.assertIsInstance(v.vat, array)                                 # 4 bytes por entrada, não uma lista de ints
        self.assertEqual(v.vat.itemsize, 4)
        self.assertLessEqual(len(v.vat), v.part_tam)

    def test_vat_acima_do_teto_vira_aviso_sem_estourar(self):
        img = iu.disco_udf([iu.Sessao(ARQ, 0, vat='2.00')], 1000)
        with mock.patch.object(udf, 'MAX_VAT', 100):
            v = sa.detectar(Mem(img))
        self.assertIsNone(v.vat)
        self.assertEqual(list(v.varrer()), [])
        self.assertIn('AVISO: VAT ilegível', v.limitacao)
        self.assertIn('teto', v.limitacao)

    def test_teto_real_e_razoavel(self):
        self.assertLessEqual(udf.MAX_VAT, 16 << 20)

    def test_extensao_se_quebra_quando_os_blocos_fisicos_nao_sao_vizinhos(self):
        v = self.abrir('2.00')
        v.vat = [10, 20, 21, 30]                                            # virtuais 1,2,3 -> físicos 20,21,30
        ad = (3 * S - 5).to_bytes(4, 'little') + (1).to_bytes(4, 'little')   # alocação curta: 3 blocos menos 5 bytes
        base = v.inicio + v.part_ini * S
        self.assertEqual(v._extents(ad, 0, 3 * S - 5, 0), [(base + 20 * S, 2 * S), (base + 30 * S, S - 5)])

    def test_vat_corrompida_vira_aviso_sem_travar(self):
        v = self.abrir('ruim')
        self.assertIsNone(v.vat)
        self.assertEqual(list(v.varrer()), [])
        self.assertIn('AVISO: VAT ilegível', v.limitacao)

    def test_vat_zerada_ou_ausente_tambem_vira_aviso(self):
        img = iu.disco_udf([iu.Sessao(ARQ, 0, vat='2.00')], 1000)
        img[999 * S:1000 * S] = bytes(S)                                    # o File Entry da VAT some
        v = sa.detectar(Mem(img))
        self.assertIsInstance(v, udf.Udf)
        self.assertEqual(list(v.varrer()), [])
        self.assertIn('AVISO: VAT ilegível', v.limitacao)

    def test_vat_com_folga_depois_do_ultimo_setor_gravado_ainda_acha(self):
        img = iu.disco_udf([iu.Sessao(ARQ, 0, vat='2.00')], 1000)
        img += bytearray(10 * S)                                            # sobra de mídia depois do último gravado
        v = sa.detectar(Mem(img))
        self.assertIsNotNone(v.vat)
        self.assertEqual(len(list(v.varrer())), 4)
        self.assertLessEqual(10, udf.MAX_BUSCA_VAT)


if __name__ == '__main__':
    unittest.main()
