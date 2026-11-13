# -*- coding: utf-8 -*-
"""Arquivo esparso (com buraco) entra na lista de achados e sai com zeros no buraco.

O buraco é o extent `[None, n]` (n bytes de zeros). Imagem ext4 montada em Python; nenhum disco real.
"""
import hashlib
import os
import unittest

import imagens_ext as ie
from test_sa_fat import conteudo
from test_varredura import BaseVarredura

from recuperacao import achados as mod_achados
from recuperacao import arquivos_sa, leitor as leitor_mod
from recuperacao.sa.base import Entrada


def ext4_com_esparso(caminho):
    """ext4 com `esparso.bin`: 2 blocos de dados, 3 de buraco, 2 de dados (tamanho corta o último bloco).
    Devolve (caminho, bytes originais do arquivo)."""
    f = ie.ExtImg('ext4')
    d1, d2 = conteudo(2048, 11), conteudo(1500, 12)
    original = d1 + bytes(3 * ie.BS) + d2
    ino = f.prox_ino
    f.prox_ino += 1
    b1, b2 = f._blocos(2), f._blocos(2)
    for k, b in enumerate(b1):
        f._escreve_bloco(b, d1[k * ie.BS:(k + 1) * ie.BS])
    for k, b in enumerate(b2):
        f._escreve_bloco(b, d2[k * ie.BS:(k + 1) * ie.BS])
    f.blocos_de[ino] = b1 + b2
    f.inodes[ino] = f._inode(0x8000 | 0o644, len(original), 1, 0x80000,
                             f._iblock_extents([(0, b1[0], 2), (5, b2[0], 2)]))
    f.pastas[2]['ents'].append([ino, 'esparso.bin', 1, False])
    f.add_file(2, 'comum.bin', conteudo(3000, 13))
    return f.gravar(caminho), original


class TestAchadoDeEsparso(unittest.TestCase):
    def entrada(self, runs, tamanho):
        return Entrada(nome='x.bin', caminho='/x.bin', eh_dir=False, tamanho=tamanho, runs=runs)

    def test_buraco_vira_extent_nulo_e_nao_some_da_lista(self):
        a, motivo = arquivos_sa.achado_de(self.entrada([(4096, 1024), (None, 3072), (9216, 100)], 4196))
        self.assertIsNone(motivo)
        self.assertEqual(a['extents'], [[4096, 1024], [None, 3072], [9216, 100]])
        self.assertEqual(a['tamanho'], 4196)

    def test_so_buraco_e_buraco_no_comeco(self):
        a, motivo = arquivos_sa.achado_de(self.entrada([(None, 2048)], 2048))
        self.assertEqual((motivo, a['extents']), (None, [[None, 2048]]))
        a, _ = arquivos_sa.achado_de(self.entrada([(None, 1024), (8192, 512)], 1536))
        linha = mod_achados.normalizar('j', a)
        self.assertEqual(linha[8], 8192)                       # offset = primeira posição real, nunca None

    def test_extents_de_aceita_nulo(self):
        self.assertEqual(mod_achados.extents_de({'extents': [[0, 10], [None, 5]], 'tamanho': 15}), [(0, 10), (None, 5)])

    def test_posicao_negativa_continua_invalida(self):
        self.assertEqual(arquivos_sa.achado_de(self.entrada([(-5, 10), (None, 10)], 20)), (None, 'invalido'))


class TestEsparsoDePontaAPonta(BaseVarredura):
    def test_lista_traz_o_arquivo_e_o_recuperado_tem_o_mesmo_sha256(self):
        caminho, original = ext4_com_esparso(os.path.join(self.trabalho, 'e4.img'))
        job = self.varrer(caminho, 'sistema')
        self.esperar_estado(job['id'], 'concluido')
        lista = {x['caminho']: mod_achados.como_dict(x) for x in self.ctx.store._con().execute(
            'SELECT * FROM achados WHERE job_id=?', (job['id'],))}
        self.assertEqual(set(lista), {'/esparso.bin', '/comum.bin'})
        a = lista['/esparso.bin']
        self.assertEqual(a['tamanho'], len(original))
        self.assertIn(None, [o for o, _ in a['extents']])
        with leitor_mod.abrir(caminho) as l:                    # a leitura pela lista também dá zeros no buraco
            self.assertEqual(b''.join(mod_achados.ler_achado(l, a)), original)
        self.destino = os.path.join(self.pasta, 'saida')
        r = self.req('POST', '/api/jobs/%s/recuperar' % job['id'], {'ids': 'todos', 'destino': self.destino})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.esperar_estado(r.get_json()['id'], 'concluido')
        achado = [os.path.join(b, n) for b, _, ns in os.walk(self.destino) for n in ns if n == 'esparso.bin']
        self.assertEqual(len(achado), 1)
        with open(achado[0], 'rb') as g:
            recuperado = g.read()
        self.assertEqual(hashlib.sha256(recuperado).hexdigest(), hashlib.sha256(original).hexdigest())
        con = self.ctx.store._con()
        self.assertEqual(con.execute("SELECT resultado FROM recuperacoes WHERE job_id=? AND achado_id=?",
                                     (r.get_json()['id'], a['id'])).fetchone()[0], 'ok')   # zeros do buraco não são falha


if __name__ == '__main__':
    unittest.main()
