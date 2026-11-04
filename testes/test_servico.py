# -*- coding: utf-8 -*-
"""Rotas, segurança do serviço, SSE (primeiro evento) e o iniciar.py."""
import json
import os
import subprocess
import threading
import sys
import time
import unittest
from unittest import mock

import auxiliar
from auxiliar import CasoComPasta, Servico, criar_origem, esperar, porta_livre

import iniciar
from recuperacao import servico


class Base(CasoComPasta):
    """Serviço em processo, com o cliente de teste do Flask, pasta e porta temporárias."""

    def setUp(self):
        super().setUp()
        self.porta = porta_livre()
        patcher = mock.patch.dict(os.environ, {'RECDADOS_DADOS': self.dados, 'RECDADOS_PORTA': str(self.porta)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('RECDADOS_ATRASO_BLOCO', None)
        self.ctx = servico.Contexto()
        self.app = servico.criar_app(self.ctx)
        self.ctx.iniciar()
        self.addCleanup(self.ctx.store.fechar)
        self.addCleanup(self.ctx.executor.encerrar, 5.0)
        self.base = 'http://127.0.0.1:%d' % self.porta
        self.c = self.app.test_client()
        self.h = {'X-RecDados-Token': self.ctx.token}

    def req(self, metodo, caminho, corpo=None, headers=None, **kw):
        h = dict(self.h if headers is None else headers)
        return self.c.open(caminho, method=metodo, json=corpo, headers=h, base_url=kw.pop('base_url', self.base), **kw)

    def ticket(self):
        r = self.req('POST', '/api/ticket')
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()['ticket']

    def entrar(self, caminho='/'):
        """Entra pelo ?t= com um ticket novo, como o iniciar.py faz."""
        return self.c.get(caminho + '?t=' + self.ticket(), base_url=self.base)

    def nova_imagem(self, tamanho=100_000, nome='img.bin', **opcoes):
        origem = criar_origem(self.trabalho, tamanho, 'o-' + nome)
        destino = os.path.join(self.trabalho, nome)
        r = self.req('POST', '/api/jobs', {'tipo': 'imagem', 'origem': origem, 'destino': destino, 'opcoes': opcoes})
        return origem, destino, r


class TestRotas(Base):
    def test_saude_sem_token_e_sem_dados_do_usuario(self):
        r = self.req('GET', '/api/saude', headers={})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(set(d), {'ok', 'versao', 'pid'})
        self.assertIs(d['ok'], True)
        self.assertEqual(d['pid'], os.getpid())

    def test_criar_lista_e_detalhe(self):
        origem, destino, r = self.nova_imagem(bloco=4096)
        self.assertEqual(r.status_code, 201, r.get_json())
        job = r.get_json()
        self.assertEqual((job['tipo'], job['origem'], job['destino']), ('imagem', origem, destino))
        esperar(lambda: self.req('GET', '/api/jobs/' + job['id']).get_json()['estado'] == 'concluido', 20)
        lista = self.req('GET', '/api/jobs').get_json()
        self.assertEqual([j['id'] for j in lista], [job['id']])
        d = self.req('GET', '/api/jobs/' + job['id']).get_json()
        self.assertIn('eventos', d)
        self.assertTrue(all({'id', 'quando', 'texto'} <= set(e) for e in d['eventos']))
        self.assertEqual(d['progresso'], 1.0)

    def test_criar_recusa_com_erro_em_portugues(self):
        origem = criar_origem(self.trabalho, 100)
        existente = os.path.join(self.trabalho, 'ja-existe.bin')
        open(existente, 'wb').close()
        casos = [
            {'tipo': 'imagem', 'origem': os.path.join(self.trabalho, 'nao-existe'), 'destino': os.path.join(self.trabalho, 'x')},
            {'tipo': 'imagem', 'origem': origem, 'destino': origem},
            {'tipo': 'imagem', 'origem': origem, 'destino': os.path.join(origem, 'dentro')},
            {'tipo': 'imagem', 'origem': origem, 'destino': existente},
            {'tipo': 'clonagem', 'origem': origem, 'destino': os.path.join(self.trabalho, 'y')},
            {'tipo': 'imagem', 'origem': 5, 'destino': 'z'},
            {'tipo': 'imagem'},
        ]
        for corpo in casos:
            r = self.req('POST', '/api/jobs', corpo)
            self.assertEqual(r.status_code, 400, corpo)
            self.assertEqual(list(r.get_json()), ['erro'])
            self.assertTrue(r.get_json()['erro'])
        r = self.c.post('/api/jobs', data='isto não é json', headers=self.h, base_url=self.base)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.req('GET', '/api/jobs').get_json(), [])             # nada foi criado

    def test_duplo_clique_no_mesmo_destino_da_409(self):
        with mock.patch('recuperacao.config.atraso_bloco', return_value=0.05):
            origem, destino, r1 = self.nova_imagem(tamanho=2_000_000, bloco=65536)
            self.assertEqual(r1.status_code, 201)
            corpo = {'tipo': 'imagem', 'origem': origem, 'destino': destino, 'opcoes': {}}
            r2 = self.req('POST', '/api/jobs', corpo)                  # o arquivo do destino já existe: ainda assim é 409
            self.assertEqual(r2.status_code, 409)
            self.assertIn('destino', r2.get_json()['erro'])
            r3 = self.req('POST', '/api/jobs', dict(corpo, destino=os.path.join(self.trabalho, '.', 'img.bin')))
            self.assertEqual(r3.status_code, 409)
            self.assertEqual(len(self.req('GET', '/api/jobs').get_json()), 1)
            self.req('POST', '/api/jobs/%s/cancelar' % r1.get_json()['id'])

    def test_duplo_clique_simultaneo_so_um_job(self):
        origem = criar_origem(self.trabalho, 50_000)
        destino = os.path.join(self.trabalho, 'dup.bin')
        codigos, barreira = [], threading.Barrier(6)

        def clicar():
            barreira.wait()
            r = self.app.test_client().post('/api/jobs', json={'tipo': 'imagem', 'origem': origem, 'destino': destino},
                                            headers=self.h, base_url=self.base)
            codigos.append(r.status_code)

        fios = [threading.Thread(target=clicar) for _ in range(6)]
        for f in fios:
            f.start()
        for f in fios:
            f.join()
        self.assertEqual(codigos.count(201), 1, codigos)
        self.assertTrue(all(c in (201, 409, 400) for c in codigos), codigos)
        self.assertEqual(len(self.req('GET', '/api/jobs').get_json()), 1)

    def test_rota_recusa_rede_e_dispositivo(self):
        origem = criar_origem(self.trabalho, 100)
        barra = chr(92)
        for ruim in (barra * 2 + 'servidor' + barra + 'x' + barra + 'a.bin', barra * 2 + '.' + barra + 'PhysicalDrive0', 'NUL'):
            for corpo in ({'origem': origem, 'destino': ruim}, {'origem': ruim, 'destino': os.path.join(self.trabalho, 'z')}):
                r = self.req('POST', '/api/jobs', dict(corpo, tipo='imagem'))
                self.assertEqual(r.status_code, 400, corpo)

    def test_conexoes_de_threads_de_requisicao_nao_ficam_abertas(self):
        def pedir():
            r = self.app.test_client().get('/api/jobs', headers=self.h, base_url=self.base)
            assert r.status_code == 200
        self.req('GET', '/api/jobs')
        antes = len(self.ctx.store._conexoes)
        for _ in range(30):
            t = threading.Thread(target=pedir)
            t.start()
            t.join()
        self.assertLessEqual(len(self.ctx.store._conexoes), antes + 1)

    def test_job_inexistente_404(self):
        for caminho in ('/api/jobs/j-20990101-abcd', '/api/jobs/qualquer-coisa'):
            r = self.req('GET', caminho)
            self.assertEqual(r.status_code, 404)
            self.assertIn('erro', r.get_json())
        self.assertEqual(self.req('POST', '/api/jobs/j-20990101-abcd/pausar').status_code, 404)

    def test_acoes_repetidas_dao_409_claro(self):
        with mock.patch('recuperacao.config.atraso_bloco', return_value=0.05):
            _, _, r = self.nova_imagem(tamanho=2_000_000, bloco=65536)
            jid = r.get_json()['id']
            esperar(lambda: self.req('GET', '/api/jobs/' + jid).get_json()['estado'] == 'rodando', 10)
            self.assertEqual(self.req('POST', '/api/jobs/%s/retomar' % jid).status_code, 409)     # rodando
            self.assertEqual(self.req('POST', '/api/jobs/%s/pausar' % jid).status_code, 200)
        r = self.req('POST', '/api/jobs/%s/pausar' % jid)                                          # já pausado
        self.assertEqual(r.status_code, 409)
        self.assertIn('pausado', r.get_json()['erro'])
        self.assertEqual(self.req('POST', '/api/jobs/%s/retomar' % jid).status_code, 200)
        esperar(lambda: self.req('GET', '/api/jobs/' + jid).get_json()['estado'] == 'concluido', 20)
        for acao in ('pausar', 'retomar', 'cancelar'):
            r = self.req('POST', '/api/jobs/%s/%s' % (jid, acao))
            self.assertEqual(r.status_code, 409, acao)
            self.assertIn('concluído', r.get_json()['erro'])

    def test_retomar_vale_para_interrompido(self):
        origem, destino, r = self.nova_imagem(tamanho=50_000)
        jid = r.get_json()['id']
        esperar(lambda: self.req('GET', '/api/jobs/' + jid).get_json()['estado'] == 'concluido', 20)
        self.ctx.store.atualizar(jid, estado='interrompido')
        r = self.req('POST', '/api/jobs/%s/retomar' % jid)
        self.assertEqual(r.status_code, 200)

    def test_cancelar(self):
        with mock.patch('recuperacao.config.atraso_bloco', return_value=0.05):
            _, destino, r = self.nova_imagem(tamanho=2_000_000, bloco=65536)
            jid = r.get_json()['id']
            esperar(lambda: self.req('GET', '/api/jobs/' + jid).get_json()['estado'] == 'rodando', 10)
            r = self.req('POST', '/api/jobs/%s/cancelar' % jid)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['estado'], 'cancelado')
        self.assertTrue(os.path.exists(destino) and os.path.exists(destino + '.mapa.json'))

    def test_sse_primeiro_evento_traz_a_lista_inteira(self):
        _, _, r = self.nova_imagem(tamanho=1000)
        jid = r.get_json()['id']
        resp = self.c.get('/api/eventos', headers=self.h, base_url=self.base, buffered=False)
        try:
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.mimetype.startswith('text/event-stream'))
            primeiro = next(iter(resp.response))
            texto = primeiro.decode() if isinstance(primeiro, bytes) else primeiro
            self.assertTrue(texto.startswith('event: jobs\ndata: '), texto)
            dados = json.loads(texto.split('data: ', 1)[1])
            self.assertEqual([j['id'] for j in dados], [jid])
        finally:
            resp.close()
        self.assertEqual(self.ctx.store._assinantes, [])            # ao fechar, deixa de assinar

    def test_desligar_responde_e_pausa_o_que_rodava(self):
        with mock.patch('recuperacao.config.atraso_bloco', return_value=0.05):
            _, _, r = self.nova_imagem(tamanho=2_000_000, bloco=65536)
            jid = r.get_json()['id']
            esperar(lambda: self.req('GET', '/api/jobs/' + jid).get_json()['estado'] == 'rodando', 10)
            resp = self.req('POST', '/api/desligar')
            self.assertEqual(resp.status_code, 200)
            esperar(lambda: self.ctx.store.obter(jid)['estado'] == 'pausado', 15)


class TestPainel(Base):
    def test_pagina_sem_cookie_so_mostra_o_aviso(self):
        for caminho in ('/', '/jobs/j-20261104-7f3a'):
            r = self.c.get(caminho, base_url=self.base)
            self.assertEqual(r.status_code, 401)
            self.assertIn('Abra pelo atalho do programa', r.get_data(as_text=True))
            self.assertNotIn('j-2026', r.get_data(as_text=True))

    def test_token_na_url_vira_cookie_e_redireciona_sem_o_t(self):
        r = self.entrar('/jobs/j-20261104-7f3a')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers['Location'].split('?')[0].rsplit('/jobs/', 1)[-1], 'j-20261104-7f3a')
        self.assertNotIn('t=', r.headers['Location'])
        cookie = r.headers['Set-Cookie']
        self.assertTrue(cookie.startswith('recdados='), cookie)
        self.assertNotIn(self.ctx.token, cookie)                 # o cookie é uma sessão, nunca o token do arquivo
        for parte in ('HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(parte, cookie)
        r2 = self.c.get('/jobs/j-20261104-7f3a', base_url=self.base)          # o cliente guardou o cookie
        self.assertEqual(r2.status_code, 200)
        r2.close()
        self.assertEqual(self.c.get('/api/jobs', base_url=self.base).status_code, 200)

    def test_token_errado_nao_da_cookie(self):
        r = self.c.get('/?t=errado', base_url=self.base)
        self.assertEqual(r.status_code, 401)
        self.assertNotIn('Set-Cookie', r.headers)

    def test_token_do_arquivo_na_url_nao_abre_mais_nada(self):
        r = self.c.get('/?t=' + self.ctx.token, base_url=self.base)
        self.assertEqual(r.status_code, 401)
        self.assertNotIn('Set-Cookie', r.headers)
        self.assertEqual(self.c.get('/api/jobs', base_url=self.base).status_code, 401)

    def test_ticket_vale_uma_vez_so(self):
        tk = self.ticket()
        r = self.c.get('/?t=' + tk, base_url=self.base)
        self.assertEqual(r.status_code, 302)
        outro = self.app.test_client()                         # histórico do navegador, outra sessão: o mesmo link
        r2 = outro.get('/?t=' + tk, base_url=self.base)
        self.assertEqual(r2.status_code, 401)
        self.assertNotIn('Set-Cookie', r2.headers)
        self.assertEqual(outro.get('/api/jobs', base_url=self.base).status_code, 401)

    def test_ticket_vence_em_60_segundos(self):
        agora = [1000.0]
        self.ctx.relogio = lambda: agora[0]
        tk = self.ticket()
        agora[0] += 59
        outro = self.app.test_client()
        agora[0] += 2
        r = outro.get('/?t=' + tk, base_url=self.base)
        self.assertEqual(r.status_code, 401)
        agora[0] += 0
        tk2 = self.ticket()
        agora[0] += 59
        self.assertEqual(self.c.get('/?t=' + tk2, base_url=self.base).status_code, 302)

    def test_sessao_do_cookie_vence_em_12_horas(self):
        agora = [1000.0]
        self.ctx.relogio = lambda: agora[0]
        self.entrar()
        self.assertEqual(self.c.get('/api/jobs', base_url=self.base).status_code, 200)
        agora[0] += 12 * 3600 - 5
        self.assertEqual(self.c.get('/api/jobs', base_url=self.base).status_code, 200)
        agora[0] += 10
        self.assertEqual(self.c.get('/api/jobs', base_url=self.base).status_code, 401)

    def test_ticket_so_com_o_token_do_arquivo(self):
        self.assertEqual(self.req('POST', '/api/ticket', headers={}).status_code, 401)
        self.assertEqual(self.req('POST', '/api/ticket', headers={'X-RecDados-Token': 'x' * 43}).status_code, 401)
        self.entrar()                                           # com sessão (cookie) também não vale
        self.assertEqual(self.c.post('/api/ticket', base_url=self.base).status_code, 401)

    def test_ticket_rejeita_origin_de_outro_site(self):
        r = self.req('POST', '/api/ticket', headers=dict(self.h, Origin='https://evil.example'))
        self.assertEqual(r.status_code, 403)

    def test_saude_com_desafio_prova_que_conhece_o_token(self):
        import hashlib
        import hmac
        r = self.req('GET', '/api/saude?desafio=abc123', headers={})
        esperado = hmac.new(self.ctx.token.encode(), b'abc123', hashlib.sha256).hexdigest()
        self.assertEqual(r.get_json()['prova'], esperado)
        self.assertNotIn(self.ctx.token, r.get_data(as_text=True))
        self.assertNotIn('prova', self.req('GET', '/api/saude', headers={}).get_json())
        self.assertNotIn('prova', self.req('GET', '/api/saude?desafio=' + 'x' * 101, headers={}).get_json())

    def test_csp_em_todas_as_respostas(self):
        csp = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'"
        for r in (self.req('GET', '/api/saude', headers={}), self.req('GET', '/api/jobs'), self.c.get('/', base_url=self.base)):
            self.assertEqual(r.headers['Content-Security-Policy'], csp)

    def test_nao_serve_nada_fora_da_pasta_do_painel(self):
        self.entrar()
        for caminho in ('/%2e%2e/servico.py', '/..%2fservico.py', '/config.py', '/api/nada'):
            r = self.c.get(caminho, base_url=self.base)
            self.assertEqual(r.status_code, 404, caminho)
            self.assertNotIn(b'import', r.data)

    def test_arquivo_do_painel_exige_cookie(self):
        pasta = servico.PAINEL
        existente = next((p for p in pasta.glob('*.js')), None) if pasta.is_dir() else None
        if existente is None:
            self.skipTest('painel ainda sem arquivos')
        self.assertEqual(self.c.get('/' + existente.name, base_url=self.base).status_code, 404)
        self.entrar()
        r = self.c.get('/' + existente.name, base_url=self.base)
        self.assertEqual(r.status_code, 200)
        r.close()


class TestSeguranca(Base):
    def test_api_sem_token_401(self):
        for metodo, caminho in (('GET', '/api/jobs'), ('POST', '/api/jobs'), ('GET', '/api/jobs/j-20261104-7f3a'),
                                ('GET', '/api/eventos'), ('POST', '/api/desligar')):
            r = self.req(metodo, caminho, headers={})
            self.assertEqual(r.status_code, 401, (metodo, caminho))
            self.assertEqual(r.get_json(), {'erro': 'abra pelo atalho do programa'})

    def test_token_errado_401(self):
        r = self.req('GET', '/api/jobs', headers={'X-RecDados-Token': 'x' * 43})
        self.assertEqual(r.status_code, 401)
        self.c.set_cookie('recdados', 'errado', domain='127.0.0.1')
        self.assertEqual(self.req('GET', '/api/jobs', headers={}).status_code, 401)

    def test_desligar_sem_token_nao_desliga(self):
        self.req('POST', '/api/desligar', headers={})
        time.sleep(0.2)
        self.assertFalse(self.ctx._desligado)

    def test_host_estranho_recusado(self):
        for host in ('evil.example.com', '127.0.0.1', '127.0.0.1:1', 'localhost', '127.0.0.1.evil.com:%d' % self.porta,
                     '10.0.0.5:%d' % self.porta):
            r = self.req('GET', '/api/jobs', base_url='http://' + host)
            self.assertEqual(r.status_code, 403, host)
            self.assertEqual(self.req('GET', '/api/saude', base_url='http://' + host).status_code, 403, host)
            self.assertEqual(self.c.get('/', base_url='http://' + host).status_code, 403, host)
        self.assertEqual(self.req('GET', '/api/jobs', base_url='http://localhost:%d' % self.porta).status_code, 200)

    def test_origin_de_outro_site_no_post_403(self):
        for origem in ('https://evil.example', 'http://127.0.0.1:%d' % (self.porta + 1), 'null', 'http://localhost'):
            h = dict(self.h, Origin=origem)
            for caminho in ('/api/jobs', '/api/desligar', '/api/jobs/j-20261104-7f3a/pausar'):
                r = self.req('POST', caminho, {}, headers=h)
                self.assertEqual(r.status_code, 403, (origem, caminho))
        self.assertFalse(self.ctx._desligado)
        ok = self.req('POST', '/api/jobs/j-20261104-7f3a/pausar', headers=dict(self.h, Origin=self.base))
        self.assertEqual(ok.status_code, 404)           # passou pela trava de Origin
        ok = self.req('POST', '/api/jobs/j-20261104-7f3a/pausar', headers=dict(self.h, Origin='http://localhost:%d' % self.porta))
        self.assertEqual(ok.status_code, 404)

    def test_cabecalhos_de_protecao(self):
        r = self.req('GET', '/api/saude', headers={})
        self.assertEqual(r.headers['Cache-Control'], 'no-store')
        self.assertEqual(r.headers['Referrer-Policy'], 'no-referrer')

    def test_token_estavel_entre_reinicios_e_so_no_arquivo(self):
        t1 = self.ctx.token
        self.assertGreaterEqual(len(t1), 40)
        outro = servico.Contexto()
        self.addCleanup(outro.store.fechar)
        self.assertEqual(outro.token, t1)
        with open(os.path.join(self.dados, 'token'), encoding='ascii') as f:
            self.assertEqual(f.read().strip(), t1)


class TestSegundaCopia(CasoComPasta):
    def test_segunda_copia_nao_sobe_e_nao_mexe_nos_jobs_da_primeira(self):
        origem = criar_origem(self.trabalho, 3_000_000)
        a = Servico(self.dados, atraso=0.05).iniciar()
        self.addCleanup(a.matar)
        job = a.criar_imagem(origem, os.path.join(self.trabalho, 'img.bin'))
        a.esperar_estado(job['id'], 'rodando')
        b = Servico(self.dados, porta=a.porta).iniciar(esperar_saude=False)
        self.addCleanup(b.matar)
        codigo = b.proc.wait(timeout=20)
        self.assertNotEqual(codigo, 0)
        erro = b.proc.stderr.read().decode('utf-8', 'replace')
        self.assertIn('já está no ar', erro)
        self.assertNotIn('Traceback', erro)
        self.assertEqual(a.job(job['id'])['estado'], 'rodando')            # a 2ª cópia não marcou nada como interrompido
        self.assertEqual(a.pedir('GET', '/api/saude', token=False)[0], 200)
        self.assertEqual(a.pedir('GET', '/api/saude', token=False)[1]['pid'], a.proc.pid)


class TestIniciar(CasoComPasta):
    def test_trava_de_subida_so_deixa_uma(self):
        trava = os.path.join(self.dados, 'servico.lock')
        self.assertTrue(iniciar.pegar_trava(trava))
        self.assertFalse(iniciar.pegar_trava(trava))
        velho = time.time() + 30                       # relógio andou: a trava já é de uma subida que morreu
        self.assertTrue(iniciar.pegar_trava(trava, agora=lambda: velho))

    def _principal(self, no_ar, trava=None, identidade=lambda p: True):
        subidas, abertas, tickets = [], [], []
        with mock.patch.dict(os.environ, {'RECDADOS_DADOS': self.dados, 'RECDADOS_PORTA': '45678'}):
            codigo = iniciar.principal(no_ar=no_ar, subir=lambda: subidas.append(1), esperar=lambda p: True,
                                       abrir=abertas.append, trava=trava, identidade=identidade,
                                       ticket=lambda p: tickets.append(p) or 'tk-abc')
        return codigo, subidas, abertas, tickets

    def test_ja_no_ar_so_abre_o_navegador_com_ticket_e_nunca_o_token(self):
        codigo, subidas, abertas, tickets = self._principal(lambda p: True)
        self.assertEqual((codigo, subidas), (0, []))
        self.assertEqual(abertas, ['http://127.0.0.1:45678/?t=tk-abc'])
        with mock.patch.dict(os.environ, {'RECDADOS_DADOS': self.dados}):
            self.assertFalse(os.path.exists(os.path.join(self.dados, 'token')) and
                             auxiliar_token(self.dados) in abertas[0])

    def test_fora_do_ar_sobe_uma_vez_e_abre(self):
        codigo, subidas, abertas, _ = self._principal(lambda p: False)
        self.assertEqual((codigo, len(subidas)), (0, 1))
        self.assertEqual(abertas, ['http://127.0.0.1:45678/?t=tk-abc'])

    def test_quem_responde_na_porta_nao_prova_identidade_nada_e_aberto_nem_enviado(self):
        codigo, _, abertas, tickets = self._principal(lambda p: True, identidade=lambda p: False)
        self.assertNotEqual(codigo, 0)
        self.assertEqual((abertas, tickets), ([], []))

    def test_dois_cliques_seguidos_sobem_uma_copia_so(self):
        with mock.patch.dict(os.environ, {'RECDADOS_DADOS': self.dados, 'RECDADOS_PORTA': '45678'}):
            subidas = []
            for _ in range(2):
                iniciar.principal(no_ar=lambda p: False, subir=lambda: subidas.append(1), esperar=lambda p: True,
                                  abrir=lambda u: None, identidade=lambda p: True, ticket=lambda p: 'tk')
        self.assertEqual(len(subidas), 1)

    def test_nao_consegue_subir_devolve_erro(self):
        with mock.patch.dict(os.environ, {'RECDADOS_DADOS': self.dados, 'RECDADOS_PORTA': '45678'}):
            codigo = iniciar.principal(no_ar=lambda p: False, subir=lambda: None, esperar=lambda p: False,
                                       abrir=lambda u: self.fail('não deveria abrir'))
        self.assertEqual(codigo, 1)

    def test_servico_no_ar_so_reconhece_o_nosso(self):
        import http.server
        import threading

        class Falso(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                corpo = b'{"ok": true}'          # sem "versao": não é o nosso
                self.send_response(200)
                self.send_header('Content-Length', str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(('127.0.0.1', 0), Falso)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.assertFalse(iniciar.servico_no_ar(srv.server_address[1]))

    def test_programa_falso_que_diz_ser_o_nosso_nao_recebe_o_token(self):
        """Responde ok+versao e até uma 'prova' inventada: a identidade não confere e nenhum pedido leva o token."""
        import http.server
        recebidos = []

        class Falso(http.server.BaseHTTPRequestHandler):
            def _responder(self):
                recebidos.append((self.command, self.path, dict(self.headers)))
                corpo = b'{"ok": true, "versao": "1", "pid": 1, "prova": "00"}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            do_GET = do_POST = _responder

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(('127.0.0.1', 0), Falso)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        porta = srv.server_address[1]
        abertas = []
        with mock.patch.dict(os.environ, {'RECDADOS_DADOS': self.dados, 'RECDADOS_PORTA': str(porta)}):
            self.assertTrue(iniciar.servico_no_ar(porta))              # parece o nosso...
            self.assertNotEqual(iniciar.principal(abrir=abertas.append), 0)       # ...mas não prova
            token = auxiliar_token(self.dados)
        self.assertEqual(abertas, [])
        self.assertTrue(recebidos)
        self.assertTrue(all(m == 'GET' and 'X-RecDados-Token' not in h for m, _, h in recebidos), recebidos)
        self.assertFalse(any(token in str(r) for r in recebidos))

    def test_iniciar_de_verdade_sobe_destacado_e_abre(self):
        """Roda o subir_servico real (processo destacado) com porta e pasta temporárias; mata no fim."""
        porta = porta_livre()
        abertas = []
        with mock.patch.dict(os.environ, {'RECDADOS_DADOS': self.dados, 'RECDADOS_PORTA': str(porta)}):
            codigo = iniciar.principal(abrir=abertas.append)
            self.assertEqual(codigo, 0)
            self.assertTrue(iniciar.servico_no_ar(porta))
            import urllib.request
            pid = json.loads(urllib.request.urlopen('http://127.0.0.1:%d/api/saude' % porta).read())['pid']
            self.addCleanup(self._matar, pid)
            self.assertEqual(len(abertas), 1)
            self.assertIn('/?t=', abertas[0])
            self.assertNotIn(auxiliar_token(self.dados), abertas[0])         # o token do arquivo não vai para a URL
            import http.client
            caminho = abertas[0].split(str(porta), 1)[1]
            for esperado in (302, 401):                                      # o ticket abre uma vez, na segunda não
                con = http.client.HTTPConnection('127.0.0.1', porta, timeout=10)
                con.request('GET', caminho)
                r = con.getresponse()
                r.read()
                con.close()
                self.assertEqual(r.status, esperado)
            # segunda chamada: já no ar, não sobe outra
            self.assertEqual(iniciar.principal(abrir=abertas.append), 0)
            pid2 = json.loads(urllib.request.urlopen('http://127.0.0.1:%d/api/saude' % porta).read())['pid']
            self.assertEqual(pid, pid2)

    @staticmethod
    def _matar(pid):
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
        else:
            os.kill(pid, 9)
        time.sleep(0.3)


def auxiliar_token(dados):
    with open(os.path.join(dados, 'token'), encoding='ascii') as f:
        return f.read().strip()


if __name__ == '__main__':
    unittest.main()
