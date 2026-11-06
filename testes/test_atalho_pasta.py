# -*- coding: utf-8 -*-
"""Só junção e link simbólico são recusados como pasta de destino; nuvem (OneDrive), volume montado e deduplicação
(outros pontos de reanálise) valem. Todos os níveis abaixo da pasta escolhida são conferidos."""
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from recuperacao.tipos import recuperar

NUVEM, MONTAGEM, DEDUP, JUNCAO, SIMB = 0x9000001A, 0xA0000003, 0x80000013, 0xA0000003, 0xA000000C


def lstat_falso(tags):
    def lstat(caminho):
        tag = tags.get(os.path.normcase(caminho), 0)
        return SimpleNamespace(st_file_attributes=0x400 if tag else 0, st_reparse_tag=tag)
    return lstat


class TestAtalho(unittest.TestCase):
    def test_nuvem_e_deduplicacao_nao_sao_atalho(self):
        for tag in (NUVEM, DEDUP):
            with mock.patch('os.path.islink', return_value=False), \
                    mock.patch('os.lstat', lstat_falso({os.path.normcase('X'): tag})):
                self.assertFalse(recuperar.eh_atalho('X'), hex(tag))

    def test_juncao_e_link_simbolico_sao_atalho(self):
        for tag in (JUNCAO, SIMB):
            with mock.patch('os.path.islink', return_value=False), \
                    mock.patch('os.lstat', lstat_falso({os.path.normcase('X'): tag})):
                self.assertTrue(recuperar.eh_atalho('X'), hex(tag))
        with mock.patch('os.path.islink', return_value=True):
            self.assertTrue(recuperar.eh_atalho('X'))

    def test_confere_todos_os_niveis_abaixo_do_destino_mas_nao_o_proprio_destino(self):
        base = os.path.join('D', 'saida')
        meio = os.path.join(base, 'Fotos')
        ponta = os.path.join(meio, 'sub')
        with mock.patch('os.path.islink', return_value=False):
            with mock.patch('os.lstat', lstat_falso({os.path.normcase(meio): MONTAGEM})):
                self.assertEqual(recuperar.atalho_abaixo(base, ponta), meio)      # nível do meio, não só o último
            with mock.patch('os.lstat', lstat_falso({os.path.normcase(base): MONTAGEM})):
                self.assertIsNone(recuperar.atalho_abaixo(base, ponta))           # a pasta escolhida vale
            with mock.patch('os.lstat', lstat_falso({os.path.normcase(ponta): NUVEM})):
                self.assertIsNone(recuperar.atalho_abaixo(base, ponta))


if __name__ == '__main__':
    unittest.main()
