# -*- coding: utf-8 -*-
"""Revisão final, item 1: a origem de um trabalho é conferida (tamanho + SHA-256 das pontas) antes de recuperar, de mostrar
prévia e de retomar. Outro disco no mesmo caminho nunca é lido como se fosse o da busca."""
import os
import unittest
from unittest import mock

import amostras as A
from auxiliar import CasoComPasta, criar_origem, esperar
from test_varredura import BaseVarredura, montar_imagem

from recuperacao import identidade
from recuperacao import rotas_achados

MIB = 1 << 20


def trocar_conteudo(caminho, onde, byte=b'\xAA'):
    """Escreve num arquivo que representa "outro disco no mesmo caminho"."""
    with open(caminho, 'r+b') as f:
        f.seek(onde)
        f.write(byte)


class TestImpressao(CasoComPasta):
    def test_muda_com_tamanho_inicio_ou_fim_e_aceita_arquivo_pequeno(self):
        base = criar_origem(self.trabalho, 3 * MIB)
        a = identidade.impressao(base)
        self.assertEqual(set(a), {'tamanho', 'inicio', 'fim'})
        self.assertIsNone(identidade.conferir(base, a))
        for onde in (0, MIB - 1, 3 * MIB - 1, 3 * MIB - MIB):
            copia = os.path.join(self.trabalho, 'c%d.bin' % onde)
            with open(base, 'rb') as f, open(copia, 'wb') as g:
                g.write(f.read())
            with open(copia, 'r+b') as g:
                g.seek(onde)
                g.write(bytes([1, 2]))
            self.assertEqual(identidade.conferir(copia, a), identidade.MSG_MUDOU, onde)
        maior = os.path.join(self.trabalho, 'maior.bin')
        with open(base, 'rb') as f, open(maior, 'wb') as g:
            g.write(f.read() + b'x')
        self.assertEqual(identidade.conferir(maior, a), identidade.MSG_MUDOU)
        pequeno = os.path.join(self.trabalho, 'p.bin')
        with open(pequeno, 'wb') as f:
            f.write(b'oi')
        self.assertIsNone(identidade.conferir(pequeno, identidade.impressao(pequeno)))

    def test_meio_do_arquivo_nao_entra_e_sem_guarda_nao_confere(self):
        base = criar_origem(self.trabalho, 4 * MIB)
        a = identidade.impressao(base)
        trocar_conteudo(base, 2 * MIB)                                 # só as pontas contam
        self.assertIsNone(identidade.conferir(base, a))
        self.assertIsNone(identidade.conferir(base, None))
        self.assertIsNone(identidade.conferir(base, {}))

    def test_origem_que_sumiu(self):
        a = identidade.impressao(criar_origem(self.trabalho, 1000))
        self.assertIsNone(identidade.impressao(os.path.join(self.trabalho, 'nao-existe')))
        self.assertIn('conectada', identidade.conferir(os.path.join(self.trabalho, 'nao-existe'), a))


class TestOrigemTrocada(BaseVarredura):
    def varrer_pronto(self):
        caminho, info, _ = montar_imagem(self.trabalho)
        job = self.varrer(caminho, bloco=65536)
        fim = self.esperar_estado(job['id'], 'concluido')
        return caminho, job, fim

    def pedir_recuperar(self, job, nome='saida'):
        return self.req('POST', '/api/jobs/%s/recuperar' % job['id'],
                        {'ids': 'todos', 'destino': os.path.join(self.pasta, nome)})

    def test_a_impressao_e_gravada_na_criacao_e_nao_pode_ser_forjada(self):
        caminho, info, _ = montar_imagem(self.trabalho)
        r = self.req('POST', '/api/jobs', {'tipo': 'varredura', 'origem': caminho,
                                           'opcoes': {'bloco': 65536, 'impressao_origem': {'tamanho': 1}}})
        job = r.get_json()
        self.assertEqual(job['opcoes']['impressao_origem'], identidade.impressao(caminho))
        self.esperar_estado(job['id'], 'concluido')
        origem = criar_origem(self.trabalho, 100000)
        im = self.req('POST', '/api/jobs', {'tipo': 'imagem', 'origem': origem,
                                            'destino': os.path.join(self.trabalho, 'i.bin')}).get_json()
        self.assertEqual(im['opcoes']['impressao_origem'], identidade.impressao(origem))
        self.esperar_estado(im['id'], 'concluido')

    def test_recuperar_recusa_origem_trocada_e_aceita_a_mesma(self):
        caminho, job, _ = self.varrer_pronto()
        ok = self.pedir_recuperar(job, 'a')
        self.assertEqual(ok.status_code, 201, ok.get_json())
        self.esperar_estado(ok.get_json()['id'], 'concluido')
        trocar_conteudo(caminho, 512 * 3)                              # "outro disco" no mesmo caminho
        r = self.pedir_recuperar(job, 'b')
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()['erro'], identidade.MSG_MUDOU)
        self.assertFalse(os.path.exists(os.path.join(self.pasta, 'b')))
        self.assertEqual([j['tipo'] for j in self.req('GET', '/api/jobs').get_json()].count('recuperar'), 1)

    def test_previa_recusa_origem_trocada_mas_icone_nao_le_a_origem(self):
        caminho, job, _ = self.varrer_pronto()
        fotos = [a for a in self.achados_do_banco(job['id']) if a['grupo'] == 'foto' and a['estado'] == 'bom']
        docs = [a for a in self.achados_do_banco(job['id']) if a['tipo'] == 'PDF']
        self.assertEqual(self.req('GET', '/api/achados/%d/previa' % fotos[0]['id']).mimetype, 'image/jpeg')
        trocar_conteudo(caminho, 0)
        with mock.patch.object(rotas_achados, 'TTL_CONFERENCIA', 0):
            r = self.req('GET', '/api/achados/%d/previa' % fotos[0]['id'])
            self.assertEqual((r.status_code, r.get_json()['erro']), (409, identidade.MSG_MUDOU))
            self.assertEqual(self.req('GET', '/api/achados/%d/previa' % docs[0]['id']).mimetype, 'image/svg+xml')

    def test_previa_confere_uma_vez_por_janela_de_tempo(self):
        caminho, job, _ = self.varrer_pronto()
        fotos = [a for a in self.achados_do_banco(job['id']) if a['grupo'] == 'foto']
        with mock.patch.object(identidade, 'impressao', wraps=identidade.impressao) as impressao:
            for a in fotos * 3:
                self.req('GET', '/api/achados/%d/previa' % a['id'])
        self.assertEqual(impressao.call_count, 1)

    def test_retomar_varredura_com_origem_trocada_e_recusado(self):
        caminho, info, _ = montar_imagem(self.trabalho)
        with mock.patch.dict(os.environ, {'RECDADOS_ATRASO_BLOCO': '0.05'}):
            job = self.varrer(caminho, bloco=65536)
            esperar(lambda: self.req('GET', '/api/jobs/' + job['id']).get_json()['progresso'] > 0.2, 30, 0.02)
            self.assertEqual(self.req('POST', '/api/jobs/%s/pausar' % job['id']).status_code, 200)
            trocar_conteudo(caminho, 0, b'\x00\x01')
            r = self.req('POST', '/api/jobs/%s/retomar' % job['id'])
            self.assertEqual((r.status_code, r.get_json()['erro']), (409, identidade.MSG_MUDOU))
            self.assertEqual(self.req('GET', '/api/jobs/' + job['id']).get_json()['estado'], 'pausado')

    def test_retomar_varredura_com_a_mesma_origem_continua_a_funcionar(self):
        caminho, info, _ = montar_imagem(self.trabalho)
        with mock.patch.dict(os.environ, {'RECDADOS_ATRASO_BLOCO': '0.05'}):
            job = self.varrer(caminho, bloco=65536)
            esperar(lambda: self.req('GET', '/api/jobs/' + job['id']).get_json()['progresso'] > 0.2, 30, 0.02)
            self.req('POST', '/api/jobs/%s/pausar' % job['id'])
            self.assertEqual(self.req('POST', '/api/jobs/%s/retomar' % job['id']).status_code, 200)
            self.esperar_estado(job['id'], 'concluido')

    def test_retomar_imagem_com_origem_trocada_e_recusado(self):
        origem = criar_origem(self.trabalho, 3 * MIB)
        destino = os.path.join(self.trabalho, 'img.bin')
        with mock.patch.dict(os.environ, {'RECDADOS_ATRASO_BLOCO': '0.05'}):
            j = self.req('POST', '/api/jobs', {'tipo': 'imagem', 'origem': origem, 'destino': destino,
                                               'opcoes': {'bloco': 65536}}).get_json()
            esperar(lambda: self.req('GET', '/api/jobs/' + j['id']).get_json()['progresso'] > 0.2, 30, 0.02)
            self.req('POST', '/api/jobs/%s/pausar' % j['id'])
            trocar_conteudo(origem, 3 * MIB - 10, b'\x07' * 4)
            r = self.req('POST', '/api/jobs/%s/retomar' % j['id'])
            self.assertEqual(r.status_code, 409)

    def test_retomar_recuperar_confere_a_origem_da_varredura(self):
        caminho, job, _ = self.varrer_pronto()
        ok = self.pedir_recuperar(job, 'c').get_json()
        self.esperar_estado(ok['id'], 'concluido')
        # recuperar concluído não retoma; um pausado sim: simula pausado e troca a origem
        self.ctx.store.transicao(ok['id'], ('concluido',), 'pausado')
        trocar_conteudo(caminho, 0, b'\x01\x02\x03')
        r = self.req('POST', '/api/jobs/%s/retomar' % ok['id'])
        self.assertEqual((r.status_code, r.get_json()['erro']), (409, identidade.MSG_MUDOU))

    def test_job_antigo_sem_impressao_continua_funcionando(self):
        caminho, info, _ = montar_imagem(self.trabalho)
        job = self.ctx.store.criar('varredura', caminho, 'varredura#antigo', {'modo': 'rapida', 'bloco': 65536})
        self.assertIsNone(identidade.da_origem_do_job(self.ctx.store, job))
        self.ctx.executor.submeter(job['id'])
        self.esperar_estado(job['id'], 'concluido')
        self.assertEqual(self.pedir_recuperar(job, 'd').status_code, 201)


if __name__ == '__main__':
    unittest.main()
