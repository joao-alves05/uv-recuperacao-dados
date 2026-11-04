# -*- coding: utf-8 -*-
"""Parte 2.4: setor ruim como no ddrescue (bisseção, pulo crescente, segunda passada, limite de velocidade) e o resumo do
mapa para o painel. Os "defeitos" são simulados em cima de arquivos temporários; nenhum disco real."""
import json
import os
import time
import unittest
from unittest import mock

import auxiliar
from auxiliar import CasoComPasta, criar_origem, sha256_de

from recuperacao import servico
from recuperacao.tipos import imagem

S = 512


class Ctrl:
    def __init__(self, pausar_apos=None, leituras=None):
        self.eventos, self.ultimo = [], None
        self.pausar_apos, self.leituras = pausar_apos, leituras

    def pausa_pedida(self):
        return self.pausar_apos is not None and len(self.leituras) >= self.pausar_apos

    def cancelamento_pedido(self):
        return False

    def evento(self, t):
        self.eventos.append(t)

    def progresso(self, feitos, total, texto=None, ruins=0):
        self.ultimo = (feitos, total, ruins)

    def checkpoint(self, salvar=None, bytes_novos=0, forcar=False):
        if salvar and (forcar or bytes_novos >= 1 << 22):         # como o de verdade: não grava a cada passo
            salvar()
        return True


class Defeitos:
    """Faz _ler_bloco falhar quando o pedido encosta num setor ruim. `ate` = deixa de falhar a partir da N-ésima leitura
    exata daquele único setor (setor "teimoso" que acaba lendo)."""

    def __init__(self, ruins, teimosos=None):
        self.ruins = set(ruins)
        self.teimosos = dict(teimosos or {})         # setor -> nº da leitura exata em que passa a funcionar
        self.exatas = {}
        self.lidas = []                               # (offset, n, deu_certo)
        self.original = imagem._ler_bloco

    def __call__(self, f, pos, n):
        primeiro, ultimo = pos // S, (pos + n - 1) // S
        ruim = [x for x in range(primeiro, ultimo + 1) if x in self.ruins]
        if ruim and n == S and primeiro in self.teimosos:
            self.exatas[primeiro] = self.exatas.get(primeiro, 0) + 1
            if self.exatas[primeiro] >= self.teimosos[primeiro]:
                ruim = []
                self.ruins.discard(primeiro)                    # curou de vez
        self.lidas.append((pos, n, not ruim))
        if ruim:
            raise OSError('setor ruim de mentira')
        return self.original(f, pos, n)


class Base(CasoComPasta):
    def setUp(self):
        super().setUp()
        self.tam = 2 * 1024 * 1024
        self.origem = criar_origem(self.trabalho, self.tam)
        self.destino = os.path.join(self.trabalho, 'img.bin')
        with open(self.origem, 'rb') as f:
            self.dados = f.read()

    def rodar(self, defeitos, ctrl=None, **opcoes):
        ctrl = ctrl or Ctrl()
        job = {'origem': self.origem, 'destino': self.destino, 'opcoes': dict({'bloco': 65536}, **opcoes)}
        with mock.patch.object(imagem, '_ler_bloco', defeitos):
            return imagem.executar(job, ctrl), ctrl

    def mapa(self):
        return imagem.carregar_mapa(imagem.caminho_mapa(self.destino), self.tam)

    def imagem_bytes(self):
        with open(self.destino, 'rb') as f:
            return f.read()


class TestBisseccao(Base):
    def test_so_os_setores_que_falham_ficam_ruins(self):
        ruins = {100, 101, 3000}
        d = Defeitos(ruins)
        r, ctrl = self.rodar(d)
        self.assertEqual(r, 'concluido')
        self.assertEqual(self.mapa(), [[0, 100 * S, 'ok'], [100 * S, 102 * S, 'ruim'], [102 * S, 3000 * S, 'ok'],
                                       [3000 * S, 3001 * S, 'ruim'], [3001 * S, self.tam, 'ok']])
        img = self.imagem_bytes()
        for x in range(self.tam // S):
            trecho = slice(x * S, (x + 1) * S)
            self.assertEqual(img[trecho], bytes(S) if x in ruins else self.dados[trecho], x)     # vizinhos intactos
        self.assertEqual(ctrl.ultimo[2], 3)                          # setores, não blocos

    def test_eventos_agrupados_por_faixa(self):
        _, ctrl = self.rodar(Defeitos({100, 101, 102, 103, 3000}))
        faixas = [e for e in ctrl.eventos if e.startswith('Setores ilegíveis')]
        self.assertEqual(len(faixas), 2)                              # uma linha por faixa, não por setor
        self.assertIn('4 setores', faixas[0])

    def test_poucas_leituras_na_bisseccao(self):
        d = Defeitos({1000})
        self.rodar(d)
        self.assertLess(len(d.lidas), 40 + 30)                       # 32 blocos + ~2 por nível de bisseção, nunca varredura por setor

    def test_disco_sem_defeito_nao_muda_o_comportamento(self):
        d = Defeitos(set())
        r, _ = self.rodar(d)
        self.assertEqual(r, 'concluido')
        self.assertEqual(self.mapa(), [[0, self.tam, 'ok']])
        self.assertEqual(sha256_de(self.destino), sha256_de(self.origem))


class TestPuloCrescente(Base):
    def test_depois_de_tres_erros_seguidos_pula_areas_e_volta(self):
        bloco = 65536
        area = set(range(2 * bloco // S, 22 * bloco // S))           # 20 blocos seguidos ilegíveis
        d = Defeitos(area)
        r, _ = self.rodar(d)
        self.assertEqual(r, 'concluido')
        primeiras = [(p, ok) for p, n, ok in d.lidas if n == bloco]
        falhas = [i for i, (p, ok) in enumerate(primeiras) if not ok]
        seg = primeiras[falhas[0]:falhas[0] + 3]
        self.assertTrue(all(not ok for _, ok in seg))
        self.assertGreater(primeiras[falhas[0] + 3][0], seg[-1][0] + bloco)       # o 4º pedido pulou uma área
        self.assertEqual(self.mapa(), [[0, 2 * bloco, 'ok'], [2 * bloco, 22 * bloco, 'ruim'], [22 * bloco, self.tam, 'ok']])
        self.assertEqual(self.imagem_bytes()[22 * bloco:], self.dados[22 * bloco:])

    def test_sem_pular_le_um_bloco_atras_do_outro(self):
        bloco = 65536
        d = Defeitos(set(range(2 * bloco // S, 22 * bloco // S)))
        self.rodar(d, pular=False)
        primeiras = [p for p, n, ok in d.lidas if n == bloco]
        self.assertEqual(primeiras[:8], [i * bloco for i in range(8)])


class TestSegundaPassada(Base):
    def test_tentativas_recuperam_setor_teimoso(self):
        # o setor 700 só passa a ler na 3ª leitura exata dele: 1 na bisseção + tentativas na segunda passada
        d = Defeitos({700}, teimosos={700: 3})
        self.rodar(d, tentativas=1)
        self.assertEqual(self.mapa()[1][2], 'ruim')
        os.remove(self.destino)
        os.remove(imagem.caminho_mapa(self.destino))
        d = Defeitos({700}, teimosos={700: 3})
        self.rodar(d, tentativas=3)
        self.assertEqual(self.mapa(), [[0, self.tam, 'ok']])
        self.assertEqual(self.imagem_bytes(), self.dados)

    def test_zero_tentativas_nao_relê(self):
        d = Defeitos({700}, teimosos={700: 2})
        self.rodar(d, tentativas=0)
        self.assertEqual(self.mapa()[1][2], 'ruim')
        self.assertEqual(d.exatas[700], 1)

    def test_ordem_reversa(self):
        d = Defeitos({100, 3000})
        self.rodar(d, tentativas=1)
        exatas = [p // S for p, n, ok in d.lidas if n == S]
        self.assertEqual(exatas[-2:], [3000, 100])                    # as ruins, do fim para o começo, na segunda passada


class TestLimiteDeVelocidade(Base):
    def test_limite_de_bytes_por_segundo(self):
        pequeno = criar_origem(self.trabalho, 300_000, 'pequeno.bin')
        t = time.monotonic()
        imagem.executar({'origem': pequeno, 'destino': os.path.join(self.trabalho, 'a.bin'), 'opcoes': {'bloco': 65536}}, Ctrl())
        livre = time.monotonic() - t
        t = time.monotonic()
        r = imagem.executar({'origem': pequeno, 'destino': os.path.join(self.trabalho, 'b.bin'),
                             'opcoes': {'bloco': 65536, 'limite_velocidade': 500_000}}, Ctrl())
        limitado = time.monotonic() - t
        self.assertEqual(r, 'concluido')
        self.assertGreater(limitado, 0.45)                           # 300 KB a 500 KB/s
        self.assertLess(livre, limitado)
        self.assertEqual(sha256_de(os.path.join(self.trabalho, 'b.bin')), sha256_de(pequeno))


class TestRetomadaNaBisseccao(Base):
    def test_pausa_no_meio_da_bisseccao_e_retomada_nao_rele_o_que_esta_ok(self):
        ruins = {100, 3000}
        lidas = []
        d = Defeitos(ruins)
        original = d.__call__

        def contado(f, pos, n):
            lidas.append(pos)
            return original(f, pos, n)

        ctrl = Ctrl(pausar_apos=40, leituras=lidas)            # 32 blocos + algumas leituras da bisseção
        r, _ = self.rodar(contado, ctrl)
        self.assertEqual(r, 'pausado')
        antes = self.mapa()
        self.assertTrue(any(e == 'suspeito' for _, _, e in antes), antes)         # parou no meio
        self.assertEqual(imagem.fase_do_mapa(imagem.caminho_mapa(self.destino)), 'bissecao')
        prontas = [(a, b) for a, b, e in antes if e == 'ok']
        d2 = Defeitos(ruins)
        r, _ = self.rodar(d2)
        self.assertEqual(r, 'concluido')
        for pos, n, _ in d2.lidas:
            for a, b in prontas:
                self.assertFalse(pos < b and pos + n > a, ('releu faixa pronta', pos, n, a, b))
        self.assertEqual([f for f in self.mapa() if f[2] == 'ruim'], [[100 * S, 101 * S, 'ruim'], [3000 * S, 3001 * S, 'ruim']])


class TestOpcoes(CasoComPasta):
    def test_validar_opcoes(self):
        o = criar_origem(self.trabalho, 100)
        d = os.path.join(self.trabalho, 'novo.bin')
        self.assertIsNone(imagem.validar(o, d, {'tentativas': 3, 'limite_velocidade': 1000, 'pular': False}))
        for ruim in ({'tentativas': -1}, {'tentativas': 21}, {'tentativas': 'a'}, {'tentativas': True}, {'limite_velocidade': -5},
                     {'limite_velocidade': 'x'}, {'limite_velocidade': True}, {'pular': 'sim'}):
            self.assertIsNotNone(imagem.validar(o, d, ruim), ruim)


def mapa_em_disco(pasta, tamanho, faixas, nome='img.bin'):
    destino = os.path.join(pasta, nome)
    imagem.salvar_mapa(imagem.caminho_mapa(destino), faixas, 'x', tamanho)
    return destino


class TestResumoDoMapa(CasoComPasta):
    def test_junta_vizinhas_traduz_estados_e_da_proporcao(self):
        faixas = [[0, 100, 'ok'], [100, 200, 'ok'], [200, 250, 'suspeito'], [250, 300, 'ruim'], [300, 400, 'pulado'],
                  [400, 1000, 'pendente']]
        r = imagem.resumir_mapa(mapa_em_disco(self.trabalho, 1000, faixas))
        self.assertEqual([(f['inicio'], f['fim'], f['estado']) for f in r['faixas']],
                         [(0, 200, 'ok'), (200, 300, 'ruim'), (300, 1000, 'pendente')])
        self.assertAlmostEqual(sum(f['fracao'] for f in r['faixas']), 1.0)
        self.assertEqual(r['totais'], {'ok': 200, 'ruim': 100, 'pendente': 700})
        self.assertFalse(r['resumido'])

    def test_no_maximo_2000_faixas_e_setor_ruim_nunca_some(self):
        tam = 10_000 * 512 * 100                                  # 512 MB, 10.000 pares de faixas
        faixas, pos = [], 0
        for i in range(10_000):
            faixas.append([pos, pos + 512 * 99, 'ok'])
            faixas.append([pos + 512 * 99, pos + 512 * 100, 'ruim' if i % 7 == 0 else 'pendente'])
            pos += 512 * 100
        r = imagem.resumir_mapa(mapa_em_disco(self.trabalho, tam, faixas))
        self.assertTrue(r['resumido'])
        self.assertLessEqual(len(r['faixas']), 2000)
        self.assertEqual(r['faixas'][0]['inicio'], 0)
        self.assertEqual(r['faixas'][-1]['fim'], tam)
        for a, b in zip(r['faixas'], r['faixas'][1:]):
            self.assertEqual(a['fim'], b['inicio'])                # sem buracos
        self.assertIn('ruim', {f['estado'] for f in r['faixas']})

    def test_um_unico_setor_ruim_num_disco_enorme_aparece(self):
        tam = 4_000_000_000_000
        faixas = [[0, 1_234_567_890_432, 'ok'], [1_234_567_890_432, 1_234_567_890_944, 'ruim'], [1_234_567_890_944, tam, 'ok']]
        r = imagem.resumir_mapa(mapa_em_disco(self.trabalho, tam, faixas))
        self.assertEqual([f['estado'] for f in r['faixas']], ['ok', 'ruim', 'ok'])

    def test_sem_mapa_devolve_none(self):
        self.assertIsNone(imagem.resumir_mapa(os.path.join(self.trabalho, 'nada.bin')))


class TestRotaMapa(CasoComPasta):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {'RECDADOS_DADOS': self.dados, 'RECDADOS_PORTA': str(auxiliar.porta_livre())})
        p.start()
        self.addCleanup(p.stop)
        self.ctx = servico.Contexto()
        self.addCleanup(self.ctx.store.fechar)
        self.c = servico.criar_app(self.ctx).test_client()
        self.base = 'http://127.0.0.1:%d' % self.ctx.porta
        self.h = {'X-RecDados-Token': self.ctx.token}

    def test_rota(self):
        destino = os.path.join(self.trabalho, 'img.bin')
        job = self.ctx.store.criar('imagem', os.path.join(self.trabalho, 'o.bin'), destino, {})
        url = '/api/jobs/%s/mapa' % job['id']
        self.assertEqual(self.c.get(url, base_url=self.base).status_code, 401)
        self.assertEqual(self.c.get(url, base_url=self.base, headers=self.h).status_code, 404)        # ainda sem mapa
        self.assertEqual(self.c.get('/api/jobs/j-20990101-abcd/mapa', base_url=self.base, headers=self.h).status_code, 404)
        mapa_em_disco(self.trabalho, 1000, [[0, 600, 'ok'], [600, 700, 'suspeito'], [700, 1000, 'pendente']])
        r = self.c.get(url, base_url=self.base, headers=self.h)
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d['tamanho'], 1000)
        self.assertEqual([f['estado'] for f in d['faixas']], ['ok', 'ruim', 'pendente'])
        self.assertEqual(set(d['faixas'][0]), {'inicio', 'fim', 'estado', 'fracao'})


if __name__ == '__main__':
    unittest.main()
