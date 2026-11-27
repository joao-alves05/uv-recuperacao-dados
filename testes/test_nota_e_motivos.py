# -*- coding: utf-8 -*-
"""A `nota` da entrada chega ao achado gravado, à API e ao relatório; arquivo que não dá para ler sai com motivo próprio
(cifrado, compressao_nao_suportada, hardlink_sem_inode), nunca como `sem_conteudo` ("apagado só com o nome")."""
import os
import unittest

import imagens_hfs as ih
import imagens_ntfs as nt
from auxiliar import CasoComPasta
from test_hfs_decmpfs import ImagemDecmpfs
from test_hfs_links import PRIVADA, link_duro
from test_sa_fat import conteudo
from test_varredura import BaseVarredura

from recuperacao import achados as mod_achados
from recuperacao import arquivos_sa, leitor as leitor_mod, sa
from recuperacao.tipos import varredura


def entradas(caminho):
    with leitor_mod.abrir(caminho) as l:
        vol = sa.detectar(l)
        return {e.caminho: e for e in vol.varrer()}


def ntfs_com_efs_e_reuso(caminho):
    f = nt.NtfsImg()
    f.add_file(5, 'cifrado.doc', conteudo(3000, 1), flags_dados=0x4000)          # EFS
    r = f.add_file(5, 'reaproveitado.bin', conteudo(3072, 2))
    lcn = f.runs_de[r][0][0]
    f.apagar(r)
    f.add_file(5, 'ocupou.bin', conteudo(1024, 3), lcn=lcn + 1)                    # reusa um cluster do apagado
    return f.gravar(caminho)


class TestMotivosProprios(CasoComPasta):
    def test_efs_no_ntfs(self):
        e = entradas(ntfs_com_efs_e_reuso(os.path.join(self.pasta, 'n.img')))['/cifrado.doc']
        self.assertEqual(arquivos_sa.achado_de(e), (None, 'cifrado'))

    def test_decmpfs_tipo8_no_hfs(self):
        m = ImagemDecmpfs()
        m.trabalho = self.pasta
        e = entradas(m.montar())['/Docs/lzvn.bin']
        self.assertEqual(arquivos_sa.achado_de(e), (None, 'compressao_nao_suportada'))

    def test_hard_link_sem_inode_no_hfs(self):
        f = ih.HfsImg()
        priv = f.add_dir(2, PRIVADA)
        docs = f.add_dir(2, 'Docs')
        link_duro(f, docs, 'ligado.bin', 77)                                      # iNode77 não existe
        e = entradas(f.gravar(os.path.join(self.pasta, 'h.img')))['/Docs/ligado.bin']
        self.assertEqual(arquivos_sa.achado_de(e), (None, 'hardlink_sem_inode'))

    def test_sem_motivo_informado_vira_ilegivel_e_nao_apagado(self):
        from recuperacao.sa.base import Entrada
        e = Entrada(nome='x', caminho='/x', tamanho=10, estado='ruim')            # vivo, sem runs
        self.assertEqual(arquivos_sa.achado_de(e), (None, 'ilegivel'))

    def test_textos_dos_motivos_sao_verdadeiros(self):
        for m in ('cifrado', 'compressao_nao_suportada', 'hardlink_sem_inode', 'ilegivel'):
            self.assertNotIn('apagados só com o nome', varredura.MOTIVOS[m])
        self.assertIn('apagados só com o nome', varredura.MOTIVOS['sem_conteudo'])


class TestNotaNoAchado(BaseVarredura):
    def test_nota_do_parcial_vai_ao_banco_a_api_e_o_evento_diz_a_verdade(self):
        job = self.varrer(ntfs_com_efs_e_reuso(os.path.join(self.trabalho, 'n.img')), 'sistema')
        self.esperar_estado(job['id'], 'concluido')
        lista = {a['caminho']: a for a in self.achados_do_banco(job['id'])}
        self.assertIn('clusters já reutilizados', lista['/reaproveitado.bin']['nota'])
        self.assertEqual(lista['/reaproveitado.bin']['estado'], 'parcial')
        self.assertNotIn('/cifrado.doc', lista)
        itens = {a['caminho']: a for a in self.req('GET', '/api/jobs/%s/achados' % job['id']).get_json()['itens']}
        self.assertIn('clusters já reutilizados', itens['/reaproveitado.bin']['nota'])
        textos = [e['texto'] for e in self.ctx.store.eventos(job['id'], 500)]
        self.assertTrue(any('criptografados' in t and 'não entraram na lista' in t for t in textos), textos)
        self.assertFalse(any('apagados só com o nome' in t for t in textos), textos)

    def test_nota_e_limitada_e_opcional(self):
        base = {'tamanho': 10, 'offset': 0, 'caminho': '/a.bin'}
        self.assertIsNone(mod_achados.normalizar('j', base)[-1])
        self.assertEqual(len(mod_achados.normalizar('j', dict(base, nota='x' * 999))[-1]), mod_achados.MAX_NOTA)
        with self.assertRaises(ValueError):
            mod_achados.normalizar('j', dict(base, nota=5))


if __name__ == '__main__':
    unittest.main()
