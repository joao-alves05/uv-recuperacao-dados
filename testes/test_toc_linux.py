# -*- coding: utf-8 -*-
"""`leitor.sessoes()` no Linux: o TOC do CD/DVD (ioctl trocado por uma API falsa) leva o UDF direto à última sessão,
lendo poucos setores em vez de varrer o disco. Em arquivo comum não há `sessoes()` e o comportamento é o de sempre."""
import os
import struct
import unittest
from unittest import mock

from auxiliar import CasoComPasta
import imagens_udf as iu

from recuperacao import leitor
from recuperacao.sa import udf

S = 2048


class Contador(leitor.LeitorPosix):
    lidos = 0

    def _bruto(self, o, n):
        Contador.lidos += n
        return super()._bruto(o, n)


class TestTocLinux(CasoComPasta):
    def imagem(self):
        antiga = iu.Sessao({'antigo.txt': b'a' * 300}, 0, rotulo='SESSAO1')
        nova = iu.Sessao({'novo.txt': b'n' * 300}, 600, rotulo='SESSAO2')
        caminho = os.path.join(self.pasta, 'multi.iso')
        with open(caminho, 'wb') as f:
            f.write(iu.disco_udf([antiga, nova], 1500))
        return caminho

    def abrir(self, toc=None):
        Contador.lidos = 0
        with mock.patch.object(leitor, 'LeitorPosix', Contador):
            return leitor.abrir(self.imagem(), toc=toc)

    def test_toc_leva_a_ultima_sessao_lendo_poucos_setores(self):
        faixas = [(4, 0), (4, 600), (0, 90000)]                      # duas de dados e uma de áudio: a de áudio fica de fora
        with self.abrir(lambda fd: faixas) as l:
            self.assertEqual(l.sessoes(), [0, 600])
            v = udf.reconhecer(l)
            self.assertEqual((v.rotulo, v.ancora), ('SESSAO2', 856))
            self.assertEqual([e.caminho for e in v.varrer()], ['/novo.txt'])
            self.assertLess(Contador.lidos, 100 * S)                 # o disco tem 1500 setores

    def test_sem_toc_arquivo_comum_nao_tem_sessoes(self):
        with self.abrir() as l:
            self.assertFalse(hasattr(l, 'sessoes'))
            self.assertEqual(udf.reconhecer(l).rotulo, 'SESSAO1')    # comportamento de antes

    def test_unidade_que_nao_responde_ao_toc_cai_no_comportamento_antigo(self):
        def falha(fd):
            raise OSError('sem TOC')
        with self.abrir(falha) as l:
            self.assertEqual(udf.reconhecer(l).rotulo, 'SESSAO1')

    def test_ioctl_real_monta_as_chamadas_do_toc(self):
        chamadas = []

        def ioctl(fd, req, arg):
            chamadas.append(req)
            if req == leitor.CDROMREADTOCHDR:
                return struct.pack('BB', 1, 2)
            n = arg[0]
            return struct.pack('BBBxIBxxx', n, 0x41, 1, (0, 0, 600)[n], 0)
        fcntl = mock.Mock(ioctl=ioctl)
        with mock.patch.dict('sys.modules', {'fcntl': fcntl}):
            self.assertEqual(leitor.toc_linux(3), [(4, 0), (4, 600)])
        self.assertEqual(chamadas, [leitor.CDROMREADTOCHDR] + [leitor.CDROMREADTOCENTRY] * 2)


if __name__ == '__main__':
    unittest.main()
