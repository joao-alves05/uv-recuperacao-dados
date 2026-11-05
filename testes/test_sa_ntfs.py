# -*- coding: utf-8 -*-
"""Fase 4, bloco 2: NTFS mínimo montado em Python; MFT em uso e livre, residente e não residente, caminho pelo pai."""
import os
import random
import unittest

from auxiliar import CasoComPasta, sha256_de

import imagens_ntfs as nt
from recuperacao import leitor as leitor_mod
from recuperacao import sa
from recuperacao.sa import ErroSA
from recuperacao.sa.base import epoch
from test_sa_fat import conteudo, sha


class TestNtfs(CasoComPasta):
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

    def montar(self):
        f = nt.NtfsImg(rotulo='MeuNTFS')
        d = self.d = {k: conteudo(n, i) for i, (k, n) in enumerate({
            'pequeno': 300, 'grande': 5000, 'frag': 4500, 'apag_res': 200, 'apag_nres': 3500, 'filho': 2200,
            'reuso': 3072, 'intruso': 1024, 'orfao': 150, 'dos': 100}.items())}
        docs = f.add_dir(5, 'Documentos e Relatórios')
        f.add_file(docs, 'pequeno.txt', d['pequeno'])
        f.add_file(5, 'grande.bin', d['grande'])
        f.add_file(docs, 'fragmentado grande.dat', d['frag'], fragmentos=2)
        a = f.add_file(docs, 'apagado residente.txt', d['apag_res'])
        f.apagar(a)
        b = f.add_file(docs, 'Apagado não residente – nome bem longo.pdf', d['apag_nres'])
        f.apagar(b)
        pasta = f.add_dir(5, 'Pasta removida')
        sub = f.add_file(pasta, 'filho.bin', d['filho'])
        f.apagar(sub)
        f.apagar(pasta)
        r = f.add_file(docs, 'reaproveitado.bin', d['reuso'])
        lcn = f.runs_de[r][0][0]
        f.apagar(r)
        f.add_file(5, 'ocupou.bin', d['intruso'], lcn=lcn + 1)
        f.add_file(77, 'orfao.txt', d['orfao'])                 # pai (registro 77) não existe
        f.add_file(5, 'Nome comprido do arquivo.txt', d['dos'], nome_dos='NOMECO~1.TXT')
        f.add_file(5, 'comprimido.bin', b'x' * 2000, flags_dados=1)
        return f.gravar(os.path.join(self.pasta, 'ntfs.img'))

    def test_vivos_residente_e_nao_residente(self):
        r = self.varrer(self.montar())
        self.assertEqual(self.vol.tipo, 'NTFS')
        self.assertEqual(self.vol.rotulo, 'MeuNTFS')
        e, b = r['/Documentos e Relatórios/pequeno.txt']
        self.assertEqual((e.apagado, e.estado, e.tamanho, sha(b)), (False, 'bom', 300, sha(self.d['pequeno'])))
        self.assertIsNotNone(e.inline)
        self.assertEqual(e.modificado, epoch(2024, 3, 15, 10, 20, 30))
        e, b = r['/grande.bin']
        self.assertEqual((e.estado, sha(b)), ('bom', sha(self.d['grande'])))
        self.assertIsNone(e.inline)
        e, b = r['/Documentos e Relatórios/fragmentado grande.dat']
        self.assertGreater(len(e.runs), 1)
        self.assertEqual((e.estado, sha(b)), ('bom', sha(self.d['frag'])))

    def test_apagados_com_caminho_pelo_pai(self):
        r = self.varrer(self.montar())
        e, b = r['/Documentos e Relatórios/apagado residente.txt']
        self.assertEqual((e.apagado, e.estado, sha(b)), (True, 'bom', sha(self.d['apag_res'])))
        e, b = r['/Documentos e Relatórios/Apagado não residente – nome bem longo.pdf']
        self.assertEqual((e.apagado, e.estado, e.tamanho, sha(b)), (True, 'bom', 3500, sha(self.d['apag_nres'])))
        self.assertTrue(r['/Pasta removida'][0].apagado)
        e, b = r['/Pasta removida/filho.bin']
        self.assertEqual((e.apagado, sha(b)), (True, sha(self.d['filho'])))

    def test_reuso_conferido_no_bitmap(self):
        r = self.varrer(self.montar())
        e, b = r['/Documentos e Relatórios/reaproveitado.bin']
        self.assertEqual((e.apagado, e.estado), (True, 'parcial'))
        self.assertNotEqual(sha(b), sha(self.d['reuso']))
        self.assertEqual(sha(r['/ocupou.bin'][1]), sha(self.d['intruso']))

    def test_orfao_nome_dos_e_sistema(self):
        r = self.varrer(self.montar())
        e, b = r['/[perdidos]/orfao.txt']
        self.assertEqual(sha(b), sha(self.d['orfao']))
        self.assertIn('/Nome comprido do arquivo.txt', r)                 # o nome Win32 vence o 8.3
        self.assertNotIn('/NOMECO~1.TXT', r)
        self.assertFalse([c for c in r if c.startswith('/$') or c == '/.'])
        self.assertEqual(r['/comprimido.bin'][0].estado, 'ruim')

    def test_mft_em_dois_pedacos_e_nao_confunde_fat(self):
        r = self.varrer(self.montar())
        self.assertEqual(len(self.vol._mft_runs), 2)
        self.assertGreaterEqual(len(r), 12)

    def test_corrompido_nao_derruba(self):
        base = self.montar()
        with open(base, 'rb') as g:
            bruto = bytearray(g.read())
        rng = random.Random(3)
        zonas = [(0, 512), (4096, 4096 + 24 * 1024), (4096 + 24 * 1024, 4096 + 60 * 1024), (100 * 1024, 140 * 1024)]
        for _ in range(80):
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


if __name__ == '__main__':
    unittest.main()
