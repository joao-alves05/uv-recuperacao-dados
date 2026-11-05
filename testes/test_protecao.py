# -*- coding: utf-8 -*-
"""Destinos proibidos (o serviço elevado não grava em pastas de sistema, programas ou inicialização), PowerShell por
caminho completo e o iniciar-admin.cmd sem injeção por nome de pasta."""
import os
import re
import unittest
from unittest import mock

from auxiliar import RAIZ, CasoComPasta, criar_origem

from recuperacao import fontes, protecao
from recuperacao.tipos import imagem, particoes, recuperar


class TestProtecao(CasoComPasta):
    def setUp(self):
        super().setUp()
        self.falso = {}
        for nome in ('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData', 'APPDATA'):
            self.falso[nome] = os.path.join(self.pasta, nome.replace('(', '').replace(')', ''))
            os.makedirs(self.falso[nome])
        p = mock.patch.dict(os.environ, self.falso)
        p.start()
        self.addCleanup(p.stop)
        self.startup_usuario = os.path.join(self.falso['APPDATA'], 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup')

    @unittest.skipUnless(os.name == 'nt', 'pastas do Windows')
    def test_recusa_pastas_do_sistema_e_filhas(self):
        for base in ('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData'):
            for caminho in (self.falso[base], os.path.join(self.falso[base], 'x', 'img.bin')):
                self.assertEqual(protecao.destino_proibido(caminho), protecao.MSG, caminho)
        self.assertEqual(protecao.destino_proibido(os.path.join(self.startup_usuario, 'a.json')), protecao.MSG)

    def test_recusa_a_pasta_do_programa_e_dentro_dela(self):
        self.assertEqual(protecao.destino_proibido(RAIZ), protecao.MSG)
        self.assertEqual(protecao.destino_proibido(os.path.join(RAIZ, 'recuperacao', 'x.json')), protecao.MSG)

    def test_aceita_pasta_comum_e_vizinha_de_nome_parecido(self):
        self.assertIsNone(protecao.destino_proibido(os.path.join(self.trabalho, 'img.bin')))
        self.assertIsNone(protecao.destino_proibido(RAIZ + '-outra'))          # só começa igual: não é filha

    @unittest.skipUnless(os.name == 'nt', 'pastas do Windows')
    def test_validadores_dos_tres_jobs_usam_a_protecao(self):
        origem = criar_origem(self.trabalho, 4096)
        proibido = os.path.join(self.falso['ProgramFiles'], 'dest')
        os.makedirs(proibido)
        self.assertIn('protegida', imagem.validar(origem, os.path.join(proibido, 'img.bin'), {}))
        self.assertIn('protegida', recuperar.validar(origem, proibido))
        self.assertIn('protegida', particoes.validar(origem, os.path.join(proibido, 't.json'), {}))
        self.assertIsNone(imagem.validar(origem, os.path.join(self.trabalho, 'img.bin'), {}))
        self.assertIsNone(particoes.validar(origem, os.path.join(self.trabalho, 't.json'), {}))

    @unittest.skipUnless(os.name == 'nt', 'PowerShell do Windows')
    def test_powershell_por_caminho_completo(self):
        exe = os.path.join(self.falso['SystemRoot'], 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
        os.makedirs(os.path.dirname(exe))
        open(exe, 'wb').close()
        self.assertEqual(fontes._powershell(), exe)
        with mock.patch('subprocess.run') as run:
            run.return_value.stdout = b'{}'
            fontes._executar_powershell('1')
        self.assertEqual(run.call_args[0][0][0], exe)


class TestIniciarAdmin(unittest.TestCase):
    def test_cmd_sem_caminho_em_aspas_simples_e_com_python_resolvido(self):
        with open(os.path.join(RAIZ, 'iniciar-admin.cmd'), encoding='utf-8') as f:
            cmd = f.read()
        self.assertNotIn("'%~f0'", cmd)                                   # pasta com ' quebrava e injetava comando
        self.assertIn('-ArgumentList', cmd)
        self.assertIn('where python', cmd)
        self.assertRegex(cmd, r'"%PY%" iniciar\.py --admin')
        self.assertNotRegex(cmd, r'(?m)^python iniciar')                  # nada de python solto no PATH


if __name__ == '__main__':
    unittest.main()
