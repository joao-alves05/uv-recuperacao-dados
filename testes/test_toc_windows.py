# -*- coding: utf-8 -*-
"""`leitor.sessoes()` no Windows: o TOC do CD/DVD (IOCTL e GetDriveType trocados por uma API falsa) leva o UDF direto à
última sessão, lendo poucos setores em vez de varrer o disco. Em arquivo ou volume comum não há `sessoes()`."""
import struct
import unittest
from unittest import mock

from auxiliar import CasoComPasta  # noqa: F401  (põe a raiz do repo no sys.path)
import imagens_udf as iu

from recuperacao import leitor
from recuperacao.sa import udf

S = 2048


class ApiCd:
    """ApiWindows falsa sobre uma imagem em memória; `tipo` é o GetDriveType da letra e `faixas` o TOC (ou um erro)."""

    def __init__(self, dados, tipo=5, faixas=(), erro_toc=None):
        self.dados, self.tipo, self.faixas, self.erro_toc = dados, tipo, list(faixas), erro_toc
        self.lidos = 0

    def CreateFileW(self, caminho, acesso, compartilha, disposicao, flags):
        return 7

    def tamanho(self, h):
        return len(self.dados)

    def setor(self, h):
        return S

    def tipo_unidade(self, letra):
        return self.tipo

    def toc(self, h):
        if self.erro_toc:
            raise self.erro_toc
        return self.faixas

    def ler(self, h, offset, n):
        self.lidos += n
        return bytes(self.dados[offset: offset + n])

    def fechar(self, h):
        pass


def imagem():
    antiga = iu.Sessao({'antigo.txt': b'a' * 300}, 0, rotulo='SESSAO1')
    nova = iu.Sessao({'novo.txt': b'n' * 300}, 600, rotulo='SESSAO2')
    return iu.disco_udf([antiga, nova], 1500)


def abrir(api, caminho=r'\\.\E:'):
    with mock.patch.object(leitor.os, 'name', 'nt'):
        return leitor.abrir(caminho, api=api)


class TestTocWindows(unittest.TestCase):
    def test_toc_leva_a_ultima_sessao_lendo_poucos_setores(self):
        api = ApiCd(imagem(), faixas=[(4, 0), (4, 600), (0, 90000)])     # a faixa de áudio fica de fora
        with abrir(api) as l:
            self.assertEqual(l.sessoes(), [0, 600])
            v = udf.reconhecer(l)
            self.assertEqual((v.rotulo, v.ancora), ('SESSAO2', 856))
            self.assertEqual([e.caminho for e in v.varrer()], ['/novo.txt'])
            self.assertLess(api.lidos, 100 * S)                          # o disco tem 1500 setores

    def test_volume_que_nao_e_cdrom_nao_tem_sessoes(self):
        api = ApiCd(imagem(), tipo=3, faixas=[(4, 0), (4, 600)])         # DRIVE_FIXED
        with abrir(api) as l:
            self.assertFalse(hasattr(l, 'sessoes'))
            self.assertEqual(udf.reconhecer(l).rotulo, 'SESSAO1')        # comportamento de antes

    def test_unidade_que_nao_responde_ao_toc_cai_no_comportamento_antigo(self):
        api = ApiCd(imagem(), erro_toc=OSError('sem TOC'))
        with abrir(api) as l:
            self.assertEqual(udf.reconhecer(l).rotulo, 'SESSAO1')

    def test_toc_do_windows_vira_faixas(self):
        def faixa(n, ctrl, lba):
            return struct.pack('>BBBBI', 0, ctrl << 4 | 1, n, 0, lba)
        dados = struct.pack('>HBB', 2 + 8 * 3, 1, 2) + faixa(1, 4, 0) + faixa(2, 4, 600) + faixa(0xAA, 4, 1500)
        self.assertEqual(leitor.faixas_do_toc(dados), [(4, 0), (4, 600)])
        with self.assertRaises(OSError):
            leitor.faixas_do_toc(b'')

    def test_ioctl_real_pede_o_formato_toc_em_lba(self):
        chamadas = []
        resposta = struct.pack('>HBB', 2 + 8 * 2, 1, 1) + struct.pack('>BBBBI', 0, 0x41, 1, 0, 0) \
            + struct.pack('>BBBBI', 0, 0x41, 0xAA, 0, 1500)

        def ioctl(h, codigo, entrada, n_entrada, saida, n_saida, devolvidos, _):
            chamadas.append((codigo, bytes(entrada._obj)))
            saida._obj[:len(resposta)] = resposta
            devolvidos._obj.value = len(resposta)
            return True
        api = leitor.ApiWindows()
        api.k = mock.Mock(DeviceIoControl=ioctl)
        self.assertEqual(api.toc(7), [(4, 0)])
        self.assertEqual(chamadas, [(leitor.IOCTL_CDROM_READ_TOC_EX, bytes(4))])


if __name__ == '__main__':
    unittest.main()
