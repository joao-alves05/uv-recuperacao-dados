# -*- coding: utf-8 -*-
"""Parte 2.2: leitor só leitura. Disco "de mentira" (API falsa do Windows e leitor em memória); nunca disco real."""
import inspect
import os
import unittest
from unittest import mock

from auxiliar import CasoComPasta, criar_origem, sha256_de

from recuperacao import leitor
from recuperacao.executor import ErroJob
from recuperacao.tipos import imagem

DISCO = '\\\\.\\PhysicalDrive7'


class ApiFalsa:
    """Imita ApiWindows sobre bytes em memória, exigindo leitura alinhada como um disco de verdade."""

    def __init__(self, dados, setor=4096, erro_abrir=None, sem_geometria=False):
        self.dados, self.tam_setor, self.erro_abrir, self.sem_geometria = dados, setor, erro_abrir, sem_geometria
        self.aberturas, self.leituras, self.fechados = [], [], []

    def CreateFileW(self, caminho, acesso, compartilha, disposicao, flags):
        self.aberturas.append((caminho, acesso, compartilha, disposicao, flags))
        if self.erro_abrir:
            e = OSError(None, 'erro', None, self.erro_abrir)
            raise e
        return 42

    def tamanho(self, h):
        return len(self.dados)

    def ler(self, h, offset, n):
        self.leituras.append((offset, n))
        assert offset % self.tam_setor == 0 and n % self.tam_setor == 0, ('leitura desalinhada', offset, n)
        return self.dados[offset: offset + n]

    def fechar(self, h):
        self.fechados.append(h)


class ApiFalsaComSetor(ApiFalsa):
    def setor(self, h):
        if self.sem_geometria:
            raise OSError(None, 'sem geometria', None, 1)
        return self.tam_setor


class TestLeitorArquivo(CasoComPasta):
    def test_ler_alinhado_desalinhado_e_alem_do_fim(self):
        caminho = criar_origem(self.trabalho, 10_000)
        with open(caminho, 'rb') as f:
            ref = f.read()
        with leitor.abrir(caminho) as l:
            self.assertEqual((l.tamanho, l.setor), (10_000, 512))
            self.assertEqual(l.ler(0, 512), ref[:512])
            self.assertEqual(l.ler(100, 1000), ref[100:1100])
            self.assertEqual(l.ler(9_000, 5_000), ref[9_000:])          # corta no fim
            self.assertEqual(l.ler(10_000, 10), b'')
            self.assertEqual(l.ler(0, 0), b'')
            with self.assertRaises(ValueError):
                l.ler(-1, 10)

    def test_abre_so_para_leitura(self):
        caminho = criar_origem(self.trabalho, 100)
        flags = []
        original = os.open

        def espiao(c, f, *a, **k):
            flags.append(f)
            return original(c, f, *a, **k)

        with mock.patch.object(os, 'open', espiao):
            leitor.abrir(caminho).fechar()
        self.assertEqual(len(flags), 1)
        escrita = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
        self.assertEqual(flags[0] & escrita, 0)

    def test_arquivo_que_nao_existe(self):
        with self.assertRaises(leitor.ErroLeitor) as c:
            leitor.abrir(os.path.join(self.trabalho, 'nao-existe'))
        self.assertIn('Não encontrei', str(c.exception))


class TestLeitorWindowsFalso(unittest.TestCase):
    def dados(self, n=40_960):
        return bytes(i % 251 for i in range(n))

    def abrir(self, api):
        return leitor.abrir(DISCO, api=api)

    def test_abre_so_com_acesso_de_leitura(self):
        api = ApiFalsaComSetor(self.dados())
        l = self.abrir(api)
        caminho, acesso, compartilha, disposicao, flags = api.aberturas[0]
        self.assertEqual(caminho, DISCO)
        self.assertEqual(acesso, leitor.GENERIC_READ)                     # exatamente leitura, nenhum outro bit
        self.assertEqual(compartilha, leitor.FILE_SHARE_READ | leitor.FILE_SHARE_WRITE)
        self.assertEqual(disposicao, leitor.OPEN_EXISTING)
        self.assertEqual(len(api.aberturas), 1)
        l.fechar()
        self.assertEqual(api.fechados, [42])

    def test_tamanho_setor_e_leitura_alinhada(self):
        d = self.dados()
        api = ApiFalsaComSetor(d, setor=4096)
        with self.abrir(api) as l:
            self.assertEqual((l.tamanho, l.setor), (len(d), 4096))
            self.assertEqual(l.ler(5, 100), d[5:105])                      # desalinhado: o leitor alarga ao setor
            self.assertEqual(l.ler(4090, 20), d[4090:4110])                # atravessa dois setores
            self.assertEqual(l.ler(0, 40_960), d)
            self.assertEqual(l.ler(40_950, 500), d[40_950:])               # corta no fim do disco
        for off, n in api.leituras:
            self.assertEqual((off % 4096, n % 4096), (0, 0))

    def test_sem_geometria_cai_em_512(self):
        with self.abrir(ApiFalsaComSetor(self.dados(), sem_geometria=True)) as l:
            self.assertEqual(l.setor, 512)

    def test_sem_administrador_mensagem_clara(self):
        with self.assertRaises(leitor.ErroPermissao) as c:
            self.abrir(ApiFalsaComSetor(b'', erro_abrir=5))
        self.assertEqual(str(c.exception), 'Para ler o disco inteiro, abra o programa como administrador')

    def test_disco_removido(self):
        with self.assertRaises(leitor.ErroLeitor) as c:
            self.abrir(ApiFalsaComSetor(b'', erro_abrir=2))
        self.assertIn('Não encontrei o disco', str(c.exception))

    def test_leitura_curta_vira_erro(self):
        api = ApiFalsaComSetor(self.dados(8192), setor=4096)
        with self.abrir(api) as l:
            l.tamanho = 16384                                              # o disco "encolheu" no meio da leitura
            with self.assertRaises(OSError):
                l.ler(12288, 4096)


class TestSomenteLeituraDoModulo(unittest.TestCase):
    def test_estatico_nenhum_acesso_de_escrita(self):
        fonte = inspect.getsource(leitor)
        for proibido in ('GENERIC_WRITE', 'GENERIC_ALL', 'O_WRONLY', 'O_RDWR', 'O_CREAT', 'O_APPEND', 'O_TRUNC', 'WriteFile',
                         "'wb'", "'r+b'", "'w'", 'FILE_WRITE_DATA', 'IOCTL_DISK_SET', 'FSCTL_LOCK', 'FSCTL_DISMOUNT'):
            self.assertNotIn(proibido, fonte, proibido)
        self.assertEqual(leitor.GENERIC_READ, 0x80000000)

    def test_dinamico_chamadas_do_windows_so_leem(self):
        """Intercepta as chamadas do kernel32 que o módulo faria e confere que só há as de leitura."""
        fonte = inspect.getsource(leitor.ApiWindows)
        chamadas = set(__import__('re').findall(r'self\.k\.(\w+)\(', fonte))
        self.assertEqual(chamadas, {'CreateFileW', 'DeviceIoControl', 'SetFilePointerEx', 'ReadFile', 'CloseHandle'})

    def test_reconhece_caminhos_de_dispositivo_sem_tocar_no_disco(self):
        for ok in ('\\\\.\\PhysicalDrive0', '\\\\.\\physicaldrive12', '\\\\.\\C:', '/dev/sda', '/dev/nvme0n1', '/dev/sr0'):
            self.assertTrue(leitor.eh_dispositivo(ok), ok)
        for ruim in ('C:\\pasta\\a.bin', 'a.bin', '\\\\servidor\\x', '\\\\.\\PhysicalDrive', '/dev/', '/devx/sda', '/dev/../etc/passwd',
                     '\\\\.\\C:\\x', '', None):
            self.assertFalse(leitor.eh_dispositivo(ruim), ruim)
        self.assertTrue(leitor.eh_optico('\\\\.\\CdRom0') and leitor.eh_optico('/dev/sr0'))
        self.assertFalse(leitor.eh_optico('\\\\.\\PhysicalDrive0'))


class DiscoFalso(leitor.Leitor):
    """Leitor em memória que se passa por disco: alinhado, setor de 512."""
    alinhar = True

    def __init__(self, dados):
        super().__init__(len(dados), 512)
        self.dados, self.leituras = dados, []

    def _bruto(self, offset, n):
        assert offset % 512 == 0 and n % 512 == 0
        self.leituras.append((offset, n))
        return self.dados[offset: offset + n]


class FalsoCtrl:
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


class TestImagemDeDisco(CasoComPasta):
    def test_validar_aceita_disco_sem_tocar_no_caminho_e_recusa_optico_e_destino_de_disco(self):
        d = os.path.join(self.trabalho, 'img.bin')
        tocados = []

        def vigia(original):
            def f(p, *a, **k):
                if isinstance(p, str) and p.startswith(('\\\\.\\', '/dev/')):
                    tocados.append(p)
                return original(p, *a, **k)
            return f

        with mock.patch.object(os.path, 'isfile', vigia(os.path.isfile)), mock.patch.object(os.path, 'exists', vigia(os.path.exists)), \
                mock.patch.object(os.path, 'realpath', vigia(os.path.realpath)), mock.patch.object(os, 'stat', vigia(os.stat)):
            self.assertIsNone(imagem.validar(DISCO, d, {}))
            self.assertIsNone(imagem.validar('\\\\.\\C:', d, {}))
            self.assertIn('CD/DVD', imagem.validar('\\\\.\\CdRom0', d, {}))
            self.assertIn('Disco bruto', imagem.validar(os.path.join(self.trabalho, 'x'), DISCO, {}))     # destino nunca é disco
            self.assertIn('pasta do destino', imagem.validar(DISCO, os.path.join(self.trabalho, 'nao', 'x.bin'), {}))
        self.assertEqual(tocados, [])

    def test_imagem_de_disco_falso_igual_byte_a_byte(self):
        dados = os.urandom(512 * 300 + 0)                    # 153.600 bytes: múltiplo do setor, como um disco
        disco = DiscoFalso(dados)
        destino = os.path.join(self.trabalho, 'img.bin')
        with mock.patch.object(leitor, 'abrir', lambda c, api=None: disco if c == DISCO else leitor.LeitorPosix(c)):
            r = imagem.executar({'origem': DISCO, 'destino': destino, 'opcoes': {'bloco': 4096}}, FalsoCtrl())
        self.assertEqual(r, 'concluido')
        with open(destino, 'rb') as f:
            self.assertEqual(f.read(), dados)
        self.assertTrue(disco.leituras)
        mapa = imagem.carregar_mapa(imagem.caminho_mapa(destino), len(dados), DISCO.upper())
        self.assertEqual(mapa, [[0, len(dados), 'ok']])

    def test_sem_administrador_vira_falha_clara_do_job(self):
        def negar(c, api=None):
            raise leitor.ErroPermissao(leitor.MSG_ADMIN)
        destino = os.path.join(self.trabalho, 'img.bin')
        with mock.patch.object(leitor, 'abrir', negar):
            with self.assertRaises(ErroJob) as c:
                imagem.executar({'origem': DISCO, 'destino': destino, 'opcoes': {}}, FalsoCtrl())
        self.assertEqual(str(c.exception), 'Para ler o disco inteiro, abra o programa como administrador')
        self.assertFalse(os.path.exists(destino))                       # nada criado antes de poder ler

    def test_arquivo_comum_continua_funcionando_pelo_mesmo_leitor(self):
        origem = criar_origem(self.trabalho, 70_001)
        destino = os.path.join(self.trabalho, 'img.bin')
        self.assertEqual(imagem.executar({'origem': origem, 'destino': destino, 'opcoes': {'bloco': 4096}}, FalsoCtrl()), 'concluido')
        self.assertEqual(sha256_de(destino), sha256_de(origem))


if __name__ == '__main__':
    unittest.main()
