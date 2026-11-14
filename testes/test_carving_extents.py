# -*- coding: utf-8 -*-
"""Carving com `extents` (arquivo fragmentado): o achado leva os extents e a varredura segue do fim do último."""
import hashlib
import unittest

import amostras as A
from test_carving import Memoria, varrer

from recuperacao import achados, carving, validar

BLOCO = 4096


def catalogo_fragmentado():
    cat = carving.carregar()
    for a in cat.assinaturas:
        if a['tipo'] == 'JPG':
            a['fragmentado'] = True          # ligado só aqui: no assinaturas.json segue desligado
    return cat


def montar():
    orig = A.jpeg(scan=12000, semente=7)
    seguinte = A.jpeg(scan=6000, semente=11)
    corte = 2 * BLOCO
    img = (bytes(BLOCO) + orig[:corte] + A.aleatorio(3 * BLOCO, 99) + orig[corte:]
           + bytes(BLOCO - len(orig[corte:]) % BLOCO if len(orig[corte:]) % BLOCO else 0) + bytes(BLOCO)
           + seguinte + bytes(BLOCO * 2))
    return img, orig, seguinte


class CarvingComExtents(unittest.TestCase):
    def test_achado_leva_os_extents_e_o_seguinte_tambem_e_achado(self):
        img, orig, seguinte = montar()
        for modo in ('rapida', 'profunda'):
            with self.subTest(modo=modo):
                res, _, _ = varrer(img, modo, catalogo=catalogo_fragmentado())
                jpgs = [x for x in res if x['tipo'] == 'JPG' and x['estado'] == 'bom']
                frag = [x for x in jpgs if x.get('extents')]
                self.assertEqual(len(frag), 1)
                x = frag[0]
                self.assertEqual(len(x['extents']), 2)
                self.assertEqual(x['tamanho'], len(orig))
                lido = b''.join(achados.ler_achado(Memoria(img), x))
                self.assertEqual(hashlib.sha256(lido).hexdigest(), hashlib.sha256(orig).hexdigest())
                ini2 = img.find(seguinte)
                self.assertIn(ini2, [y['offset'] for y in jpgs])      # o arquivo depois do último extent também é achado

    def test_livre_comeca_no_fim_do_ultimo_extent(self):
        img, orig, _ = montar()
        _, _, v = varrer(img, 'profunda', catalogo=catalogo_fragmentado())
        self.assertGreater(v.livre, 0)
        r = {'tamanho': 100, 'extents': [[10, 40], [500, 60]]}
        self.assertEqual(carving.Varredor._fim(10, r), 560)

    def test_extents_com_deslocamento_e_buraco(self):
        v = carving.Varredor(carving.Fatia(Memoria(bytes(8192)), 1000, 4000), carving.carregar(), 'rapida')
        a = carving.carregar().assinaturas[0]
        r = {'tamanho': 30, 'estado': 'bom', 'pontuacao': 70, 'extents': [[None, 10], [200, 20]]}
        x = v._achado(0, a, r)
        self.assertEqual(x['extents'], [[None, 10], [1200, 20]])

    def test_sem_extents_nada_muda(self):
        orig = A.jpeg(scan=8000)
        img = bytes(BLOCO) + orig + bytes(BLOCO)
        res, _, v = varrer(img, 'profunda', catalogo=catalogo_fragmentado())
        x = [y for y in res if y['tipo'] == 'JPG'][0]
        self.assertNotIn('extents', x)
        self.assertEqual(v.livre, BLOCO + len(orig))


if __name__ == '__main__':
    unittest.main()
