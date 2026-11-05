# -*- coding: utf-8 -*-
"""Fase 2, parte 2.8: "disco" sintético com setores ruins em posições conhecidas e queda REAL do processo do serviço
(kill) no meio da bisseção, da varredura e do recuperar. Depois de subir de novo e retomar, nada se perde nem se repete.

O serviço roda em outro processo, por `servico_com_defeitos.py` (que só desvia a leitura da imagem e deixa passos mais
lentos, para dar tempo de matar no ponto certo). Nenhum disco real, nenhuma porta fixa.
"""
import json
import os
import sqlite3
import subprocess
import sys
import unittest

import amostras as A
import imagens_sa as im
from auxiliar import CasoComPasta, RAIZ, Servico, criar_origem, esperar, sha256_de
from test_sa_fat import conteudo, sha

from recuperacao.tipos import imagem

S = 512
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'servico_com_defeitos.py')


class ServicoComDefeitos(Servico):
    """O mesmo Servico dos outros testes, subindo pelo script que instala o disco defeituoso."""

    def __init__(self, dados, defeitos=None, **kw):
        super().__init__(dados, **kw)
        self.defeitos = defeitos or {}

    def env(self):
        e = super().env()
        e['RECDADOS_TESTE_DEFEITOS'] = json.dumps(self.defeitos)
        return e

    def iniciar(self, esperar_saude=True, prazo=20.0):
        original = sys.executable
        self.proc = subprocess.Popen([original, SCRIPT], cwd=RAIZ, env=self.env(), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if esperar_saude:
            def no_ar():
                if self.proc.poll() is not None:
                    raise AssertionError('o serviço saiu: %s' % self.proc.stderr.read().decode('utf-8', 'replace'))
                try:
                    return self.pedir('GET', '/api/saude', token=False)[0] == 200
                except OSError:
                    return False
            esperar(no_ar, prazo, 0.1, 'o serviço não subiu')
        return self


def banco(dados):
    return sqlite3.connect(os.path.join(dados, 'jobs.db'))


def mapa_cru(destino):
    try:
        with open(imagem.caminho_mapa(destino), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class TestSetorRuimEKillNaBisseccao(CasoComPasta):
    RUINS = [[100, 102], [3000, 3001]]                # setores 100 e 101, e 3000 (posições conhecidas)

    def setUp(self):
        super().setUp()
        self.tam = 2 * 1024 * 1024
        self.origem = criar_origem(self.trabalho, self.tam)
        with open(self.origem, 'rb') as f:
            self.dados_origem = f.read()
        self.destino = os.path.join(self.trabalho, 'img.bin')
        self.log = os.path.join(self.trabalho, 'leituras.log')

    def conferir_imagem_final(self):
        with open(self.destino, 'rb') as f:
            img = f.read()
        ruins = {x for a, b in self.RUINS for x in range(a, b)}
        for x in range(self.tam // S):
            trecho = slice(x * S, (x + 1) * S)
            self.assertEqual(img[trecho], bytes(S) if x in ruins else self.dados_origem[trecho], 'setor %d' % x)
        self.assertEqual(mapa_cru(self.destino)['faixas'],
                         [[0, 100 * S, 'ok'], [100 * S, 102 * S, 'ruim'], [102 * S, 3000 * S, 'ok'],
                          [3000 * S, 3001 * S, 'ruim'], [3001 * S, self.tam, 'ok']])

    def test_kill_real_no_meio_da_bisseccao_retoma_sem_reler_o_que_esta_ok(self):
        s = ServicoComDefeitos(self.dados, {'ruins': self.RUINS, 'lento_falha': 0.4}, log_leituras=self.log).iniciar()
        self.addCleanup(s.matar)
        job = s.criar_imagem(self.origem, self.destino, bloco=65536)
        base = {}

        def no_meio():
            m = mapa_cru(self.destino)
            if not m or m.get('fase') != 'bissecao':
                return False
            base.setdefault('n', len(m['faixas']))
            return len(m['faixas']) > base['n'] and m
        antes = esperar(no_meio, 40, 0.05, 'a bisseção não avançou')
        s.matar()                                              # kill de verdade: sem encerramento limpo
        with open(self.log, 'a') as f:
            f.write('---\n')                                   # separa as leituras do 1º processo das do 2º
        mapa_morte = mapa_cru(self.destino)
        self.assertEqual(mapa_morte['fase'], 'bissecao')
        self.assertTrue(any(e == 'suspeito' for _, _, e in mapa_morte['faixas']), 'ainda faltava resolver blocos')
        ok_antes = [(a, b) for a, b, e in mapa_morte['faixas'] if e == 'ok']
        self.assertTrue(ok_antes, 'precisava haver trecho ok para provar que não é relido')

        s2 = ServicoComDefeitos(self.dados, {'ruins': self.RUINS}, log_leituras=self.log).iniciar()
        self.addCleanup(s2.matar)
        self.assertEqual(s2.job(job['id'])['estado'], 'interrompido')
        st, _, _ = s2.pedir('POST', '/api/jobs/%s/retomar' % job['id'])
        self.assertEqual(st, 200)
        s2.esperar_estado(job['id'], 'concluido', 60)
        s2.matar()

        self.conferir_imagem_final()
        with open(self.log) as f:
            linhas = f.read().split('---\n')
        depois = [tuple(map(int, l.split())) for l in linhas[1].splitlines() if l.strip()]
        self.assertTrue(depois, 'o 2º processo precisava ler algo')
        for ini, n in depois:
            for a, b in ok_antes:
                self.assertFalse(ini < b and ini + n > a, 'releu o que já estava ok no mapa: %d+%d em %d-%d' % (ini, n, a, b))
        self.assertEqual(sha256_de(self.origem), sha(self.dados_origem))
        self.assertGreater(len(antes['faixas']), 0)


class TestKillNaVarreduraPeloSistema(CasoComPasta):
    def montar_fat(self, n=60):
        f = im.FatImg('FAT32')
        self.esperados = {}
        for i in range(n):
            nome = 'arquivo %02d.txt' % i
            d = conteudo(100 + i, i)
            e, cs = f.add('/', nome, d)
            if i % 3 == 0:
                f.apagar(e, cs)
            self.esperados['/' + nome] = (sha(d), i % 3 == 0)
        return f.gravar(os.path.join(self.trabalho, 'pendrive.img'))

    def achados(self, job_id):
        con = banco(self.dados)
        try:
            return con.execute('SELECT caminho, offset, tamanho, apagado FROM achados WHERE job_id=? ORDER BY id',
                               (job_id,)).fetchall()
        finally:
            con.close()

    def test_kill_real_no_meio_da_lista_retoma_sem_perder_nem_repetir(self):
        caminho = self.montar_fat()
        s = ServicoComDefeitos(self.dados, {'lote_sistema': 5, 'lento_sistema': 0.05}).iniciar()
        self.addCleanup(s.matar)
        st, job, _ = s.pedir('POST', '/api/jobs', {'tipo': 'varredura', 'origem': caminho, 'opcoes': {'modo': 'sistema'}})
        self.assertEqual(st, 201, job)
        esperar(lambda: 5 <= len(self.achados(job['id'])) < 50, 40, 0.05, 'a lista não ficou pela metade')
        s.matar()
        feitos = len(self.achados(job['id']))
        self.assertTrue(5 <= feitos < len(self.esperados), feitos)
        s2 = ServicoComDefeitos(self.dados, {'lote_sistema': 5}).iniciar()
        self.addCleanup(s2.matar)
        self.assertEqual(s2.job(job['id'])['estado'], 'interrompido')
        self.assertEqual(s2.pedir('POST', '/api/jobs/%s/retomar' % job['id'])[0], 200)
        s2.esperar_estado(job['id'], 'concluido', 60)
        s2.matar()
        lista = self.achados(job['id'])
        self.assertEqual(len(lista), len(self.esperados))
        self.assertEqual(len({l[0] for l in lista}), len(lista), 'achado repetido')
        self.assertEqual({l[0] for l in lista}, set(self.esperados))
        for cam, off, tam, apagado in lista:
            self.assertEqual(bool(apagado), self.esperados[cam][1], cam)
            with open(caminho, 'rb') as f:
                f.seek(off)
                self.assertEqual(sha(f.read(tam)), self.esperados[cam][0], cam)

    def test_kill_real_na_fase_do_carving_depois_da_lista_com_nomes(self):
        f = im.FatImg('FAT16')
        jpg = A.jpeg(scan=3000, data='2021:05:17 10:20:30')
        e, cs = f.add('/', 'foto apagada.jpg', jpg)
        f.apagar(e, cs)
        f.add('/', 'leia-me.txt', conteudo(300, 5))
        caminho = f.gravar(os.path.join(self.trabalho, 'f16.img'))
        cedo, tarde = A.jpeg(scan=2000, semente=11), A.jpeg(scan=2500, semente=12)
        base = f.dados_ini * 512
        pontos = {base + 100 * f.csize: cedo, base + 4500 * f.csize: tarde}
        with open(caminho, 'r+b') as g:
            for off, dados in pontos.items():
                g.seek(off)
                g.write(dados)
        s = ServicoComDefeitos(self.dados, {}, atraso=0.08).iniciar()
        self.addCleanup(s.matar)
        st, job, _ = s.pedir('POST', '/api/jobs', {'tipo': 'varredura', 'origem': caminho,
                                                   'opcoes': {'modo': 'rapida', 'sistema_antes': True, 'bloco': 65536}})
        self.assertEqual(st, 201, job)

        def no_carving():
            con = banco(self.dados)
            try:
                l = con.execute('SELECT dados FROM checkpoints WHERE job_id=?', (job['id'],)).fetchone()
            finally:
                con.close()
            c = json.loads(l[0]) if l else {}
            return c.get('fase') == 'carving' and 0 < c.get('pos', 0) < os.path.getsize(caminho) // 2
        esperar(no_carving, 40, 0.05, 'não chegou ao carving')
        s.matar()
        s2 = ServicoComDefeitos(self.dados, {}).iniciar()
        self.addCleanup(s2.matar)
        self.assertEqual(s2.pedir('POST', '/api/jobs/%s/retomar' % job['id'])[0], 200)
        s2.esperar_estado(job['id'], 'concluido', 60)
        s2.matar()
        lista = self.achados(job['id'])
        com_nome = sorted(l[0] for l in lista if l[0])
        self.assertEqual(com_nome, ['/foto apagada.jpg', '/leia-me.txt'])
        sem_nome = sorted((l[1], l[2]) for l in lista if not l[0])
        self.assertEqual(sem_nome, sorted((off, len(d)) for off, d in pontos.items()),
                         'o carving acha só os dois solto, uma vez cada, e não repete o que tem nome')


class TestKillNoRecuperar(CasoComPasta):
    def test_kill_real_no_meio_do_recuperar_termina_sem_perder_nem_duplicar(self):
        f = im.FatImg('FAT16')
        d = {}
        for i in range(8):
            nome = 'foto%d.jpg' % i
            d[nome] = A.jpeg(scan=2000 + 300 * i, semente=20 + i)
            e, cs = f.add('/', nome, d[nome])
            f.apagar(e, cs)
        caminho = f.gravar(os.path.join(self.trabalho, 'cartao.img'))
        saida = os.path.join(self.pasta, 'saida')
        antes = (sha256_de(caminho), os.stat(caminho).st_mtime_ns)

        s = ServicoComDefeitos(self.dados, {}).iniciar()
        st, job, _ = s.pedir('POST', '/api/jobs', {'tipo': 'varredura', 'origem': caminho, 'opcoes': {'modo': 'sistema'}})
        self.assertEqual(st, 201, job)
        s.esperar_estado(job['id'], 'concluido', 60)
        s.matar()

        s = ServicoComDefeitos(self.dados, {}, atraso=0.3).iniciar()
        self.addCleanup(s.matar)
        st, rec, _ = s.pedir('POST', '/api/jobs/%s/recuperar' % job['id'], {'ids': 'todos', 'destino': saida})
        self.assertEqual(st, 201, rec)

        def feitos():
            con = banco(self.dados)
            try:
                return con.execute("SELECT COUNT(*) FROM recuperacoes WHERE job_id=? AND resultado='ok'",
                                   (rec['id'],)).fetchone()[0]
            finally:
                con.close()
        esperar(lambda: 2 <= feitos() < 8, 40, 0.05, 'o recuperar não ficou pela metade')
        s.matar()
        n_morte = feitos()
        self.assertTrue(2 <= n_morte < 8)

        s2 = ServicoComDefeitos(self.dados, {}).iniciar()
        self.addCleanup(s2.matar)
        self.assertEqual(s2.job(rec['id'])['estado'], 'interrompido')
        self.assertEqual(s2.pedir('POST', '/api/jobs/%s/retomar' % rec['id'])[0], 200)
        s2.esperar_estado(rec['id'], 'concluido', 60)
        s2.matar()

        arquivos = []
        for raiz, _, nomes in os.walk(saida):
            arquivos += [os.path.join(raiz, n) for n in nomes if n.startswith('foto') or n.endswith('.parte')]
        self.assertFalse([a for a in arquivos if a.endswith('.parte')], 'sobrou arquivo pela metade')
        self.assertEqual(len(arquivos), 8)
        self.assertEqual(len({os.path.basename(a).casefold() for a in arquivos}), 8, 'arquivo repetido')
        recuperados = sorted(sha256_de(a) for a in arquivos)
        import hashlib
        self.assertEqual(recuperados, sorted(hashlib.sha256(v).hexdigest() for v in d.values()))
        con = banco(self.dados)
        self.assertEqual(con.execute("SELECT COUNT(*), COUNT(DISTINCT achado_id) FROM recuperacoes "
                                     "WHERE job_id=? AND resultado='ok'", (rec['id'],)).fetchone(), (8, 8))
        con.close()
        self.assertEqual((sha256_de(caminho), os.stat(caminho).st_mtime_ns), antes, 'a origem mudou')


if __name__ == '__main__':
    unittest.main()
