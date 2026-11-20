# -*- coding: utf-8 -*-
"""HFS+ embrulhado em HFS clássico (MDB 'BD' no setor 2 com drEmbedSigWord 'H+'): abre o volume embutido com os
offsets corrigidos; extent fora da imagem só avisa; HFS clássico puro não é lido."""
import os
import struct
import unittest

from auxiliar import CasoComPasta

import imagens_hfs as ih

from recuperacao import leitor as leitor_mod
from recuperacao import sa
from recuperacao.sa import hfs
from test_hfs_decmpfs import ImagemDecmpfs
from test_sa_fat import sha

AL_BLK, AL_BL_ST, INI = 2048, 10, 7                  # bloco de alocação, primeiro bloco (setores), início do embutido
DESLOC = AL_BL_ST * 512 + INI * AL_BLK               # 19456: não é múltiplo do bloco de 4 KiB do HFS+


def mdb(embutido=True, ini=INI, cont=0):
    b = bytearray(512)
    b[0:2] = b'BD'
    struct.pack_into('>I', b, 20, AL_BLK)
    struct.pack_into('>H', b, 28, AL_BL_ST)
    if embutido:
        b[124:126] = b'H+'
        struct.pack_into('>HH', b, 126, ini, cont)
    return bytes(b)


class TestHfsEmbrulhado(ImagemDecmpfs, CasoComPasta):
    def embrulhar(self, **kw):
        with open(self.montar(), 'rb') as f:
            interno = f.read()
        cont = kw.pop('cont', -(-len(interno) // AL_BLK))
        cab = mdb(cont=cont, **kw)
        saida = bytearray(DESLOC) + interno + bytes(4096)
        saida[1024:1536] = cab
        cam = os.path.join(self.trabalho, 'wrapper.img')
        with open(cam, 'wb') as f:
            f.write(saida)
        return cam

    def varrer(self, caminho):
        with leitor_mod.abrir(caminho) as l:
            vol = sa.detectar(l)
            self.assertIsNotNone(vol)
            return vol, {e.caminho: (e, b'' if e.eh_dir else b''.join(vol.ler(e))) for e in vol.varrer()}

    def test_arquivos_do_hfs_embutido_com_sha_certo(self):
        vol, r = self.varrer(self.embrulhar())
        self.assertEqual(vol.tipo, 'HFS+')
        self.assertTrue(vol.embrulhado)
        for c, dados in (('/Docs/pequeno.txt', self.t3), ('/Docs/grande.bin', self.t4)):       # decmpfs tipo 3 e 4
            e, b = r[c]
            self.assertEqual((e.estado, e.tamanho, sha(b)), ('bom', len(dados), sha(dados)), c)
        self.assertEqual(r['/Docs/lzvn.bin'][0].estado, 'ruim')

    def test_cabecalho_principal_apagado_usa_a_copia_do_fim_do_disco(self):
        cam = self.embrulhar()
        with open(cam, 'r+b') as f:                       # cabeçalho do HFS+ embutido zerado; a cópia vai para o fim dele
            f.seek(DESLOC + ih.TOTAL_BLOCOS * ih.BS - 1024)       # (a imagem de teste passa um pouco do TOTAL_BLOCOS)
            copia = f.read(512)
            f.seek(DESLOC + 1024)
            f.write(bytes(512))
            f.seek(0, 2)
            fim = f.tell() - 4096                          # fim do embutido: o wrapper de teste tem 4 KiB depois dele
            f.seek(fim - 1024)
            f.write(copia)
        vol, r = self.varrer(cam)
        self.assertTrue(vol.usou_reserva)
        self.assertEqual(sha(r['/Docs/pequeno.txt'][1]), sha(self.t3))

    def test_extent_fora_da_imagem_avisa_sem_travar(self):
        with leitor_mod.abrir(self.embrulhar(cont=60000)) as l:
            vol = sa.detectar(l)
            self.assertIsNotNone(vol)
            self.assertEqual(list(vol.varrer()), [])
            self.assertIn('embutido', vol.limitacao)
            self.assertIn('inválida', vol.limitacao)

    def test_hfs_classico_sem_embutido_continua_nao_lido(self):
        cam = os.path.join(self.trabalho, 'classico.img')
        with open(cam, 'wb') as f:
            f.write(bytes(1024) + mdb(embutido=False) + bytes(64 * 1024))
        with leitor_mod.abrir(cam) as l:
            self.assertIsNone(hfs.reconhecer(l))


if __name__ == '__main__':
    unittest.main()
