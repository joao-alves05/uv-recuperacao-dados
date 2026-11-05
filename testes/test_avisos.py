# -*- coding: utf-8 -*-
"""Parte 2.6: avisos de mídia (TRIM, disquete, óptico) e o pedido de elevação. Tudo com falsos; nada abre disco."""
import ctypes
import os
import unittest
from unittest import mock

import auxiliar
from auxiliar import CasoComPasta

from recuperacao import fontes, leitor, servico


def fonte(**kw):
    base = {'id': 'x', 'caminho': '/dev/x', 'nome': 'Mídia', 'tamanho': 8_000_000_000, 'tipo': 'hd', 'removivel': False,
            'setor': 512, 'sistema': False, 'volumes': []}
    base.update(kw)
    return fontes.listar(lambda: [base])[0]


class TestAvisos(unittest.TestCase):
    def test_trim_em_ssd_e_pen_drive(self):
        for tipo in ('ssd', 'usb'):
            self.assertEqual(fonte(tipo=tipo)['avisos'], [fontes.AVISO_TRIM], tipo)
        self.assertIn('TRIM', fontes.AVISO_TRIM)
        self.assertIn('zera', fontes.AVISO_TRIM)

    def test_sem_aviso_em_hd_e_cartao(self):
        for tipo in ('hd', 'cartao', 'desconhecido', 'arquivo'):
            self.assertEqual(fonte(tipo=tipo)['avisos'], [], tipo)

    def test_optico_avisa_que_a_leitura_completa_vem_depois(self):
        a = fonte(tipo='optico', tamanho=0)['avisos']
        self.assertEqual(a, [fontes.AVISO_OPTICO])
        self.assertIn('CD/DVD', a[0])

    def test_o_provedor_nao_escolhe_o_aviso(self):
        self.assertEqual(fonte(tipo='hd', avisos=['inventado'])['avisos'], [])

    def test_disquete_pelo_tamanho_exato(self):
        for tam in (1474560, 737280, 1228800, 368640):
            f = fonte(tipo='usb', tamanho=tam, removivel=True)
            self.assertEqual((f['tipo'], f['avisos']), ('disquete', [fontes.AVISO_DISQUETE]), tam)
        self.assertEqual(fonte(tipo='usb', tamanho=1474561)['tipo'], 'usb')            # quase não vale
        self.assertEqual(fonte(tipo='usb', tamanho=2_000_000_000)['tipo'], 'usb')
        self.assertEqual(fonte(tipo='hd', tamanho=1474560)['tipo'], 'hd')              # disco interno nunca vira disquete

    def test_disquete_pelo_nome(self):
        self.assertEqual(fonte(tipo='desconhecido', tamanho=0, nome='USB Floppy Disk Drive')['tipo'], 'disquete')

    def test_windows_usb_floppy_vira_disquete_na_lista_completa(self):
        bruto = {'Discos': [{'Index': 2, 'Model': 'Generic USB Floppy', 'Size': 1474560, 'MediaType': 'Removable Media',
                             'InterfaceType': 'USB', 'BytesPerSector': 512}]}
        r = fontes.listar(lambda: fontes.montar_windows(bruto))
        self.assertEqual((r[0]['tipo'], r[0]['setor'], r[0]['avisos']), ('disquete', 512, [fontes.AVISO_DISQUETE]))


class TestSeekPenalty(unittest.TestCase):
    BRUTO = {'Discos': [{'Index': 0, 'Model': 'Disco sem tipo', 'Size': 500_000_000_000, 'MediaType': 'Unspecified',
                         'InterfaceType': 'SCSI', 'BytesPerSector': 512}]}

    def tipo(self, consultar):
        return fontes.listar(lambda: fontes.montar_windows(self.BRUTO, consultar))[0]

    def test_sem_penalidade_de_busca_e_ssd_com_aviso(self):
        pedidos = []
        r = self.tipo(lambda i: pedidos.append(i) or False)
        self.assertEqual((r['tipo'], r['avisos']), ('ssd', [fontes.AVISO_TRIM]))
        self.assertEqual(pedidos, [0])

    def test_com_penalidade_de_busca_e_hd(self):
        r = self.tipo(lambda i: True)
        self.assertEqual((r['tipo'], r['avisos']), ('hd', []))

    def test_consulta_que_falha_deixa_desconhecido(self):
        def explode(i):
            raise OSError('sem acesso')
        self.assertEqual(self.tipo(explode)['tipo'], 'desconhecido')
        self.assertEqual(self.tipo(lambda i: None)['tipo'], 'desconhecido')
        self.assertEqual(fontes.listar(lambda: fontes.montar_windows(self.BRUTO))[0]['tipo'], 'desconhecido')

    def test_so_pergunta_quando_o_windows_nao_disse(self):
        bruto = {'Discos': [dict(self.BRUTO['Discos'][0], MediaType='Fixed hard disk media')]}
        pedidos = []
        r = fontes.montar_windows(bruto, lambda i: pedidos.append(i))
        self.assertEqual((r[0]['tipo'], pedidos), ('hd', []))

    def test_consulta_real_abre_com_acesso_zero_e_so_consulta(self):
        class Api:
            def __init__(self):
                self.aberturas, self.fechados = [], []

            def CreateFileW(self, caminho, acesso, compartilha, disposicao, flags):
                self.aberturas.append((caminho, acesso))
                return 5

            def seek_penalty(self, h):
                return False

            def fechar(self, h):
                self.fechados.append(h)
        api = Api()
        self.assertIs(leitor.seek_penalty_do_disco(3, api), False)
        self.assertEqual(api.aberturas, [('\\\\.\\PhysicalDrive3', 0)])
        self.assertEqual(api.fechados, [5])

    def test_estrutura_da_consulta_do_windows(self):
        api = object.__new__(leitor.ApiWindows)
        api.ct = ctypes
        from ctypes import wintypes
        api.wt = wintypes
        chamadas = []

        class K:
            def DeviceIoControl(self, h, codigo, entrada, n_in, saida, n_out, devolvidos, over):
                chamadas.append(codigo)
                ctypes.memset(saida, 0, n_out)
                ctypes.cast(saida, ctypes.POINTER(ctypes.c_ubyte))[8] = 1        # IncursSeekPenalty = TRUE
                return 1
        api.k = K()
        self.assertTrue(api.seek_penalty(1))
        self.assertEqual(chamadas, [leitor.IOCTL_STORAGE_QUERY_PROPERTY])

    def test_linux_rotacional_zero_e_ssd_com_aviso(self):
        pasta = auxiliar.tempfile.mkdtemp(prefix='recdados-teste-')
        self.addCleanup(auxiliar.shutil.rmtree, pasta, True)
        base = os.path.join(pasta, 'sda')
        os.makedirs(os.path.join(base, 'queue'))
        for rel, v in (('size', '1000000'), ('removable', '0'), ('queue/rotational', '0')):
            with open(os.path.join(base, rel), 'w') as f:
                f.write(v)
        r = fontes.listar(lambda: fontes.provedor_linux(pasta, os.path.join(pasta, 'sem-mounts')))
        self.assertEqual((r[0]['tipo'], r[0]['avisos']), ('ssd', [fontes.AVISO_TRIM]))


class TestRotaComAvisos(CasoComPasta):
    def test_api_fontes_traz_avisos(self):
        with mock.patch.dict(os.environ, {'RECDADOS_DADOS': self.dados, 'RECDADOS_PORTA': str(auxiliar.porta_livre())}):
            ctx = servico.Contexto(provedor_fontes=lambda: [fonte(tipo='ssd'), fonte(id='y', caminho='/dev/y', tipo='hd')])
            self.addCleanup(ctx.store.fechar)
            r = servico.criar_app(ctx).test_client().get('/api/fontes', base_url='http://127.0.0.1:%d' % ctx.porta,
                                                         headers={'X-RecDados-Token': ctx.token})
        self.assertEqual([x['avisos'] for x in r.get_json()], [[fontes.AVISO_TRIM], []])


class TestElevar(CasoComPasta):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {'RECDADOS_DADOS': self.dados, 'RECDADOS_PORTA': str(auxiliar.porta_livre())})
        p.start()
        self.addCleanup(p.stop)
        self.ctx = servico.Contexto()
        self.addCleanup(self.ctx.store.fechar)
        self.ctx.eh_windows = True
        self.chamadas = []
        self.codigo = 42
        self.ctx.elevador = lambda arq, par, pasta: self.chamadas.append((arq, par, pasta)) or self.codigo
        self.c = servico.criar_app(self.ctx).test_client()
        self.base = 'http://127.0.0.1:%d' % self.ctx.porta
        self.h = {'X-RecDados-Token': self.ctx.token}
        p2 = mock.patch.object(leitor, 'eh_administrador', return_value=False)
        p2.start()
        self.addCleanup(p2.stop)

    def post(self, headers=None):
        return self.c.post('/api/elevar', base_url=self.base, headers=self.h if headers is None else headers)

    def test_pede_elevacao_do_iniciar_com_admin_e_responde_202(self):
        r = self.post()
        self.assertEqual(r.status_code, 202)
        self.assertTrue(r.get_json()['ok'])
        (arquivo, parametros, pasta), = self.chamadas
        self.assertEqual(os.path.basename(arquivo), 'iniciar.py')
        self.assertEqual(parametros, '--admin')
        self.assertEqual(pasta, os.path.dirname(arquivo))

    def test_ja_administrador_da_409(self):
        with mock.patch.object(leitor, 'eh_administrador', return_value=True):
            r = self.post()
        self.assertEqual(r.status_code, 409)
        self.assertIn('já está rodando como administrador', r.get_json()['erro'])
        self.assertEqual(self.chamadas, [])

    def test_fora_do_windows_da_409(self):
        self.ctx.eh_windows = False
        r = self.post()
        self.assertEqual(r.status_code, 409)
        self.assertIn('sudo', r.get_json()['erro'])
        self.assertEqual(self.chamadas, [])

    def test_usuario_recusa_o_uac(self):
        self.codigo = 5                                        # ShellExecute: acesso negado/cancelado
        r = self.post()
        self.assertEqual(r.status_code, 409)
        self.assertIn('recusada ou cancelada', r.get_json()['erro'])

    def test_exige_sessao_e_origin_do_proprio_servico(self):
        self.assertEqual(self.post(headers={}).status_code, 401)
        self.assertEqual(self.post(headers=dict(self.h, Origin='https://evil.example')).status_code, 403)
        self.assertEqual(self.chamadas, [])

    def test_o_shell_execute_real_recebe_runas_e_o_pythonw(self):
        """A função de verdade, com o ShellExecuteW trocado por um falso (nenhum UAC aparece)."""
        recebido = []

        class Shell:
            def ShellExecuteW(self, hwnd, verbo, arquivo, parametros, pasta, mostrar):
                recebido.append((verbo, arquivo, parametros, pasta, mostrar))
                return 42

        class Janelas:
            shell32 = Shell()
        fake = mock.MagicMock()
        fake.windll = Janelas()
        with mock.patch.object(servico, 'ctypes', fake):
            codigo = servico.pedir_elevacao('C:\\app\\iniciar.py', '--admin', 'C:\\app')
        self.assertEqual(codigo, 42)
        verbo, exe, parametros, pasta, mostrar = recebido[0]
        self.assertEqual(verbo, 'runas')
        self.assertTrue(os.path.basename(exe).lower() in ('python.exe', 'pythonw.exe', 'python', 'python3'))
        self.assertEqual(parametros, '"C:\\app\\iniciar.py" --admin')


class TestVolumeComoOrigem(CasoComPasta):
    """O painel manda o caminho de volume (E:) e sugere como destino a raiz de outro volume."""
    VOLUME = chr(92) * 2 + '.' + chr(92) + 'E:'

    def setUp(self):
        super().setUp()
        from recuperacao import unidade
        p = mock.patch.object(unidade, 'MAPA', type('M', (), {'discos_da_origem': lambda s, o: {'1'},
                                                              'discos_do_destino': lambda s, d: {'2'}})())
        p.start()
        self.addCleanup(p.stop)

    @unittest.skipUnless(os.name == 'nt', 'raiz de unidade do Windows')
    def test_validacao_aceita_volume_na_origem_e_raiz_de_outro_volume_no_destino(self):
        from recuperacao.tipos import imagem
        raiz = os.path.splitdrive(os.path.abspath(self.trabalho))[0] + chr(92)
        destino = raiz + 'Recuperacao-E-2099-01-01.img'
        self.assertFalse(os.path.exists(destino))
        self.assertIsNone(imagem.validar(self.VOLUME, destino, {'bloco': 1048576}))
        self.assertEqual(imagem.validar(self.VOLUME.lower(), destino, {}), None)       # letra minúscula também

    def test_imagem_de_volume_pela_api_do_windows_falsa(self):
        from recuperacao.tipos import imagem
        from test_leitor import ApiFalsaComSetor, FalsoCtrl
        dados = os.urandom(4096 * 40)
        api = ApiFalsaComSetor(dados, setor=4096)
        destino = os.path.join(self.trabalho, 'volume.img')
        abrir_real = leitor.abrir
        with mock.patch.object(leitor, 'abrir', lambda c, api_=None: abrir_real(c, api=api) if c == self.VOLUME else abrir_real(c)):
            r = imagem.executar({'origem': self.VOLUME, 'destino': destino, 'opcoes': {'bloco': 8192}}, FalsoCtrl())
        self.assertEqual(r, 'concluido')
        with open(destino, 'rb') as f:
            self.assertEqual(f.read(), dados)
        caminho, acesso, *_ = api.aberturas[0]
        self.assertEqual((caminho, acesso), (self.VOLUME, leitor.GENERIC_READ))
        self.assertTrue(all(o % 4096 == 0 and n % 4096 == 0 for o, n in api.leituras))

    def test_volume_sem_geometria_cai_em_512(self):
        api = ApiFalsaSemGeometria(os.urandom(512 * 64))
        with leitor.abrir(self.VOLUME, api=api) as l:
            self.assertEqual((l.tamanho, l.setor), (512 * 64, 512))
            self.assertEqual(len(l.ler(100, 1000)), 1000)


class ApiFalsaSemGeometria:
    def __init__(self, dados):
        self.dados = dados

    def CreateFileW(self, *a):
        return 1

    def tamanho(self, h):
        return len(self.dados)

    def setor(self, h):
        raise OSError(None, 'volume sem geometria', None, 1)

    def ler(self, h, offset, n):
        assert offset % 512 == 0 and n % 512 == 0
        return self.dados[offset: offset + n]

    def fechar(self, h):
        pass


if __name__ == '__main__':
    unittest.main()
