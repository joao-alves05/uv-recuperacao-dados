# -*- coding: utf-8 -*-
"""A origem nunca é escrita: hash e mtime iguais, e nenhuma abertura dela com modo de escrita."""
import builtins
import io
import os
import time
import unittest
from unittest import mock

from auxiliar import CasoComPasta, criar_origem, esperar, sha256_de

from recuperacao.executor import Executor
from recuperacao.jobs import JobStore
from recuperacao.tipos import imagem

FLAGS_ESCRITA = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | os.O_EXCL


class Espiao:
    """Registra todo open/os.open e toda operação que altera arquivo, para o caminho que nos interessa."""

    def __init__(self, alvo):
        self.alvo = os.path.normcase(os.path.abspath(alvo))
        self.aberturas = []        # (funcao, argumento de modo)
        self.mutacoes = []
        self._patches = []

    def _eh_alvo(self, caminho):
        try:
            return os.path.normcase(os.path.abspath(os.fsdecode(caminho))) == self.alvo
        except (TypeError, ValueError):
            return False

    def __enter__(self):
        orig_open, orig_osopen, orig_io_open = builtins.open, os.open, io.open

        def open_(arq, modo='r', *a, **k):
            if self._eh_alvo(arq):
                self.aberturas.append(('open', modo))
            return orig_open(arq, modo, *a, **k)

        def os_open(caminho, flags, *a, **k):
            if self._eh_alvo(caminho):
                self.aberturas.append(('os.open', flags))
            return orig_osopen(caminho, flags, *a, **k)

        def io_open(arq, modo='r', *a, **k):
            if self._eh_alvo(arq):
                self.aberturas.append(('io.open', modo))
            return orig_io_open(arq, modo, *a, **k)

        self._patches = [mock.patch.object(builtins, 'open', open_), mock.patch.object(os, 'open', os_open),
                         mock.patch.object(io, 'open', io_open)]
        for nome in ('truncate', 'utime', 'chmod', 'remove', 'unlink', 'rename', 'replace'):
            original = getattr(os, nome)

            def vigia(*args, _n=nome, _o=original, **k):
                if any(self._eh_alvo(a) for a in args[:2] if isinstance(a, (str, bytes, os.PathLike))):
                    self.mutacoes.append(_n)
                return _o(*args, **k)
            self._patches.append(mock.patch.object(os, nome, vigia))
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in reversed(self._patches):
            p.stop()

    def escritas(self):
        ruins = []
        for funcao, modo in self.aberturas:
            if funcao == 'os.open':
                if modo & FLAGS_ESCRITA:
                    ruins.append((funcao, modo))
            elif set(modo) & set('wax+'):
                ruins.append((funcao, modo))
        return ruins + [('mutacao', m) for m in self.mutacoes]


class TestSomenteLeitura(CasoComPasta):
    def setUp(self):
        super().setUp()
        self.store = JobStore(os.path.join(self.dados, 'jobs.db'))
        self.addCleanup(self.store.fechar)
        self.executor = Executor(self.store, {'imagem': imagem})
        self.executor.iniciar()
        self.addCleanup(self.executor.encerrar, 5.0)

    def foto(self, caminho):
        st = os.stat(caminho)
        return sha256_de(caminho), st.st_mtime_ns, st.st_size

    def esperar_estado(self, job_id, *estados):
        return esperar(lambda: (j := self.store.obter(job_id))['estado'] in estados and j, 20, 0.02, 'estado')

    def test_origem_sai_byte_a_byte_igual_inclusive_mtime(self):
        origem = criar_origem(self.trabalho, 900_000)
        os.utime(origem, (1_600_000_000, 1_600_000_000))         # mtime antigo e conhecido
        antes = self.foto(origem)
        destino = os.path.join(self.trabalho, 'img.bin')
        j = self.store.criar('imagem', origem, destino, {'bloco': 65536})
        self.executor.submeter(j['id'])
        self.assertEqual(self.esperar_estado(j['id'], 'concluido', 'falhou')['estado'], 'concluido')
        self.assertEqual(self.foto(origem), antes)
        self.assertEqual(sha256_de(destino), antes[0])

    def test_nenhuma_abertura_da_origem_com_escrita_em_todo_o_ciclo(self):
        """Roda criar, pausar, retomar, cancelar e concluir, interceptando open/os.open."""
        origem = criar_origem(self.trabalho, 1_500_000)
        antes = self.foto(origem)
        d1, d2 = os.path.join(self.trabalho, 'a.bin'), os.path.join(self.trabalho, 'b.bin')
        with Espiao(origem) as espiao, mock.patch('recuperacao.config.atraso_bloco', return_value=0.03):
            j1 = self.store.criar('imagem', origem, d1, {'bloco': 65536})
            self.executor.submeter(j1['id'])
            self.esperar_estado(j1['id'], 'rodando')
            time.sleep(0.2)
            self.executor.pausar(j1['id'])
            self.executor.retomar(j1['id'])
            self.assertEqual(self.esperar_estado(j1['id'], 'concluido', 'falhou')['estado'], 'concluido')
            j2 = self.store.criar('imagem', origem, d2, {'bloco': 65536})
            self.executor.submeter(j2['id'])
            self.esperar_estado(j2['id'], 'rodando')
            time.sleep(0.1)
            self.executor.cancelar(j2['id'])
        self.assertTrue(espiao.aberturas, 'o espião não viu nenhuma abertura: o teste não prova nada')
        self.assertEqual(espiao.escritas(), [])
        self.assertTrue(all(f == 'os.open' for f, _ in espiao.aberturas))
        self.assertEqual(self.foto(origem), antes)

    def test_o_espiao_enxerga_escrita_se_houver(self):
        """Controle: com uma abertura de escrita de propósito, o interceptador tem de acusar."""
        origem = criar_origem(self.trabalho, 100)
        with Espiao(origem) as espiao:
            with open(origem, 'r+b'):
                pass
            fd = os.open(origem, os.O_RDWR | getattr(os, 'O_BINARY', 0))
            os.close(fd)
            os.utime(origem, (1, 1))
        self.assertEqual(len(espiao.escritas()), 3)

    def test_origem_da_api_com_destino_igual_nao_toca_em_nada(self):
        origem = criar_origem(self.trabalho, 4096)
        antes = self.foto(origem)
        for destino in (origem, os.path.join(origem, 'x'), os.path.join(self.trabalho, '.', 'origem.bin')):
            self.assertIsNotNone(imagem.validar(origem, destino, {}))
        j = self.store.criar('imagem', origem, origem, {})        # mesmo se alguém furar a validação da rota
        self.executor.submeter(j['id'])
        self.assertEqual(self.esperar_estado(j['id'], 'falhou', 'concluido')['estado'], 'falhou')
        self.assertEqual(self.foto(origem), antes)
        self.assertFalse(os.path.exists(origem + '.mapa.json'))

    def test_codigo_nao_tem_modo_de_escrita_na_abertura_da_origem(self):
        """A única abertura da origem no código do tipo é com O_RDONLY."""
        import inspect
        fonte = inspect.getsource(imagem.executar)
        self.assertIn('leitor.abrir(origem)', fonte)
        self.assertEqual(fonte.count('os.open(origem'), 0)
        self.assertEqual(fonte.count('leitor.abrir(origem)'), 1)


if __name__ == '__main__':
    unittest.main()
