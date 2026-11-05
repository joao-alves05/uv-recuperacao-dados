# -*- coding: utf-8 -*-
"""Imagem: falha na conferência final não derruba a imagem pronta; espaço livre e limite do FAT32 são conferidos
antes de criar o destino."""
import os
import unittest
from collections import namedtuple
from unittest import mock

from auxiliar import CasoComPasta, criar_origem, esperar

from recuperacao.executor import Executor
from recuperacao.jobs import JobStore
from recuperacao.tipos import imagem

Uso = namedtuple('Uso', 'total used free')


class Base(CasoComPasta):
    def setUp(self):
        super().setUp()
        self.store = JobStore(os.path.join(self.dados, 'jobs.db'))
        self.addCleanup(self.store.fechar)
        self.executor = Executor(self.store, {'imagem': imagem})
        self.executor.iniciar()
        self.addCleanup(self.executor.encerrar, 5.0)

    def rodar(self, origem, destino):
        j = self.store.criar('imagem', origem, destino, {'bloco': 65536})
        self.executor.submeter(j['id'])
        return esperar(lambda: (x := self.store.obter(j['id']))['estado'] in ('concluido', 'falhou') and x, 30, 0.02, 'job')


class TestConferenciaFinal(Base):
    def test_erro_de_leitura_na_conferencia_conclui_com_aviso(self):
        origem = criar_origem(self.trabalho, 300_000)
        destino = os.path.join(self.trabalho, 'img.bin')
        with mock.patch.object(imagem, '_sha256', side_effect=OSError(5, 'erro de E/S')):
            job = self.rodar(origem, destino)
        self.assertEqual(job['estado'], 'concluido', job['erro'])
        textos = ' '.join(e['texto'] for e in self.store.eventos(job['id']))
        self.assertIn('conferência por SHA-256 não pôde terminar', textos)
        with open(origem, 'rb') as a, open(destino, 'rb') as b:
            self.assertEqual(a.read(), b.read())


class TestEspaco(Base):
    def test_sem_espaco_livre_falha_antes_de_criar_o_destino(self):
        origem = criar_origem(self.trabalho, 300_000)
        destino = os.path.join(self.trabalho, 'img.bin')
        with mock.patch('shutil.disk_usage', return_value=Uso(10 ** 9, 10 ** 9 - 1000, 1000)):
            job = self.rodar(origem, destino)
        self.assertEqual(job['estado'], 'falhou')
        self.assertIn('espaço livre', job['erro'])
        self.assertFalse(os.path.exists(destino))
        self.assertFalse(os.path.exists(destino + '.mapa.json'))

    def test_limite_de_4_gib_do_fat32(self):
        destino = os.path.join(self.trabalho, 'img.bin')
        grande = Uso(10 ** 13, 0, 10 ** 13)
        with mock.patch('shutil.disk_usage', return_value=grande):
            with mock.patch.object(imagem, '_sistema_de_arquivos', return_value='FAT32'):
                self.assertIn('FAT32', imagem.conferir_espaco(destino, 5 * 1024 ** 3))
                self.assertIsNone(imagem.conferir_espaco(destino, 1024 ** 3))
            with mock.patch.object(imagem, '_sistema_de_arquivos', return_value='NTFS'):
                self.assertIsNone(imagem.conferir_espaco(destino, 5 * 1024 ** 3))

    def test_espaco_suficiente_passa(self):
        origem = criar_origem(self.trabalho, 100_000)
        self.assertEqual(self.rodar(origem, os.path.join(self.trabalho, 'ok.bin'))['estado'], 'concluido')


if __name__ == '__main__':
    unittest.main()
