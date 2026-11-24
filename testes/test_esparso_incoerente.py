# -*- coding: utf-8 -*-
"""Inode vivo cujo mapa é só buraco e cujo tamanho passa do volume não vira arquivo `bom` de zeros (ext2/3 e ext4)."""
import os
import unittest

import imagens_ext as ie
from auxiliar import CasoComPasta
from test_esparso import ext4_com_esparso

from recuperacao import arquivos_sa, leitor as leitor_mod, sa


def varrer(caminho):
    with leitor_mod.abrir(caminho) as l:
        vol = sa.detectar(l)
        return {e.caminho: e for e in vol.varrer()}


def com_inode(versao, tam, iblock, flags, caminho):
    f = ie.ExtImg(versao)
    ino = f.prox_ino
    f.prox_ino += 1
    f.inodes[ino] = f._inode(0x8000 | 0o644, tam, 1, flags, iblock)
    f.pastas[2]['ents'].append([ino, 'falso.bin', 1, False])
    return f.gravar(caminho)


class TestEsparsoIncoerente(CasoComPasta):
    def conferir(self, e):
        self.assertNotEqual(e.estado, 'bom')
        a, motivo = arquivos_sa.achado_de(e)
        if a is not None:
            self.assertNotEqual(a['estado'], 'bom')
            self.assertLessEqual(a['tamanho'], 4 << 20)        # nunca grava tamanho do inode em zeros

    def test_ext2_tamanho_enorme_e_ponteiros_zerados(self):
        r = varrer(com_inode('ext2', 8 << 20, b'', 0, os.path.join(self.pasta, 'e2.img')))
        self.conferir(r['/falso.bin'])

    def test_ext4_cabecalho_valido_sem_entradas(self):
        iblock = ie.ExtImg('ext4')._iblock_extents([])
        r = varrer(com_inode('ext4', 8 << 20, iblock, 0x80000, os.path.join(self.pasta, 'e4.img')))
        self.conferir(r['/falso.bin'])

    def test_esparso_legitimo_continua_bom(self):
        caminho, original = ext4_com_esparso(os.path.join(self.pasta, 'ok.img'))
        e = varrer(caminho)['/esparso.bin']
        self.assertEqual((e.estado, e.tamanho), ('bom', len(original)))

    def test_todo_zero_pequeno_e_coerente_continua_bom(self):
        iblock = ie.ExtImg('ext4')._iblock_extents([])
        r = varrer(com_inode('ext4', 4096, iblock, 0x80000, os.path.join(self.pasta, 'z.img')))
        self.assertEqual(r['/falso.bin'].estado, 'bom')


if __name__ == '__main__':
    unittest.main()
