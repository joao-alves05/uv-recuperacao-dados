# -*- coding: utf-8 -*-
"""HFS+: hard link de arquivo (registro 'hlnk'/'hfs+' -> iNode<n> na pasta oculta) aparece com o conteúdo do inode."""
import os
import struct
import unittest

from auxiliar import CasoComPasta

import imagens_hfs as ih
from recuperacao import leitor as leitor_mod
from recuperacao import sa
from test_sa_fat import conteudo, sha

PRIVADA = '\0\0\0\0HFS+ Private Data'


def link_duro(f, pai, nome, n_inode, tipo=b'hlnk', criador=b'hfs+'):
    i = f.prox_id
    f.prox_id += 1
    r = bytearray(248)
    struct.pack_into('>hHII', r, 0, 2, 0, 0, i)
    r[12:28] = struct.pack('>IIII', ih.DATA_MAC, ih.DATA_MAC, ih.DATA_MAC, ih.DATA_MAC)
    struct.pack_into('>I', r, 44, n_inode)
    struct.pack_into('>H', r, 42, 0o100644)
    r[48:52], r[52:56] = tipo, criador
    f.cat.append((pai, nome, i, False, bytes(r)))
    f.n_arquivos += 1


class TestHfsLinks(CasoComPasta):
    def varrer(self, caminho):
        with leitor_mod.abrir(caminho) as l:
            vol = sa.detectar(l)
            self.assertIsNotNone(vol)
            return {e.caminho: (e, b'' if e.eh_dir else b''.join(vol.ler(e))) for e in vol.varrer()}

    def montar(self, inode_existe=True, ordem_invertida=False):
        f = ih.HfsImg()
        dados = conteudo(6000, 7)
        if ordem_invertida:                                   # link numa pasta de id menor que a pasta privada
            docs = f.add_dir(2, 'Docs')
            priv = f.add_dir(2, PRIVADA)
        else:
            priv = f.add_dir(2, PRIVADA)
            docs = f.add_dir(2, 'Docs')
        f.add_file(2, 'original.bin', conteudo(10, 1))
        outro = f.add_dir(2, 'Outra')
        if inode_existe:
            f.add_file(priv, 'iNode77', dados)
        link_duro(f, docs, 'ligado.bin', 77)
        link_duro(f, outro, 'ligado2.bin', 77)
        link_duro(f, outro, 'pasta-tm', 99, tipo=b'fdrp', criador=b'MACS')     # hard link de pasta: fora
        return f.gravar(os.path.join(self.trabalho, 'h.img')), dados

    def test_link_com_conteudo_do_inode(self):
        for inv in (False, True):
            cam, dados = self.montar(ordem_invertida=inv)
            r = self.varrer(cam)
            for c in ('/Docs/ligado.bin', '/Outra/ligado2.bin'):
                self.assertIn(c, r, inv)
                e, b = r[c]
                self.assertEqual((e.estado, e.tamanho, sha(b)), ('bom', 6000, sha(dados)), c)
            self.assertEqual(sha(r['/Docs/ligado.bin'][1]), sha(r['/Outra/ligado2.bin'][1]))
            self.assertFalse([c for c in r if 'iNode' in c or 'Private' in c or 'tm' in c])

    def test_inode_ausente_sai_ruim(self):
        cam, _ = self.montar(inode_existe=False)
        e, _b = self.varrer(cam)['/Docs/ligado.bin']
        self.assertEqual(e.estado, 'ruim')
        self.assertIn('iNode77', e.nota)


if __name__ == '__main__':
    unittest.main()
