# -*- coding: utf-8 -*-
"""Fase 5.3: API de achados (filtros, ordem, paginação, resumo) e job `recuperar` (por grupo, retomável, relatório)."""
import csv
import os
import unittest
from unittest import mock

import amostras as A
from auxiliar import esperar, sha256_de

from recuperacao import unidade
from recuperacao.executor import ErroJob
from recuperacao.tipos import recuperar
from test_somente_leitura import Espiao
from test_varredura import BaseVarredura, montar_imagem


def fazer_job_com_achados(base, lista, origem=None):
    origem = origem or os.path.join(base.trabalho, 'o.img')
    if not os.path.exists(origem):
        with open(origem, 'wb') as f:
            f.write(bytes(8192))
    job = base.ctx.store.criar('varredura', origem, 'varredura#' + os.urandom(4).hex())
    ids = base.ctx.store.adicionar_achados(job['id'], lista)
    return job, ids


def ler(caminho, modo='rb'):
    with open(caminho, modo) as f:
        return f.read()


def a(nome, tamanho=100, **kw):
    d = {'nome': nome, 'tamanho': tamanho, 'offset': 0, 'grupo': 'foto', 'tipo': 'JPG', 'extensao': 'jpg', 'estado': 'bom'}
    d.update(kw)
    return d


class TestListaDeAchados(BaseVarredura):
    def setUp(self):
        super().setUp()
        lista = []
        for i in range(120):
            lista.append(a('foto%03d.jpg' % i, tamanho=1000 + i, data='2021-05-%02d 10:00:00' % (1 + i % 28) if i % 10 else None))
        lista += [a('doc_a.pdf', 5000, grupo='documento', tipo='PDF', extensao='pdf', data='2020-01-01'),
                  a('Doc_b.docx', 300, grupo='documento', tipo='DOCX', extensao='docx', estado='parcial', data='2022-12-31 23:59:59'),
                  a('video.mp4', 90000, grupo='video', tipo='MP4', extensao='mp4', estado='ruim'),
                  a('100%_certo.zip', 70, grupo='compactado', tipo='ZIP', extensao='zip')]
        self.job, self.ids = fazer_job_com_achados(self, lista)
        self.url = '/api/jobs/%s/achados' % self.job['id']

    def get(self, **q):
        r = self.req('GET', self.url + ('?' + '&'.join('%s=%s' % kv for kv in q.items()) if q else ''))
        return r

    def test_forma_da_resposta_e_paginacao(self):
        d = self.get(por_pagina=50).get_json()
        self.assertEqual((d['total'], d['pagina'], d['por_pagina'], len(d['itens'])), (124, 1, 50, 50))
        self.assertEqual(d['resumo']['por_grupo'], {'foto': 120, 'documento': 2, 'video': 1, 'compactado': 1})
        item = d['itens'][0]
        for campo in ('id', 'nome', 'caminho', 'apagado', 'grupo', 'tipo', 'extensao', 'tamanho', 'offset', 'extents',
                      'estado', 'pontuacao', 'data', 'largura', 'altura', 'recuperado_em', 'previa'):
            self.assertIn(campo, item)
        self.assertEqual(item['previa'], '/api/achados/%d/previa' % item['id'])
        p1 = [i['id'] for i in d['itens']]
        p3 = [i['id'] for i in self.get(por_pagina=50, pagina=3).get_json()['itens']]
        self.assertEqual(len(p3), 24)
        self.assertFalse(set(p1) & set(p3))
        self.assertEqual(self.get(pagina=99).get_json()['itens'], [])
        todos = []
        for pg in (1, 2, 3):
            todos += [i['id'] for i in self.get(por_pagina=50, pagina=pg).get_json()['itens']]
        self.assertEqual(sorted(todos), sorted(self.ids))

    def nomes(self, **q):
        return [i['nome'] for i in self.get(por_pagina=500, **q).get_json()['itens']]

    def test_ordem_por_nome_tamanho_data_e_tipo_nos_dois_sentidos(self):
        asc = self.nomes(ordem='nome', sentido='asc')
        self.assertEqual(asc, sorted(asc, key=str.lower))
        self.assertEqual(self.nomes(ordem='nome', sentido='desc'), asc[::-1])
        self.assertEqual(asc[0], '100%_certo.zip')
        t = [i['tamanho'] for i in self.get(por_pagina=500, ordem='tamanho', sentido='asc').get_json()['itens']]
        self.assertEqual(t, sorted(t))
        t = [i['tamanho'] for i in self.get(por_pagina=500, ordem='tamanho', sentido='desc').get_json()['itens']]
        self.assertEqual(t, sorted(t, reverse=True))
        for sentido in ('asc', 'desc'):
            datas = [i['data'] for i in self.get(por_pagina=500, ordem='data', sentido=sentido).get_json()['itens']]
            com = [x for x in datas if x]
            self.assertEqual(com, sorted(com, reverse=sentido == 'desc'))
            sem = datas.index(None)
            self.assertTrue(all(x is None for x in datas[sem:]), 'sem data vai para o fim em %s' % sentido)
        tipos = [i['tipo'] for i in self.get(por_pagina=500, ordem='tipo').get_json()['itens']]
        self.assertEqual(tipos, sorted(tipos, key=str.lower))

    def test_filtros(self):
        self.assertEqual(self.get(grupo='documento').get_json()['total'], 2)
        self.assertEqual(self.get(grupo='documento,video').get_json()['total'], 3)
        self.assertEqual(self.get(tipo='PDF').get_json()['total'], 1)
        self.assertEqual(self.get(estado='ruim').get_json()['total'], 1)
        self.assertEqual(self.get(estado='parcial,ruim').get_json()['total'], 2)
        self.assertEqual(self.get(min=90000).get_json()['total'], 1)
        self.assertEqual(self.get(max=100).get_json()['total'], 1)
        self.assertEqual(self.get(min=300, max=1000).get_json()['total'], 2)
        self.assertEqual(self.nomes(de='2022-12-31', ate='2022-12-31'), ['Doc_b.docx'])       # o dia inteiro conta
        self.assertEqual(self.get(de='2020-01-01', ate='2020-01-01').get_json()['total'], 1)
        self.assertEqual(self.nomes(busca='DOC_'), ['doc_a.pdf', 'Doc_b.docx'])
        self.assertEqual(self.nomes(busca='100%'), ['100%_certo.zip'])                         # % é texto, não curinga
        self.assertEqual(self.get(busca='_').get_json()['total'], 3)                            # _ também
        self.assertEqual(self.get(grupo='foto', estado='bom', busca='foto01').get_json()['total'], 10)

    def test_resumo_ignora_o_filtro_de_grupo_mas_respeita_os_outros(self):
        d = self.get(grupo='documento', estado='parcial').get_json()
        self.assertEqual(d['total'], 1)
        self.assertEqual(d['resumo']['por_grupo'], {'documento': 1})
        d = self.get(grupo='video', estado='bom').get_json()
        self.assertEqual(d['total'], 0)
        self.assertEqual(d['resumo']['por_grupo'], {'foto': 120, 'documento': 1, 'compactado': 1})

    def test_parametros_invalidos_dao_400_com_frase(self):
        ruins = [dict(grupo='planeta'), dict(estado='otimo'), dict(min='abc'), dict(min=-1), dict(min=10, max=5),
                 dict(de='ontem'), dict(ate='2021-13'), dict(ordem='cor'), dict(sentido='cima'), dict(pagina=0),
                 dict(pagina='x'), dict(por_pagina=0), dict(por_pagina=501), dict(busca='x' * 101)]
        for q in ruins:
            r = self.get(**q)
            self.assertEqual(r.status_code, 400, q)
            self.assertTrue(r.get_json()['erro'])

    def test_job_inexistente_outro_tipo_e_sem_login(self):
        self.assertEqual(self.req('GET', '/api/jobs/j-20200101-abcd/achados').status_code, 404)
        self.assertEqual(self.req('GET', '/api/jobs/xyz/achados').status_code, 404)
        img = self.ctx.store.criar('imagem', 'x', 'y')
        self.assertEqual(self.req('GET', '/api/jobs/%s/achados' % img['id']).status_code, 409)
        self.assertEqual(self.req('GET', self.url, headers={}).status_code, 401)

    def test_so_mostra_achados_do_proprio_job(self):
        outro, _ = fazer_job_com_achados(self, [a('so_dele.jpg')], origem=os.path.join(self.trabalho, 'o2.img'))
        self.assertEqual(self.get().get_json()['total'], 124)
        self.assertEqual(self.req('GET', '/api/jobs/%s/achados' % outro['id']).get_json()['total'], 1)


class TestPastaETotal(BaseVarredura):
    def test_filtro_de_pasta_tamanho_total_e_relatorio_abrir(self):
        lista = [a('a.jpg', 10, caminho='C:' + chr(92) + 'Fotos' + chr(92) + 'a.jpg', apagado=False),
                 a('b.jpg', 20, caminho='C:' + chr(92) + 'Fotos' + chr(92) + 'ferias' + chr(92) + 'b.jpg'),
                 a('c.jpg', 40, caminho='C:/Fotos2/c.jpg'),
                 a('d.jpg', 80, caminho='c:' + chr(92) + 'fotos' + chr(92) + 'ferias' + chr(92) + 'praia' + chr(92) + 'd.jpg'),
                 a('e.jpg', 160)]
        job, ids = fazer_job_com_achados(self, lista)
        url = '/api/jobs/%s/achados' % job['id']

        def nomes(q):
            return sorted(i['nome'] for i in self.req('GET', url + '?pasta=' + q).get_json()['itens'])
        self.assertEqual(nomes('C:/Fotos'), ['a.jpg', 'b.jpg', 'd.jpg'])
        self.assertEqual(nomes('C:%5CFotos%5C'), ['a.jpg', 'b.jpg', 'd.jpg'])
        self.assertEqual(nomes('C:/Fotos/ferias'), ['b.jpg', 'd.jpg'])
        self.assertEqual(nomes('C:/Fotos/ferias/praia'), ['d.jpg'])
        self.assertEqual(nomes('C:/Foto'), [])
        self.assertEqual(nomes('C:/Fotos2'), ['c.jpg'])
        self.assertEqual(nomes('%25'), [])
        d = self.req('GET', url).get_json()
        self.assertEqual(d['resumo']['tamanho_total'], 310)
        d = self.req('GET', url + '?pasta=C:/Fotos/ferias&por_pagina=100').get_json()
        self.assertEqual((d['resumo']['tamanho_total'], d['total']), (100, 2))
        d = self.req('GET', url + '?grupo=video').get_json()
        self.assertEqual((d['resumo']['tamanho_total'], d['resumo']['por_grupo']['foto']), (0, 5))

    def test_abrir_e_relatorio(self):
        caminho, info, _ = montar_imagem(self.trabalho)
        v = self.varrer(caminho, bloco=65536)
        self.esperar_estado(v['id'], 'concluido')
        destino = os.path.join(self.pasta, 'saida')
        r = self.req('POST', '/api/jobs/%s/recuperar' % v['id'], {'ids': 'todos', 'destino': destino}).get_json()
        self.esperar_estado(r['id'], 'concluido')
        rel = self.req('GET', '/api/jobs/%s/relatorio' % r['id'])
        self.assertEqual((rel.status_code, rel.mimetype), (200, 'text/html'))
        self.assertIn(b'7 arquivos', rel.data)
        self.assertNotIn(b'<style>', rel.data)
        self.assertIn(b'/relatorio.css', rel.data)
        css = self.req('GET', '/api/jobs/%s/relatorio.css' % r['id'])
        self.assertEqual((css.status_code, css.mimetype), (200, 'text/css'))
        self.assertEqual(self.req('GET', '/api/jobs/%s/relatorio' % r['id'], headers={}).status_code, 401)
        self.assertEqual(self.req('GET', '/api/jobs/%s/relatorio' % v['id']).status_code, 409)
        self.assertEqual(self.req('GET', '/api/jobs/j-20200101-abcd/relatorio').status_code, 404)
        abertos = []
        with mock.patch('os.startfile', abertos.append, create=True):
            self.ctx.eh_windows = False
            self.assertEqual(self.req('POST', '/api/jobs/%s/abrir' % r['id']).status_code, 409)
            self.ctx.eh_windows = True
            self.assertEqual(self.req('POST', '/api/jobs/%s/abrir' % r['id']).status_code, 200)
            self.assertEqual(abertos, [destino])                      # a pasta vem do job, não do pedido
            self.assertEqual(self.req('POST', '/api/jobs/%s/abrir' % v['id']).status_code, 409)
            self.assertEqual(self.req('POST', '/api/jobs/%s/abrir' % r['id'], headers={}).status_code, 401)


class TestRecuperar(BaseVarredura):
    def varrer_imagem(self, **kw):
        caminho, info, _ = montar_imagem(self.trabalho)
        job = self.varrer(caminho, bloco=65536)
        self.esperar_estado(job['id'], 'concluido')
        self.destino = os.path.join(self.pasta, 'saida')
        return caminho, info, job

    def recuperar(self, job, corpo=None, esperado=201):
        corpo = {'ids': 'todos', 'destino': self.destino} if corpo is None else corpo
        r = self.req('POST', '/api/jobs/%s/recuperar' % job['id'], corpo)
        self.assertEqual(r.status_code, esperado, r.get_json())
        return r.get_json()

    def arquivos(self):
        achados = []
        for raiz, _, nomes in os.walk(self.destino):
            achados += [os.path.relpath(os.path.join(raiz, n), self.destino).replace('\\', '/') for n in nomes]
        return sorted(achados)

    def test_recuperar_todos_por_grupo_com_bytes_iguais_relatorio_e_origem_intacta(self):
        caminho, info, job = self.varrer_imagem()
        antes = (sha256_de(caminho), os.stat(caminho).st_mtime_ns)
        with Espiao(caminho) as espiao:
            r = self.recuperar(job)
            self.assertEqual((r['tipo'], r['destino'], r['origem']), ('recuperar', self.destino, caminho))
            fim = self.esperar_estado(r['id'], 'concluido')
        self.assertEqual(espiao.escritas(), [])
        self.assertEqual((sha256_de(caminho), os.stat(caminho).st_mtime_ns), antes)
        self.assertEqual(fim['progresso'], 1.0)
        arqs = self.arquivos()
        self.assertEqual(len([x for x in arqs if not x.startswith('relatorio')]), 7)
        self.assertFalse([x for x in arqs if x.endswith('.parte')])
        for pasta in ('Fotos', 'Documentos', 'Compactados', 'Videos'):
            self.assertTrue(os.path.isdir(os.path.join(self.destino, pasta)), pasta)
        achados = {x['offset']: x for x in self.achados_do_banco(job['id'])}
        sha_originais = {A.sha256(f['dados']) for f in info if f['nome'] != 'CORTADO.JPG'}
        achou = set()
        for a_ in achados.values():
            pasta = {'foto': 'Fotos', 'video': 'Videos', 'documento': 'Documentos', 'compactado': 'Compactados'}[a_['grupo']]
            caminho_rec = os.path.join(self.destino, pasta, a_['nome'])
            self.assertTrue(os.path.isfile(caminho_rec), caminho_rec)
            self.assertEqual(os.path.getsize(caminho_rec), a_['tamanho'])
            if a_['estado'] == 'bom':
                achou.add(sha256_de(caminho_rec))
            self.assertIsNotNone(a_['recuperado_em'])
        self.assertEqual(achou, sha_originais)
        # relatório
        with open(os.path.join(self.destino, 'relatorio.csv'), encoding='utf-8-sig', newline='') as f:
            linhas = list(csv.DictReader(f))
        self.assertEqual(len(linhas), 7)
        self.assertEqual(sorted(l['estado'] for l in linhas), ['bom'] * 6 + ['parcial'])
        self.assertTrue(all(l['resultado'] == 'ok' and len(l['sha256']) == 64 for l in linhas))
        with open(os.path.join(self.destino, 'relatorio.html'), encoding='utf-8') as f:
            pagina = f.read()
        self.assertIn('7 arquivos', pagina)
        self.assertIn('parcial', pagina)

    def test_ids_escolhidos_e_filtros(self):
        _, _, job = self.varrer_imagem()
        todos = self.achados_do_banco(job['id'])
        fotos = [x['id'] for x in todos if x['grupo'] == 'foto']
        r = self.recuperar(job, {'ids': fotos[:1], 'destino': self.destino})
        self.esperar_estado(r['id'], 'concluido')
        self.assertEqual([x for x in self.arquivos() if not x.startswith('relatorio')], ['Fotos/' + todos[0]['nome']])
        d2 = os.path.join(self.pasta, 'saida2')
        r = self.recuperar(job, {'ids': 'todos', 'filtros': {'estado': 'bom', 'grupo': 'documento'}, 'destino': d2})
        self.esperar_estado(r['id'], 'concluido')
        self.assertEqual(len(os.listdir(os.path.join(d2, 'Documentos'))), 2)
        self.assertFalse(os.path.exists(os.path.join(d2, 'Fotos')))

    def test_nomes_repetidos_ganham_numero_e_nada_existente_e_sobrescrito(self):
        origem = os.path.join(self.trabalho, 'o.img')
        with open(origem, 'wb') as f:
            f.write(b'AAAABBBBCCCC')
        job, ids = fazer_job_com_achados(self, [
            a('praia.jpg', 4, offset=0, caminho='C:\\Fotos\\praia.jpg', apagado=True),
            a('praia.jpg', 4, offset=4, caminho='D:\\Outra\\praia.jpg', apagado=False),
            a('praia.jpg', 4, offset=8, caminho='praia.jpg'),
            a('..\\..\\fora.jpg', 4, offset=0), a('CON.jpg', 4, offset=0)], origem=origem)
        self.destino = os.path.join(self.pasta, 'saida')
        os.makedirs(os.path.join(self.destino, 'Fotos'))
        with open(os.path.join(self.destino, 'Fotos', 'praia.jpg'), 'wb') as f:
            f.write(b'JA EXISTIA')
        r = self.recuperar(job)
        self.esperar_estado(r['id'], 'concluido')
        self.assertEqual(ler(os.path.join(self.destino, 'Fotos', 'praia.jpg')), b'JA EXISTIA')
        lidos = {n: ler(os.path.join(self.destino, 'Fotos', n)) for n in os.listdir(os.path.join(self.destino, 'Fotos'))}
        self.assertEqual(lidos['praia (2).jpg'], b'AAAA')
        self.assertEqual(lidos['praia (3).jpg'], b'BBBB')
        self.assertEqual(lidos['praia (4).jpg'], b'CCCC')
        self.assertIn('fora.jpg', lidos)
        self.assertIn('_CON.jpg', lidos)
        self.assertFalse(os.path.exists(os.path.join(self.pasta, 'fora.jpg')))
        self.assertFalse(os.path.exists(os.path.join(self.destino, 'fora.jpg')))

    def test_recusas(self):
        _, _, job = self.varrer_imagem()
        ids = [x['id'] for x in self.achados_do_banco(job['id'])]
        outro, ids_outro = fazer_job_com_achados(self, [a('x.jpg')], origem=os.path.join(self.trabalho, 'o2.img'))
        arq = os.path.join(self.pasta, 'arquivo.txt')
        open(arq, 'w').close()
        casos = [
            {'ids': 'todos'}, {'ids': 'todos', 'destino': ''}, {'ids': 'todos', 'destino': arq},
            {'ids': 'todos', 'destino': job['origem']}, {'ids': 'todos', 'destino': '\\\\servidor\\x\\y'},
            {'ids': 'todos', 'destino': os.path.join(job['origem'], 'dentro')},
            {'destino': self.destino}, {'ids': [], 'destino': self.destino}, {'ids': 'tudo', 'destino': self.destino},
            {'ids': [ids[0], 'a'], 'destino': self.destino}, {'ids': [True], 'destino': self.destino},
            {'ids': ids_outro, 'destino': self.destino}, {'ids': [ids[0], 999999], 'destino': self.destino},
            {'ids': 'todos', 'filtros': {'grupo': 'x'}, 'destino': self.destino},
            {'ids': 'todos', 'filtros': {'busca': 'nada-disto-existe'}, 'destino': self.destino}, 'texto']
        for corpo in casos:
            r = self.req('POST', '/api/jobs/%s/recuperar' % job['id'], corpo)
            self.assertEqual(r.status_code, 400, (corpo, r.get_json()))
            self.assertTrue(r.get_json()['erro'])
        self.assertEqual(self.req('POST', '/api/jobs/j-20200101-abcd/recuperar', {}).status_code, 404)
        self.assertEqual(self.req('POST', '/api/jobs/%s/recuperar' % job['id'], {'ids': 'todos', 'destino': self.destino},
                                  headers={}).status_code, 401)
        img = self.ctx.store.criar('imagem', 'x', 'y')
        self.assertEqual(self.req('POST', '/api/jobs/%s/recuperar' % img['id'], {'ids': 'todos', 'destino': self.destino}).status_code, 409)
        self.assertFalse(os.path.exists(self.destino))
        self.assertEqual([j['tipo'] for j in self.req('GET', '/api/jobs').get_json()].count('recuperar'), 0)

    def test_destino_no_mesmo_disco_da_origem_e_recusado(self):
        class Mapa:
            def discos_da_origem(self, o):
                return {'7'}

            def discos_do_destino(self, d):
                return self.destino
        mapa = Mapa()
        origem = '\\\\.\\PhysicalDrive7'                           # só o nome: nada abre este disco
        job = self.ctx.store.criar('varredura', origem, 'varredura#disco')
        self.ctx.store.adicionar_achados(job['id'], [a('x.jpg')])
        with mock.patch.object(unidade, 'MAPA', mapa):
            mapa.destino = {'7'}
            r = self.req('POST', '/api/jobs/%s/recuperar' % job['id'], {'ids': 'todos', 'destino': os.path.join(self.pasta, 'rec')})
            self.assertEqual(r.status_code, 400)
            self.assertIn('mesmo disco', r.get_json()['erro'])
            mapa.destino = None                                    # não dá para saber: também recusa
            r = self.req('POST', '/api/jobs/%s/recuperar' % job['id'], {'ids': 'todos', 'destino': os.path.join(self.pasta, 'rec')})
            self.assertEqual(r.status_code, 400)
        self.assertFalse(os.path.exists(os.path.join(self.pasta, 'rec')))

    def test_destino_em_uso_da_409(self):
        _, _, job = self.varrer_imagem()
        self.ctx.store.criar('recuperar', 'x', self.destino)
        self.recuperar(job, esperado=409)

    def test_pausar_e_retomar_nao_repete_nem_perde(self):
        _, info, job = self.varrer_imagem()
        with mock.patch.dict(os.environ, {'RECDADOS_ATRASO_BLOCO': '0.15'}):
            r = self.recuperar(job)
            esperar(lambda: len(os.listdir(os.path.join(self.destino, 'Fotos'))) >= 1 if os.path.isdir(os.path.join(self.destino, 'Fotos')) else False, 20, 0.02)
            self.assertEqual(self.req('POST', '/api/jobs/%s/pausar' % r['id']).status_code, 200)
            n = self.ctx.store._con().execute("SELECT COUNT(*) FROM recuperacoes WHERE job_id=? AND resultado='ok'", (r['id'],)).fetchone()[0]
            self.assertTrue(0 < n < 7, n)
            self.assertEqual(self.req('POST', '/api/jobs/%s/retomar' % r['id']).status_code, 200)
            self.esperar_estado(r['id'], 'concluido')
        arqs = [x for x in self.arquivos() if not x.startswith('relatorio')]
        self.assertEqual(len(arqs), 7)
        self.assertEqual(len(arqs), len(set(x.casefold() for x in arqs)))
        con = self.ctx.store._con()
        self.assertEqual(con.execute('SELECT COUNT(*), COUNT(DISTINCT achado_id) FROM recuperacoes WHERE job_id=?', (r['id'],)).fetchone()[:], (7, 7))

    def test_queda_no_meio_e_nova_execucao_termina_sem_duplicar(self):
        _, info, job = self.varrer_imagem()
        r = self.recuperar(job)
        self.esperar_estado(r['id'], 'concluido')
        # simula a queda: apaga a pasta e o resultado de metade, deixa uma linha "copiando" e um .parte velho
        store = self.ctx.store
        con = store._con()
        linhas = con.execute('SELECT achado_id, arquivo FROM recuperacoes WHERE job_id=? ORDER BY ordem', (r['id'],)).fetchall()
        for l in linhas[3:]:
            con.execute("UPDATE recuperacoes SET resultado='copiando' WHERE job_id=? AND achado_id=?", (r['id'], l['achado_id']))
        os.remove(os.path.join(self.destino, *linhas[3]['arquivo'].split('/')))            # sumiu antes do rename
        with open(os.path.join(self.destino, *linhas[4]['arquivo'].split('/')) + '.parte', 'wb') as f:
            f.write(b'lixo de uma queda')
        job_rec = store.obter(r['id'])
        ctrl = mock.Mock(store=store)
        ctrl.pausa_pedida.return_value = ctrl.cancelamento_pedido.return_value = False
        self.assertEqual(recuperar.executar(job_rec, ctrl), 'concluido')
        arqs = [x for x in self.arquivos() if not x.startswith('relatorio')]
        self.assertEqual(len(arqs), 7)
        self.assertFalse([x for x in arqs if x.endswith('.parte')])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM recuperacoes WHERE job_id=? AND resultado='ok'", (r['id'],)).fetchone()[0], 7)
        self.assertEqual(sorted(x for x in os.listdir(self.destino) if x.startswith('relatorio')),
                         sorted(['relatorio.csv', 'relatorio.html', 'relatorio-%s.csv' % r['id'], 'relatorio-%s.html' % r['id']]))

    def test_leitura_que_falha_vira_zeros_e_o_resultado_diz_isso(self):
        origem = os.path.join(self.trabalho, 'o.img')
        with open(origem, 'wb') as f:
            f.write(b'12345678')
        job, ids = fazer_job_com_achados(self, [a('longe.bin', 50, offset=4, grupo='outro', tipo='BIN', extensao='bin')], origem=origem)
        self.destino = os.path.join(self.pasta, 'saida')
        r = self.recuperar(job)
        self.esperar_estado(r['id'], 'concluido')
        self.assertEqual(ler(os.path.join(self.destino, 'Outros', 'longe.bin')), bytes(50))
        with open(os.path.join(self.destino, 'relatorio.csv'), encoding='utf-8-sig', newline='') as f:
            self.assertIn('ilegíveis', list(csv.DictReader(f))[0]['resultado'])

    def test_relatorio_nao_sobrescreve_e_protege_planilha_e_html(self):
        origem = os.path.join(self.trabalho, 'o.img')
        with open(origem, 'wb') as f:
            f.write(b'0123456789')
        job, _ = fazer_job_com_achados(self, [a('=cmd.txt', 5, caminho='=cmd|x', tipo='<b>X</b>', grupo='documento', extensao='txt')], origem=origem)
        self.destino = os.path.join(self.pasta, 'saida')
        os.makedirs(self.destino)
        with open(os.path.join(self.destino, 'relatorio.html'), 'w') as f:
            f.write('de outro programa')
        r = self.recuperar(job)
        self.esperar_estado(r['id'], 'concluido')
        self.assertEqual(ler(os.path.join(self.destino, 'relatorio.html'), 'r'), 'de outro programa')
        with open(os.path.join(self.destino, 'relatorio.csv'), encoding='utf-8-sig', newline='') as f:
            linha = list(csv.DictReader(f))[0]
        self.assertEqual((linha['nome'], linha['caminho_original']), ("'=cmd.txt", "'=cmd|x"))
        with open(os.path.join(self.destino, 'relatorio-%s.html' % r['id']), encoding='utf-8') as f:
            pagina = f.read()
        self.assertIn('&lt;b&gt;X&lt;/b&gt;', pagina)
        self.assertNotIn('<b>X</b>', pagina)

    def test_cancelar_guarda_o_que_ja_foi_recuperado(self):
        _, _, job = self.varrer_imagem()
        with mock.patch.dict(os.environ, {'RECDADOS_ATRASO_BLOCO': '0.2'}):
            r = self.recuperar(job)
            esperar(lambda: os.path.isdir(os.path.join(self.destino, 'Fotos')), 20, 0.02)
            self.assertEqual(self.req('POST', '/api/jobs/%s/cancelar' % r['id']).status_code, 200)
        self.assertTrue([x for x in self.arquivos() if x.startswith('Fotos/')])
        self.assertFalse([x for x in self.arquivos() if x.endswith('.parte')])

    def test_o_tipo_recuperar_nao_nasce_pelo_post_de_jobs(self):
        r = self.req('POST', '/api/jobs', {'tipo': 'recuperar', 'origem': 'x', 'destino': 'y'})
        self.assertEqual(r.status_code, 400)

    def test_lista_de_ids_perdida_falha_com_frase(self):
        job = self.ctx.store.criar('recuperar', os.path.join(self.trabalho, 'o.img'), os.path.join(self.pasta, 's'),
                                   {'varredura': 'j-x'})
        with open(os.path.join(self.trabalho, 'o.img'), 'wb') as f:
            f.write(b'x')
        with self.assertRaises(ErroJob):
            recuperar.executar(job, mock.Mock(store=self.ctx.store))


class TestNomeSeguro(unittest.TestCase):
    def test_nomes(self):
        n = recuperar.nome_seguro
        self.assertEqual(n('a:b?.jpg'), 'a_b_.jpg')
        self.assertEqual(n('..\\..\\x.jpg'), 'x.jpg')
        self.assertEqual(n('/etc/passwd'), 'passwd')
        self.assertEqual(n('CON.txt'), '_CON.txt')
        self.assertEqual(n('nul'), '_nul')
        self.assertEqual(n('  ...  '), 'arquivo')
        self.assertLessEqual(len(n('x' * 500 + '.jpg')), 140)


if __name__ == '__main__':
    unittest.main()
