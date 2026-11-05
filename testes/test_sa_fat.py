# -*- coding: utf-8 -*-
"""Fase 4, bloco 1: FAT12/16/32 e exFAT sobre imagens montadas em Python; a origem nunca muda."""
import glob
import hashlib
import os
import random
import re
import unittest

from auxiliar import CasoComPasta, RAIZ, sha256_de

import imagens_sa as im
from recuperacao import leitor as leitor_mod
from recuperacao import sa
from recuperacao.sa import ErroSA


def conteudo(n, semente):
    return random.Random(semente).randbytes(n)


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Base(CasoComPasta):
    def varrer(self, caminho):
        """Abre pelo Leitor de verdade, varre e lê todos os arquivos. Devolve {caminho: (entrada, bytes)}."""
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


class TestFat32(Base):
    def montar(self):
        f = im.FatImg('FAT32', rotulo='MEUDISCO')
        self.dados_ = {
            'vivo': conteudo(1300, 1), 'longo': conteudo(2000, 2), 'frag': conteudo(2600, 3),
            'dentro': conteudo(900, 4), 'reuso': conteudo(2048, 5), 'intruso': conteudo(1024, 6),
            'filho': conteudo(700, 7), 'vazio': b''}
        f.add('/', 'Relatório anual 2023 – versão final.docx', self.dados_['vivo'])
        f.mkdir('/', 'Fotos de férias')
        f.add('/Fotos de férias', 'VIAGEM.JPG', self.dados_['dentro'], curto=b'VIAGEM  JPG', lfn=False)
        e, cs = f.add('/', 'documento apagado com nome bem comprido.pdf', self.dados_['longo'])
        f.apagar(e, cs)
        f.add('/', 'fragmentado.bin', self.dados_['frag'], fragmentar=True)
        # apagado cujo espaço foi reaproveitado: 'reuso' (4 clusters) apagado, 'intruso' vivo sobre os 2 últimos
        e, cs = f.add('/', 'perdido.dat', self.dados_['reuso'])
        f.apagar(e, cs)
        f.add('/', 'novo.dat', self.dados_['intruso'], cluster=cs[2])
        # pasta apagada com arquivo dentro
        f.mkdir('/', 'Pasta velha')
        eparent = f.pastas['/'].entradas[-1]
        e, cs = f.add('/Pasta velha', 'filho.txt', self.dados_['filho'])
        f.apagar(e, cs)
        f.apagar_pasta('/Pasta velha', eparent)
        f.add('/', 'vazio.txt', b'')
        # pasta com mais entradas que cabem em um cluster (16): encadeia
        f.mkdir('/', 'Muitos')
        for i in range(30):
            f.add('/Muitos', 'arquivo numero %02d.txt' % i, conteudo(10 + i, 100 + i))
        return f.gravar(os.path.join(self.pasta, 'fat32.img'))

    def test_nomes_caminhos_e_conteudo(self):
        r = self.varrer(self.montar())
        self.assertEqual(self.vol.tipo, 'FAT32')
        self.assertEqual(self.vol.rotulo, 'MEUDISCO')
        e, b = r['/Relatório anual 2023 – versão final.docx']
        self.assertFalse(e.apagado)
        self.assertEqual(e.estado, 'bom')
        self.assertEqual((e.tamanho, sha(b)), (1300, sha(self.dados_['vivo'])))
        from recuperacao.sa.base import epoch
        self.assertEqual(e.modificado, epoch(2024, 3, 15, 10, 20, 30))
        self.assertEqual(r['/Fotos de férias'][0].eh_dir, True)
        e, b = r['/Fotos de férias/VIAGEM.JPG']
        self.assertEqual(sha(b), sha(self.dados_['dentro']))
        self.assertFalse(e.apagado)

    def test_apagado_com_nome_longo_recupera_inteiro(self):
        r = self.varrer(self.montar())
        e, b = r['/documento apagado com nome bem comprido.pdf']
        self.assertTrue(e.apagado)
        self.assertEqual(e.estado, 'bom')
        self.assertEqual((e.tamanho, sha(b)), (2000, sha(self.dados_['longo'])))

    def test_apagado_com_espaco_reaproveitado_sai_parcial(self):
        r = self.varrer(self.montar())
        e, b = r['/perdido.dat']
        self.assertTrue(e.apagado)
        self.assertEqual(e.estado, 'parcial')
        self.assertNotEqual(sha(b), sha(self.dados_['reuso']))
        self.assertEqual(b[:1024], self.dados_['reuso'][:1024])       # os 2 primeiros clusters seguem intactos
        e2, b2 = r['/novo.dat']
        self.assertEqual((e2.estado, sha(b2)), ('bom', sha(self.dados_['intruso'])))

    def test_fragmentado_vivo_segue_a_cadeia(self):
        r = self.varrer(self.montar())
        e, b = r['/fragmentado.bin']
        self.assertEqual(e.estado, 'bom')
        self.assertGreater(len(e.runs), 1)
        self.assertEqual(sha(b), sha(self.dados_['frag']))

    def test_pasta_apagada_e_o_que_estava_dentro(self):
        r = self.varrer(self.montar())
        p, _ = r['/Pasta velha']
        self.assertTrue(p.apagado and p.eh_dir)
        e, b = r['/Pasta velha/filho.txt']
        self.assertTrue(e.apagado)
        self.assertEqual(sha(b), sha(self.dados_['filho']))

    def test_pasta_com_varios_clusters_e_arquivo_vazio(self):
        r = self.varrer(self.montar())
        filhos = [c for c in r if c.startswith('/Muitos/')]
        self.assertEqual(len(filhos), 30)
        e, b = r['/Muitos/arquivo numero 29.txt']
        self.assertEqual(b, conteudo(39, 129))
        e, b = r['/vazio.txt']
        self.assertEqual((e.tamanho, b, e.runs), (0, b'', []))

    def test_listar_filhos_diretos(self):
        self.varrer(self.montar())
        with leitor_mod.abrir(os.path.join(self.pasta, 'fat32.img')) as l:
            vol = sa.detectar(l)
            nomes = {e.nome for e in vol.listar('/Fotos de férias')}
            self.assertEqual(nomes, {'VIAGEM.JPG'})
            self.assertIn('Muitos', {e.nome for e in vol.listar('/')})
            self.assertNotIn('arquivo numero 00.txt', {e.nome for e in vol.listar('/')})

    def test_nome_com_barra_nao_vira_caminho(self):
        f = im.FatImg('FAT32')
        f.add('/', '..\\..\\fora.txt', b'x' * 10)
        r = self.varrer(f.gravar(os.path.join(self.pasta, 'a.img')))
        self.assertEqual(list(r), ['/.._.._fora.txt'])


class TestFat16e12(Base):
    def roda(self, tipo):
        f = im.FatImg(tipo, rotulo='DISCO')
        a, b = conteudo(3000, 11), conteudo(1500, 12)
        f.add('/', 'Leia-me bem.txt', a)
        f.mkdir('/', 'sub')
        e, cs = f.add('/sub', 'apagado longo.bin', b)
        f.apagar(e, cs)
        r = self.varrer(f.gravar(os.path.join(self.pasta, tipo + '.img')))
        self.assertEqual(self.vol.tipo, tipo)
        self.assertEqual(self.vol.rotulo, 'DISCO')
        self.assertEqual(sha(r['/Leia-me bem.txt'][1]), sha(a))
        e, bb = r['/sub/apagado longo.bin']
        self.assertTrue(e.apagado)
        self.assertEqual((e.estado, sha(bb)), ('bom', sha(b)))

    def test_fat16(self):
        self.roda('FAT16')

    def test_fat12(self):
        self.roda('FAT12')


class TestExFat(Base):
    def test_arquivos_apagados_com_nome_longo_utf16(self):
        f = im.ExFatImg(rotulo='EXF')
        d = {k: conteudo(n, i) for i, (k, n) in enumerate({'vivo': 5000, 'apagado': 3000, 'reuso': 4096, 'intruso': 1024,
                                                           'dentro': 800, 'filho': 1500, 'fat': 2500}.items())}
        f.add('/', 'Relatório – 日本語 😀 com nome muito mais comprido que quinze caracteres.txt', d['vivo'])
        f.mkdir('/', 'Pasta')
        f.add('/Pasta', 'dentro.bin', d['dentro'])
        e, cs = f.add('/', 'Apagado – ção com nome longo longo longo.mp4', d['apagado'])
        f.apagar(e, cs)
        e, cs = f.add('/', 'reaproveitado.dat', d['reuso'])
        f.apagar(e, cs)
        f.add('/', 'ocupou.dat', d['intruso'], cluster=cs[2])
        f.add('/', 'com cadeia.bin', d['fat'], nofat=False)
        f.mkdir('/', 'Apagada')
        pe = f.entradas['/'][-1]
        e, cs = f.add('/Apagada', 'filho.txt', d['filho'])
        f.apagar(e, cs)
        f.apagar(pe)
        r = self.varrer(f.gravar(os.path.join(self.pasta, 'exfat.img')))
        self.assertEqual(self.vol.tipo, 'exFAT')
        self.assertEqual(self.vol.rotulo, 'EXF')
        e, b = r['/Relatório – 日本語 😀 com nome muito mais comprido que quinze caracteres.txt']
        self.assertEqual((e.estado, e.apagado, sha(b)), ('bom', False, sha(d['vivo'])))
        from recuperacao.sa.base import epoch
        self.assertEqual(e.criado, epoch(2024, 3, 15, 10, 20, 30))
        self.assertEqual(sha(r['/Pasta/dentro.bin'][1]), sha(d['dentro']))
        e, b = r['/Apagado – ção com nome longo longo longo.mp4']
        self.assertEqual((e.apagado, e.estado, e.tamanho, sha(b)), (True, 'bom', 3000, sha(d['apagado'])))
        e, b = r['/reaproveitado.dat']
        self.assertEqual((e.apagado, e.estado), (True, 'parcial'))
        self.assertNotEqual(sha(b), sha(d['reuso']))
        self.assertEqual(sha(r['/ocupou.dat'][1]), sha(d['intruso']))
        self.assertEqual(sha(r['/com cadeia.bin'][1]), sha(d['fat']))
        self.assertTrue(r['/Apagada'][0].apagado)
        e, b = r['/Apagada/filho.txt']
        self.assertEqual((e.apagado, sha(b)), (True, sha(d['filho'])))


class TestRobustez(Base):
    def test_nao_e_fat_nem_exfat(self):
        caminho = os.path.join(self.pasta, 'lixo.img')
        with open(caminho, 'wb') as f:
            f.write(os.urandom(8192))
        with leitor_mod.abrir(caminho) as l:
            self.assertIsNone(sa.detectar(l))

    def test_imagem_corrompida_nao_derruba(self):
        """Estraga bytes de boot/FAT/pastas ao acaso: pode dar ErroSA, nunca outra exceção nem laço infinito."""
        for tipo, f in (('FAT32', im.FatImg('FAT32')), ('FAT16', im.FatImg('FAT16')), ('exFAT', im.ExFatImg())):
            f.mkdir('/', 'Pasta')
            f.add('/Pasta', 'um arquivo qualquer.txt', conteudo(3000, 1))
            e, cs = f.add('/', 'apagado grande.bin', conteudo(5000, 2))
            f.apagar(e, cs)
            base = f.gravar(os.path.join(self.pasta, tipo + '.img'))
            with open(base, 'rb') as g:
                bruto = bytearray(g.read())
            rng = random.Random(7)
            zonas = [(0, 512), (512, 70000)]
            for rodada in range(60):
                d = bytearray(bruto)
                for _ in range(rng.randint(1, 40)):
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

    def test_modulos_nao_abrem_nada_nem_escrevem(self):
        for caminho in glob.glob(os.path.join(RAIZ, 'recuperacao', 'sa', '*.py')):
            with open(caminho, encoding='utf-8') as g:
                texto = g.read()
            self.assertIsNone(re.search(r'\bopen\s*\(|os\.open|O_WRONLY|O_RDWR|GENERIC_WRITE|\.write\(', texto), caminho)


if __name__ == '__main__':
    unittest.main()
