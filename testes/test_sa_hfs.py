# -*- coding: utf-8 -*-
"""Fase 4: HFS+ de leitura (catálogo em B-tree, extents overflow, nome Unicode) sobre volume montado em Python."""
import os
import random
import unicodedata
import unittest

from auxiliar import CasoComPasta, sha256_de

import imagens_hfs as ih
from recuperacao import leitor as leitor_mod
from recuperacao import sa
from recuperacao.sa import ErroSA, hfs
from recuperacao.sa.base import epoch
from test_sa_fat import conteudo, sha

NUL4 = chr(0) * 4


def nfc(texto):
    return unicodedata.normalize('NFC', texto)


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


class TestHfs(Base):
    def montar(self, **kw):
        f = ih.HfsImg(rotulo='Vol Teste')
        d = self.d = {k: conteudo(n, i) for i, (k, n) in enumerate({
            'acento': 1500, 'foto': 9000, 'frag': 10 * 4096 + 100, 'semov': 10 * 4096, 'comp': 700, 'priv': 400,
            'filho': 800, 'link': 50}.items())}
        f.add_file(2, 'Relatório – versão final.txt', d['acento'])
        fotos = f.add_dir(2, 'Fotos')
        ano = f.add_dir(fotos, '2024')
        f.add_file(ano, 'viagem à praia.jpg', d['foto'])
        f.add_file(2, 'fragmentado.bin', d['frag'], fragmentos=10)         # 10 extents: 2 vão para o overflow
        f.add_file(2, 'sem overflow.bin', d['semov'], fragmentos=10, sem_overflow=True)
        f.add_file(2, 'comprimido.txt', d['comp'], comprimido=True)
        f.add_file(2, 'atalho', d['link'], simbolico=True)
        privada = f.add_dir(2, NUL4 + 'HFS+ Private Data')
        f.add_file(privada, 'iNode99', d['priv'])
        velha, nova = f.add_dir(2, 'Velha'), f.add_dir(2, 'Nova')
        f.add_file(velha, 'filho de pasta movida.txt', d['filho'])
        f.mover(velha, nova)                                     # Velha (id menor) passa a morar numa pasta de id MAIOR
        muitas = f.add_dir(2, 'Muitas')
        for i in range(60):
            f.add_file(muitas, 'arquivo com nome comprido numero %02d.txt' % i, conteudo(30 + i, 300 + i))
        return f.gravar(os.path.join(self.pasta, 'hfs.img'), **kw)

    def test_lista_com_caminho_nome_acentuado_e_bytes_que_batem(self):
        r = self.varrer(self.montar())
        self.assertEqual((self.vol.tipo, self.vol.rotulo), ('HFS+', 'Vol Teste'))
        self.assertIsInstance(self.vol, hfs.Hfs)
        e, b = r['/' + nfc('Relatório – versão final.txt')]
        self.assertEqual((e.apagado, e.estado, e.tamanho, sha(b)), (False, 'bom', 1500, sha(self.d['acento'])))
        self.assertEqual(e.modificado, epoch(2024, 3, 15, 10, 20, 30))
        self.assertTrue(r['/Fotos'][0].eh_dir and r['/Fotos/2024'][0].eh_dir)
        e, b = r['/Fotos/2024/' + nfc('viagem à praia.jpg')]
        self.assertEqual(sha(b), sha(self.d['foto']))

    def test_extents_no_overflow(self):
        r = self.varrer(self.montar())
        e, b = r['/fragmentado.bin']
        self.assertEqual((e.estado, len(e.runs), sha(b)), ('bom', 10, sha(self.d['frag'])))
        e, b = r['/sem overflow.bin']
        self.assertEqual((e.estado, len(e.runs)), ('parcial', 8))
        self.assertEqual(b, self.d['semov'][:8 * 4096])

    def test_o_que_nao_se_lista_ou_nao_se_le(self):
        r = self.varrer(self.montar())
        self.assertEqual(r['/comprimido.txt'][0].estado, 'ruim')
        self.assertIn('decmpfs', r['/comprimido.txt'][0].nota)
        self.assertNotIn('/atalho', r)
        self.assertFalse([c for c in r if 'Private' in c or chr(0) in c])
        self.assertTrue(all(not e.apagado for e, _ in r.values()))

    def test_pasta_com_numero_maior_que_o_filho_espera_e_ganha_o_caminho(self):
        r = self.varrer(self.montar())
        e, b = r['/Nova/Velha/filho de pasta movida.txt']
        self.assertEqual(sha(b), sha(self.d['filho']))
        self.assertFalse([c for c in r if c.startswith('/[perdidos]')])

    def test_varias_folhas_da_btree(self):
        r = self.varrer(self.montar())
        self.assertEqual(len([c for c in r if c.startswith('/Muitas/')]), 60)
        e, b = r['/Muitas/arquivo com nome comprido numero 59.txt']
        self.assertEqual(b, conteudo(30 + 59, 300 + 59))

    def test_cabecalho_principal_destruido_usa_a_copia_do_fim(self):
        r = self.varrer(self.montar(sem_cabecalho_principal=True))
        self.assertTrue(self.vol.usou_reserva)
        self.assertIn('/fragmentado.bin', r)

    def test_ao_ler(self):
        caminho = self.montar()
        with leitor_mod.abrir(caminho) as l:
            vol = sa.detectar(l)
            fr = []
            vol.ao_ler = fr.append
            list(vol.varrer())
            self.assertEqual(fr[-1], 1.0)

    def test_numero_de_registros_absurdo_no_no_vira_erro_do_sa_e_nao_struct_error(self):
        caminho = self.montar()
        with open(caminho, 'r+b') as g:
            g.seek(3 * ih.BS + 1 * ih.NO + 10)                    # numRecords do 1º nó-folha
            g.write(bytes([0xFF, 0xFF]))
        with leitor_mod.abrir(caminho) as l:
            vol = sa.detectar(l)
            self.assertIsInstance(vol, hfs.Hfs)
            with self.assertRaises(ErroSA):
                list(vol.varrer())

    def test_no_ruim_no_meio_entrega_o_que_leu_e_depois_avisa_com_erro(self):
        caminho = self.montar()
        with open(caminho, 'r+b') as g:
            g.seek(3 * ih.BS + 3 * ih.NO + 10)                    # 3º nó-folha
            g.write(bytes([0xFF, 0xFF]))
        with leitor_mod.abrir(caminho) as l:
            vol = sa.detectar(l)
            lidas = []
            with self.assertRaises(ErroSA) as r:
                for e in vol.varrer():
                    lidas.append(e.caminho)
            self.assertTrue(lidas, 'o que veio antes do nó ruim precisa sair')
            self.assertIn('ilegível', str(r.exception))

    def test_extents_sobrepostos_que_somam_mais_que_o_volume_saem_ruins(self):
        f = ih.HfsImg()
        # 8 extents de 100 blocos (cada um cabe nos 300 do volume), soma 3,2 MB > 1,2 MB do volume
        f.add_file(2, 'sobreposto.bin', b'x', extents_forcados=[(10, 100)] * 8, tamanho_forcado=8 * 100 * ih.BS)
        f.add_file(2, 'normal.bin', conteudo(500, 1))
        r = self.varrer(f.gravar(os.path.join(self.pasta, 'h.img')))
        self.assertEqual((r['/sobreposto.bin'][0].estado, r['/sobreposto.bin'][0].runs), ('ruim', []))
        self.assertEqual(r['/normal.bin'][0].estado, 'bom')

    def test_imagem_corrompida_nao_derruba(self):
        base = self.montar()
        with open(base, 'rb') as g:
            bruto = bytearray(g.read())
        rng = random.Random(4)
        zonas = [(1024, 1024 + 512), (2 * ih.BS, 2 * ih.BS + 2048), (3 * ih.BS, 3 * ih.BS + 12 * ih.BS)]
        for _ in range(100):
            d = bytearray(bruto)
            for _ in range(rng.randint(1, 60)):
                a, z = rng.choice(zonas)
                d[rng.randrange(a, z)] = rng.randrange(256)
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

    def test_volume_sem_catalogo_legivel_cai_no_so_reconhecido(self):
        caminho = self.montar()
        with open(caminho, 'r+b') as g:
            g.seek(3 * ih.BS)
            g.write(b'\xff' * 1024)                               # destrói o nó de cabeçalho do catálogo
        with leitor_mod.abrir(caminho) as l:
            vol = sa.detectar(l)
            self.assertEqual(vol.tipo, 'HFS+')
            self.assertNotIsInstance(vol, hfs.Hfs)
            self.assertEqual(list(vol.varrer()), [])
            self.assertIn('carving', vol.limitacao)


if __name__ == '__main__':
    unittest.main()
