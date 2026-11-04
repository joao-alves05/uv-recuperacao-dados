# -*- coding: utf-8 -*-
"""Os 4 cenários obrigatórios de persistência (a, b, c, d) e o encerramento limpo.

O serviço roda em outro processo, com pasta de dados e porta temporárias. Origens são arquivos temporários.
"""
import http.client
import json
import os
import sqlite3
import threading
import time
import unittest

from auxiliar import CasoComPasta, Servico, criar_origem, esperar, sha256_de

from recuperacao.tipos import imagem


def ok_no_mapa(destino):
    with open(destino + '.mapa.json', encoding='utf-8') as f:
        faixas = json.load(f)['faixas']
    return sum(b - a for a, b, e in faixas if e == 'ok'), faixas


class ClienteSSE(threading.Thread):
    """Um cliente do /api/eventos em conexão própria (como uma aba), que guarda tudo o que recebe."""

    def __init__(self, servico, cabecalhos=None):
        super().__init__(daemon=True)
        self.servico = servico
        self.eventos = []            # (instante, nome, dados)
        self.pings = 0
        self.conectado = threading.Event()
        self.parar = False
        self.cabecalhos = cabecalhos if cabecalhos is not None else {'X-RecDados-Token': servico.token}
        self.con = None

    def run(self):
        self.con = http.client.HTTPConnection('127.0.0.1', self.servico.porta, timeout=60)
        try:
            self.con.request('GET', '/api/eventos', headers=self.cabecalhos)
            r = self.con.getresponse()
            nome = None
            while not self.parar:
                linha = r.fp.readline()
                if not linha:
                    break
                linha = linha.decode('utf-8').rstrip('\r\n')
                if linha.startswith('event: '):
                    nome = linha[7:]
                elif linha.startswith('data: '):
                    self.eventos.append((time.monotonic(), nome, json.loads(linha[6:])))
                    self.conectado.set()
                elif linha.startswith(': ping'):
                    self.pings += 1
        except (OSError, ValueError):
            pass

    def parar_e_fechar(self):
        self.parar = True
        if self.con:
            self.con.close()

    def jobs(self, job_id):
        return [d for _, n, d in self.eventos if n == 'job' and d['id'] == job_id]


class TestPersistencia(CasoComPasta):
    def servico(self, atraso=0.0, porta=None):
        s = Servico(self.dados, porta=porta, atraso=atraso).iniciar()
        self.addCleanup(s.matar)
        return s

    # (a)
    def test_a_sem_nenhum_cliente_o_job_termina_sozinho(self):
        origem = criar_origem(self.trabalho, 3_000_000)
        destino = os.path.join(self.trabalho, 'img.bin')
        s = self.servico()
        job = s.criar_imagem(origem, destino)
        # nenhum cliente fica conectado; só olhamos depois, por requisições soltas
        final = s.esperar_estado(job['id'], ('concluido', 'falhou'))
        self.assertEqual(final['estado'], 'concluido', final['erro'])
        self.assertEqual(sha256_de(destino), sha256_de(origem))
        self.assertEqual(final['progresso'], 1.0)
        self.assertIsNotNone(final['terminado_em'])

    # (b)
    def test_b_dois_clientes_sse_e_outra_aba_veem_o_mesmo_job(self):
        origem = criar_origem(self.trabalho, 4_000_000)
        destino = os.path.join(self.trabalho, 'img.bin')
        s = self.servico(atraso=0.03)
        c1, c2 = ClienteSSE(s), ClienteSSE(s)
        for c in (c1, c2):
            c.start()
            self.addCleanup(c.parar_e_fechar)
        self.assertTrue(c1.conectado.wait(10) and c2.conectado.wait(10))
        for c in (c1, c2):
            self.assertEqual(c.eventos[0][1], 'jobs')              # ao conectar: a lista inteira
            self.assertEqual(c.eventos[0][2], [])
        # a "outra aba": sessão HTTP separada, entra pelo link com o token e usa só o cookie
        aba = http.client.HTTPConnection('127.0.0.1', s.porta, timeout=30)
        self.addCleanup(aba.close)
        ticket = s.pedir('POST', '/api/ticket')[1]['ticket']        # o ?t= é um ticket de uso único, nunca o token
        aba.request('GET', '/?t=' + ticket)
        r = aba.getresponse()
        r.read()
        self.assertEqual(r.status, 302)
        cookie = r.getheader('Set-Cookie').split(';')[0]
        job = s.criar_imagem(origem, destino, bloco=65536)
        vistos = []

        def da_aba():
            aba.request('GET', '/api/jobs/' + job['id'], headers={'Cookie': cookie})
            resp = aba.getresponse()
            d = json.loads(resp.read())
            vistos.append(d)
            return d['estado'] == 'concluido'

        esperar(da_aba, 40, 0.1, 'a outra aba não viu o fim')
        esperar(lambda: all(c.jobs(job['id']) and c.jobs(job['id'])[-1]['estado'] == 'concluido' for c in (c1, c2)),
                10, 0.1, 'o SSE não entregou o estado final')
        ultimo1, ultimo2 = c1.jobs(job['id'])[-1], c2.jobs(job['id'])[-1]
        final = s.job(job['id'])
        for campo in ('estado', 'progresso', 'bytes_feitos', 'bytes_total', 'terminado_em'):
            self.assertEqual(ultimo1[campo], ultimo2[campo], campo)
            self.assertEqual(ultimo1[campo], final[campo], campo)
            self.assertEqual(vistos[-1][campo], final[campo], campo)
        # o progresso que cada cliente viu só cresce e é o mesmo job
        for c in (c1, c2):
            prog = [d['progresso'] for d in c.jobs(job['id'])]
            self.assertEqual(prog, sorted(prog))
            self.assertGreater(len(prog), 3)
            # no máximo 2 eventos por segundo por job (com folga de 1 evento)
            dur = c.eventos[-1][0] - c.eventos[1][0]
            self.assertLessEqual(len(prog), 2 * dur + 3, (len(prog), dur))
        self.assertEqual(sha256_de(destino), sha256_de(origem))

    # (c)
    def test_c_matar_o_processo_no_meio_e_retomar(self):
        origem = criar_origem(self.trabalho, 8_000_000)
        destino = os.path.join(self.trabalho, 'img.bin')
        log1 = os.path.join(self.pasta, 'leituras1.log')
        log2 = os.path.join(self.pasta, 'leituras2.log')
        s = Servico(self.dados, atraso=0.04, log_leituras=log1).iniciar()
        self.addCleanup(s.matar)
        job = s.criar_imagem(origem, destino, bloco=65536)
        esperar(lambda: s.job(job['id'])['bytes_feitos'] > 1_500_000, 30, 0.05, 'o job não andou')
        # o mapa só é gravado a cada 2 s (checkpoint): espera o primeiro aparecer em disco e mata em seguida
        esperar(lambda: os.path.exists(destino + '.mapa.json') and ok_no_mapa(destino)[0] > 0, 30, 0.02,
                'o checkpoint não apareceu')
        s.matar()                                           # TerminateProcess/kill: sem encerramento limpo
        self.assertNotEqual(s.proc.returncode, 0)
        prontos, faixas = ok_no_mapa(destino)               # o mapa em disco é válido (gravação atômica)
        self.assertGreater(prontos, 0)
        self.assertLess(prontos, 8_000_000)
        self.assertTrue(any(e == 'pendente' for _, _, e in faixas))

        s2 = Servico(self.dados, porta=s.porta, atraso=0, log_leituras=log2)
        # a porta recém-liberada pode demorar um instante no Windows
        esperar(lambda: self._sobe(s2), 20, 0.5, 'o serviço não subiu de novo')
        self.addCleanup(s2.matar)
        j = s2.job(job['id'])
        self.assertEqual(j['estado'], 'interrompido')       # nunca retoma sozinho
        time.sleep(0.5)
        self.assertEqual(s2.job(job['id'])['estado'], 'interrompido')
        st, resp, _ = s2.pedir('POST', '/api/jobs/%s/retomar' % job['id'])
        self.assertEqual(st, 200, resp)
        final = s2.esperar_estado(job['id'], ('concluido', 'falhou'), 40)
        self.assertEqual(final['estado'], 'concluido', final['erro'])
        self.assertEqual(sha256_de(destino), sha256_de(origem))
        eventos = [e['texto'] for e in s2.job(job['id'])['eventos']]
        self.assertTrue(any('Retomando: %d bytes já prontos' % prontos in t for t in eventos), (prontos, eventos))
        self.assertEqual(ok_no_mapa(destino)[1], [[0, 8_000_000, 'ok']])
        # prova de que a retomada não releu o que já estava ok: o log de leituras do 2º processo só tem faixas pendentes
        with open(log2, encoding='ascii') as f:
            lidas = [tuple(map(int, l.split())) for l in f if l.strip()]
        self.assertTrue(lidas)
        for ini, n in lidas:
            for a, b, e in faixas:
                if e != 'pendente':
                    self.assertFalse(ini < b and ini + n > a, ('releu faixa pronta', (ini, n), (a, b, e)))
        pendentes = sum(b - a for a, b, e in faixas if e == 'pendente')
        self.assertEqual(sum(n for _, n in lidas), pendentes)            # leu exatamente o que faltava
        with open(log1, encoding='ascii') as f:
            self.assertGreater(len(f.readlines()), 0)

    def _sobe(self, s):
        try:
            s.iniciar(prazo=3)
            return True
        except AssertionError:
            if s.proc and s.proc.poll() is None:
                s.proc.kill()
            s.fechar()
            return False

    # (d)
    def test_d_duas_acoes_ao_mesmo_tempo_uma_passa_a_outra_409(self):
        origem = criar_origem(self.trabalho, 6_000_000)
        destino = os.path.join(self.trabalho, 'img.bin')
        s = self.servico(atraso=0.05)
        job = s.criar_imagem(origem, destino, bloco=65536)
        s.esperar_estado(job['id'], 'rodando')
        time.sleep(0.3)

        def disparar(acoes):
            barreira = threading.Barrier(len(acoes))
            resultados = [None] * len(acoes)

            def agir(i, acao):
                barreira.wait()
                st, corpo, _ = s.pedir('POST', '/api/jobs/%s/%s' % (job['id'], acao))
                resultados[i] = (st, corpo)

            fios = [threading.Thread(target=agir, args=(i, a)) for i, a in enumerate(acoes)]
            for f in fios:
                f.start()
            for f in fios:
                f.join(30)
            return resultados

        for acoes in (('pausar', 'pausar'), ('cancelar', 'cancelar')):
            if acoes[0] == 'cancelar':
                s.pedir('POST', '/api/jobs/%s/retomar' % job['id'])
                s.esperar_estado(job['id'], 'rodando')
            res = disparar(acoes)
            codigos = sorted(r[0] for r in res)
            self.assertEqual(codigos, [200, 409], res)
            perdedora = next(r for r in res if r[0] == 409)[1]
            self.assertIn('erro', perdedora)
            self.assertTrue(perdedora['erro'])
            if acoes == ('pausar', 'pausar'):
                self.assertEqual(s.job(job['id'])['estado'], 'pausado')
        # nada quebrou: o serviço segue de pé, e o estado final é coerente com a ação que passou
        self.assertEqual(s.pedir('GET', '/api/saude', token=False)[0], 200)
        self.assertEqual(s.job(job['id'])['estado'], 'cancelado')

    def test_d_retomar_em_dobro_ao_mesmo_tempo(self):
        origem = criar_origem(self.trabalho, 3_000_000)
        destino = os.path.join(self.trabalho, 'img.bin')
        s = self.servico(atraso=0.05)
        job = s.criar_imagem(origem, destino, bloco=65536)
        s.esperar_estado(job['id'], 'rodando')
        st, _, _ = s.pedir('POST', '/api/jobs/%s/pausar' % job['id'])
        self.assertEqual(st, 200)
        barreira = threading.Barrier(2)
        codigos = []

        def agir():
            barreira.wait()
            codigos.append(s.pedir('POST', '/api/jobs/%s/retomar' % job['id'])[0])

        fios = [threading.Thread(target=agir) for _ in range(2)]
        for f in fios:
            f.start()
        for f in fios:
            f.join(30)
        self.assertEqual(sorted(codigos), [200, 409])
        s.pedir('POST', '/api/jobs/%s/cancelar' % job['id'])

    # 7
    def test_7_desligar_com_job_rodando_deixa_pausado_e_mapa_em_dia(self):
        origem = criar_origem(self.trabalho, 6_000_000)
        destino = os.path.join(self.trabalho, 'img.bin')
        s = self.servico(atraso=0.05)
        job = s.criar_imagem(origem, destino, bloco=65536)
        segundo = s.criar_imagem(criar_origem(self.trabalho, 6_000_000, 'o2.bin'), os.path.join(self.trabalho, 'img2.bin'),
                                 bloco=65536)
        fila = s.criar_imagem(criar_origem(self.trabalho, 6_000_000, 'o3.bin'), os.path.join(self.trabalho, 'img3.bin'),
                              bloco=65536)       # o terceiro espera na fila (máximo 2 ao mesmo tempo)
        self.assertEqual(s.job(fila['id'])['estado'], 'na_fila')
        esperar(lambda: s.job(job['id'])['bytes_feitos'] > 500_000, 30, 0.05)
        st, corpo, _ = s.pedir('POST', '/api/desligar')
        self.assertEqual(st, 200, corpo)
        s.proc.wait(timeout=20)                              # o processo sai sozinho
        self.assertEqual(s.proc.returncode, 0)
        con = sqlite3.connect(os.path.join(self.dados, 'jobs.db'))
        try:
            estados = dict(con.execute('SELECT id, estado FROM jobs'))
            feitos = con.execute('SELECT bytes_feitos FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(estados[job['id']], 'pausado')
        self.assertEqual(estados[segundo['id']], 'pausado')
        self.assertEqual(estados[fila['id']], 'pausado')                  # o que esperava na fila também fica pausado
        self.assertEqual(set(estados.values()), {'pausado'})
        prontos, faixas = ok_no_mapa(destino)
        self.assertGreater(prontos, 0)
        self.assertEqual(prontos, feitos)                    # o mapa e o banco dizem o mesmo
        self.assertEqual(os.path.getsize(destino), 6_000_000)
        # na volta nada fica `interrompido`: o desligamento foi limpo
        s2 = Servico(self.dados, porta=s.porta)
        esperar(lambda: self._sobe(s2), 20, 0.5)
        self.addCleanup(s2.matar)
        self.assertEqual(s2.job(job['id'])['estado'], 'pausado')
        self.assertNotIn('interrompido', {j['estado'] for j in s2.pedir('GET', '/api/jobs')[1]})
        s2.pedir('POST', '/api/desligar')


if __name__ == '__main__':
    unittest.main()
