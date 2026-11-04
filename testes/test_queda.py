# -*- coding: utf-8 -*-
"""Parte 2.5: queda antes do primeiro mapa. O mapa nasce antes da imagem; destino que não é nosso nunca é tocado."""
import json
import os
import unittest
from unittest import mock

from auxiliar import CasoComPasta, criar_origem, sha256_de

from recuperacao.executor import ErroJob
from recuperacao.tipos import imagem


class Queda(BaseException):
    """Simula o processo morrendo no meio (não é Exception: nada do código a captura)."""


class Ctrl:
    def __init__(self):
        self.eventos = []

    def pausa_pedida(self):
        return False

    def cancelamento_pedido(self):
        return False

    def evento(self, t):
        self.eventos.append(t)

    def progresso(self, *a, **k):
        pass

    def checkpoint(self, salvar=None, bytes_novos=0, forcar=False):
        if salvar:
            salvar()
        return True


class TestQuedaAntesDoPrimeiroMapa(CasoComPasta):
    def setUp(self):
        super().setUp()
        self.origem = criar_origem(self.trabalho, 6 * 4096)
        self.destino = os.path.join(self.trabalho, 'img.bin')
        self.mapa = imagem.caminho_mapa(self.destino)
        self.job = {'origem': self.origem, 'destino': self.destino, 'opcoes': {'bloco': 4096}}
        self.tam = os.path.getsize(self.origem)
        self.real = imagem._normal(self.origem)

    def mapa_ja_gravado(self, etapa='criando', dono=imagem.DONO, faixas=None):
        f = faixas or [[0, self.tam, 'pendente']]
        imagem.salvar_mapa(self.mapa, f, self.real, self.tam, etapa=etapa)
        if dono != imagem.DONO:
            with open(self.mapa, encoding='utf-8') as a:
                d = json.load(a)
            d['dono'] = dono
            with open(self.mapa, 'w', encoding='utf-8') as b:
                json.dump(d, b)

    def rodar(self):
        return imagem.executar(self.job, Ctrl())

    def completo(self):
        self.assertEqual(self.rodar(), 'concluido')
        self.assertEqual(sha256_de(self.destino), sha256_de(self.origem))
        self.assertEqual(imagem.carregar_mapa(self.mapa, self.tam, self.real, completo=True), ([[0, self.tam, 'ok']], 'pronto'))

    # --- o mapa nasce antes ---
    def test_o_mapa_ja_existe_quando_a_imagem_e_criada(self):
        vistos = []
        original = imagem._criar_destino

        def espiao(destino, mapa, tamanho, remover_mapa):
            vistos.append(os.path.exists(mapa) and not os.path.exists(destino))
            return original(destino, mapa, tamanho, remover_mapa)

        with mock.patch.object(imagem, '_criar_destino', espiao):
            self.completo()
        self.assertEqual(vistos, [True])

    # --- quedas em cada ponto ---
    def test_queda_depois_do_mapa_e_antes_da_imagem(self):
        with mock.patch.object(imagem, '_criar_destino', side_effect=Queda()):
            with self.assertRaises(Queda):
                self.rodar()
        self.assertTrue(os.path.exists(self.mapa) and not os.path.exists(self.destino))
        self.completo()                                   # a retomada cria a imagem e termina

    def test_queda_depois_de_criar_a_imagem_vazia(self):
        with mock.patch('os.ftruncate', side_effect=Queda()):
            with self.assertRaises(Queda):
                self.rodar()
        self.assertEqual(os.path.getsize(self.destino), 0)
        self.assertEqual(imagem.carregar_mapa(self.mapa, self.tam, completo=True)[1], 'criando')
        self.completo()

    def test_queda_depois_do_tamanho_e_antes_do_segundo_mapa(self):
        gravacoes = []
        original = imagem.salvar_mapa

        def so_a_primeira(*a, **k):
            gravacoes.append(k.get('etapa', 'pronto'))
            if len(gravacoes) == 2:
                raise Queda()
            return original(*a, **k)

        with mock.patch.object(imagem, 'salvar_mapa', so_a_primeira):
            with self.assertRaises(Queda):
                self.rodar()
        self.assertEqual(gravacoes, ['criando', 'pronto'])
        self.assertEqual(os.path.getsize(self.destino), self.tam)          # a imagem já tem o tamanho, mas o mapa ainda diz "criando"
        # não dá para provar que aquele arquivo é nosso: por segurança não segue (e não mexe em nada)
        antes = (sha256_de(self.destino), sha256_de(self.mapa))
        with self.assertRaises(ErroJob) as c:
            self.rodar()
        self.assertIn('não dá para confirmar', str(c.exception))
        self.assertEqual((sha256_de(self.destino), sha256_de(self.mapa)), antes)

    # --- destino que não é nosso nunca é tocado ---
    def test_destino_alheio_sem_mapa_nao_e_tocado(self):
        with open(self.destino, 'wb') as f:
            f.write(b'importante')
        with self.assertRaises(ErroJob):
            self.rodar()
        with open(self.destino, 'rb') as f:
            self.assertEqual(f.read(), b'importante')
        self.assertFalse(os.path.exists(self.mapa))

    def test_destino_alheio_com_mapa_nosso_de_antes_da_imagem_nao_e_tocado(self):
        """Caiu depois do mapa; antes de voltar, o usuário criou outro arquivo com esse nome."""
        self.mapa_ja_gravado()
        for conteudo in (b'importante', bytes(self.tam)):                    # com outro tamanho ou com o mesmo tamanho
            with open(self.destino, 'wb') as f:
                f.write(conteudo)
            antes = sha256_de(self.mapa)
            with self.assertRaises(ErroJob):
                self.rodar()
            with open(self.destino, 'rb') as f:
                self.assertEqual(f.read(), conteudo)
            self.assertEqual(sha256_de(self.mapa), antes)

    def test_arquivo_alheio_aparece_no_instante_da_criacao(self):
        original = os.open

        def com_intruso(caminho, flags, *a, **k):
            if os.fspath(caminho) == self.destino and flags & os.O_EXCL and not os.path.exists(self.destino):
                with open(self.destino, 'wb') as f:
                    f.write(b'intruso')
            return original(caminho, flags, *a, **k)

        with mock.patch.object(os, 'open', com_intruso):
            with self.assertRaises(ErroJob) as c:
                self.rodar()
        self.assertIn('Apareceu um arquivo', str(c.exception))
        with open(self.destino, 'rb') as f:
            self.assertEqual(f.read(), b'intruso')
        self.assertFalse(os.path.exists(self.mapa))        # o mapa que criamos não fica para fazer o intruso parecer nosso

    def test_mapa_de_outro_dono_ou_de_outra_origem_nao_segue(self):
        self.mapa_ja_gravado(dono='outro-programa')
        with self.assertRaises(ErroJob) as c:
            self.rodar()
        self.assertIn('não foi criado por este programa', str(c.exception))
        self.assertFalse(os.path.exists(self.destino))
        os.remove(self.mapa)
        imagem.salvar_mapa(self.mapa, [[0, self.tam, 'pendente']], 'C:\\outra\\origem.bin', self.tam, etapa='criando')
        with self.assertRaises(ErroJob):
            self.rodar()
        self.assertFalse(os.path.exists(self.destino))

    def test_mapa_pronto_com_a_imagem_apagada_nao_recria(self):
        self.mapa_ja_gravado(etapa='pronto', faixas=[[0, 4096, 'ok'], [4096, self.tam, 'pendente']])
        with self.assertRaises(ErroJob) as c:
            self.rodar()
        self.assertIn('não está mais no destino', str(c.exception))
        self.assertFalse(os.path.exists(self.destino))

    def test_retomada_normal_continua_funcionando(self):
        class Pausa(Ctrl):
            def __init__(self):
                super().__init__()
                self.n = 0

            def pausa_pedida(self):
                return self.n >= 2

            def progresso(self, *a, **k):
                self.n += 1
        self.assertEqual(imagem.executar(self.job, Pausa()), 'pausado')
        self.assertEqual(imagem.carregar_mapa(self.mapa, self.tam, completo=True)[1], 'pronto')
        self.completo()

    def test_validar_aceita_retomar_so_com_mapa(self):
        self.mapa_ja_gravado()
        self.assertIsNone(imagem.validar(self.origem, self.destino, {}, retomada=True))
        self.assertIn('mapa', imagem.validar(self.origem, self.destino, {}))     # job novo no mesmo nome continua recusado


if __name__ == '__main__':
    unittest.main()
