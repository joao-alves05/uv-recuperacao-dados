# -*- coding: utf-8 -*-
"""Parte 2.3: destino no mesmo disco físico da origem é recusado. Mapeamento falso; nunca abre volume de verdade."""
import ctypes
import os
import struct
import unittest
from unittest import mock

import auxiliar
from auxiliar import CasoComPasta

from recuperacao import leitor, servico, unidade
from recuperacao.tipos import imagem

DISCO = '\\\\.\\PhysicalDrive7'


class MapaFalso:
    def __init__(self, origem=None, destino=None, erro=None):
        self.origem, self.destino, self.erro = origem, destino, erro
        self.pedidos = []

    def discos_da_origem(self, o):
        self.pedidos.append(('origem', o))
        if self.erro:
            raise self.erro
        return self.origem

    def discos_do_destino(self, d):
        self.pedidos.append(('destino', d))
        return self.destino


class TestConferir(unittest.TestCase):
    def conferir(self, **kw):
        with mock.patch.object(unidade, 'MAPA', MapaFalso(**kw)):
            return unidade.conferir(DISCO, 'x')

    def test_mesmo_disco_e_recusado(self):
        self.assertEqual(self.conferir(origem={'7'}, destino={'7'}), unidade.MSG_MESMO_DISCO)
        self.assertIn('mesmo disco da origem', unidade.MSG_MESMO_DISCO)

    def test_discos_diferentes_passam(self):
        self.assertIsNone(self.conferir(origem={'7'}, destino={'0'}))

    def test_volume_espalhado_em_varios_discos(self):
        self.assertEqual(self.conferir(origem={'1', '2'}, destino={'2', '3'}), unidade.MSG_MESMO_DISCO)

    def test_sem_certeza_recusa(self):
        for kw in ({'origem': {'7'}, 'destino': None}, {'origem': None, 'destino': {'0'}}, {'origem': set(), 'destino': {'0'}},
                   {'origem': {'7'}, 'destino': {'0'}, 'erro': OSError('sem acesso')}):
            self.assertEqual(self.conferir(**kw), unidade.MSG_SEM_CERTEZA, kw)


class TestIntegracao(CasoComPasta):
    def test_validar_recusa_destino_no_disco_da_origem_e_aceita_em_outro(self):
        d = os.path.join(self.trabalho, 'img.bin')
        with mock.patch.object(unidade, 'MAPA', MapaFalso({'7'}, {'7'})):
            self.assertEqual(imagem.validar(DISCO, d, {}), unidade.MSG_MESMO_DISCO)
        with mock.patch.object(unidade, 'MAPA', MapaFalso({'7'}, {'0'})):
            self.assertIsNone(imagem.validar(DISCO, d, {}))

    def test_a_checagem_vale_tambem_na_retomada(self):
        d = os.path.join(self.trabalho, 'img.bin')
        with mock.patch.object(unidade, 'MAPA', MapaFalso({'7'}, {'7'})):
            self.assertEqual(imagem.validar(DISCO, d, {}, retomada=True), unidade.MSG_MESMO_DISCO)

    def test_arquivo_comum_na_mesma_unidade_continua_valendo(self):
        origem = auxiliar.criar_origem(self.trabalho, 100)
        with mock.patch.object(unidade, 'MAPA', MapaFalso({'7'}, {'7'})) as m:
            self.assertIsNone(imagem.validar(origem, os.path.join(self.trabalho, 'img.bin'), {}))

    def test_rota_devolve_400_e_nao_cria_job(self):
        with mock.patch.dict(os.environ, {'RECDADOS_DADOS': self.dados, 'RECDADOS_PORTA': str(auxiliar.porta_livre())}):
            ctx = servico.Contexto()
            self.addCleanup(ctx.store.fechar)
            c = servico.criar_app(ctx).test_client()
            base = 'http://127.0.0.1:%d' % ctx.porta
            with mock.patch.object(unidade, 'MAPA', MapaFalso({'7'}, {'7'})):
                r = c.post('/api/jobs', json={'tipo': 'imagem', 'origem': DISCO, 'destino': os.path.join(self.trabalho, 'i.bin')},
                           base_url=base, headers={'X-RecDados-Token': ctx.token})
            self.assertEqual(r.status_code, 400)
            self.assertIn('O destino está no mesmo disco da origem', r.get_json()['erro'])
            self.assertEqual(ctx.store.listar(), [])


class ApiFalsaVolume:
    def __init__(self, extensoes):
        self.ext, self.aberturas, self.fechados = extensoes, [], []

    def CreateFileW(self, caminho, acesso, compartilha, disposicao, flags):
        self.aberturas.append((caminho, acesso, compartilha, disposicao, flags))
        return 9

    def extensoes(self, h):
        return self.ext

    def fechar(self, h):
        self.fechados.append(h)


@unittest.skipUnless(os.name == 'nt', 'caminho do Windows')
class TestWindows(CasoComPasta):
    def test_disco_fisico_da_origem_e_volume_do_destino(self):
        api = ApiFalsaVolume([0])
        s = unidade.Sistema(api=api)
        self.assertEqual(s.discos_da_origem('\\\\.\\PhysicalDrive3'), {'3'})
        letra = os.path.splitdrive(os.path.realpath(self.trabalho))[0].upper()
        self.assertEqual(s.discos_do_destino(os.path.join(self.trabalho, 'img.bin')), {'0'})
        caminho, acesso, compartilha, disposicao, _ = api.aberturas[0]
        self.assertEqual(caminho, '\\\\.\\' + letra)
        self.assertEqual(acesso, 0)                              # nem leitura: só para perguntar em que disco o volume está
        self.assertEqual(api.fechados, [9])

    def test_origem_volume_usa_os_discos_do_volume(self):
        s = unidade.Sistema(api=ApiFalsaVolume([1, 2]))
        self.assertEqual(s.discos_da_origem('\\\\.\\E:'), {'1', '2'})

    def test_fim_a_fim_mesmo_disco(self):
        with mock.patch.object(unidade, 'MAPA', unidade.Sistema(api=ApiFalsaVolume([4]))):
            self.assertEqual(unidade.conferir('\\\\.\\PhysicalDrive4', os.path.join(self.trabalho, 'x')), unidade.MSG_MESMO_DISCO)
            self.assertIsNone(unidade.conferir('\\\\.\\PhysicalDrive5', os.path.join(self.trabalho, 'x')))

    def test_destino_de_rede_sem_letra_recusa(self):
        s = unidade.Sistema(api=ApiFalsaVolume([0]))
        with mock.patch('os.path.realpath', return_value='\\\\servidor\\pasta'):
            self.assertIsNone(s.discos_do_destino('\\\\servidor\\pasta\\x'))


class TestExtensoesDaApi(unittest.TestCase):
    def test_interpreta_a_estrutura_do_windows(self):
        carga = struct.pack('<I4x', 2) + struct.pack('<I4xqq', 3, 0, 100) + struct.pack('<I4xqq', 5, 100, 200)
        api = object.__new__(leitor.ApiWindows)
        api.ct = ctypes
        chamadas = []

        def ioctl(h, codigo, buf):
            chamadas.append(codigo)
            ctypes.memmove(buf, carga, len(carga))
        api._ioctl = ioctl
        self.assertEqual(api.extensoes(1), [3, 5])
        self.assertEqual(chamadas, [leitor.IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS])


@unittest.skipIf(os.name == 'nt', 'depende de st_dev e /sys')
class TestLinux(CasoComPasta):
    def arvore(self):
        sys = os.path.join(self.pasta, 'sys')
        disco = os.path.join(sys, 'devices', 'sda')
        part = os.path.join(disco, 'sda1')
        os.makedirs(part)
        open(os.path.join(part, 'partition'), 'w').close()
        os.makedirs(os.path.join(sys, 'class', 'block'))
        os.makedirs(os.path.join(sys, 'dev', 'block'))
        os.symlink(disco, os.path.join(sys, 'class', 'block', 'sda'))
        os.symlink(part, os.path.join(sys, 'class', 'block', 'sda1'))
        dev = os.stat(self.trabalho).st_dev
        os.symlink(part, os.path.join(sys, 'dev', 'block', '%d:%d' % (os.major(dev), os.minor(dev))))
        return sys

    def test_particao_sobe_para_o_disco_pai(self):
        s = unidade.Sistema(raiz_sys=self.arvore())
        self.assertEqual(s.discos_da_origem('/dev/sda1'), {'sda'})
        self.assertEqual(s.discos_da_origem('/dev/sda'), {'sda'})
        self.assertEqual(s.discos_do_destino(os.path.join(self.trabalho, 'x.bin')), {'sda'})
        with mock.patch.object(unidade, 'MAPA', s):
            self.assertEqual(unidade.conferir('/dev/sda', os.path.join(self.trabalho, 'x.bin')), unidade.MSG_MESMO_DISCO)

    def test_destino_sem_mapeamento_recusa(self):
        s = unidade.Sistema(raiz_sys=os.path.join(self.pasta, 'vazio'))
        with mock.patch.object(unidade, 'MAPA', s):
            self.assertEqual(unidade.conferir('/dev/sda', os.path.join(self.trabalho, 'x.bin')), unidade.MSG_SEM_CERTEZA)


if __name__ == '__main__':
    unittest.main()
