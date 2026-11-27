# -*- coding: utf-8 -*-
"""Pasta, arquivo ou entrada ilegível do ISO e do UDF (CD/DVD riscado) não some em silêncio: conta em `ilegiveis`, o caminho vai
para o aviso e o job sai `incompleta`."""
import os
import struct
import unittest
from unittest import mock

import auxiliar  # noqa: F401  (ajusta o sys.path)
import imagens_iso as ii
import imagens_udf as iu
from test_varredura import BaseVarredura

from recuperacao import leitor as leitor_mod
from recuperacao import sa
from recuperacao.sa import udf

S = 2048


class Mem:
    """Leitor em memória que falha (OSError) nas faixas pedidas, como mídia riscada."""

    def __init__(self, d):
        self.d, self.tamanho, self.ruins, self.falhas = d, len(d), [], []

    def ler(self, off, n):
        for ini, fim in self.falhas:
            if off < fim and off + n > ini:
                raise OSError('setor ilegível de mentira')
        return bytes(self.d[off:off + n])


def com_setor_ruim(caso, ini, fim):
    original = leitor_mod.LeitorPosix._bruto

    def falho(self_, offset, n):
        if offset < fim and offset + n > ini:
            raise OSError('setor ruim de mentira')
        return original(self_, offset, n)
    p = mock.patch.object(leitor_mod.LeitorPosix, '_bruto', falho)
    p.start()
    caso.addCleanup(p.stop)


class TestIso(BaseVarredura):
    def imagem(self):
        iso = ii.IsoImg(joliet=False)
        pasta = iso.add_dir('/', 'FOTOS')
        for i in range(5):
            iso.add_file(pasta, 'F%d.JPG' % i, os.urandom(300 + i))
        iso.add_file('/', 'LEIA.TXT', b'oi')
        caminho = iso.gravar(os.path.join(self.trabalho, 'cd.iso')) or os.path.join(self.trabalho, 'cd.iso')
        return caminho, iso._achar('/FOTOS').lba['p'] * S

    def test_pasta_ilegivel_conta_e_o_job_sai_incompleto_com_o_caminho(self):
        caminho, setor_fotos = self.imagem()
        com_setor_ruim(self, setor_fotos, setor_fotos + S)
        fim = self.esperar_estado(self.varrer(caminho, 'sistema')['id'], 'concluido')
        self.assertIn('ficou incompleta', fim['mensagem'])
        self.assertIn('/FOTOS', fim['mensagem'])
        self.assertGreaterEqual(fim['setores_ruins'], 1)

    def test_volume_conta_e_guarda_o_caminho(self):
        caminho, setor_fotos = self.imagem()
        with open(caminho, 'rb') as f:
            m = Mem(bytearray(f.read()))
        v = sa.detectar(m)
        m.falhas.append((setor_fotos, setor_fotos + S))
        nomes = [e.caminho for e in v.varrer()]
        self.assertEqual(nomes, ['/FOTOS', '/LEIA.TXT'])
        self.assertGreaterEqual(v.ilegiveis, 1)
        self.assertEqual(v.ilegiveis_onde, ['/FOTOS'])

    def test_registro_com_tamanho_impossivel_nao_perde_o_resto_dos_setores(self):
        iso = ii.IsoImg(joliet=False)
        pasta = iso.add_dir('/', 'FOTOS')
        for i in range(120):                                # a pasta passa de 1 setor
            iso.add_file(pasta, 'F%03d.JPG' % i, b'x' * 10)
        caminho = os.path.join(self.trabalho, 'rr.iso')
        iso.gravar(caminho)
        with open(caminho, 'rb') as f:
            dados = bytearray(f.read())
        base = iso._achar('/FOTOS').lba['p'] * S
        dados[base + 34 + 34 + 44] = 7                      # 2º arquivo da pasta (registros de 44 bytes): tamanho impossível (< 34)
        v = sa.detectar(Mem(dados))
        nomes = [e.caminho for e in v.varrer()]
        self.assertIn('/FOTOS/F119.JPG', nomes)              # o que está no setor seguinte continua na lista
        self.assertGreaterEqual(v.ilegiveis, 1)
        self.assertEqual(v.ilegiveis_onde, ['/FOTOS'])


class TestUdf(BaseVarredura):
    ARQ = {'docs/a.bin': os.urandom(300), 'docs/b.bin': os.urandom(300), 'docs/c.bin': os.urandom(300),
           'docs/d.bin': os.urandom(300), 'raiz.txt': b'dentro'}

    def disco(self):
        return iu.disco_udf([iu.Sessao(self.ARQ, 0, alt_fim=999)], 1000)

    def abrir(self):
        m = Mem(self.disco())
        v = sa.detectar(m)
        self.assertIsInstance(v, udf.Udf)
        return m, v

    def dados_da_pasta(self, v, caminho):
        ent = next(e for e in v.varrer() if e.caminho == caminho)
        return v._ler_icb(ent.ident)['runs'][0][0]

    def test_subpasta_ilegivel_conta_e_guarda_o_caminho(self):
        m, v = self.abrir()
        ini = self.dados_da_pasta(v, '/docs')
        v = sa.detectar(m)
        m.falhas.append((ini, ini + S))
        nomes = [e.caminho for e in v.varrer()]
        self.assertEqual(sorted(nomes), ['/docs', '/raiz.txt'])
        self.assertGreaterEqual(v.ilegiveis, 1)
        self.assertEqual(v.ilegiveis_onde, ['/docs'])

    def test_job_com_subpasta_udf_ilegivel_sai_incompleto_com_o_caminho(self):
        m, v = self.abrir()
        ini = self.dados_da_pasta(v, '/docs')
        caminho = os.path.join(self.trabalho, 'dvd.udf')
        with open(caminho, 'wb') as f:
            f.write(m.d)
        com_setor_ruim(self, ini, ini + S)
        fim = self.esperar_estado(self.varrer(caminho, 'sistema')['id'], 'concluido')
        self.assertIn('ficou incompleta', fim['mensagem'])
        self.assertIn('/docs', fim['mensagem'])

    def test_fid_corrompida_no_meio_nao_perde_os_arquivos_seguintes(self):
        m, v = self.abrir()
        ini = self.dados_da_pasta(v, '/docs')
        dados = m.d[ini: ini + S]
        offs, off = [], 0
        while off + 38 <= len(dados) and dados[off + 1] == 1:          # tag 257
            offs.append(off)
            off += (38 + struct.unpack_from('<H', dados, off + 36)[0] + dados[off + 19] + 3) & ~3
        self.assertGreaterEqual(len(offs), 5)                          # pai + a, b, c, d
        m.d[ini + offs[2] + 4] ^= 0xFF                                 # checksum da FID de b.bin: tag inválida
        v = sa.detectar(m)
        nomes = sorted(e.caminho for e in v.varrer())
        self.assertEqual(nomes, ['/docs', '/docs/a.bin', '/docs/c.bin', '/docs/d.bin', '/raiz.txt'])
        self.assertGreaterEqual(v.ilegiveis, 1)
        self.assertEqual(v.ilegiveis_onde, ['/docs'])

    def test_pasta_so_com_zeros_no_fim_nao_conta_nada(self):
        m, v = self.abrir()
        self.assertEqual(len(list(v.varrer())), 6)
        self.assertEqual(v.ilegiveis, 0)


if __name__ == '__main__':
    unittest.main()
