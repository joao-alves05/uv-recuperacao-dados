# -*- coding: utf-8 -*-
"""Fase 4, bloco 3: ext2/3/4 e ISO9660/Joliet sobre imagens montadas em Python; HFS+/APFS/UDF só reconhecidos."""
import os
import random
import struct
import unittest

from auxiliar import CasoComPasta, sha256_de

import imagens_ext as ie
import imagens_iso as ii
from recuperacao import leitor as leitor_mod
from recuperacao import sa
from recuperacao.sa import ErroSA
from recuperacao.sa.base import epoch
from test_sa_fat import conteudo, sha


class Base(CasoComPasta):
    def varrer(self, caminho):
        antes = (sha256_de(caminho), os.stat(caminho).st_mtime_ns)
        with leitor_mod.abrir(caminho) as l:
            vol = sa.detectar(l)
            self.assertIsNotNone(vol)
            self.vol = vol
            r = {}
            for e in vol.varrer():
                r[e.caminho] = (e, b'' if e.eh_dir else b''.join(vol.ler(e)))
        self.assertEqual((sha256_de(caminho), os.stat(caminho).st_mtime_ns), antes, 'a origem mudou')
        return r

    def fuzz(self, base, zonas, rodadas=80, semente=5):
        with open(base, 'rb') as g:
            bruto = bytearray(g.read())
        rng = random.Random(semente)
        for _ in range(rodadas):
            d = bytearray(bruto)
            for _ in range(rng.randint(1, 50)):
                a, z = rng.choice(zonas)
                d[rng.randrange(a, min(z, len(d)))] = rng.randrange(256)
            alvo = os.path.join(self.pasta, 'c.img')
            with open(alvo, 'wb') as g:
                g.write(d)
            with leitor_mod.abrir(alvo) as l:
                vol = sa.detectar(l)
                if vol is None:
                    continue
                try:
                    for e in vol.varrer():
                        for _ in vol.ler(e):
                            pass
                except ErroSA:
                    pass


class TestExt2(Base):
    def montar(self):
        f = ie.ExtImg('ext2', rotulo='meuext')
        d = self.d = {k: conteudo(n, i) for i, (k, n) in enumerate({
            'pequeno': 700, 'grande': 300 * 1024 - 17, 'apagado': 5000, 'filho': 2500, 'reuso': 4096, 'intruso': 1024,
            'fantasma': 900}.items())}
        f.add_file(2, 'leia-me.txt', d['pequeno'])
        f.add_file(2, 'arquivo grande com indireto duplo.bin', d['grande'])
        pasta = f.mkdir(2, 'Documentos úteis')
        ap = f.add_file(2, 'apagado de nome comprido.pdf', d['apagado'])
        f.apagar(2, ap)
        fi = f.add_file(pasta, 'filho apagado.txt', d['filho'])
        f.apagar(pasta, fi)
        ru = f.add_file(2, 'reaproveitado.dat', d['reuso'])
        bl = f.blocos_de[ru]
        f.apagar(2, ru)
        f.add_file(2, 'ocupou.dat', d['intruso'], bloco=bl[2])
        for i in range(40):                                     # pasta com mais de um bloco
            f.add_file(pasta, 'arquivo numero %02d.txt' % i, conteudo(20 + i, 200 + i))
        fa = f.add_file(pasta, 'fantasma tardio.txt', d['fantasma'])
        f.apagar(pasta, fa)
        return f.gravar(os.path.join(self.pasta, 'ext2.img'))

    def test_vivos_incluindo_indireto_duplo(self):
        r = self.varrer(self.montar())
        self.assertEqual((self.vol.tipo, self.vol.rotulo), ('ext2', 'meuext'))
        e, b = r['/leia-me.txt']
        self.assertEqual((e.apagado, e.estado, e.tamanho, sha(b)), (False, 'bom', 700, sha(self.d['pequeno'])))
        self.assertEqual(e.modificado, float(ie.T))
        e, b = r['/arquivo grande com indireto duplo.bin']
        self.assertEqual((e.tamanho, sha(b)), (300 * 1024 - 17, sha(self.d['grande'])))
        self.assertTrue(r['/Documentos úteis'][0].eh_dir)
        self.assertEqual(len([c for c in r if c.startswith('/Documentos úteis/arquivo numero')]), 40)

    def test_apagado_recuperado_pelos_ponteiros(self):
        r = self.varrer(self.montar())
        e, b = r['/apagado de nome comprido.pdf']
        self.assertEqual((e.apagado, e.estado, e.tamanho, sha(b)), (True, 'bom', 5000, sha(self.d['apagado'])))
        e, b = r['/Documentos úteis/filho apagado.txt']
        self.assertEqual((e.apagado, sha(b)), (True, sha(self.d['filho'])))
        e, b = r['/Documentos úteis/fantasma tardio.txt']          # entrada escondida num 2º bloco da pasta
        self.assertEqual((e.apagado, sha(b)), (True, sha(self.d['fantasma'])))

    def test_reuso_pelo_bitmap(self):
        r = self.varrer(self.montar())
        e, b = r['/reaproveitado.dat']
        self.assertEqual((e.apagado, e.estado), (True, 'parcial'))
        self.assertNotEqual(sha(b), sha(self.d['reuso']))
        self.assertEqual(sha(r['/ocupou.dat'][1]), sha(self.d['intruso']))

    def test_corrompido_nao_derruba(self):
        base = self.montar()
        self.fuzz(base, [(1024, 1024 + 264), (2048, 2048 + 32), (5 * 1024, 5 * 1024 + 4096), (28 * 1024, 100 * 1024)])


class TestExt3e4(Base):
    def test_ext3_nome_fica_conteudo_nao(self):
        f = ie.ExtImg('ext3')
        ap = f.add_file(2, 'apagado ext3.bin', conteudo(3000, 1))
        f.add_file(2, 'vivo.bin', conteudo(1500, 2))
        f.apagar(2, ap)
        r = self.varrer(f.gravar(os.path.join(self.pasta, 'e3.img')))
        self.assertEqual(self.vol.tipo, 'ext3')
        e, b = r['/apagado ext3.bin']
        self.assertEqual((e.apagado, e.estado, b), (True, 'ruim', b''))
        self.assertIn('zerou os ponteiros', e.nota)
        self.assertEqual(sha(r['/vivo.bin'][1]), sha(conteudo(1500, 2)))

    def test_ext4_extents_vivos_e_apagado_zerado(self):
        f = ie.ExtImg('ext4', rotulo='quatro')
        d = {'um': conteudo(9000, 1), 'frag': conteudo(40000, 2), 'ap': conteudo(4000, 3), 'pres': conteudo(6000, 4)}
        f.add_file(2, 'um extent.bin', d['um'])
        f.add_file(2, 'fragmentado em seis.bin', d['frag'], fragmentos=6)
        pasta = f.mkdir(2, 'sub')
        ap = f.add_file(pasta, 'apagado ext4.dat', d['ap'])
        f.apagar(pasta, ap)
        pr = f.add_file(2, 'extents preservados.dat', d['pres'])
        f.apagar(2, pr, preservar=True)
        r = self.varrer(f.gravar(os.path.join(self.pasta, 'e4.img')))
        self.assertEqual((self.vol.tipo, self.vol.rotulo), ('ext4', 'quatro'))
        e, b = r['/um extent.bin']
        self.assertEqual((e.estado, len(e.runs), sha(b)), ('bom', 1, sha(d['um'])))
        self.assertEqual(e.criado, float(ie.T))
        e, b = r['/fragmentado em seis.bin']
        self.assertEqual((e.estado, sha(b)), ('bom', sha(d['frag'])))
        self.assertGreaterEqual(len(e.runs), 6)
        e, b = r['/sub/apagado ext4.dat']
        self.assertEqual((e.apagado, e.estado, b), (True, 'ruim', b''))
        self.assertIn('ext4', e.nota)
        e, b = r['/extents preservados.dat']
        self.assertEqual((e.apagado, e.estado, sha(b)), (True, 'bom', sha(d['pres'])))

    def test_ext4_corrompido_nao_derruba(self):
        f = ie.ExtImg('ext4')
        f.mkdir(2, 'p')
        f.add_file(2, 'a.bin', conteudo(9000, 1), fragmentos=6)
        f.apagar(2, f.add_file(2, 'b.bin', conteudo(3000, 2)))
        self.fuzz(f.gravar(os.path.join(self.pasta, 'e.img')),
                  [(1024, 1288), (2048, 2080), (5 * 1024, 5 * 1024 + 8192), (28 * 1024, 90 * 1024)], semente=9)


class TestIso(Base):
    def montar(self, joliet):
        f = ii.IsoImg(joliet=joliet, rotulo='Disco Teste')
        d = self.d = {'a': conteudo(3000, 1), 'b': conteudo(100, 2), 'grande': conteudo(5000, 3), 'c': conteudo(2049, 4)}
        f.add_file('/', 'Relatório final de março.txt', d['a'], curto='RELATO~1.TXT;1')
        f.add_dir('/', 'Fotos')
        f.add_file('/Fotos', 'viagem.jpg', d['b'], curto='VIAGEM.JPG;1')
        f.add_dir('/Fotos', 'Antigas')
        f.add_file('/Fotos/Antigas', 'velha.bin', d['c'], curto='VELHA.BIN;1')
        f.add_file('/', 'em duas partes.iso', d['grande'], curto='PARTES.ISO;1', multi=True)
        return f.gravar(os.path.join(self.pasta, 'disco.iso'))

    def test_joliet_nomes_longos_e_multiextent(self):
        r = self.varrer(self.montar(True))
        self.assertEqual((self.vol.tipo, self.vol.rotulo), ('ISO9660/Joliet', 'Disco Teste'))
        e, b = r['/Relatório final de março.txt']
        self.assertEqual((e.apagado, e.estado, sha(b)), (False, 'bom', sha(self.d['a'])))
        self.assertEqual(e.modificado, epoch(2024, 3, 15, 10, 20, 30))
        self.assertEqual(sha(r['/Fotos/viagem.jpg'][1]), sha(self.d['b']))
        self.assertEqual(sha(r['/Fotos/Antigas/velha.bin'][1]), sha(self.d['c']))
        e, b = r['/em duas partes.iso']
        self.assertEqual((e.tamanho, len(e.runs), sha(b)), (5000, 2, sha(self.d['grande'])))

    def test_sem_joliet_usa_nomes_curtos_sem_versao(self):
        r = self.varrer(self.montar(False))
        self.assertEqual(self.vol.tipo, 'ISO9660')
        self.assertEqual(sha(r['/RELATO~1.TXT'][1]), sha(self.d['a']))
        self.assertIn('/FOTOS/ANTIGAS/VELHA.BIN', r)

    def test_corrompido_nao_derruba(self):
        self.fuzz(self.montar(True), [(16 * 2048, 16 * 2048 + 300), (17 * 2048, 17 * 2048 + 300), (20 * 2048, 30 * 2048)])


class TestSoReconhecidos(Base):
    def escreve(self, nome, partes, tam=200 * 1024):
        caminho = os.path.join(self.pasta, nome)
        with open(caminho, 'wb') as f:
            f.truncate(tam)
            for off, d in partes:
                f.seek(off)
                f.write(d)
        return caminho

    def test_hfsplus_apfs_udf(self):
        casos = {
            'HFS+': [(1024, b'H+' + struct.pack('>H', 4))],
            'HFSX': [(1024, b'HX' + struct.pack('>H', 5))],
            'APFS': [(32, b'NXSB')],
            'UDF': [(16 * 2048 + 1, b'BEA01'), (17 * 2048 + 1, b'NSR03'), (18 * 2048 + 1, b'TEA01')],
        }
        for tipo, partes in casos.items():
            with leitor_mod.abrir(self.escreve(tipo.replace('+', 'p') + '.img', partes)) as l:
                vol = sa.detectar(l)
                self.assertIsNotNone(vol, tipo)
                self.assertEqual(vol.tipo, tipo)
                self.assertEqual(list(vol.varrer()), [])
                self.assertIn('carving', vol.limitacao)

    def test_lixo_nao_e_nada(self):
        caminho = os.path.join(self.pasta, 'lixo.img')
        with open(caminho, 'wb') as f:
            f.write(os.urandom(200 * 1024))
        with leitor_mod.abrir(caminho) as l:
            self.assertIsNone(sa.detectar(l))


if __name__ == '__main__':
    unittest.main()
