# -*- coding: utf-8 -*-
"""Parte 3: log por job (o que foi lido, pulado e falhou, sem conteúdo de arquivo) e relatório final da imagem."""
import csv
import errno
import os
import re
import unittest
from unittest import mock

from auxiliar import CasoComPasta, criar_origem, esperar
from test_ddrescue import Defeitos
from test_recuperar import fazer_job_com_achados
from test_varredura import BaseVarredura, montar_imagem

from recuperacao import logjob
from recuperacao.executor import Controle
from recuperacao.jobs import JobStore
from recuperacao.tipos import imagem, recuperar

S = 512


def ler_texto(caminho):
    with open(caminho, encoding='utf-8') as f:
        return f.read()


class TestLogJob(CasoComPasta):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {'RECDADOS_DADOS': self.dados})
        p.start()
        self.addCleanup(p.stop)

    def test_linhas_com_hora_em_logs_jobs_e_texto_sempre_numa_linha(self):
        log = logjob.LogJob('j-20250101-abcd')
        log.escrever('um fato\ncom quebra\tde linha')
        log.escrever('outro')
        log.fechar()
        caminho = os.path.join(self.dados, 'logs', 'jobs', 'j-20250101-abcd.log')
        linhas = ler_texto(caminho).splitlines()
        self.assertEqual(len(linhas), 2)
        self.assertRegex(linhas[0], r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d um fato com quebra de linha$')

    def test_id_estranho_nao_sai_da_pasta(self):
        self.assertEqual(os.path.dirname(logjob.caminho('../../x')), logjob.pasta())
        self.assertEqual(os.path.basename(logjob.caminho('../../x')), 'sem-id.log')

    def test_corta_depois_do_limite_e_avisa(self):
        log = logjob.LogJob('j-1')
        with mock.patch.object(logjob, 'MAX_LINHAS', 5):
            for i in range(20):
                log.escrever('linha %d' % i)
        log.fechar()
        linhas = ler_texto(logjob.caminho('j-1')).splitlines()
        self.assertEqual(len(linhas), 6)
        self.assertIn('Log cortado', linhas[-1])

    def test_falha_ao_escrever_nunca_levanta(self):
        log = logjob.LogJob('j-2')
        with mock.patch('recuperacao.logjob.os.open', side_effect=OSError('disco cheio')):
            log.escrever('x')
            log.escrever('y')
        log.fechar()

    def test_controle_grava_evento_nos_dois_lugares_e_log_so_no_arquivo(self):
        store = JobStore(os.path.join(self.dados, 'jobs.db'))
        self.addCleanup(store.fechar)
        job = store.criar('imagem', 'a', 'b')
        c = Controle(store, job['id'])
        c.evento('para a tela')
        c.log('só detalhe')
        c._log.fechar()
        self.assertEqual([e['texto'] for e in store.eventos(job['id'])], ['para a tela'])
        texto = ler_texto(logjob.caminho(job['id']))
        self.assertIn('para a tela', texto)
        self.assertIn('só detalhe', texto)


class TestLogEmJobs(BaseVarredura):
    def log_do(self, job_id):
        return ler_texto(logjob.caminho(job_id))

    def esperar_fim(self, job_id, estado='concluido'):
        fim = self.esperar_estado(job_id, estado)
        esperar(lambda: os.path.exists(logjob.caminho(job_id)) and 'Resultado:' in self.log_do(job_id), 10, 0.02, 'log final')
        return fim

    def imagem_com_defeitos(self, ruins, **opcoes):
        origem = criar_origem(self.trabalho, 1024 * 1024)
        destino = os.path.join(self.trabalho, 'img.bin')
        defeitos = Defeitos(ruins)
        with mock.patch.object(imagem, '_ler_bloco', defeitos):
            r = self.req('POST', '/api/jobs', {'tipo': 'imagem', 'origem': origem, 'destino': destino,
                                               'opcoes': dict({'bloco': 65536}, **opcoes)})
            self.assertEqual(r.status_code, 201, r.get_json())
            job = r.get_json()
            self.esperar_fim(job['id'])
        return origem, destino, job

    def test_imagem_com_setores_ruins_loga_o_que_leu_pulou_e_falhou_e_gera_relatorio(self):
        ruins = [100, 101, 102, 900]
        origem, destino, job = self.imagem_com_defeitos(ruins, tentativas=0)
        log = self.log_do(job['id'])
        for trecho in ('Começou a rodar', 'Fase: Copiando', 'Erro de leitura no bloco',
                       'Setor ilegível: %d a %d' % (900 * S, 901 * S),
                       'Faixa ilegível final: %d a %d (3 setores)' % (100 * S, 103 * S), 'Resultado: concluido', origem):
            self.assertIn(trecho, log)
        with open(origem, 'rb') as f:
            conteudo = f.read()
        self.assertNotIn(conteudo[:64].hex(), log)                       # conteúdo nunca vai para o log
        for linha in log.splitlines():
            self.assertRegex(linha, r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \S')
        with open(destino + '.relatorio.csv', encoding='utf-8-sig', newline='') as f:
            linhas = list(csv.DictReader(f))
        self.assertEqual([(int(l['inicio']), int(l['fim']), int(l['setores'])) for l in linhas],
                         [(100 * S, 103 * S, 3), (900 * S, 901 * S, 1)])
        pagina = ler_texto(destino + '.relatorio.html')
        self.assertIn('Relatório da imagem', pagina)
        self.assertIn('2 faixas, 4 setores', pagina)
        self.assertIn('não se aplica', pagina)
        eventos = ' '.join(e['texto'] for e in self.req('GET', '/api/jobs/' + job['id']).get_json()['eventos'])
        self.assertIn('Relatório da imagem', eventos)
        self.assertEqual(self.ctx.store.obter_checkpoint(job['id'])['relatorio'],
                         ['img.bin.relatorio.csv', 'img.bin.relatorio.html'])

    def test_imagem_sem_defeito_traz_o_sha256_no_relatorio(self):
        origem, destino, job = self.imagem_com_defeitos([])
        pagina = ler_texto(destino + '.relatorio.html')
        self.assertRegex(pagina, r'[0-9a-f]{64}')
        self.assertIn('0 faixas', pagina)
        with open(destino + '.relatorio.csv', encoding='utf-8-sig', newline='') as f:
            self.assertEqual(list(csv.DictReader(f)), [])

    def test_relatorio_da_imagem_nao_sobrescreve_arquivo_existente(self):
        destino = os.path.join(self.trabalho, 'img.bin')
        with open(destino + '.relatorio.html', 'w', encoding='utf-8') as f:
            f.write('de outro programa')
        _, _, job = self.imagem_com_defeitos([])
        self.assertEqual(ler_texto(destino + '.relatorio.html'), 'de outro programa')
        self.assertTrue(os.path.exists('%s.relatorio-%s.html' % (destino, job['id'])))

    def test_log_que_falha_nao_derruba_o_job(self):
        arquivo = os.path.join(self.pasta, 'arquivo-no-lugar-da-pasta')
        open(arquivo, 'w').close()
        with mock.patch.object(logjob, 'pasta', lambda: os.path.join(arquivo, 'jobs')):    # não dá para criar a pasta
            origem = criar_origem(self.trabalho, 200000)
            destino = os.path.join(self.trabalho, 'img.bin')
            r = self.req('POST', '/api/jobs', {'tipo': 'imagem', 'origem': origem, 'destino': destino}).get_json()
            self.esperar_estado(r['id'], 'concluido')
        self.assertFalse(os.path.exists(os.path.join(self.dados, 'logs', 'jobs', r['id'] + '.log')))

    def test_pausar_e_retomar_acrescenta_no_mesmo_log(self):
        origem = criar_origem(self.trabalho, 3 * 1024 * 1024)
        destino = os.path.join(self.trabalho, 'img.bin')
        with mock.patch.dict(os.environ, {'RECDADOS_ATRASO_BLOCO': '0.05'}):
            r = self.req('POST', '/api/jobs', {'tipo': 'imagem', 'origem': origem, 'destino': destino,
                                               'opcoes': {'bloco': 65536}}).get_json()
            esperar(lambda: self.req('GET', '/api/jobs/' + r['id']).get_json()['progresso'] > 0.2, 30, 0.02)
            self.assertEqual(self.req('POST', '/api/jobs/%s/pausar' % r['id']).status_code, 200)
            self.assertEqual(self.req('POST', '/api/jobs/%s/retomar' % r['id']).status_code, 200)
            self.esperar_fim(r['id'])
        log = self.log_do(r['id'])
        self.assertEqual(log.count('Começou a rodar'), 2)
        self.assertEqual(log.count('Resultado: pausado'), 1)
        self.assertEqual(log.count('Resultado: concluido'), 1)

    def test_varredura_e_recuperar_logam_achados_e_arquivos_sem_conteudo(self):
        caminho, info, _ = montar_imagem(self.trabalho)
        v = self.varrer(caminho, bloco=65536)
        self.esperar_fim(v['id'])
        log = self.log_do(v['id'])
        self.assertEqual(len(re.findall(r'Achado \w+ em \d+: \d+ bytes, ', log)), 7)
        self.assertIn('Achado JPG em %d: ' % info[0]['offset'], log)
        destino = os.path.join(self.pasta, 'saida')
        r = self.req('POST', '/api/jobs/%s/recuperar' % v['id'], {'ids': 'todos', 'destino': destino}).get_json()
        self.esperar_fim(r['id'])
        log = self.log_do(r['id'])
        self.assertEqual(len(re.findall(r': ok, \d+ bytes, achado \d+ ', log)), 7)
        self.assertRegex(log, r'Fotos/f\d+\.jpg: ok')
        self.assertRegex(log, r'sha256 [0-9a-f]{64}')

    def test_recuperar_loga_o_arquivo_que_nao_gravou(self):
        origem = os.path.join(self.trabalho, 'o.img')
        with open(origem, 'wb') as f:
            f.write(b'0123456789')
        job, _ = fazer_job_com_achados(
            self, [{'nome': 'a.txt', 'tamanho': 5, 'offset': 0, 'grupo': 'documento'}], origem=origem)

        def falha(*a):
            raise OSError(errno.ENOSPC, 'No space left on device')
        with mock.patch.object(recuperar, '_copiar', falha):
            r = self.req('POST', '/api/jobs/%s/recuperar' % job['id'],
                         {'ids': 'todos', 'destino': os.path.join(self.pasta, 's')}).get_json()
            self.esperar_fim(r['id'])
        self.assertRegex(self.log_do(r['id']), r'Falhou ao gravar Documentos/a\.txt \(achado \d+\): No space left')


if __name__ == '__main__':
    unittest.main()
