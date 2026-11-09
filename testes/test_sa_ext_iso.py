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
from limite import com_limite
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

    def test_ext4_extent_nao_inicializado_le_como_zeros_e_nao_como_lixo_do_disco(self):
        f = ie.ExtImg('ext4')
        dados = conteudo(2 * 1024, 7)
        f.add_file_fallocate(2, 'fallocate.bin', dados, 3)           # 2 blocos de dados + 3 do fallocate (lixo 0xAA no disco)
        caminho = f.gravar(os.path.join(self.pasta, 'fa.img'))
        r = self.varrer(caminho)
        e, b = r['/fallocate.bin']
        self.assertEqual(e.tamanho, 5 * 1024)
        self.assertEqual(b, dados + bytes(3 * 1024), 'o extent não inicializado precisa valer zero')
        self.assertNotIn(0xAA, b[2 * 1024:])
        self.assertEqual([o is None for o, _ in e.runs], [False, True])

    def test_arvore_de_extents_hostil_tem_teto_e_o_arquivo_sai_ruim(self):
        f = ie.ExtImg('ext4')
        f.add_file_arvore_hostil(2, 'hostil.bin')
        f.add_file(2, 'normal.bin', conteudo(3000, 1))
        caminho = f.gravar(os.path.join(self.pasta, 'h.img'))

        def listar():
            with leitor_mod.abrir(caminho) as l:
                return {e.nome: e for e in sa.detectar(l).varrer()}
        r = com_limite(listar)
        self.assertEqual((r['hostil.bin'].estado, r['hostil.bin'].runs), ('ruim', []))
        self.assertIn('grande demais', r['hostil.bin'].nota)
        self.assertEqual(r['normal.bin'].estado, 'bom')                  # o resto da lista segue

    def test_tetos_de_extents_por_arquivo(self):
        import unittest.mock as m
        from recuperacao.sa import ext as mod_ext
        f = ie.ExtImg('ext4')
        f.add_file(2, 'fragmentado.bin', conteudo(40000, 2), fragmentos=6)
        caminho = f.gravar(os.path.join(self.pasta, 'f.img'))
        for campo in ('MAX_NOS_EXTENTS', 'MAX_EXTENTS'):
            with m.patch.object(mod_ext, campo, 1 if campo == 'MAX_NOS_EXTENTS' else 3):
                with leitor_mod.abrir(caminho) as l:
                    e = {x.nome: x for x in sa.detectar(l).varrer()}['fragmentado.bin']
                self.assertEqual((e.estado, e.runs), ('ruim', []), campo)

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


class TestTetosDeTrabalho(Base):
    def test_ext2_arquivo_com_mapa_de_blocos_grande_demais_sai_parcial_com_o_comeco(self):
        import unittest.mock as m
        from recuperacao.sa import ext as mod_ext
        f = ie.ExtImg('ext2')
        f.add_file(2, 'grande.bin', conteudo(100 * 1024, 1))
        f.add_file(2, 'pequeno.bin', conteudo(30 * 1024, 2))
        caminho = f.gravar(os.path.join(self.pasta, 'g.img'))
        with m.patch.object(mod_ext, 'MAX_BLOCOS_MAPA', 64), leitor_mod.abrir(caminho) as l:
            r = {e.nome: e for e in sa.detectar(l).varrer()}
        g = r['grande.bin']
        self.assertEqual(g.estado, 'parcial')
        self.assertEqual(g.tamanho, 100 * 1024)
        n = sum(q for _, q in g.runs)
        self.assertEqual(n, 64 * 1024)                                            # só até o teto; o resto não lido
        self.assertIn('não foi lido', g.nota)
        with leitor_mod.abrir(caminho) as l:
            self.assertEqual(b''.join(l.ler(o, q) for o, q in g.runs), conteudo(100 * 1024, 1)[:n])
        self.assertEqual(r['pequeno.bin'].estado, 'bom')

    def test_iso_pastas_sobrepostas_estouram_o_teto_total_de_leitura(self):
        import unittest.mock as m
        from recuperacao.sa import iso as mod_iso
        f = ii.IsoImg(joliet=False)
        for i in range(4):
            f.add_dir('/', 'D%d' % i)
        caminho = f.gravar(os.path.join(self.pasta, 'p.iso'))
        with leitor_mod.abrir(caminho) as l:
            self.assertEqual(len(list(sa.detectar(l).varrer())), 4)           # sem teto apertado, lista tudo
        with m.patch.object(mod_iso, 'MAX_TOTAL_PASTAS', 2 * 2048), leitor_mod.abrir(caminho) as l:
            with self.assertRaises(ErroSA):
                list(sa.detectar(l).varrer())

    def test_udf_pastas_estouram_o_teto_total_de_leitura(self):
        import unittest.mock as m
        import imagens_udf as iu
        from recuperacao.sa import udf as mod_udf
        arq = {'D%d/a.txt' % i: conteudo(100, i) for i in range(4)}
        caminho = os.path.join(self.pasta, 'p.udf')
        with open(caminho, 'wb') as g:
            g.write(iu.disco_udf([iu.Sessao(arq, 0, alt_fim=1199)], 1200))
        with leitor_mod.abrir(caminho) as l:
            self.assertEqual(sum(not e.eh_dir for e in sa.detectar(l).varrer()), 4)    # sem teto apertado, lista tudo
        with m.patch.object(mod_udf, 'MAX_TOTAL_PASTAS', 100), leitor_mod.abrir(caminho) as l:
            vol = sa.detectar(l)
            self.assertIsNotNone(vol)
            with self.assertRaises(ErroSA):
                list(vol.varrer())

    def test_iso_multiextent_cuja_continuacao_passa_do_fim_da_imagem_nao_sai_bom(self):
        f = ii.IsoImg(joliet=False)
        f.add_file('/', 'dois.bin', conteudo(5000, 1), curto='DOIS.BIN;1', multi=True)
        caminho = f.gravar(os.path.join(self.pasta, 'm.iso'))
        r = {e.nome: e for e in self.listar_iso(caminho)}
        self.assertEqual(r['DOIS.BIN'].estado, 'bom')
        with open(caminho, 'r+b') as g:
            g.truncate(os.path.getsize(caminho) - 2048)                     # perde o fim da 2ª parte
        r = {e.nome: e for e in self.listar_iso(caminho)}
        self.assertEqual(r['DOIS.BIN'].estado, 'parcial')

    def listar_iso(self, caminho):
        with leitor_mod.abrir(caminho) as l:
            return list(sa.detectar(l).varrer())

    def test_conferir_tamanho_recusa_soma_e_tamanho_absurdos(self):
        from recuperacao.sa.base import Entrada
        f = ii.IsoImg(joliet=False)
        f.add_file('/', 'a.txt', conteudo(10, 1), curto='A.TXT;1')
        with leitor_mod.abrir(f.gravar(os.path.join(self.pasta, 'c.iso'))) as l:
            vol = sa.detectar(l)
            soma = Entrada(nome='s', caminho='/s', tamanho=vol.tamanho, runs=[(0, vol.tamanho), (0, vol.tamanho)])
            enorme = Entrada(nome='e', caminho='/e', tamanho=5 * vol.tamanho, runs=[(0, 10)])
            bom = Entrada(nome='b', caminho='/b', tamanho=100, runs=[(0, 100)])
            for e in (soma, enorme, bom):
                vol.conferir_tamanho(e)
            self.assertEqual([(e.estado, e.runs == []) for e in (soma, enorme, bom)],
                             [('ruim', True), ('ruim', True), ('bom', False)])

    def test_ao_ler_e_chamado_a_cada_pasta_e_pode_interromper(self):
        from recuperacao.sa.base import ErroSA as _E
        f = ii.IsoImg(joliet=False)
        for i in range(3):
            f.add_dir('/', 'D%d' % i)
        caminho = f.gravar(os.path.join(self.pasta, 'q.iso'))
        with leitor_mod.abrir(caminho) as l:
            vol = sa.detectar(l)
            chamadas = []
            vol.ao_ler = chamadas.append
            list(vol.varrer())
            self.assertEqual(len(chamadas), 4)                               # a raiz e as 3 pastas

            class Pare(Exception):
                pass

            def para(_):
                raise Pare()
            vol.ao_ler = para
            with self.assertRaises(Pare):
                list(vol.varrer())


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
