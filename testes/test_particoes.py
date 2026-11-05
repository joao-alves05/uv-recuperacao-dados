# -*- coding: utf-8 -*-
"""Fase 3: tabelas de partição (MBR, GPT, BSD, APM), busca de partição perdida, tabela reconstruída, rota e job.

Tudo com imagens montadas em Python (imagens_particoes.py) em pasta temporária: nunca disco real."""
import hashlib
import json
import os
import struct
import time
import unittest
from unittest import mock

import auxiliar
from auxiliar import CasoComPasta, esperar, porta_livre, sha256_de
import imagens_particoes as ip
from test_somente_leitura import Espiao

from recuperacao import leitor, particoes, servico, unidade
from recuperacao.executor import Executor
from recuperacao.jobs import JobStore
from recuperacao.tipos import particoes as tipo_particoes

MIB = ip.MIB
GUID_DADOS = 'ebd0a0a2-b9e5-4433-87c0-68b6b72699c7'
GUID_LINUX = '0fc63daf-8483-4772-8e79-3d69d8477de4'


class Base(CasoComPasta):
    def gravar(self, img, nome='disco.img'):
        caminho = os.path.join(self.trabalho, nome)
        with open(caminho, 'wb') as f:
            f.write(img)
        return caminho

    def abrir(self, img, nome='disco.img'):
        l = leitor.abrir(self.gravar(img, nome))
        self.addCleanup(l.fechar)
        return l

    def resumo(self, tabela):
        return [(p['inicio'], p['tamanho'], p['sistema']) for p in tabela['particoes']]


class TestMBR(Base):
    def test_duas_primarias_estendida_e_duas_logicas(self):
        img, esperado = ip.disco_mbr_completo()
        t = particoes.analisar(self.abrir(img))
        self.assertEqual((t['esquema'], t['estado'], t['avisos']), ('mbr', 'ok', []))
        self.assertEqual(self.resumo(t), esperado)
        self.assertEqual([p['origem_tabela'] for p in t['particoes']], ['mbr', 'mbr', 'mbr', 'mbr-logica', 'mbr-logica'])
        self.assertTrue(t['particoes'][0]['ativa'])
        self.assertFalse(t['particoes'][1]['ativa'])
        self.assertTrue(t['particoes'][2].get('estendida'))
        self.assertTrue(all(p['logica'] for p in t['particoes'] if p['origem_tabela'] == 'mbr-logica'))
        self.assertEqual(t['particoes'][0]['tipo'], '0x0C')

    def test_sem_assinatura_55aa_e_tabela_danificada_mas_lista(self):
        img, esperado = ip.disco_mbr_completo()
        img[510:512] = b'\0\0'
        t = particoes.analisar(self.abrir(img))
        self.assertEqual((t['esquema'], t['estado']), ('mbr', 'tabela_danificada'))
        self.assertEqual(self.resumo(t)[:2], esperado[:2])
        self.assertTrue(any('55AA' in a for a in t['avisos']))

    def test_cadeia_de_logicas_em_laco_nao_trava(self):
        img, _ = ip.disco_mbr_completo()
        ip.mbr(img, [(0, 0x0C, 2048, 4096), (0, 0x05, 34816, 6144)], base=(55296 + 34816) * 512)   # aponta para si mesmo
        t = particoes.analisar(self.abrir(img))
        self.assertEqual(t['estado'], 'tabela_danificada')
        self.assertTrue(any('volta' in a for a in t['avisos']))
        self.assertEqual(len([p for p in t['particoes'] if p['logica']]), 2)

    def test_particoes_sobrepostas_e_fora_do_disco_geram_aviso(self):
        img = bytearray(8 * MIB)
        ip.mbr(img, [(0, 0x83, 100, 8000), (0, 0x83, 4000, 20000)])
        t = particoes.analisar(self.abrir(img))
        self.assertTrue(any('Sobrepõe' in a for p in t['particoes'] for a in p['avisos']))
        self.assertTrue(any('fim do disco' in a for a in t['particoes'][1]['avisos']))

    def test_volume_sem_tabela(self):
        img = bytearray(16 * MIB)
        ip.fat32(img, 0, 16 * MIB)
        t = particoes.analisar(self.abrir(img))
        self.assertEqual((t['esquema'], t['estado']), ('volume', 'sem_tabela'))
        self.assertEqual(self.resumo(t), [(0, 16 * MIB, 'fat32')])

    def test_disco_zerado_e_ruido_nao_tem_tabela(self):
        t = particoes.analisar(self.abrir(bytearray(2 * MIB)))
        self.assertEqual((t['esquema'], t['estado'], t['particoes']), ('nenhum', 'sem_tabela', []))
        self.assertTrue(any('zerados' in a for a in t['avisos']))
        t = particoes.analisar(self.abrir(bytearray(os.urandom(2 * MIB)), 'ruido.img'))
        self.assertEqual((t['esquema'], t['particoes']), ('nenhum', []))


class TestGPT(Base):
    def disco(self, setor=512):
        img = bytearray(48 * MIB)
        ip.fat32(img, 1 * MIB, 12 * MIB)
        ip.ntfs(img, 14 * MIB, 12 * MIB)
        ip.gpt(img, [(GUID_DADOS, 1 * MIB // setor, 13 * MIB // setor - 1, 'Dados'),
                     (GUID_DADOS, 14 * MIB // setor, 26 * MIB // setor - 1, 'Windows')], setor)
        return img

    def test_gpt_integro(self):
        t = particoes.analisar(self.abrir(self.disco()))
        self.assertEqual((t['esquema'], t['estado'], t['avisos']), ('gpt', 'ok', []))
        self.assertEqual(self.resumo(t), [(1 * MIB, 12 * MIB, 'fat32'), (14 * MIB, 12 * MIB, 'ntfs')])
        self.assertEqual([p['nome'] for p in t['particoes']], ['Dados', 'Windows'])
        self.assertEqual(t['particoes'][0]['tipo_nome'], 'Dados básicos (Windows)')

    def test_cabecalho_principal_corrompido_usa_a_reserva(self):
        original = self.resumo(particoes.analisar(self.abrir(self.disco())))
        for nome, estraga in (('byte do cabeçalho', lambda i: i.__setitem__(512 + 40, i[512 + 40] ^ 0xFF)),
                              ('assinatura apagada', lambda i: i.__setitem__(slice(512, 520), bytes(8))),
                              ('entradas', lambda i: i.__setitem__(1024 + 5, i[1024 + 5] ^ 1)),
                              ('LBA 1 inteiro', lambda i: i.__setitem__(slice(512, 1024), bytes(512)))):
            with self.subTest(nome):
                img = self.disco()
                estraga(img)
                t = particoes.analisar(self.abrir(img, 'g.img'))
                self.assertEqual(t['estado'], 'gpt_reserva_usada')
                self.assertEqual(self.resumo(t), original)
                self.assertTrue(any('cópia reserva' in a for a in t['avisos']))

    def test_reserva_danificada_com_principal_bom_continua_ok_com_aviso(self):
        img = self.disco()
        img[-512 + 16] ^= 0xFF
        t = particoes.analisar(self.abrir(img))
        self.assertEqual(t['estado'], 'ok')
        self.assertEqual(len(t['particoes']), 2)
        self.assertTrue(any('reserva' in a and 'danificada' in a for a in t['avisos']))

    def test_gpt_sem_reserva(self):
        img = self.disco()
        img[-512:] = bytes(512)
        t = particoes.analisar(self.abrir(img))
        self.assertEqual((t['estado'], len(t['particoes'])), ('ok', 2))
        self.assertTrue(any('não tem cópia reserva' in a for a in t['avisos']))

    def test_principal_e_reserva_ruins_e_tabela_danificada(self):
        img = self.disco()
        img[512 + 40] ^= 0xFF
        img[-512 + 40] ^= 0xFF
        t = particoes.analisar(self.abrir(img))
        self.assertEqual((t['esquema'], t['estado'], t['particoes']), ('gpt', 'tabela_danificada', []))

    def test_mbr_protetor_sem_gpt_e_danificada(self):
        img = bytearray(8 * MIB)
        ip.mbr(img, [(0, 0xEE, 1, 16383)])
        t = particoes.analisar(self.abrir(img))
        self.assertEqual((t['esquema'], t['estado']), ('gpt', 'tabela_danificada'))

    def test_setor_de_4096_numa_imagem_de_arquivo(self):
        """O leitor de arquivo diz setor 512, mas o GPT está em setores de 4096: a assinatura decide."""
        img = self.disco(4096)
        l = self.abrir(img)
        self.assertEqual(l.setor, 512)
        t = particoes.analisar(l)
        self.assertEqual((t['esquema'], t['estado'], t['setor']), ('gpt', 'ok', 4096))
        self.assertEqual(self.resumo(t), [(1 * MIB, 12 * MIB, 'fat32'), (14 * MIB, 12 * MIB, 'ntfs')])


class TestBsdEApm(Base):
    def rotulo(self, img, base, partes, c_ini):
        b = bytearray(512)
        struct.pack_into('<I', b, 0, particoes.BSD_MAGIA)
        struct.pack_into('<I', b, 132, particoes.BSD_MAGIA)
        struct.pack_into('<IH', b, 40, 512, 0)
        struct.pack_into('<H', b, 138, 3)
        todas = [(partes[0][0], partes[0][1], 0, partes[0][2]), (partes[1][0], partes[1][1], 0, partes[1][2]),
                 (20480, c_ini, 0, 0)]
        for i, (tam, ini, _, fs) in enumerate(todas):
            struct.pack_into('<IIIB', b, 148 + 16 * i, tam, ini, 0, fs)
        struct.pack_into('<IIIB', b, 148 + 32, 20480, c_ini, 0, 8)            # 'c' = a fatia toda
        ip.poe(img, base + 512, b)

    def test_disklabel_em_fatia_mbr_relativo_e_absoluto(self):
        for c_ini, a_ini, b_ini, nome in ((0, 16, 4112, 'relativo'), (2048, 2048 + 16, 2048 + 4112, 'absoluto')):
            with self.subTest(nome):
                img = bytearray(16 * MIB)
                ip.mbr(img, [(0, 0xA5, 2048, 20480)])
                self.rotulo(img, 2048 * 512, [(4096, a_ini, 7), (2048, b_ini, 1)], c_ini)
                t = particoes.analisar(self.abrir(img))
                subs = t['particoes'][0]['subparticoes']
                self.assertEqual([(s['letra'], s['inicio'], s['tamanho'], s['tipo_nome']) for s in subs],
                                 [('a', (2048 + 16) * 512, 4096 * 512, 'UFS (4.2BSD)'), ('b', (2048 + 4112) * 512, 2048 * 512, 'swap'),
                                  ('c', 2048 * 512, 20480 * 512, 'MSDOS')])
                self.assertTrue(all(s['somente_listar'] for s in subs))

    def test_apple_partition_map(self):
        img = bytearray(4 * MIB)
        img[0:2] = b'ER'
        struct.pack_into('>H', img, 2, 512)

        def pm(bloco, n, ini, cnt, nome, tipo):
            e = bytearray(512)
            e[0:2] = b'PM'
            struct.pack_into('>III', e, 4, n, ini, cnt)
            e[16:16 + len(nome)] = nome.encode()
            e[48:48 + len(tipo)] = tipo.encode()
            ip.poe(img, bloco * 512, e)
        pm(1, 3, 1, 63, 'Apple', 'Apple_partition_map')
        pm(2, 3, 64, 4096, 'Macintosh HD', 'Apple_HFS')
        pm(3, 3, 4160, 100, 'livre', 'Apple_Free')
        t = particoes.analisar(self.abrir(img))
        self.assertEqual((t['esquema'], t['estado']), ('apm', 'ok'))
        self.assertEqual([(p['inicio'], p['tamanho'], p['tipo'], p['nome']) for p in t['particoes']],
                         [(64 * 512, 4096 * 512, 'Apple_HFS', 'Macintosh HD')])


class TestVolume(Base):
    def test_volume_mapeia_corta_e_nao_fecha_a_base(self):
        dados = os.urandom(20000)
        l = leitor.abrir(self.gravar(dados))
        with particoes.abrir_volume(l, 1000, 5000) as v:
            self.assertEqual((v.tamanho, v.setor, v.truncado), (5000, l.setor, False))
            self.assertEqual(v.ler(0, 10), dados[1000:1010])
            self.assertEqual(v.ler(4995, 100), dados[5995:6000])         # não passa do fim da partição
            self.assertEqual(v.ler(5000, 10), b'')
            with self.assertRaises(ValueError):
                v.ler(-1, 1)
        self.assertEqual(l.ler(0, 4), dados[:4])                          # a base continua aberta
        t = particoes.abrir_volume(l, {'inicio': 19000, 'tamanho': 5000})
        self.assertTrue(t.truncado)
        self.assertEqual((t.tamanho, t.ler(0, 2000)), (1000, dados[19000:]))
        with self.assertRaises(ValueError):
            particoes.abrir_volume(l, 20001)
        l.fechar()

    def test_volume_de_uma_particao_da_tabela_entrega_o_sistema_de_arquivos(self):
        img, _ = ip.disco_mbr_completo()
        l = self.abrir(img)
        p = particoes.analisar(l)['particoes'][0]
        with particoes.abrir_volume(l, p) as v:
            self.assertEqual(particoes.identificar(v, 0)['sistema'], 'fat32')


class TestBusca(Base):
    def conferir_tres(self, r, esperado, minimo=0.5):
        prop = r['proposta']
        self.assertEqual([(c['inicio'], c['tamanho'], c['sistema']) for c in prop], esperado)
        self.assertTrue(all(c['confianca'] >= minimo for c in prop), prop)

    def test_tabela_zerada_reencontra_fat32_ntfs_e_ext(self):
        img, esperado = ip.disco_tabela_zerada()
        l = self.abrir(img)
        self.assertEqual(particoes.analisar(l)['esquema'], 'nenhum')
        r = particoes.buscar(l)
        self.conferir_tres(r, esperado, 0.8)
        self.assertEqual(len(r['candidatos']), 3)                          # as cópias reservas não viram candidatos
        self.assertEqual([c['nivel'] for c in r['proposta']], ['alta'] * 3)
        self.assertTrue(any('cópia reserva' in e for e in r['proposta'][0]['evidencias']))

    def test_janelas_pequenas_dao_o_mesmo_resultado(self):
        img, esperado = ip.disco_tabela_zerada()
        l = self.abrir(img)
        grande = particoes.buscar(l)
        for bloco in (64 * 1024, 100 * 4096 + 4096):
            self.assertEqual(particoes.buscar(l, bloco=bloco), grande)

    def test_estrutura_cortada_na_borda_da_janela(self):
        """O sinal fica nos últimos bytes de uma janela; o resto da estrutura vem na janela seguinte."""
        img = bytearray(4 * MIB)
        ip.ntfs(img, 65536 - 512, 1 * MIB)                                 # boot sector termina exatamente na borda
        ip.fat32(img, 2 * MIB - 512 * 6 - 0, 1 * MIB)                      # backup do boot cai na borda de 64 KiB
        l = self.abrir(img)
        a = particoes.buscar(l, bloco=65536)
        b = particoes.buscar(l, bloco=4 * MIB)
        self.assertEqual(a, b)
        self.assertEqual({c['sistema'] for c in a['candidatos']}, {'ntfs', 'fat32'})

    def test_so_a_copia_reserva_basta_para_achar_o_inicio(self):
        img, esperado = ip.disco_tabela_zerada()
        img[1 * MIB: 1 * MIB + 512] = bytes(512)                           # FAT32: some o setor 0 (fica o do setor 6)
        img[14 * MIB: 14 * MIB + 512] = bytes(512)                         # NTFS: some o boot (fica o do fim e a MFT)
        img[27 * MIB + 1024: 27 * MIB + 2048] = bytes(1024)                # ext: some o superbloco principal (ficam as cópias)
        r = particoes.buscar(self.abrir(img))
        self.assertEqual([(c['inicio'], c['tamanho'], c['sistema']) for c in r['proposta']], esperado)
        for c in r['proposta']:
            self.assertEqual(c['nivel'], 'media', c)                       # confiança menor que a do volume inteiro
            self.assertTrue(any('só a cópia reserva' in e for e in c['evidencias']), c)
            self.assertLess(c['confianca'], 0.8)

    def test_exfat_hfs_iso_e_apfs(self):
        img = bytearray(64 * MIB)
        ip.exfat(img, 1 * MIB, 8 * MIB)
        ip.hfs(img, 10 * MIB, 8 * MIB)
        ip.iso(img, 20 * MIB, 8 * MIB)
        ip.apfs(img, 30 * MIB, 8 * MIB)
        r = particoes.buscar(self.abrir(img))
        self.conferir_tres(r, [(1 * MIB, 8 * MIB, 'exfat'), (10 * MIB, 8 * MIB, 'hfs+'), (20 * MIB, 8 * MIB, 'iso9660'),
                               (30 * MIB, 8 * MIB, 'apfs')])
        por = {c['sistema']: c for c in r['proposta']}
        self.assertEqual(por['exfat']['nivel'], 'alta')
        self.assertEqual(por['hfs+']['nivel'], 'alta')                     # cabeçalho principal + o do fim
        self.assertEqual(por['apfs']['nivel'], 'media')
        self.assertTrue(any('estrutura interna' in e for e in por['apfs']['evidencias']))      # checksum Fletcher-64

    def test_apfs_com_checksum_errado_perde_confianca(self):
        img = bytearray(16 * MIB)
        ip.apfs(img, 2 * MIB, 8 * MIB)
        img[2 * MIB + 100] ^= 0xFF
        r = particoes.buscar(self.abrir(img))
        self.assertEqual([c['nivel'] for c in r['candidatos']], ['baixa'])
        self.assertEqual(r['proposta'], [])

    def test_exfat_so_com_a_copia(self):
        img = bytearray(16 * MIB)
        ip.exfat(img, 2 * MIB, 8 * MIB)
        img[2 * MIB: 2 * MIB + 512] = bytes(512)
        r = particoes.buscar(self.abrir(img))
        self.assertEqual([(c['inicio'], c['sistema']) for c in r['proposta']], [(2 * MIB, 'exfat')])

    def test_ruido_e_texto_nao_geram_proposta(self):
        img = bytearray(os.urandom(6 * MIB))
        img[1000:1100] = b'NTFS    ' * 12                                   # texto que lembra as assinaturas
        img[MIB: MIB + 8] = b'\x53\xef' * 4
        r = particoes.buscar(self.abrir(img))
        self.assertEqual(r['proposta'], [])

    def test_origem_nao_muda_na_analise_nem_na_busca(self):
        img, _ = ip.disco_tabela_zerada()
        caminho = self.gravar(img)
        antes = (sha256_de(caminho), os.stat(caminho).st_mtime_ns)
        with Espiao(caminho) as espiao, leitor.abrir(caminho) as l:
            particoes.analisar(l)
            r = particoes.buscar(l)
            particoes.montar_mbr(r['proposta'], l.tamanho)
            particoes.montar_gpt(r['proposta'], l.tamanho)
        self.assertTrue(espiao.aberturas)
        self.assertEqual(espiao.escritas(), [])
        self.assertEqual((sha256_de(caminho), os.stat(caminho).st_mtime_ns), antes)


class TestTabelaReconstruida(Base):
    def proposta(self):
        img, esperado = ip.disco_tabela_zerada()
        return img, esperado, particoes.buscar(self.abrir(img))['proposta']

    def test_mbr_montado_e_lido_de_volta(self):
        img, esperado, prop = self.proposta()
        mbr, av = particoes.montar_mbr(prop, len(img))
        self.assertEqual((len(mbr), mbr[510:512], av), (512, b'\x55\xaa', []))
        self.assertEqual([mbr[446 + 16 * k + 4] for k in range(3)], [0x0C, 0x07, 0x83])
        img[0:512] = mbr
        t = particoes.analisar(self.abrir(img, 'com_mbr.img'))
        self.assertEqual((t['esquema'], t['estado']), ('mbr', 'ok'))
        self.assertEqual(self.resumo(t), esperado)

    def test_gpt_montado_vai_no_inicio_e_no_fim_e_a_reserva_salva(self):
        img, esperado, prop = self.proposta()
        ini, fim, av = particoes.montar_gpt(prop, len(img))
        n = 32
        self.assertEqual((len(ini), len(fim), av), ((2 + n) * 512, (n + 1) * 512, []))
        img[0: len(ini)] = ini
        img[len(img) - len(fim):] = fim
        t = particoes.analisar(self.abrir(img, 'com_gpt.img'))
        self.assertEqual((t['esquema'], t['estado'], t['avisos']), ('gpt', 'ok', []))
        self.assertEqual(self.resumo(t), esperado)
        img[512 + 30] ^= 0xFF                                              # estraga o principal: a reserva montada serve
        t = particoes.analisar(self.abrir(img, 'com_gpt2.img'))
        self.assertEqual(t['estado'], 'gpt_reserva_usada')
        self.assertEqual(self.resumo(t), esperado)

    def test_mbr_com_mais_de_quatro_e_tipos_desconhecidos_avisa(self):
        prop = [{'inicio': (i + 1) * MIB, 'tamanho': MIB, 'fim': (i + 2) * MIB, 'sistema': 'fat32'} for i in range(5)]
        prop.append({'inicio': 8 * MIB, 'tamanho': MIB, 'fim': 9 * MIB, 'sistema': 'iso9660'})
        mbr, av = particoes.montar_mbr(prop, 16 * MIB)
        self.assertEqual(sum(1 for k in range(4) if mbr[446 + 16 * k + 4]), 4)
        self.assertTrue(any('4 entradas' in a for a in av))
        self.assertTrue(any('tipo de MBR' in a for a in av))
        self.assertEqual(particoes.montar_mbr([], 16 * MIB)[0], None)


class TestJobERota(CasoComPasta):
    def setUp(self):
        super().setUp()
        self.porta = porta_livre()
        p = mock.patch.dict(os.environ, {'RECDADOS_DADOS': self.dados, 'RECDADOS_PORTA': str(self.porta)})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop('RECDADOS_ATRASO_BLOCO', None)
        img, self.esperado = ip.disco_tabela_zerada()
        self.origem = os.path.join(self.trabalho, 'disco.img')
        with open(self.origem, 'wb') as f:
            f.write(img)
        self.saida = os.path.join(self.pasta, 'saida')
        os.makedirs(self.saida)
        self.destino = os.path.join(self.saida, 'tabela.json')
        fonte = {'id': 'img0', 'caminho': self.origem, 'nome': 'Imagem de teste', 'tamanho': len(img), 'tipo': 'arquivo'}
        self.ctx = servico.Contexto(provedor_fontes=lambda: [fonte])
        self.app = servico.criar_app(self.ctx)
        self.ctx.iniciar()
        self.addCleanup(self.ctx.store.fechar)
        self.addCleanup(self.ctx.executor.encerrar, 5.0)
        self.c = self.app.test_client()
        self.h = {'X-RecDados-Token': self.ctx.token}
        self.base = 'http://127.0.0.1:%d' % self.porta

    def req(self, metodo, caminho, corpo=None):
        return self.c.open(caminho, method=metodo, json=corpo, headers=self.h, base_url=self.base)

    def criar(self, destino=None, **opcoes):
        r = self.req('POST', '/api/jobs', {'tipo': 'particoes', 'origem': self.origem, 'destino': destino or self.destino,
                                           'opcoes': opcoes})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()['id']

    def esperar(self, job_id, *estados):
        return esperar(lambda: (j := self.ctx.store.obter(job_id))['estado'] in estados and j, 30, 0.02, 'estado')

    def ler_json(self, nome):
        with open(os.path.join(self.saida, nome), encoding='utf-8') as f:
            return json.load(f)

    def foto(self):
        return sha256_de(self.origem), os.stat(self.origem).st_mtime_ns

    def test_rota_da_fonte_lista_a_tabela(self):
        r = self.req('GET', '/api/fontes/img0/particoes')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual((d['fonte'], d['esquema'], d['estado'], d['particoes']), ('img0', 'nenhum', 'sem_tabela', []))
        img, esperado = ip.disco_mbr_completo()
        with open(self.origem, 'wb') as f:
            f.write(img)
        d = self.req('GET', '/api/fontes/img0/particoes').get_json()
        self.assertEqual([(p['inicio'], p['tamanho'], p['sistema']) for p in d['particoes']], esperado)
        self.assertEqual(self.req('GET', '/api/fontes/nao-existe/particoes').status_code, 404)
        self.assertEqual(self.c.get('/api/fontes/img0/particoes', base_url=self.base).status_code, 401)     # sem token

    def test_job_gera_json_mbr_gpt_e_a_origem_fica_igual(self):
        antes = self.foto()
        with Espiao(self.origem) as espiao:
            job_id = self.criar()
            self.assertEqual(self.esperar(job_id, 'concluido', 'falhou')['estado'], 'concluido')
        self.assertTrue(espiao.aberturas)
        self.assertEqual(espiao.escritas(), [])
        self.assertEqual(self.foto(), antes)
        self.assertEqual(sorted(os.listdir(self.saida)), ['tabela.gpt', 'tabela.gpt-fim', 'tabela.json', 'tabela.mbr'])
        with open(self.destino, encoding='utf-8') as f:
            d = json.load(f)
        self.assertEqual([(c['inicio'], c['tamanho'], c['sistema']) for c in d['proposta']], self.esperado)
        self.assertEqual(d['tabela_atual']['esquema'], 'nenhum')
        self.assertTrue(all(c['na_tabela_atual'] is False for c in d['candidatos']))
        self.assertIn('nunca grava', d['aviso'])
        ap = d['aplicacao']
        self.assertEqual((ap['mbr']['arquivo'], ap['mbr']['gravar_em_byte']), ('tabela.mbr', 0))
        self.assertEqual(ap['gpt_fim']['gravar_em_byte'], (48 * MIB // 512 - 33) * 512)
        with open(os.path.join(self.saida, 'tabela.mbr'), 'rb') as f:
            self.assertEqual(f.read()[510:512], b'\x55\xaa')
        self.assertEqual(os.path.getsize(os.path.join(self.saida, 'tabela.gpt')), 34 * 512)
        self.assertEqual(os.path.getsize(os.path.join(self.saida, 'tabela.gpt-fim')), 33 * 512)
        self.assertFalse(os.path.exists(self.destino + '.busca.json'))
        r = self.req('GET', '/api/jobs/%s/particoes' % job_id)
        self.assertEqual((r.status_code, r.get_json()['proposta']), (200, d['proposta']))
        self.assertEqual(self.ctx.store.obter(job_id)['progresso'], 1.0)

    def test_pausar_e_retomar_da_o_mesmo_resultado(self):
        sem_pausa = self.criar(os.path.join(self.saida, 'a.json'), bloco=1 * MIB)
        self.esperar(sem_pausa, 'concluido')
        with mock.patch('recuperacao.config.atraso_bloco', return_value=0.05):
            job_id = self.criar(os.path.join(self.saida, 'b.json'), bloco=1 * MIB)
            self.esperar(job_id, 'rodando')
            time.sleep(0.4)
            self.assertEqual(self.req('POST', '/api/jobs/%s/pausar' % job_id).status_code, 200)
            self.assertTrue(os.path.exists(os.path.join(self.saida, 'b.json.busca.json')))
            self.assertEqual(self.req('GET', '/api/jobs/%s/particoes' % job_id).status_code, 404)    # ainda sem resultado
            pos = self.ler_json('b.json.busca.json')['pos']
            self.assertTrue(0 < pos < 48 * MIB, pos)
            self.assertEqual(self.req('POST', '/api/jobs/%s/retomar' % job_id).status_code, 200)
            self.assertEqual(self.esperar(job_id, 'concluido', 'falhou')['estado'], 'concluido')
        a, b = self.ler_json('a.json'), self.ler_json('b.json')
        self.assertEqual(a['proposta'], b['proposta'])
        self.assertEqual(a['candidatos'], b['candidatos'])

    def test_validacao_recusa_destino_ruim(self):
        for destino, trecho in ((os.path.join(self.saida, 'sem_extensao'), '.json'),
                                (os.path.join(self.saida, 'x', 'y.json'), 'pasta do destino'),
                                (self.origem, '.json')):
            r = self.req('POST', '/api/jobs', {'tipo': 'particoes', 'origem': self.origem, 'destino': destino})
            self.assertEqual(r.status_code, 400, destino)
            self.assertIn(trecho, r.get_json()['erro'])
        como_json = os.path.join(self.trabalho, 'origem.json')          # origem que seria sobrescrita pelo próprio resultado
        os.replace(self.origem, como_json)
        r = self.req('POST', '/api/jobs', {'tipo': 'particoes', 'origem': como_json, 'destino': como_json})
        self.assertEqual(r.status_code, 400)
        self.assertIn('própria origem', r.get_json()['erro'])
        os.replace(como_json, self.origem)
        r = self.req('POST', '/api/jobs', {'tipo': 'particoes', 'origem': os.path.join(self.trabalho, 'nao.img'), 'destino': self.destino})
        self.assertEqual(r.status_code, 400)
        r = self.req('POST', '/api/jobs', {'tipo': 'particoes', 'origem': self.origem, 'destino': self.destino, 'opcoes': {'bloco': 5}})
        self.assertEqual(r.status_code, 400)
        job_id = self.criar()
        self.esperar(job_id, 'concluido')
        r = self.req('POST', '/api/jobs', {'tipo': 'particoes', 'origem': self.origem, 'destino': self.destino})
        self.assertEqual(r.status_code, 400)                              # não sobrescreve resultado existente
        self.assertIn('Já existe', r.get_json()['erro'])

    def test_disco_na_mesma_unidade_do_destino_e_recusado(self):
        class Mapa:
            def __init__(self, o, d):
                self.o, self.d = o, d

            def discos_da_origem(self, _):
                return self.o

            def discos_do_destino(self, _):
                return self.d
        with mock.patch.object(unidade, 'MAPA', Mapa({'7'}, {'7'})):
            erro = tipo_particoes.validar('/dev/sdz', self.destino, {})
        self.assertEqual(erro, unidade.MSG_MESMO_DISCO)
        with mock.patch.object(unidade, 'MAPA', Mapa({'7'}, {'8'})):
            self.assertIsNone(tipo_particoes.validar('/dev/sdz', self.destino, {}))

    def test_retomada_com_origem_trocada_falha_sem_tocar_em_nada(self):
        busca = self.destino + '.busca.json'
        with open(busca, 'w', encoding='utf-8') as f:
            json.dump({'origem': 'outra', 'tamanho': 1, 'pos': 0, 'hits': [], 'ruins': 0}, f)
        job = self.ctx.store.criar('particoes', self.origem, self.destino, {})
        self.ctx.executor.submeter(job['id'])
        j = self.esperar(job['id'], 'falhou', 'concluido')
        self.assertEqual(j['estado'], 'falhou')
        self.assertIn('ponto de retomada', j['erro'])
        self.assertFalse(os.path.exists(self.destino))

    def test_job_e_erro_para_job_de_outro_tipo(self):
        self.assertEqual(self.req('GET', '/api/jobs/j-20200101-abcd/particoes').status_code, 404)
        self.assertEqual(self.req('GET', '/api/jobs/zzz/particoes').status_code, 404)


if __name__ == '__main__':
    unittest.main()
