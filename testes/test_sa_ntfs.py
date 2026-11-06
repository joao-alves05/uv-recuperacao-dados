# -*- coding: utf-8 -*-
"""Fase 4, bloco 2: NTFS mínimo montado em Python; MFT em uso e livre, residente e não residente, caminho pelo pai."""
import os
import random
import unittest

from auxiliar import CasoComPasta, sha256_de

import imagens_ntfs as nt
from recuperacao import leitor as leitor_mod
from recuperacao import sa
from recuperacao.sa import ntfs as mod_ntfs
from recuperacao.sa import ErroSA
from recuperacao.sa.base import epoch
from limite import com_limite
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


class TestMftEmLotes(CasoComPasta):
    def montar(self):
        f = nt.NtfsImg()
        pasta = f.add_dir(5, 'Docs')
        for i in range(10):
            f.add_file(pasta, 'arquivo %02d.bin' % i, conteudo(2000 + i, i))
        f.prox_reg = 38                                             # a pasta "Tardia" (41) tem número MAIOR que o do filho (38)
        f.add_file(41, 'filho de pasta tardia.txt', conteudo(300, 99))
        f.prox_reg = 41
        f.add_dir(5, 'Tardia')
        return f.gravar(os.path.join(self.pasta, 'ntfs.img'))

    def varrer(self, caminho, pedaco=None):
        import unittest.mock as m
        with leitor_mod.abrir(caminho) as l, m.patch.object(mod_ntfs, 'PEDACO_MFT', pedaco or mod_ntfs.PEDACO_MFT):
            vol = sa.detectar(l)
            chamadas = []
            vol.ao_ler = chamadas.append
            saida, no_primeiro = [], None
            for e in vol.varrer():
                if no_primeiro is None:
                    no_primeiro = len(chamadas)
                saida.append((e.caminho, e.apagado, e.estado, b''.join(vol.ler(e))))
            return saida, chamadas, no_primeiro

    def test_entrega_antes_de_acabar_de_ler_a_mft(self):
        caminho = self.montar()
        saida, chamadas, no_primeiro = self.varrer(caminho, pedaco=8 * 1024)         # 8 registros por pedaço
        self.assertEqual(len(chamadas), 8)
        self.assertEqual(chamadas[-1], 1.0)
        self.assertLess(no_primeiro, 8, 'o 1º arquivo só saiu depois de ler a MFT inteira')

    def test_mesmo_resultado_em_qualquer_tamanho_de_pedaco(self):
        caminho = self.montar()
        grande, _, _ = self.varrer(caminho)
        pequeno, _, _ = self.varrer(caminho, pedaco=8 * 1024)
        minimo, _, _ = self.varrer(caminho, pedaco=1024)
        self.assertEqual(sorted(grande), sorted(pequeno))
        self.assertEqual(sorted(grande), sorted(minimo))
        self.assertEqual(len(grande), 13)

    def test_filho_lido_antes_da_pasta_espera_e_ganha_o_caminho_certo(self):
        caminho = self.montar()
        saida, _, _ = self.varrer(caminho, pedaco=8 * 1024)
        caminhos = [c for c, _, _, _ in saida]
        self.assertIn('/Tardia/filho de pasta tardia.txt', caminhos)
        self.assertFalse([c for c in caminhos if c.startswith('/[perdidos]')])
        self.assertLess(caminhos.index('/Docs/arquivo 00.bin'), caminhos.index('/Tardia/filho de pasta tardia.txt'))

    def test_orfao_de_verdade_so_vira_perdido_no_fim(self):
        f = nt.NtfsImg()
        f.add_file(77, 'orfao.txt', conteudo(100, 1))
        f.add_file(5, 'normal.txt', conteudo(100, 2))
        saida, _, _ = self.varrer(f.gravar(os.path.join(self.pasta, 'o.img')), pedaco=8 * 1024)
        self.assertEqual(sorted(c for c, _, _, _ in saida), ['/[perdidos]/orfao.txt', '/normal.txt'])


class TestNtfsHostil(CasoComPasta):
    """Data runs e $Bitmap de disco hostil: nada de laço sem fim, offset negativo nem exceção solta."""

    def listar(self, f, nome='h.img'):
        caminho = f.gravar(os.path.join(self.pasta, nome))

        def tudo():
            with leitor_mod.abrir(caminho) as l:
                vol = sa.detectar(l)
                return {e.nome: e for e in vol.varrer()}
        return com_limite(tudo)

    def hostil(self, sem_bitmap):
        f = nt.NtfsImg()
        f.sem_bitmap = sem_bitmap
        # 1 cluster de verdade (arquivo de 1 KiB) e depois um run de 2**40 clusters que o tamanho nem alcança
        n = f.add_file(5, 'apagado hostil.bin', conteudo(1000, 1), runs_forcadas=[(500, 1), (600, 2 ** 40)])
        f.apagar(n)
        return f

    def test_run_gigante_alem_do_tamanho_sem_bitmap_nao_trava(self):
        e = self.listar(self.hostil(sem_bitmap=True))['apagado hostil.bin']
        self.assertTrue(e.apagado)
        self.assertEqual(len(e.runs), 1)
        self.assertIn('sem $Bitmap', e.nota)

    def test_run_gigante_alem_do_tamanho_com_bitmap_nao_trava(self):
        e = self.listar(self.hostil(sem_bitmap=False))['apagado hostil.bin']
        self.assertEqual((e.estado, len(e.runs)), ('bom', 1))

    def test_run_gigante_dentro_do_tamanho_e_recusado_sem_demora(self):
        f = nt.NtfsImg()
        f.sem_bitmap = True
        f.apagar(f.add_file(5, 'enorme.bin', b'x' * 1000, runs_forcadas=[(500, 2 ** 40)]))
        e = self.listar(f)['enorme.bin']
        self.assertEqual((e.estado, e.runs), ('ruim', []))

    def test_lcn_negativo_nao_vira_offset_negativo(self):
        f = nt.NtfsImg()
        f.add_file(5, 'negativo.bin', b'x' * 1000, runs_forcadas=[(-5, 1)])
        e = self.listar(f)['negativo.bin']
        self.assertEqual((e.estado, e.runs), ('ruim', []))
        self.assertIn('fora do volume', e.nota)

    def test_bitmap_apontando_para_fora_do_volume_nao_dispara_indexerror(self):
        f = nt.NtfsImg()
        f.add_file(5, 'x.bin', b'x' * 10)
        caminho = f.gravar(os.path.join(self.pasta, 'b.img'))
        with leitor_mod.abrir(caminho) as l:
            vol = sa.detectar(l)
            vol._bitmap = [(0, 10 ** 9, vol.tamanho)]               # a página do bitmap começa exatamente no fim
            self.assertIsNone(vol._bit_ocupado(0))
            vol._bitmap = [(0, 10 ** 9, vol.tamanho + 8192)]
            self.assertIsNone(vol._bit_ocupado(0))

    def test_soma_das_faixas_maior_que_o_volume_sai_ruim(self):
        f = nt.NtfsImg()
        # 3 runs de 1000 clusters sobrepostos: cada um cabe no volume (2000 clusters), a soma (3 MB) não
        f.add_file(5, 'sobreposto.bin', b'x' * (3000 * 1024), runs_forcadas=[(500, 1000), (500, 1000), (500, 1000)])
        f.add_file(5, 'normal.bin', b'y' * 1000)
        r = self.listar(f)
        self.assertEqual((r['sobreposto.bin'].estado, r['sobreposto.bin'].runs), ('ruim', []))
        self.assertEqual(r['normal.bin'].estado, 'bom')

    def test_teto_de_clusters_conferidos(self):
        f = nt.NtfsImg()
        f.apagar(f.add_file(5, 'grande.bin', b'x' * (50 * 1024), runs_forcadas=[(500, 50)]))
        import unittest.mock as m
        chamadas = []
        original = mod_ntfs.Ntfs._bit_ocupado
        with m.patch.object(mod_ntfs, 'MAX_CLUSTERS_CONFERIDOS', 10),                 m.patch.object(mod_ntfs.Ntfs, '_bit_ocupado', lambda self_, c: chamadas.append(c) or original(self_, c)):
            self.listar(f)
        self.assertLessEqual(len(chamadas), 10)


if __name__ == '__main__':
    unittest.main()
