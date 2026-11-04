# -*- coding: utf-8 -*-
"""JobStore, mapfile e o tipo `imagem` (em processo, só com arquivos temporários)."""
import os
import re
import time
import unittest
from unittest import mock

import auxiliar
from auxiliar import CasoComPasta, criar_origem, esperar, sha256_de

from recuperacao import jobs as jobs_mod
from recuperacao import leitor
from recuperacao.executor import Executor
from recuperacao.jobs import DestinoEmUso, JobStore
from recuperacao.tipos import imagem


class FalsoCtrl:
    """Controle de mentira: pede pausa depois de `pausar_apos` leituras."""

    def __init__(self, pausar_apos=None, leituras=None):
        self.pausar_apos = pausar_apos
        self.leituras = leituras if leituras is not None else []
        self.eventos = []
        self.ultimo = None

    def pausa_pedida(self):
        return self.pausar_apos is not None and len(self.leituras) >= self.pausar_apos

    def cancelamento_pedido(self):
        return False

    def evento(self, t):
        self.eventos.append(t)

    def progresso(self, feitos, total, texto=None, ruins=0):
        self.ultimo = (feitos, total, ruins)

    def checkpoint(self, salvar=None, bytes_novos=0, forcar=False):
        if salvar and (forcar or True):      # salva sempre: simplifica a prova
            salvar()
        return True


class TestJobStore(CasoComPasta):
    def setUp(self):
        super().setUp()
        self.store = JobStore(os.path.join(self.dados, 'jobs.db'))
        self.addCleanup(self.store.fechar)

    def test_criar_e_listar_mais_novo_primeiro(self):
        a = self.store.criar('imagem', 'o1', 'd1', {'bloco': 4096})
        time.sleep(0.01)
        b = self.store.criar('imagem', 'o2', 'd2')
        self.assertRegex(a['id'], r'^j-\d{8}-[0-9a-f]{4}$')
        self.assertEqual(a['estado'], 'na_fila')
        self.assertEqual(a['opcoes'], {'bloco': 4096})
        self.assertEqual([j['id'] for j in self.store.listar()], [b['id'], a['id']])
        for campo in jobs_mod.COLUNAS:
            self.assertIn(campo, a)

    def test_wal_ligado(self):
        modo = self.store._con().execute('PRAGMA journal_mode').fetchone()[0]
        self.assertEqual(modo.lower(), 'wal')

    def test_transicao_so_vale_do_estado_certo(self):
        j = self.store.criar('imagem', 'o', 'd')
        self.assertFalse(self.store.transicao(j['id'], ('rodando',), 'pausado'))
        self.assertTrue(self.store.transicao(j['id'], ('na_fila',), 'rodando'))
        self.assertFalse(self.store.transicao(j['id'], ('na_fila',), 'rodando'))
        self.assertEqual(self.store.obter(j['id'])['estado'], 'rodando')

    def test_progresso_no_maximo_duas_vezes_por_segundo(self):
        j = self.store.criar('imagem', 'o', 'd')
        gravou = [self.store.progresso(j['id'], i, 100) for i in range(1, 50)]
        self.assertTrue(gravou[0])
        self.assertEqual(sum(gravou), 1)            # 49 chamadas em poucos ms: só a primeira grava
        self.assertEqual(self.store.obter(j['id'])['bytes_feitos'], 1)
        self.assertTrue(self.store.progresso(j['id'], 60, 100, forcar=True))
        self.assertEqual(self.store.obter(j['id'])['progresso'], 0.6)

    def test_marcar_interrompidos_so_o_que_rodava(self):
        r = self.store.criar('imagem', 'o', 'd1')
        self.store.transicao(r['id'], ('na_fila',), 'rodando')
        p = self.store.criar('imagem', 'o', 'd2')
        self.store.transicao(p['id'], ('na_fila',), 'pausado')
        c = self.store.criar('imagem', 'o', 'd3')
        self.store.transicao(c['id'], ('na_fila',), 'concluido')
        self.store.marcar_interrompidos()
        estados = {j['id']: j['estado'] for j in self.store.listar()}
        self.assertEqual(estados[r['id']], 'interrompido')
        self.assertEqual(estados[p['id']], 'pausado')
        self.assertEqual(estados[c['id']], 'concluido')

    def test_eventos_ultimos_200_em_ordem(self):
        j = self.store.criar('imagem', 'o', 'd')
        for i in range(250):
            self.store.evento(j['id'], 'e%d' % i)
        ev = self.store.eventos(j['id'], 200)
        self.assertEqual(len(ev), 200)
        self.assertEqual(ev[0]['texto'], 'e50')
        self.assertEqual(ev[-1]['texto'], 'e249')

    def test_conexao_por_thread(self):
        import threading
        ids = []
        t = threading.Thread(target=lambda: ids.append(id(self.store._con())))
        t.start()
        t.join()
        self.assertNotEqual(ids[0], id(self.store._con()))

    def test_destino_em_uso_so_ate_o_job_terminar(self):
        x = os.path.join(self.trabalho, 'x.bin')
        a = self.store.criar('imagem', 'o', x)
        for igual in (x, os.path.join(self.trabalho, '.', 'x.bin'), x.upper() if os.name == 'nt' else x):
            with self.assertRaises(DestinoEmUso):
                self.store.criar('imagem', 'o2', igual)
        self.assertEqual(self.store.job_com_destino(x), a['id'])
        for estado in ('pausado', 'interrompido', 'rodando'):             # job que não terminou ainda é dono do destino
            self.store.atualizar(a['id'], estado=estado)
            with self.assertRaises(DestinoEmUso):
                self.store.criar('imagem', 'o2', x)
        self.store.atualizar(a['id'], estado='cancelado')
        self.store.criar('imagem', 'o2', x)                               # terminado: libera

    def test_duplo_clique_em_threads_so_cria_um(self):
        import threading
        resultados, barreira = [], threading.Barrier(8)

        def clicar():
            barreira.wait()
            try:
                self.store.criar('imagem', 'o', os.path.join(self.trabalho, 'dup.bin'))
                resultados.append('criou')
            except DestinoEmUso:
                resultados.append('recusou')

        fios = [threading.Thread(target=clicar) for _ in range(8)]
        for f in fios:
            f.start()
        for f in fios:
            f.join()
        self.assertEqual(sorted(resultados), ['criou'] + ['recusou'] * 7)

    def test_fechar_conexao_da_thread(self):
        import threading
        antes = len(self.store._conexoes)

        def usar():
            self.store.listar()
            self.store.fechar_conexao_da_thread()
        for _ in range(10):
            t = threading.Thread(target=usar)
            t.start()
            t.join()
        self.assertEqual(len(self.store._conexoes), antes)

    def test_tabelas_do_contrato(self):
        nomes = {l[0] for l in self.store._con().execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'jobs', 'eventos', 'achados'} <= nomes)


class TestMapfile(unittest.TestCase):
    def test_marcar_corta_e_junta(self):
        f = [[0, 100, 'pendente']]
        imagem.marcar(f, 0, 30, 'ok')
        imagem.marcar(f, 30, 50, 'ruim')
        imagem.marcar(f, 50, 60, 'ok')
        self.assertEqual(f, [[0, 30, 'ok'], [30, 50, 'ruim'], [50, 60, 'ok'], [60, 100, 'pendente']])
        imagem.marcar(f, 30, 50, 'ok')
        self.assertEqual(f, [[0, 60, 'ok'], [60, 100, 'pendente']])
        imagem.marcar(f, 10, 20, 'ruim')            # no meio de uma faixa
        self.assertEqual(f, [[0, 10, 'ok'], [10, 20, 'ruim'], [20, 60, 'ok'], [60, 100, 'pendente']])

    def test_setores_ruins_arredonda_para_cima(self):
        self.assertEqual(imagem.setores_ruins([[0, 513, 'ruim'], [513, 600, 'ok']]), 2)


class TestImagem(CasoComPasta):
    def setUp(self):
        super().setUp()
        self.store = JobStore(os.path.join(self.dados, 'jobs.db'))
        self.addCleanup(self.store.fechar)
        self.executor = Executor(self.store, {'imagem': imagem})
        self.executor.iniciar()
        self.addCleanup(self.executor.encerrar, 5.0)

    def rodar(self, origem, destino, bloco=65536):
        j = self.store.criar('imagem', origem, destino, {'bloco': bloco})
        self.executor.submeter(j['id'])
        return j['id']

    def esperar_estado(self, job_id, *estados):
        return esperar(lambda: (j := self.store.obter(job_id))['estado'] in estados and j, 20, 0.02, 'estado %s' % (estados,))

    def test_imagem_igual_a_origem(self):
        origem = criar_origem(self.trabalho, 700_001)           # tamanho que não é múltiplo do bloco
        destino = os.path.join(self.trabalho, 'img.bin')
        j = self.esperar_estado(self.rodar(origem, destino), 'concluido', 'falhou')
        self.assertEqual(j['estado'], 'concluido', j['erro'])
        self.assertEqual(sha256_de(destino), sha256_de(origem))
        self.assertEqual(j['progresso'], 1.0)
        self.assertEqual(j['bytes_feitos'], 700_001)
        self.assertEqual(j['setores_ruins'], 0)
        textos = [e['texto'] for e in self.store.eventos(j['id'])]
        self.assertTrue(any('SHA-256 igual' in t for t in textos), textos)
        mapa = imagem.carregar_mapa(imagem.caminho_mapa(destino), 700_001)
        self.assertEqual(mapa, [[0, 700_001, 'ok']])

    def test_arquivo_vazio(self):
        origem = criar_origem(self.trabalho, 0)
        destino = os.path.join(self.trabalho, 'img.bin')
        j = self.esperar_estado(self.rodar(origem, destino), 'concluido', 'falhou')
        self.assertEqual(j['estado'], 'concluido', j['erro'])
        self.assertEqual(os.path.getsize(destino), 0)

    def test_bloco_ruim_tenta_duas_vezes_marca_zera_e_segue(self):
        origem = criar_origem(self.trabalho, 4 * 4096)
        destino = os.path.join(self.trabalho, 'img.bin')
        original = imagem._ler_bloco
        tentativas = []

        def com_defeito(fd, pos, n):
            if pos == 4096:
                tentativas.append(pos)
                raise OSError('setor ruim de mentira')
            return original(fd, pos, n)

        with mock.patch.object(imagem, '_ler_bloco', com_defeito):
            j = self.esperar_estado(self.rodar(origem, destino, bloco=4096), 'concluido', 'falhou')
        self.assertEqual(j['estado'], 'concluido', j['erro'])
        self.assertEqual(len(tentativas), 2)                    # tenta 2 vezes
        self.assertEqual(j['setores_ruins'], 8)                 # 4096 / 512
        mapa = imagem.carregar_mapa(imagem.caminho_mapa(destino), 4 * 4096)
        self.assertEqual(mapa, [[0, 4096, 'ok'], [4096, 8192, 'ruim'], [8192, 16384, 'ok']])
        with open(origem, 'rb') as a, open(destino, 'rb') as b:
            o, d = a.read(), b.read()
        self.assertEqual(d[4096:8192], bytes(4096))             # preenchido com zero
        self.assertEqual(d[:4096], o[:4096])
        self.assertEqual(d[8192:], o[8192:])

    def test_retomar_nao_rele_o_que_ja_esta_ok(self):
        origem = criar_origem(self.trabalho, 10 * 4096)
        destino = os.path.join(self.trabalho, 'img.bin')
        job = {'origem': origem, 'destino': destino, 'opcoes': {'bloco': 4096}}
        leituras = []
        original = imagem._ler_bloco

        def espiao(fd, pos, n):
            leituras.append((pos, n))
            return original(fd, pos, n)

        with mock.patch.object(imagem, '_ler_bloco', espiao):
            r1 = imagem.executar(job, FalsoCtrl(pausar_apos=4, leituras=leituras))
            self.assertEqual(r1, 'pausado')
            mapa = imagem.carregar_mapa(imagem.caminho_mapa(destino), 10 * 4096)
            self.assertEqual(mapa, [[0, 4 * 4096, 'ok'], [4 * 4096, 10 * 4096, 'pendente']])
            leituras.clear()
            ctrl2 = FalsoCtrl(leituras=[])
            r2 = imagem.executar(job, ctrl2)
        self.assertEqual(r2, 'concluido')
        primeiras = [p for p, n in leituras if n == 4096]
        self.assertEqual(primeiras[:6], [i * 4096 for i in range(4, 10)])
        # nenhuma leitura de bloco da cópia tocou nas faixas ok (a conferência final lê o arquivo todo em blocos de 4 MiB)
        self.assertFalse([p for p, n in leituras if n == 4096 and p < 4 * 4096])
        self.assertEqual(sha256_de(destino), sha256_de(origem))
        self.assertTrue(any('Retomando: 16384 bytes já prontos' in e for e in ctrl2.eventos), ctrl2.eventos)

    def test_pausar_e_retomar_pelo_executor(self):
        origem = criar_origem(self.trabalho, 2_000_000)
        destino = os.path.join(self.trabalho, 'img.bin')
        with mock.patch('recuperacao.config.atraso_bloco', return_value=0.05):
            jid = self.rodar(origem, destino, bloco=65536)
            self.esperar_estado(jid, 'rodando')
            time.sleep(0.3)
            self.executor.pausar(jid)
            j = self.store.obter(jid)
            self.assertEqual(j['estado'], 'pausado')
            self.assertGreater(j['bytes_feitos'], 0)
            self.assertLess(j['bytes_feitos'], 2_000_000)
        self.executor.retomar(jid)
        j = self.esperar_estado(jid, 'concluido', 'falhou')
        self.assertEqual(j['estado'], 'concluido', j['erro'])
        self.assertEqual(sha256_de(destino), sha256_de(origem))

    def test_cancelar_mantem_mapa_e_imagem_parcial(self):
        origem = criar_origem(self.trabalho, 2_000_000)
        destino = os.path.join(self.trabalho, 'img.bin')
        with mock.patch('recuperacao.config.atraso_bloco', return_value=0.05):
            jid = self.rodar(origem, destino)
            self.esperar_estado(jid, 'rodando')
            time.sleep(0.2)
            self.executor.cancelar(jid)
        self.assertEqual(self.store.obter(jid)['estado'], 'cancelado')
        self.assertTrue(os.path.exists(destino))
        imagem.carregar_mapa(imagem.caminho_mapa(destino), 2_000_000)

    def test_destino_sem_mapa_nao_e_sobrescrito(self):
        origem = criar_origem(self.trabalho, 5000)
        destino = os.path.join(self.trabalho, 'img.bin')
        with open(destino, 'wb') as f:
            f.write(b'importante')
        j = self.esperar_estado(self.rodar(origem, destino), 'concluido', 'falhou')
        self.assertEqual(j['estado'], 'falhou')
        with open(destino, 'rb') as f:
            self.assertEqual(f.read(), b'importante')

    def test_no_maximo_dois_jobs_ao_mesmo_tempo(self):
        origens = [criar_origem(self.trabalho, 1_000_000, 'o%d.bin' % i) for i in range(4)]
        with mock.patch('recuperacao.config.atraso_bloco', return_value=0.05):
            ids = [self.rodar(o, os.path.join(self.trabalho, 'd%d.bin' % i)) for i, o in enumerate(origens)]
            time.sleep(0.4)
            estados = [self.store.obter(i)['estado'] for i in ids]
            self.assertEqual(estados.count('rodando'), 2)
            self.assertEqual(estados.count('na_fila'), 2)
            for i in ids:
                self.executor.cancelar(i)

    def test_validar(self):
        o = criar_origem(self.trabalho, 100)
        d = os.path.join(self.trabalho, 'img.bin')
        self.assertIsNone(imagem.validar(o, d, {}))
        self.assertIn('origem', imagem.validar(os.path.join(self.trabalho, 'nao-existe'), d, {}))
        self.assertIn('origem', imagem.validar(self.trabalho, d, {}))                 # pasta não vale
        self.assertIn('própria origem', imagem.validar(o, o, {}))
        self.assertIn('própria origem', imagem.validar(o, os.path.join(self.trabalho, '.', 'origem.bin'), {}))
        self.assertIn('dentro', imagem.validar(o, os.path.join(o, 'x'), {}))
        with open(d, 'wb') as f:
            f.write(b'x')
        self.assertIn('Já existe', imagem.validar(o, d, {}))
        self.assertIn('bloco', imagem.validar(o, os.path.join(self.trabalho, 'novo.bin'), {'bloco': 3}))
        self.assertIn('pasta do destino', imagem.validar(o, os.path.join(self.trabalho, 'sub', 'x.bin'), {}))

    def test_validar_recusa_rede_dispositivo_e_disco_bruto(self):
        o = criar_origem(self.trabalho, 100)
        d = os.path.join(self.trabalho, 'novo.bin')
        barra = chr(92)
        proibidos = [barra * 2 + 'servidor' + barra + 'pasta' + barra + 'a.bin', '//servidor/pasta/a.bin',
                     barra * 2 + '.' + barra + 'PhysicalDrive0', barra * 2 + '?' + barra + 'C:' + barra + 'x.bin',
                     '//./PhysicalDrive0', 'NUL', 'nul.txt', 'CON', 'C:' + barra + 'pasta' + barra + 'aux.img', 'COM1',
                     'lpt9.bin', 'PRN ']
        for p in proibidos:
            if not leitor.eh_dispositivo(p):          # \.\PhysicalDriveN como ORIGEM passou a valer (parte 2.2)
                self.assertIsNotNone(imagem.validar(p, d, {}), p)
            self.assertIsNotNone(imagem.validar(o, p, {}), p)         # como DESTINO, nunca
        self.assertIn('rede', imagem.validar(proibidos[0], d, {}))
        self.assertIn('Disco bruto', imagem.validar(o, proibidos[2], {}))
        self.assertIsNone(imagem.validar(o, os.path.join(self.trabalho, 'console.bin'), {}))     # parecido, mas não é
        self.assertIsNone(imagem.validar(o, os.path.join(self.trabalho, 'com10.bin'), {}))

    def test_retomar_com_outra_origem_ou_tamanho_falha_sem_tocar_na_imagem(self):
        origem = criar_origem(self.trabalho, 10 * 4096, 'a.bin')
        outra = criar_origem(self.trabalho, 10 * 4096, 'b.bin')
        maior = criar_origem(self.trabalho, 11 * 4096, 'c.bin')
        destino = os.path.join(self.trabalho, 'img.bin')
        leituras, original = [], imagem._ler_bloco

        def espiao(fd, pos, n):
            leituras.append(pos)
            return original(fd, pos, n)

        with mock.patch.object(imagem, '_ler_bloco', espiao):
            self.assertEqual(imagem.executar({'origem': origem, 'destino': destino, 'opcoes': {'bloco': 4096}},
                                             FalsoCtrl(pausar_apos=3, leituras=leituras)), 'pausado')
        antes = (sha256_de(destino), sha256_de(destino + '.mapa.json'))
        for diferente in (outra, maior):
            with self.assertRaises(Exception) as ctx:
                imagem.executar({'origem': diferente, 'destino': destino, 'opcoes': {'bloco': 4096}}, FalsoCtrl())
            self.assertIn('nada foi alterado', str(ctx.exception))
            self.assertEqual((sha256_de(destino), sha256_de(destino + '.mapa.json')), antes)
        r = imagem.executar({'origem': origem, 'destino': destino, 'opcoes': {'bloco': 4096}}, FalsoCtrl())
        self.assertEqual(r, 'concluido')
        self.assertEqual(sha256_de(destino), sha256_de(origem))

    def test_imagem_e_mapa_nascem_privados_e_o_temporario_nao_sobrescreve_alheio(self):
        origem = criar_origem(self.trabalho, 3 * 4096)
        destino = os.path.join(self.trabalho, 'img.bin')
        alheio = destino + '.mapa.json.tmp'
        with open(alheio, 'wb') as f:
            f.write(b'alheio')
        criados = []
        original = os.open

        def espiao(caminho, flags, modo=0o777, *a, **k):
            if os.fspath(caminho).startswith(destino):
                criados.append((os.fspath(caminho), flags, modo))
            return original(caminho, flags, modo, *a, **k)

        with mock.patch.object(os, 'open', espiao):
            r = imagem.executar({'origem': origem, 'destino': destino, 'opcoes': {'bloco': 4096}}, FalsoCtrl())
        self.assertEqual(r, 'concluido')
        escritas = [c for c in criados if c[1] & os.O_CREAT]
        self.assertTrue(any(c[0] == destino for c in escritas))
        self.assertTrue(any(c[0].endswith('.tmp') for c in escritas))
        for caminho, flags, modo in escritas:
            self.assertEqual(modo, 0o600, caminho)
            self.assertTrue(flags & os.O_EXCL, caminho)            # nunca sobrescreve o que já existe
        with open(alheio, 'rb') as f:
            self.assertEqual(f.read(), b'alheio')
        self.assertEqual(sorted(n for n in os.listdir(self.trabalho) if n.endswith('.tmp')), ['img.bin.mapa.json.tmp'])
        if os.name != 'nt':
            self.assertEqual(os.stat(destino).st_mode & 0o777, 0o600)

    def test_fsync_do_destino_vem_antes_de_cada_mapa(self):
        origem = criar_origem(self.trabalho, 5 * 4096)
        destino = os.path.join(self.trabalho, 'img.bin')
        ordem, dentro = [], []
        fsync_real, salvar_real = os.fsync, imagem.salvar_mapa

        def fsync(fd):
            if not dentro:
                ordem.append('fsync')
            return fsync_real(fd)

        def salvar(*a, **k):
            ordem.append('mapa')
            dentro.append(1)
            try:
                return salvar_real(*a, **k)
            finally:
                dentro.pop()

        with mock.patch.object(os, 'fsync', fsync), mock.patch.object(imagem, 'salvar_mapa', salvar):
            imagem.executar({'origem': origem, 'destino': destino, 'opcoes': {'bloco': 4096}}, FalsoCtrl())
        posicoes = [i for i, x in enumerate(ordem) if x == 'mapa']
        self.assertGreater(len(posicoes), 2)
        for i in posicoes[1:]:                      # o primeiro mapa (tudo pendente) não tem dado para sincronizar
            self.assertEqual(ordem[i - 1], 'fsync', ordem)

    def test_destino_e_hardlink_da_origem(self):
        o = criar_origem(self.trabalho, 100)
        d = os.path.join(self.trabalho, 'ln.bin')
        try:
            os.link(o, d)
        except OSError:
            self.skipTest('sem hardlink neste sistema')
        self.assertIn('própria origem', imagem.validar(o, d, {}))


class TestExecutorFila(CasoComPasta):
    def setUp(self):
        super().setUp()
        self.store = JobStore(os.path.join(self.dados, 'jobs.db'))
        self.addCleanup(self.store.fechar)
        self.executor = Executor(self.store, {'imagem': imagem})
        self.executor.iniciar()
        self.addCleanup(self.executor.encerrar, 5.0)

    def novo(self, i, tamanho=2_000_000):
        o = criar_origem(self.trabalho, tamanho, 'o%d.bin' % i)
        j = self.store.criar('imagem', o, os.path.join(self.trabalho, 'd%d.bin' % i), {'bloco': 65536})
        self.executor.submeter(j['id'])
        return j['id']

    def estado(self, jid):
        return self.store.obter(jid)['estado']

    def test_job_pausado_e_retomado_na_fila_roda_uma_vez_so(self):
        with mock.patch('recuperacao.config.atraso_bloco', return_value=0.05):
            j1, j2, j3 = self.novo(1), self.novo(2), self.novo(3)
            esperar(lambda: self.estado(j1) == 'rodando' and self.estado(j2) == 'rodando', 10, 0.02)
            self.assertEqual(self.estado(j3), 'na_fila')
            self.executor.pausar(j3)
            self.executor.retomar(j3)                      # o id não pode entrar duas vezes na fila
            self.executor.cancelar(j1)
            self.executor.cancelar(j2)
            esperar(lambda: self.estado(j3) == 'rodando', 10, 0.02)
            time.sleep(0.3)
            eventos = [e['texto'] for e in self.store.eventos(j3)]
            self.assertEqual(eventos.count('Começou a rodar.'), 1, eventos)
            self.executor.pausar(j3)                       # o controle do job não pode ter sido apagado
            self.assertEqual(self.estado(j3), 'pausado')
            self.executor.retomar(j3)
            esperar(lambda: self.estado(j3) == 'rodando', 10, 0.02)
            self.executor.cancelar(j3)
            self.assertEqual(self.estado(j3), 'cancelado')

    def test_entrada_repetida_na_fila_nao_apaga_o_controle_de_quem_roda(self):
        with mock.patch('recuperacao.config.atraso_bloco', return_value=0.05):
            j1 = self.novo(1)
            esperar(lambda: self.estado(j1) == 'rodando', 10, 0.02)
            self.executor._fila.put(j1)                    # entrada repetida que escapou da deduplicação
            self.executor._fila.put(j1)
            time.sleep(0.3)
            self.executor.pausar(j1)
            self.assertEqual(self.estado(j1), 'pausado')

    def test_encerrar_pausa_todos(self):
        with mock.patch('recuperacao.config.atraso_bloco', return_value=0.05):
            ids = [self.novo(i) for i in range(1, 4)]
            esperar(lambda: [self.estado(i) for i in ids].count('rodando') == 2, 10, 0.02)
            self.executor.encerrar(10.0)
        self.assertEqual([self.estado(i) for i in ids], ['pausado'] * 3)


if __name__ == '__main__':
    unittest.main()
