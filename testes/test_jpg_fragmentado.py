# -*- coding: utf-8 -*-
"""JPG fragmentado (heurística do PhotoRec): lixo no meio dos dados; a validação retoma depois dele.
A heurística só liga com `fragmentado: true` na linha da assinatura (o motor de carving ainda precisa tratar `extents`)."""
import hashlib
import unittest

import amostras as A
from test_carving import Memoria

from recuperacao import validar

BLOCO = 4096
ASSIN = {'tipo': 'JPG', 'fragmentado': True}


def validar_jpg(dados, off=0, a=ASSIN):
    j = validar.Janela(Memoria(dados))
    return validar.jpg(j, off, len(dados) - off, a)


def montar(scan=12000, lixo_blocos=3, corte=2 * BLOCO, semente=7):
    """Devolve (imagem, jpg original, offset). O JPG começa no bloco 1; o pedaço 1 tem `corte` bytes (múltiplo do bloco)."""
    orig = A.jpeg(scan=scan, semente=semente)
    ini = BLOCO
    lixo = A.aleatorio(lixo_blocos * BLOCO, 99)
    img = bytes(ini) + orig[:corte] + lixo + orig[corte:] + bytes(BLOCO * 2)
    return img, orig, ini


def ler_extents(img, extents):
    return b''.join(img[o:o + c] for o, c in extents)


class JpgFragmentado(unittest.TestCase):
    def test_lixo_no_meio_recupera_os_dois_pedacos(self):
        img, orig, ini = montar()
        r = validar_jpg(img, ini)
        self.assertEqual(r['estado'], 'bom')
        self.assertEqual(r['tamanho'], len(orig))
        self.assertEqual(sum(c for _, c in r['extents']), r['tamanho'])
        self.assertEqual(r['extents'][0][0], ini)
        self.assertEqual(len(r['extents']), 2)
        self.assertEqual(hashlib.sha256(ler_extents(img, r['extents'])).hexdigest(), hashlib.sha256(orig).hexdigest())
        self.assertLess(r['pontuacao'], 100)                     # retomada por palpite vale menos que contíguo

    def test_lixo_com_blocos_de_2_kib_e_de_1_kib(self):
        orig = A.jpeg(scan=9000, semente=3)
        lixo = A.aleatorio(2048, 5)
        img = bytes(2048) + orig[:2048] + lixo + orig[2048:] + bytes(4096)
        r = validar_jpg(img, 2048)
        self.assertEqual(r['estado'], 'bom')
        self.assertEqual(ler_extents(img, r['extents']), orig)
        lixo = A.aleatorio(1024, 8)
        img = bytes(1024) + orig[:3072] + lixo + orig[3072:] + bytes(4096)
        r = validar_jpg(img, 1024)
        self.assertEqual(r['estado'], 'bom')
        self.assertEqual(ler_extents(img, r['extents']), orig)

    def test_sem_a_heuristica_ligada_continua_parcial(self):
        img, orig, ini = montar()
        r = validar_jpg(img, ini, {'tipo': 'JPG'})
        self.assertEqual(r['estado'], 'parcial')
        self.assertNotIn('extents', r)

    def test_jpeg_inteiro_continua_bom_e_contiguo(self):
        orig = A.jpeg(scan=8000)
        img = bytes(BLOCO) + orig + bytes(BLOCO)
        r = validar_jpg(img, BLOCO)
        self.assertEqual((r['estado'], r['tamanho'], r['pontuacao']), ('bom', len(orig), 100))
        self.assertNotIn('extents', r)

    def test_jpeg_cortado_de_verdade_continua_parcial(self):
        orig = A.jpeg(scan=8000)
        img = bytes(BLOCO) + orig[:5000] + bytes(BLOCO * 3)
        r = validar_jpg(img, BLOCO)
        self.assertEqual((r['estado'], r['tamanho']), ('parcial', 5000))
        self.assertNotIn('extents', r)

    def test_cortado_seguido_de_lixo_sem_fim_continua_parcial(self):
        orig = A.jpeg(scan=8000)
        img = bytes(BLOCO) + orig[:BLOCO + 1000] + A.aleatorio(BLOCO * 4, 11) + bytes(BLOCO * 2)
        r = validar_jpg(img, BLOCO)
        self.assertEqual(r['estado'], 'parcial')


if __name__ == '__main__':
    unittest.main()
