# -*- coding: utf-8 -*-
"""MKV/WebM (EBML): o tamanho vem do Segment (conhecido ou desconhecido, percorrido de Cluster em Cluster)."""
import unittest

import amostras as A
from test_carving import Memoria, com_folga, varrer

from recuperacao import validar

ASSIN = {'tipo': 'MKV', 'tamanho_max': 1 << 34}


def vint(n, desconhecido=False):
    if desconhecido:
        return b'\xff'
    for t in range(1, 9):
        if n < (1 << (7 * t)) - 1:
            return (n | 1 << (7 * t)).to_bytes(t, 'big')


def el(ident, corpo=b'', tam=None):
    return ident + (tam if tam is not None else vint(len(corpo))) + corpo


def cabecalho(doctype=b'matroska'):
    return el(b'\x1a\x45\xdf\xa3', el(b'\x42\x86', b'\x01') + el(b'\x42\x82', doctype) + el(b'\x42\x87', b'\x04'))


def cluster(n, semente, tam=None):
    return el(b'\x1f\x43\xb6\x75', el(b'\xe7', bytes([n])) + el(b'\xa3', A.aleatorio(700, semente)), tam)


def corpo(clusters=4):
    return (el(b'\x15\x49\xa9\x66', el(b'\x2a\xd7\xb1', b'\x0f\x42\x40')) + el(b'\x16\x54\xae\x6b', b'\xae\x81\x01')
            + b''.join(cluster(i, i) for i in range(clusters)))


def mkv(doctype=b'matroska', conhecido=True, clusters=4):
    c = corpo(clusters)
    return cabecalho(doctype) + el(b'\x18\x53\x80\x67', c, None if conhecido else vint(0, True))


def validar_mkv(dados, off=0):
    return validar.mkv(validar.Janela(Memoria(dados)), off, len(dados) - off, ASSIN)


class Mkv(unittest.TestCase):
    def test_tamanho_conhecido_e_bom(self):
        for doc, tipo, ext in ((b'matroska', 'MKV', 'mkv'), (b'webm', 'WEBM', 'webm')):
            arq = mkv(doc)
            r = validar_mkv(bytes(100) + arq + bytes(3000) + b'lixo', 100)
            self.assertEqual((r['estado'], r['tamanho'], r['tipo'], r['extensao'], r['grupo']),
                             ('bom', len(arq), tipo, ext, 'video'), doc)

    def test_tamanho_desconhecido_e_percorrido_ate_o_fim(self):
        arq = mkv(conhecido=False)
        r = validar_mkv(bytes(50) + arq + bytes(5000), 50)
        self.assertEqual((r['estado'], r['tamanho']), ('bom', len(arq)))

    def test_desconhecido_com_cluster_ao_vivo_tambem(self):
        c = corpo(1) + cluster(1, 5, vint(0, True)) + cluster(2, 6, vint(0, True))
        arq = cabecalho(b'webm') + el(b'\x18\x53\x80\x67', c, vint(0, True))
        r = validar_mkv(arq + bytes(4000))
        self.assertEqual((r['estado'], r['tamanho'], r['tipo']), ('bom', len(arq), 'WEBM'))

    def test_cortado_e_parcial(self):
        for conhecido in (True, False):
            arq = mkv(conhecido=conhecido)
            corte = arq[:len(arq) - 1000]
            r = validar_mkv(corte)
            self.assertEqual((r['estado'], r['tamanho']), ('parcial', len(corte)), conhecido)

    def test_lixo_com_o_cabecalho_nao_vira_achado(self):
        for lixo in (b'\x1a\x45\xdf\xa3' + A.aleatorio(2000, 1), cabecalho() + A.aleatorio(2000, 2),
                     cabecalho(b'outro') + el(b'\x18\x53\x80\x67', corpo(1)), cabecalho() + bytes(2000)):
            self.assertIsNone(validar_mkv(lixo))
        achados, _, _ = varrer(bytes(1024) + b'\x1a\x45\xdf\xa3' + A.aleatorio(3000, 3) + bytes(3000))
        self.assertEqual([a for a in achados if a['tipo'] in ('MKV', 'WEBM')], [])

    def test_varredura_acha_mkv_e_webm_com_os_bytes_certos(self):
        for doc, tipo in ((b'matroska', 'MKV'), (b'webm', 'WEBM')):
            arq = mkv(doc)
            img = com_folga(arq)
            for modo in ('rapida', 'profunda'):
                achados, _, v = varrer(img, modo)
                self.assertEqual(len(achados), 1, (modo, achados))
                a = achados[0]
                self.assertEqual((a['tipo'], a['offset'], a['tamanho'], a['estado']), (tipo, 1024, len(arq), 'bom'))
                self.assertEqual(v.erros_validador, 0)


if __name__ == '__main__':
    unittest.main()
