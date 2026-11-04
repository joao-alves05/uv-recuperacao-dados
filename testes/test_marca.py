# -*- coding: utf-8 -*-
"""Marca vem de configuração (nunca do código) e o repositório não carrega dado da empresa."""
import json
import os
import re
import unittest
from unittest import mock

import auxiliar
from auxiliar import RAIZ, CasoComPasta

from recuperacao import config
from recuperacao import servico

NEUTRA = {'app': 'UV Recuperação de Dados', 'empresa': '', 'site': '', 'cor': ''}
EXTENSOES = {'.py', '.js', '.css', '.html', '.md', '.json', '.txt', '.cmd', '.bat', '.ps1', '.sh'}
IGNORAR = {'referencia-antiga', 'docs', '__pycache__', '.git'}


class TestMarca(CasoComPasta):
    def amb(self, **extra):
        env = {'RECDADOS_DADOS': self.dados}
        env.update(extra)
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        for k in [k for k in os.environ if k.startswith('RECDADOS_MARCA_') and k not in env]:
            del os.environ[k]

    def test_padrao_neutro(self):
        self.amb()
        self.assertEqual(config.marca(), NEUTRA)

    def test_variaveis_de_ambiente(self):
        self.amb(RECDADOS_MARCA_APP='Meu App', RECDADOS_MARCA_EMPRESA='Empresa X',
                 RECDADOS_MARCA_SITE='https://exemplo.com.br', RECDADOS_MARCA_COR='#12aB34')
        self.assertEqual(config.marca(), {'app': 'Meu App', 'empresa': 'Empresa X',
                                          'site': 'https://exemplo.com.br', 'cor': '#12aB34'})

    def test_marca_json_e_variavel_vence(self):
        self.amb(RECDADOS_MARCA_APP='Do ambiente')
        with open(os.path.join(self.dados, 'marca.json'), 'w', encoding='utf-8') as f:
            json.dump({'app': 'Do arquivo', 'empresa': 'Arq Ltda', 'cor': '#000000'}, f)
        self.assertEqual(config.marca(), {'app': 'Do ambiente', 'empresa': 'Arq Ltda', 'site': '', 'cor': '#000000'})

    def test_valores_invalidos_caem_no_padrao(self):
        self.amb(RECDADOS_MARCA_APP='x' * 81, RECDADOS_MARCA_EMPRESA='y' * 81,
                 RECDADOS_MARCA_SITE='http://sem-tls.com', RECDADOS_MARCA_COR='vermelho')
        self.assertEqual(config.marca(), NEUTRA)
        for site in ('javascript:alert(1)', 'ftp://x', '//x.com'):
            self.amb(RECDADOS_MARCA_SITE=site)
            self.assertEqual(config.marca()['site'], '')
        for cor in ('#fff', '#12345g', 'red', '#1234567', 'url(x)'):
            self.amb(RECDADOS_MARCA_COR=cor)
            self.assertEqual(config.marca()['cor'], '')

    def test_marca_json_quebrado_ou_de_tipo_errado(self):
        self.amb()
        caminho = os.path.join(self.dados, 'marca.json')
        for conteudo in ('{quebrado', '[1,2]', json.dumps({'app': 5, 'cor': ['#000000'], 'site': 'http://a.com'})):
            with open(caminho, 'w', encoding='utf-8') as f:
                f.write(conteudo)
            self.assertEqual(config.marca(), NEUTRA)

    def test_rota_api_marca_exige_token(self):
        self.amb(RECDADOS_PORTA=str(auxiliar.porta_livre()), RECDADOS_MARCA_APP='Meu App')
        ctx = servico.Contexto()
        self.addCleanup(ctx.store.fechar)
        c = servico.criar_app(ctx).test_client()
        base = 'http://127.0.0.1:%d' % ctx.porta
        self.assertEqual(c.get('/api/marca', base_url=base).status_code, 401)
        r = c.get('/api/marca', base_url=base, headers={'X-RecDados-Token': ctx.token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {**NEUTRA, 'app': 'Meu App'})


class TestSemDadoDaEmpresaNoCodigo(unittest.TestCase):
    def test_nenhum_arquivo_do_projeto_cita_a_empresa(self):
        # só DADO da empresa reprova (o nome do produto pode ter "UV"); padrões em pedaços para este arquivo não se acusar
        proibidos = [re.compile(p, re.I) for p in (
            'uv' + 'informatica', 'cachoeir' + 'inha', r'cn' + r'pj', r'\(\d{2}\)\s?9?\d{4}-\d{4}',
            r'[\w.+-]+@[\w-]+\.(?:com|net|org)(?:\.br)?')]
        achados = []
        for pasta, dirs, arquivos in os.walk(RAIZ):
            dirs[:] = [d for d in dirs if d not in IGNORAR]
            for nome in arquivos:
                if os.path.splitext(nome)[1].lower() not in EXTENSOES:
                    continue
                caminho = os.path.join(pasta, nome)
                with open(caminho, encoding='utf-8', errors='replace') as f:
                    for n, linha in enumerate(f, 1):
                        if any(p.search(linha) for p in proibidos):
                            achados.append('%s:%d' % (os.path.relpath(caminho, RAIZ), n))
        self.assertEqual(achados, [])


if __name__ == '__main__':
    unittest.main()
