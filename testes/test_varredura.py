# -*- coding: utf-8 -*-
"""Fase 5.1 e 5.2: o job `varredura` (rápida e profunda, checkpoint e retomada), a tabela de achados e a prévia.

Imagem sintética de 8 MiB com arquivos "apagados" (entrada de diretório 0xE5), um JPG cortado e lixo; nenhum disco real.
"""
import os
import sqlite3
import time
import unittest
from unittest import mock

import amostras as A
import fat_sintetica
from auxiliar import CasoComPasta, Servico, esperar, sha256_de

from recuperacao import achados as mod_achados
from recuperacao.jobs import JobStore
from recuperacao.tipos import varredura
from test_servico import Base

CORTE = 3000


def arquivos_da_imagem():
    jpg = A.jpeg(scan=5000, data='2021:05:17 10:20:30', largura=640, altura=480)
    return [('FOTO.JPG', jpg), ('IMAGEM.PNG', A.png(32, 32)), ('RELAT.PDF', A.pdf()), ('CARTA.DOCX', A.docx()),
            ('PACOTE.ZIP', A.zip_simples()), ('VIDEO.MP4', A.mp4(mdat=6000)),
            ('CORTADO.JPG', A.jpeg(scan=9000, semente=7)[:CORTE])]


def montar_imagem(pasta, escondida=False):
    """Grava a imagem em `pasta`. Devolve (caminho, info, offset_do_jpg_escondido)."""
    arquivos = arquivos_da_imagem()
    img, info = fat_sintetica.montar(arquivos, apagados={n for n, _ in arquivos}, lixo=[0, 1, 0, 2, 0, 1, 0],
                                     vazio=[0, 200, 250, 200, 250, 200, 250])
    escondido = None
    if escondida:
        escondido = 7 * 1024 * 1024 + 101                       # no meio de uma área vazia, fora do setor
        jpg = A.jpeg(semente=21)
        img = img[:escondido] + jpg + img[escondido + len(jpg):]
    caminho = os.path.join(pasta, 'pendrive.img')
    with open(caminho, 'wb') as f:
        f.write(img)
    return caminho, info, escondido


class BaseVarredura(Base):
    def achados_do_banco(self, job_id):
        con = self.ctx.store._con()
        return [dict(l) for l in con.execute('SELECT * FROM achados WHERE job_id=? ORDER BY offset', (job_id,))]

    def varrer(self, origem, modo='rapida', **opcoes):
        r = self.req('POST', '/api/jobs', {'tipo': 'varredura', 'origem': origem, 'opcoes': dict(opcoes, modo=modo)})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()

    def esperar_estado(self, job_id, estado, prazo=60):
        return esperar(lambda: (j := self.req('GET', '/api/jobs/' + job_id).get_json())['estado'] == estado and j,
                       prazo, 0.05, 'job não chegou a %s' % estado)


class TestVarreduraNaImagem(BaseVarredura):
    def conferir_achados(self, lista, info, tamanho_cortado=True):
        por_offset = {a['offset']: a for a in lista}
        for f in info:
            a = por_offset.get(f['offset'])
            self.assertIsNotNone(a, 'não achou %s em %d' % (f['nome'], f['offset']))
            if f['nome'] == 'CORTADO.JPG':
                self.assertEqual(a['estado'], 'parcial')
                self.assertGreaterEqual(a['tamanho'], len(f['dados']))
                self.assertLess(a['tamanho'], len(f['dados']) + 16)
                continue
            self.assertEqual(a['estado'], 'bom', f['nome'])
            self.assertEqual(a['tamanho'], len(f['dados']), f['nome'])

    def test_rapida_acha_todos_com_tamanho_certo_e_bytes_que_batem(self):
        caminho, info, _ = montar_imagem(self.trabalho)
        antes = (sha256_de(caminho), os.stat(caminho).st_mtime_ns)
        job = self.varrer(caminho, 'rapida', bloco=65536)
        self.assertTrue(job['destino'].startswith('varredura#'))
        fim = self.esperar_estado(job['id'], 'concluido')
        self.assertEqual(fim['progresso'], 1.0)
        lista = self.achados_do_banco(job['id'])
        self.assertEqual(len(lista), 7, [(a['nome'], a['tipo']) for a in lista])
        self.conferir_achados(lista, info)
        with open(caminho, 'rb') as f:
            img = f.read()
        por_offset = {a['offset']: a for a in lista}
        for f_ in info:
            if f_['nome'] != 'CORTADO.JPG':
                a = por_offset[f_['offset']]
                self.assertEqual(A.sha256(img[a['offset']:a['offset'] + a['tamanho']]), A.sha256(f_['dados']), f_['nome'])
        self.assertEqual((sha256_de(caminho), os.stat(caminho).st_mtime_ns), antes)        # a origem não mudou
        eventos = ' '.join(e['texto'] for e in self.req('GET', '/api/jobs/' + job['id']).get_json()['eventos'])
        self.assertIn('7 arquivos', eventos)

    def test_campos_do_achado_seguem_o_contrato(self):
        caminho, info, _ = montar_imagem(self.trabalho)
        job = self.varrer(caminho)
        self.esperar_estado(job['id'], 'concluido')
        foto = next(a for a in self.achados_do_banco(job['id']) if a['offset'] == info[0]['offset'])
        self.assertEqual((foto['tipo'], foto['grupo'], foto['extensao'], foto['estado'], foto['data'],
                          foto['largura'], foto['altura']), ('JPG', 'foto', 'jpg', 'bom', '2021-05-17 10:20:30', 640, 480))
        self.assertEqual(foto['nome'], 'f%07d.jpg' % (foto['offset'] // 512))
        self.assertIsNone(foto['caminho'])
        self.assertIsNone(foto['apagado'])
        self.assertIsNone(foto['recuperado_em'])
        self.assertEqual(foto['pontuacao'], 100)
        self.assertIsNone(foto['extents'])
        grupos = {a['tipo']: a['grupo'] for a in self.achados_do_banco(job['id'])}
        self.assertEqual(grupos['MP4'], 'video')
        self.assertEqual(grupos['DOCX'], 'documento')
        self.assertEqual(grupos['ZIP'], 'compactado')

    def test_profunda_acha_o_que_nao_esta_no_inicio_do_setor_e_a_rapida_nao(self):
        caminho, info, escondido = montar_imagem(self.trabalho, escondida=True)
        rapida = self.varrer(caminho, 'rapida', bloco=65536)
        self.esperar_estado(rapida['id'], 'concluido')
        self.assertNotIn(escondido, [a['offset'] for a in self.achados_do_banco(rapida['id'])])
        profunda = self.varrer(caminho, 'profunda', bloco=65536)
        self.esperar_estado(profunda['id'], 'concluido')
        lista = self.achados_do_banco(profunda['id'])
        self.assertEqual(len(lista), 8)
        self.conferir_achados(lista, info)
        extra = next(a for a in lista if a['offset'] == escondido)
        self.assertEqual((extra['tipo'], extra['estado']), ('JPG', 'bom'))

    def test_os_achados_aparecem_enquanto_a_varredura_anda(self):
        caminho, info, _ = montar_imagem(self.trabalho)
        with mock.patch.dict(os.environ, {'RECDADOS_ATRASO_BLOCO': '0.03'}):
            job = self.varrer(caminho, bloco=65536)
            esperar(lambda: 0 < len(self.achados_do_banco(job['id'])) < 7, 20, 0.02, 'achados não apareceram aos poucos')
            j = self.req('GET', '/api/jobs/' + job['id']).get_json()
            self.assertEqual(j['estado'], 'rodando')
            self.assertIn('achados', j['mensagem'])
            self.esperar_estado(job['id'], 'concluido')

    def test_pausar_e_retomar_nao_perde_nem_repete_achado(self):
        caminho, info, _ = montar_imagem(self.trabalho)
        with mock.patch.dict(os.environ, {'RECDADOS_ATRASO_BLOCO': '0.03'}):
            job = self.varrer(caminho, bloco=65536)
            esperar(lambda: self.req('GET', '/api/jobs/' + job['id']).get_json()['progresso'] > 0.3, 30, 0.02)
            r = self.req('POST', '/api/jobs/%s/pausar' % job['id'])
            self.assertEqual(r.status_code, 200, r.get_json())
            parado = self.req('GET', '/api/jobs/' + job['id']).get_json()
            self.assertEqual(parado['estado'], 'pausado')
            cp = self.ctx.store.obter_checkpoint(job['id'])
            self.assertGreater(cp['pos'], 0)
            self.assertLess(cp['pos'], os.path.getsize(caminho))
            # tudo o que está antes do checkpoint já está gravado, e nada além dele
            antes = self.achados_do_banco(job['id'])
            self.assertTrue(all(a['offset'] < cp['pos'] for a in antes))
            self.assertLess(len(antes), 7)
            r = self.req('POST', '/api/jobs/%s/retomar' % job['id'])
            self.assertEqual(r.status_code, 200, r.get_json())
            self.esperar_estado(job['id'], 'concluido')
        lista = self.achados_do_banco(job['id'])
        self.assertEqual(len(lista), 7)
        self.assertEqual(len({a['offset'] for a in lista}), 7)
        self.conferir_achados(lista, info)

    def test_cancelar_guarda_o_que_ja_foi_achado(self):
        caminho, _, _ = montar_imagem(self.trabalho)
        with mock.patch.dict(os.environ, {'RECDADOS_ATRASO_BLOCO': '0.03'}):
            job = self.varrer(caminho, bloco=65536)
            esperar(lambda: len(self.achados_do_banco(job['id'])) >= 2, 30, 0.02)
            self.assertEqual(self.req('POST', '/api/jobs/%s/cancelar' % job['id']).status_code, 200)
        self.assertEqual(self.req('GET', '/api/jobs/' + job['id']).get_json()['estado'], 'cancelado')
        self.assertGreaterEqual(len(self.achados_do_banco(job['id'])), 2)

    def test_retomada_recusa_origem_que_mudou(self):
        caminho, _, _ = montar_imagem(self.trabalho)
        job = self.varrer(caminho)
        self.esperar_estado(job['id'], 'concluido')
        self.ctx.store.salvar_checkpoint(job['id'], {'pos': 4096, 'tamanho': 123, 'modo': 'rapida', 'ruins': 0})
        ctrl = mock.Mock(store=self.ctx.store)
        with self.assertRaises(Exception) as e:
            varredura.executar(dict(self.ctx.store.obter(job['id'])), ctrl)
        self.assertIn('mudaram', str(e.exception))

    def test_duas_varreduras_da_mesma_origem_ao_mesmo_tempo_dao_409(self):
        caminho, _, _ = montar_imagem(self.trabalho)
        with mock.patch.dict(os.environ, {'RECDADOS_ATRASO_BLOCO': '0.03'}):
            job = self.varrer(caminho, bloco=65536)
            r = self.req('POST', '/api/jobs', {'tipo': 'varredura', 'origem': caminho, 'opcoes': {'bloco': 65536}})
            self.assertEqual(r.status_code, 409)
            self.req('POST', '/api/jobs/%s/cancelar' % job['id'])


class TestPedidoInvalido(BaseVarredura):
    def pedir(self, **kw):
        corpo = {'tipo': 'varredura', 'origem': kw.pop('origem', None), 'opcoes': kw}
        return self.req('POST', '/api/jobs', corpo)

    def test_origem_e_opcoes_invalidas_dao_400_com_frase(self):
        caminho, _, _ = montar_imagem(self.trabalho)
        casos = [self.pedir(), self.pedir(origem=''), self.pedir(origem=os.path.join(self.trabalho, 'nao-existe')),
                 self.pedir(origem=self.trabalho), self.pedir(origem=caminho, modo='lenta'),
                 self.pedir(origem=caminho, modo=3), self.pedir(origem=caminho, bloco=10),
                 self.pedir(origem=caminho, bloco='4096'), self.pedir(origem='\\\\servidor\\pasta\\x.img'),
                 self.pedir(origem='\\\\.\\CdRom0'), self.pedir(origem=caminho + '\x00')]
        for r in casos:
            self.assertEqual(r.status_code, 400, r.get_json())
            self.assertTrue(r.get_json()['erro'])
        self.assertEqual(self.req('GET', '/api/jobs').get_json(), [])

    def test_origem_de_disco_passa_na_validacao_sem_abrir_nada(self):
        with mock.patch('recuperacao.leitor.abrir', side_effect=AssertionError('abriu o disco')):
            self.assertIsNone(varredura.validar('\\\\.\\PhysicalDrive7', None, {'modo': 'profunda'}))
            self.assertIsNone(varredura.validar('/dev/sdb', None, None))
            self.assertIn('CD/DVD', varredura.validar('\\\\.\\CdRom0', None, None))


class TestTabelaDeAchados(CasoComPasta):
    def setUp(self):
        super().setUp()
        self.store = JobStore(os.path.join(self.dados, 'jobs.db'))
        self.addCleanup(self.store.fechar)
        self.job = self.store.criar('varredura', 'x.img', 'varredura#1')['id']

    def achado(self, **kw):
        base = {'tamanho': 100, 'offset': 512, 'tipo': 'JPG', 'grupo': 'foto', 'extensao': 'jpg'}
        base.update(kw)
        return base

    def linhas(self):
        return [dict(l) for l in self.store._con().execute('SELECT * FROM achados WHERE job_id=? ORDER BY id', (self.job,))]

    def test_adicionar_achados_grava_o_contrato_inteiro(self):
        ids = self.store.adicionar_achados(self.job, [
            self.achado(),
            {'caminho': 'C:\\Fotos\\ferias\\praia.jpg', 'apagado': True, 'grupo': 'foto', 'tipo': 'JPG', 'tamanho': 30,
             'extents': [[4096, 10], [9000, 20]], 'estado': 'parcial', 'pontuacao': 55, 'data': '2020-01-02',
             'largura': 10, 'altura': 20}])
        self.assertEqual(len(ids), 2)
        a, b = self.linhas()
        self.assertEqual((a['nome'], a['estado'], a['apagado'], a['caminho']), ('f0000001.jpg', 'bom', None, None))
        self.assertEqual((b['nome'], b['apagado'], b['offset'], b['extents'], b['extensao']),
                         ('praia.jpg', 1, 4096, '[[4096, 10], [9000, 20]]', 'jpg'))
        c = mod_achados.obter(self.store, ids[1])
        self.assertEqual((c['apagado'], c['extents']), (True, [[4096, 10], [9000, 20]]))
        self.assertEqual(mod_achados.extents_de(c), [(4096, 10), (9000, 20)])
        self.assertEqual(mod_achados.extents_de(mod_achados.obter(self.store, ids[0])), [(512, 100)])

    def test_achado_invalido_nao_grava_nada_do_lote(self):
        ruins = [{'offset': 1}, self.achado(tamanho=-1), self.achado(grupo='planeta'), self.achado(estado='otimo'),
                 self.achado(pontuacao=101), self.achado(data='ontem'), self.achado(offset=None),
                 self.achado(extents=[[0, 5]], tamanho=6), self.achado(extents=[]), self.achado(nome='a\x00b'),
                 self.achado(extensao='../x'), 'texto', self.achado(caminho=5)]
        for r in ruins:
            with self.assertRaises(ValueError, msg=str(r)):
                self.store.adicionar_achados(self.job, [self.achado(), r])
        self.assertEqual(self.linhas(), [])

    def test_achados_e_checkpoint_andam_na_mesma_transacao(self):
        self.store.adicionar_achados(self.job, [self.achado()], checkpoint={'pos': 4096})
        self.assertEqual(self.store.obter_checkpoint(self.job), {'pos': 4096})
        with mock.patch('recuperacao.jobs.json.dumps', side_effect=RuntimeError('caiu')):
            with self.assertRaises(RuntimeError):
                self.store.adicionar_achados(self.job, [self.achado(offset=1024)], checkpoint={'pos': 8192})
        self.assertEqual(len(self.linhas()), 1)                                # nada do segundo lote ficou
        self.assertEqual(self.store.obter_checkpoint(self.job), {'pos': 4096})

    def test_banco_da_fase_1_ganha_as_colunas_novas_sem_perder_dados(self):
        antigo = os.path.join(self.pasta, 'antigo.db')
        con = sqlite3.connect(antigo)
        con.executescript('''CREATE TABLE achados (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, caminho TEXT,
            tipo TEXT, tamanho INTEGER, data TEXT, estado TEXT, offset INTEGER);
            INSERT INTO achados (job_id, caminho, tipo, tamanho, offset) VALUES ('j-1', 'a.txt', 'TXT', 5, 0);''')
        con.commit()
        con.close()
        store = JobStore(antigo)
        self.addCleanup(store.fechar)
        colunas = {l['name'] for l in store._con().execute('PRAGMA table_info(achados)')}
        for c in ('nome', 'grupo', 'extensao', 'apagado', 'extents', 'pontuacao', 'largura', 'altura', 'recuperado_em'):
            self.assertIn(c, colunas)
        self.assertEqual(store._con().execute('SELECT caminho FROM achados').fetchone()[0], 'a.txt')
        store.adicionar_achados('j-1', [self.achado()])
        JobStore(antigo).fechar()                                                # abrir de novo não quebra

    def test_leitura_dos_bytes_do_achado_com_extents(self):
        dados = bytes(range(256)) * 4

        class L:
            def ler(self, off, n):
                return dados[off:off + n]
        a = {'tamanho': 30, 'offset': 10, 'extents': [[10, 10], [100, 20]]}
        self.assertEqual(b''.join(mod_achados.ler_achado(L(), a)), dados[10:20] + dados[100:120])
        self.assertEqual(b''.join(mod_achados.ler_achado(L(), a, maximo=15)), dados[10:20] + dados[100:105])


class TestPrevia(BaseVarredura):
    def montar(self, scan=2000):
        jpg = A.jpeg(scan=scan)
        png = A.png()
        pdf = A.pdf()
        img = bytearray(64 * 1024)
        img[512:512 + len(jpg)] = jpg
        img[8192:8192 + len(png)] = png
        img[16384:16384 + len(pdf)] = pdf
        caminho = os.path.join(self.trabalho, 'p.img')
        with open(caminho, 'wb') as f:
            f.write(img)
        job = self.varrer(caminho)
        self.esperar_estado(job['id'], 'concluido')
        ids = {a['tipo']: a['id'] for a in self.achados_do_banco(job['id'])}
        return caminho, job, ids, jpg, png

    def test_foto_vem_como_esta_com_o_tipo_pelo_conteudo(self):
        _, job, ids, jpg, png = self.montar()
        r = self.req('GET', '/api/achados/%d/previa' % ids['JPG'])
        self.assertEqual((r.status_code, r.mimetype, r.data), (200, 'image/jpeg', jpg))
        r = self.req('GET', '/api/achados/%d/previa' % ids['PNG'])
        self.assertEqual((r.mimetype, r.data), ('image/png', png))
        self.assertEqual(r.headers['X-Content-Type-Options'], 'nosniff')

    def test_o_que_nao_e_foto_devolve_icone_do_grupo(self):
        _, _, ids, _, _ = self.montar()
        r = self.req('GET', '/api/achados/%d/previa' % ids['PDF'])
        self.assertEqual((r.status_code, r.mimetype), (200, 'image/svg+xml'))
        self.assertIn(b'<svg', r.data)
        self.assertIn(b'PDF', r.data)

    def test_foto_maior_que_2_mb_devolve_icone(self):
        caminho = os.path.join(self.trabalho, 'g.img')
        jpg = A.jpeg(scan=2 * 1024 * 1024 + 100)
        with open(caminho, 'wb') as f:
            f.write(bytes(512) + jpg + bytes(4096))
        job = self.varrer(caminho)
        self.esperar_estado(job['id'], 'concluido')
        achado = self.achados_do_banco(job['id'])[0]
        self.assertGreater(achado['tamanho'], 2 * 1024 * 1024)
        r = self.req('GET', '/api/achados/%d/previa' % achado['id'])
        self.assertEqual(r.mimetype, 'image/svg+xml')

    def test_extensao_mentirosa_ou_bytes_que_nao_sao_imagem_viram_icone_e_texto_e_escapado(self):
        caminho, job, ids, _, _ = self.montar()
        store = self.ctx.store
        [novo] = store.adicionar_achados(job['id'], [{
            'tamanho': 64, 'offset': 40000, 'grupo': 'foto', 'tipo': '<img src=x onerror=alert(1)>',
            'extensao': 'jpg', 'nome': '<script>.jpg'}])
        r = self.req('GET', '/api/achados/%d/previa' % novo)
        self.assertEqual(r.mimetype, 'image/svg+xml')                     # 64 bytes de zeros não são JPG
        r = self.req('GET', '/api/achados/%d/previa' % novo)
        self.assertNotIn(b'<script', r.data)
        self.assertNotIn(b'onerror', r.data)

    def test_previa_exige_login_e_id_inexistente_da_404(self):
        _, _, ids, _, _ = self.montar()
        self.assertEqual(self.req('GET', '/api/achados/%d/previa' % ids['JPG'], headers={}).status_code, 401)
        self.assertEqual(self.req('GET', '/api/achados/999999/previa').status_code, 404)
        self.assertEqual(self.req('GET', '/api/achados/abc/previa').status_code, 404)


class TestQuedaDoServico(CasoComPasta):
    def test_matar_o_servico_no_meio_e_retomar_nao_repete_nem_perde_achado(self):
        caminho, info, _ = montar_imagem(self.trabalho)
        s = Servico(self.dados, atraso=0.05).iniciar()
        self.addCleanup(s.matar)
        st, job, _ = s.pedir('POST', '/api/jobs', {'tipo': 'varredura', 'origem': caminho,
                                                   'opcoes': {'modo': 'rapida', 'bloco': 65536}})
        self.assertEqual(st, 201, job)
        esperar(lambda: s.job(job['id'])['progresso'] > 0.4, 30, 0.05)
        s.matar()
        con = sqlite3.connect(os.path.join(self.dados, 'jobs.db'))
        feitos = con.execute('SELECT COUNT(*) FROM achados WHERE job_id=?', (job['id'],)).fetchone()[0]
        con.close()
        self.assertLess(feitos, 7)
        s2 = Servico(self.dados).iniciar()
        self.addCleanup(s2.matar)
        self.assertEqual(s2.job(job['id'])['estado'], 'interrompido')
        st, _, _ = s2.pedir('POST', '/api/jobs/%s/retomar' % job['id'])
        self.assertEqual(st, 200)
        s2.esperar_estado(job['id'], 'concluido')
        s2.matar()
        con = sqlite3.connect(os.path.join(self.dados, 'jobs.db'))
        ofs = [l[0] for l in con.execute('SELECT offset FROM achados WHERE job_id=? ORDER BY offset', (job['id'],))]
        con.close()
        self.assertEqual(ofs, sorted(f['offset'] for f in info))


if __name__ == '__main__':
    unittest.main()
