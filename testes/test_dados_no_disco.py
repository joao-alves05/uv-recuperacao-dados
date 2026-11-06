# -*- coding: utf-8 -*-
"""Revisão final, item 6: aviso forte quando a pasta de dados do serviço (jobs.db, logs) está no disco que será recuperado.
Nenhum disco é aberto: o leitor é trocado por um que recusa, e o mapeamento de discos é falso."""
import os
import unittest
from unittest import mock

from test_servico import Base

from recuperacao import servico, unidade
from recuperacao.leitor import ErroLeitor

DISCO = '\\\\.\\PhysicalDrive97'          # nome que não existe; nada é aberto


class MapaFalso:
    def __init__(self, origem, destino):
        self.origem, self.destino = origem, destino

    def discos_da_origem(self, _):
        return self.origem

    def discos_do_destino(self, _):
        return self.destino


class TestAvisoPastaDeDados(Base):
    def criar(self, tipo, origem=DISCO, mapa=None, **extra):
        corpo = {'tipo': tipo, 'origem': origem}
        corpo.update(extra)
        with mock.patch('recuperacao.leitor.abrir', side_effect=ErroLeitor('sem disco de verdade nos testes')), \
                mock.patch.object(unidade, 'MAPA', mapa or MapaFalso({'97'}, {'97'})):
            return self.req('POST', '/api/jobs', corpo)

    def test_varredura_do_disco_onde_estao_os_dados_avisa_forte_mas_cria_o_trabalho(self):
        r = self.criar('varredura')
        self.assertEqual(r.status_code, 201, r.get_json())
        job = r.get_json()
        self.assertEqual(job['aviso'], servico.AVISO_PASTA_DE_DADOS)
        self.assertIn('mesmo disco', job['aviso'])
        eventos = [e['texto'] for e in self.req('GET', '/api/jobs/' + job['id']).get_json()['eventos']]
        self.assertIn(servico.AVISO_PASTA_DE_DADOS, eventos)

    def test_disco_diferente_nao_avisa(self):
        r = self.criar('varredura', mapa=MapaFalso({'97'}, {'1'}))
        self.assertEqual(r.status_code, 201)
        self.assertNotIn('aviso', r.get_json())

    def test_nao_saber_em_que_disco_fica_nao_gera_aviso_falso(self):
        r = self.criar('varredura', mapa=MapaFalso({'97'}, None))
        self.assertEqual(r.status_code, 201)
        self.assertNotIn('aviso', r.get_json())

    def test_origem_que_e_arquivo_nunca_avisa_nem_consulta_discos(self):
        origem = os.path.join(self.trabalho, 'o.img')
        with open(origem, 'wb') as f:
            f.write(bytes(4096))
        with mock.patch.object(unidade, 'MAPA', mock.Mock(side_effect=AssertionError('consultou discos'))):
            r = self.req('POST', '/api/jobs', {'tipo': 'varredura', 'origem': origem})
        self.assertEqual(r.status_code, 201)
        self.assertNotIn('aviso', r.get_json())

    def test_pasta_de_dados_e_a_usada_na_comparacao(self):
        vistos = []

        class Espia(MapaFalso):
            def discos_do_destino(self, d):
                vistos.append(d)
                return {'97'}
        self.criar('varredura', mapa=Espia({'97'}, None))
        self.assertTrue(vistos and os.path.normcase(os.path.abspath(vistos[0])).startswith(os.path.normcase(self.dados)))


if __name__ == '__main__':
    unittest.main()
