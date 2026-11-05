# -*- coding: utf-8 -*-
"""Fase 4, bloco 4: a varredura "pelo sistema de arquivos" (modo `sistema`) e a rota da árvore de pastas.

Imagens montadas em Python (FAT, NTFS, ext3) e um disco com MBR e duas partições; nenhum disco real.
"""
import os
import struct
import unittest
from unittest import mock
from urllib.parse import quote

import amostras as A
import imagens_ext as ie
import imagens_ntfs as nt
import imagens_sa as im
from auxiliar import sha256_de
from test_sa_fat import conteudo, sha
from test_varredura import BaseVarredura

from recuperacao import achados as mod_achados
from recuperacao import leitor as leitor_mod
from recuperacao.sa import ntfs as mod_ntfs
from recuperacao.tipos import varredura


def disco_mbr(pasta, partes):
    """MBR com as imagens `partes` [(tipo MBR, caminho)] a partir de 1 MiB. Devolve (caminho do disco, inícios em bytes)."""
    pos, blobs, entradas = 1 << 20, [], []
    for tipo, caminho in partes:
        with open(caminho, 'rb') as f:
            dados = f.read()
        dados += b'\0' * (-len(dados) % 512)
        entradas.append((tipo, pos // 512, len(dados) // 512))
        blobs.append((pos, dados))
        pos += -(-len(dados) // (1 << 20)) * (1 << 20)
    mbr = bytearray(512)
    for i, (tipo, lba, n) in enumerate(entradas):
        mbr[446 + 16 * i: 462 + 16 * i] = struct.pack('<B3sB3sII', 0, b'\xfe\xff\xff', tipo, b'\xfe\xff\xff', lba, n)
    mbr[510:512] = b'\x55\xaa'
    destino = os.path.join(pasta, 'disco.img')
    with open(destino, 'wb') as f:
        f.truncate(pos)
        f.write(mbr)
        for off, d in blobs:
            f.seek(off)
            f.write(d)
    return destino, [b[0] for b in blobs]


def bytes_do_achado(caminho_origem, achado):
    with leitor_mod.abrir(caminho_origem) as l:
        return b''.join(mod_achados.ler_achado(l, achado))


class Base(BaseVarredura):
    def lista(self, job_id):
        return [mod_achados.como_dict(l) for l in self.ctx.store._con().execute(
            'SELECT * FROM achados WHERE job_id=? ORDER BY id', (job_id,))]

    def por_caminho(self, job_id):
        return {a['caminho']: a for a in self.lista(job_id)}

    def eventos(self, job_id):
        return [e['texto'] if isinstance(e, dict) else e for e in self.ctx.store.eventos(job_id, 500)]


class TestSistemaFat(Base):
    def montar(self):
        f = im.FatImg('FAT32')
        self.d = {'vivo': conteudo(1500, 1), 'longo': conteudo(2300, 2), 'dentro': conteudo(800, 3),
                  'filho': conteudo(600, 4)}
        f.add('/', 'Relatório anual.docx', self.d['vivo'])
        e, cs = f.add('/', 'Apagado com nome longo.pdf', self.d['longo'])
        f.apagar(e, cs)
        f.mkdir('/', 'Fotos')
        f.add('/Fotos', 'viagem de férias.jpg', self.d['dentro'])
        f.mkdir('/', 'Velha')
        pe = f.pastas['/'].entradas[-1]
        e, cs = f.add('/Velha', 'filho.txt', self.d['filho'])
        f.apagar(e, cs)
        f.apagar_pasta('/Velha', pe)
        return f.gravar(os.path.join(self.trabalho, 'pendrive.img'))

    def rodar(self, caminho, **opcoes):
        job = self.varrer(caminho, 'sistema', **opcoes)
        return self.esperar_estado(job['id'], 'concluido')

    def test_lista_com_nome_caminho_apagado_e_bytes_que_batem(self):
        caminho = self.montar()
        antes = (sha256_de(caminho), os.stat(caminho).st_mtime_ns)
        job = self.rodar(caminho)
        self.assertEqual(job['progresso'], 1.0)
        a = self.por_caminho(job['id'])
        self.assertEqual(set(a), {'/Relatório anual.docx', '/Apagado com nome longo.pdf', '/Fotos/viagem de férias.jpg',
                                  '/Velha/filho.txt'})
        x = a['/Apagado com nome longo.pdf']
        self.assertEqual((x['apagado'], x['estado'], x['grupo'], x['tipo'], x['nome'], x['tamanho']),
                         (True, 'bom', 'documento', 'PDF', 'Apagado com nome longo.pdf', 2300))
        self.assertEqual(x['data'], '2024-03-15 10:20:30')
        self.assertIs(a['/Relatório anual.docx']['apagado'], False)
        self.assertEqual(a['/Fotos/viagem de férias.jpg']['grupo'], 'foto')
        self.assertIs(a['/Velha/filho.txt']['apagado'], True)
        for cam, chave in (('/Relatório anual.docx', 'vivo'), ('/Apagado com nome longo.pdf', 'longo'),
                           ('/Fotos/viagem de férias.jpg', 'dentro'), ('/Velha/filho.txt', 'filho')):
            self.assertEqual(sha(bytes_do_achado(caminho, a[cam])), sha(self.d[chave]), cam)
        self.assertEqual((sha256_de(caminho), os.stat(caminho).st_mtime_ns), antes, 'a origem mudou')
        self.assertTrue(any('FAT32' in t for t in self.eventos(job['id'])))

    def test_arvore_de_pastas(self):
        job = self.rodar(self.montar())
        r = self.req('GET', '/api/jobs/%s/arvore' % job['id'])
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json(), {'caminho': '/', 'pastas': [
            {'nome': 'Fotos', 'caminho': '/Fotos', 'total': 1}, {'nome': 'Velha', 'caminho': '/Velha', 'total': 1}]})
        self.assertEqual(self.req('GET', '/api/jobs/%s/arvore?caminho=' % job['id']).get_json()['caminho'], '/')
        sub = self.req('GET', '/api/jobs/%s/arvore?caminho=%s' % (job['id'], quote('/Fotos/'))).get_json()
        self.assertEqual(sub, {'caminho': '/Fotos', 'pastas': []})
        vazio = self.req('GET', '/api/jobs/%s/arvore?caminho=/nao/existe' % job['id']).get_json()
        self.assertEqual(vazio['pastas'], [])

    def test_arvore_conta_subpastas(self):
        f = im.FatImg('FAT32')
        f.mkdir('/', 'A')
        f.mkdir('/A', 'B')
        f.add('/A', 'um.txt', b'1')
        f.add('/A/B', 'dois.txt', b'22')
        f.add('/A/B', 'tres.txt', b'333')
        job = self.rodar(f.gravar(os.path.join(self.trabalho, 'arv.img')))
        raiz = self.req('GET', '/api/jobs/%s/arvore' % job['id']).get_json()
        self.assertEqual(raiz['pastas'], [{'nome': 'A', 'caminho': '/A', 'total': 3}])
        a = self.req('GET', '/api/jobs/%s/arvore?caminho=/A' % job['id']).get_json()
        self.assertEqual(a['pastas'], [{'nome': 'B', 'caminho': '/A/B', 'total': 2}])

    def test_arvore_pedidos_invalidos(self):
        job = self.rodar(self.montar())
        self.assertEqual(self.req('GET', '/api/jobs/j-20240101-abcd/arvore').status_code, 404)
        self.assertEqual(self.req('GET', '/api/jobs/%s/arvore?caminho=%%00' % job['id']).status_code, 400)
        self.assertEqual(self.req('GET', '/api/jobs/%s/arvore' % job['id'], headers={}).status_code, 401)
        _o, _d, r = self.nova_imagem()
        self.assertEqual(self.req('GET', '/api/jobs/%s/arvore' % r.get_json()['id']).status_code, 404)

    def test_origem_sem_sistema_de_arquivos_falha_com_frase(self):
        caminho = os.path.join(self.trabalho, 'lixo.bin')
        with open(caminho, 'wb') as f:
            f.write(os.urandom(300_000))
        job = self.varrer(caminho, 'sistema')
        j = self.esperar_estado(job['id'], 'falhou')
        self.assertIn('Não reconheci nenhum sistema de arquivos', j['erro'])

    def test_opcoes_invalidas(self):
        caminho = self.montar()
        for opcoes in ({'modo': 'tudo'}, {'modo': 'sistema', 'sistema_antes': 'sim'}):
            r = self.req('POST', '/api/jobs', {'tipo': 'varredura', 'origem': caminho, 'opcoes': opcoes})
            self.assertEqual(r.status_code, 400, opcoes)

    def test_sistema_antes_do_carving_no_mesmo_job_sem_duplicar(self):
        f = im.FatImg('FAT16')
        jpg = A.jpeg(scan=3000, data='2021:05:17 10:20:30')
        escondido = A.jpeg(scan=2000, semente=9)
        e, cs = f.add('/', 'foto antiga apagada.jpg', jpg)
        f.apagar(e, cs)
        f.add('/', 'leia-me.txt', conteudo(300, 5))
        caminho = f.gravar(os.path.join(self.trabalho, 'fat16.img'))
        fora = f.dados_ini * 512 + 3000 * f.csize               # área livre que nenhuma pasta aponta
        with open(caminho, 'r+b') as g:
            g.seek(fora)
            g.write(escondido)
        job = self.varrer(caminho, 'rapida', sistema_antes=True, bloco=65536)
        self.esperar_estado(job['id'], 'concluido')
        lista = self.lista(job['id'])
        com_nome = [a for a in lista if a['caminho']]
        sem_nome = [a for a in lista if not a['caminho']]
        self.assertEqual({a['caminho'] for a in com_nome}, {'/foto antiga apagada.jpg', '/leia-me.txt'})
        foto = next(a for a in com_nome if a['caminho'].endswith('.jpg'))
        self.assertFalse([a for a in sem_nome if a['offset'] == foto['offset']], 'o carving repetiu o arquivo com nome')
        self.assertEqual([(a['offset'], a['tamanho']) for a in sem_nome if a['tipo'] == 'JPG'], [(fora, len(escondido))],
                         'o que só o carving acha continua aparecendo')
        self.assertEqual(self.ctx.store.obter_checkpoint(job['id'])['fase'], 'carving')
        self.assertLess(com_nome[0]['id'], sem_nome[0]['id'], 'o sistema de arquivos vem antes')
        self.assertTrue(any('ficaram só com o nome' in t for t in self.eventos(job['id'])))

    def test_sem_sistema_antes_o_carving_mantem_o_arquivo_repetido(self):
        f = im.FatImg('FAT16')
        jpg = A.jpeg(scan=3000)
        e, cs = f.add('/', 'foto.jpg', jpg)
        f.apagar(e, cs)
        caminho = f.gravar(os.path.join(self.trabalho, 'f16.img'))
        job = self.varrer(caminho, 'rapida', bloco=65536)
        self.esperar_estado(job['id'], 'concluido')
        self.assertTrue(any(a['tipo'] == 'JPG' and a['tamanho'] == len(jpg) for a in self.lista(job['id'])))


class TestDiscoComDuasParticoes(Base):
    def test_prefixo_por_particao_offsets_absolutos_e_residente_do_ntfs(self):
        fat = im.FatImg('FAT32')
        dfat = conteudo(2000, 1)
        e, cs = fat.add('/', 'apagado na primeira.bin', dfat)
        fat.apagar(e, cs)
        p1 = fat.gravar(os.path.join(self.trabalho, 'p1.img'))
        n = nt.NtfsImg()
        residente = conteudo(590, 2)                             # cruza o ponto onde o NTFS troca os 2 últimos bytes do setor
        grande = conteudo(4000, 3)
        pasta = n.add_dir(5, 'Docs')
        n.apagar(n.add_file(pasta, 'pequeno apagado.txt', residente))
        n.apagar(n.add_file(pasta, 'grande apagado.bin', grande, fragmentos=2))
        n.add_file(5, 'vivo.txt', conteudo(100, 4))
        p2 = n.gravar(os.path.join(self.trabalho, 'p2.img'))
        disco, _ = disco_mbr(self.trabalho, [(0x0C, p1), (0x07, p2)])
        antes = (sha256_de(disco), os.stat(disco).st_mtime_ns)
        job = self.varrer(disco, 'sistema')
        self.esperar_estado(job['id'], 'concluido')
        a = self.por_caminho(job['id'])
        self.assertEqual(set(a), {'/[Partição 1]/apagado na primeira.bin', '/[Partição 2]/Docs/pequeno apagado.txt',
                                  '/[Partição 2]/Docs/grande apagado.bin', '/[Partição 2]/vivo.txt'})
        self.assertEqual(sha(bytes_do_achado(disco, a['/[Partição 1]/apagado na primeira.bin'])), sha(dfat))
        r = a['/[Partição 2]/Docs/pequeno apagado.txt']
        self.assertEqual(sha(bytes_do_achado(disco, r)), sha(residente))
        self.assertGreater(len(r['extents'] or []), 1, 'residente que cruza o fixup precisa de mais de uma faixa')
        g = a['/[Partição 2]/Docs/grande apagado.bin']
        self.assertEqual((g['apagado'], len(g['extents'])), (True, 2))
        self.assertEqual(sha(bytes_do_achado(disco, g)), sha(grande))
        self.assertEqual((sha256_de(disco), os.stat(disco).st_mtime_ns), antes)
        raiz = self.req('GET', '/api/jobs/%s/arvore' % job['id']).get_json()
        self.assertEqual([(i['nome'], i['total']) for i in raiz['pastas']], [('[Partição 1]', 1), ('[Partição 2]', 3)])
        p2 = self.req('GET', '/api/jobs/%s/arvore?caminho=%s' % (job['id'], quote('/[Partição 2]'))).get_json()
        self.assertEqual(p2['pastas'], [{'nome': 'Docs', 'caminho': '/[Partição 2]/Docs', 'total': 2}])


class TestExtApagadoSemConteudo(Base):
    def test_ext3_apagado_so_com_nome_nao_entra_e_o_job_avisa(self):
        f = ie.ExtImg('ext3')
        f.apagar(2, f.add_file(2, 'sumiu.bin', conteudo(3000, 1)))
        viva = conteudo(1500, 2)
        f.add_file(2, 'viva.bin', viva)
        caminho = f.gravar(os.path.join(self.trabalho, 'e3.img'))
        job = self.varrer(caminho, 'sistema')
        self.esperar_estado(job['id'], 'concluido')
        a = self.por_caminho(job['id'])
        self.assertEqual(set(a), {'/viva.bin'})
        self.assertEqual(sha(bytes_do_achado(caminho, a['/viva.bin'])), sha(viva))
        self.assertTrue(any('só com o nome' in t for t in self.eventos(job['id'])))


class TestRetomada(Base):
    def test_pausa_no_meio_da_lista_e_retoma_sem_repetir(self):
        f = im.FatImg('FAT32')
        for i in range(23):
            f.add('/', 'arquivo %02d.txt' % i, conteudo(50 + i, i))
        caminho = f.gravar(os.path.join(self.trabalho, 'muitos.img'))
        store = self.ctx.store

        def novo_job(nome):
            return store.criar('varredura', caminho, 'varredura#' + nome, {'modo': 'sistema'})['id']
        completo = novo_job('a')
        ctrl = mock.Mock(store=store)
        ctrl.pausa_pedida.return_value = False
        ctrl.cancelamento_pedido.return_value = False
        with leitor_mod.abrir(caminho) as l:
            op = {'modo': 'sistema', 'bloco': 1, 'sistema_antes': False}
            self.assertIsNone(varredura._fase_sistema(ctrl, store, completo, l, op, None, l.tamanho))
        esperado = [a['caminho'] for a in self.lista(completo)]
        self.assertEqual(len(esperado), 23)
        parcial = novo_job('b')
        with mock.patch.object(varredura, 'LOTE_SISTEMA', 5), leitor_mod.abrir(caminho) as l:
            ctrl.pausa_pedida.side_effect = [True]
            self.assertEqual(varredura._fase_sistema(ctrl, store, parcial, l, op, None, l.tamanho), 'pausado')
            cp = store.obter_checkpoint(parcial)
            self.assertEqual(cp['fase'], 'sistema')
            antes = len(self.lista(parcial))
            self.assertTrue(0 < antes < 23, antes)
            ctrl.pausa_pedida.side_effect = None
            ctrl.pausa_pedida.return_value = False
            self.assertIsNone(varredura._fase_sistema(ctrl, store, parcial, l, op, cp, l.tamanho))
        depois = [a['caminho'] for a in self.lista(parcial)]
        self.assertEqual(depois, esperado)


class TestPausaNoMeioDaMft(Base):
    def test_pausa_e_cancelamento_sao_vistos_a_cada_pedaco_da_mft_mesmo_sem_arquivo_novo(self):
        n = nt.NtfsImg()
        for i in range(6):
            n.add_file(5, 'f%d.txt' % i, conteudo(80 + i, i))
        caminho = n.gravar(os.path.join(self.trabalho, 'ntfs.img'))
        store = self.ctx.store
        job = store.criar('varredura', caminho, 'varredura#mft', {'modo': 'sistema'})['id']
        op = {'modo': 'sistema', 'bloco': 1, 'sistema_antes': False}
        ctrl = mock.Mock(store=store)
        ctrl.cancelamento_pedido.return_value = False
        with mock.patch.object(mod_ntfs, 'PEDACO_MFT', 8 * 1024), leitor_mod.abrir(caminho) as l:
            ctrl.pausa_pedida.side_effect = [True]              # logo no 1º pedaço (registros 0 a 7: só do sistema)
            self.assertEqual(varredura._fase_sistema(ctrl, store, job, l, op, None, l.tamanho), 'pausado')
            self.assertEqual(self.lista(job), [])
            cp = store.obter_checkpoint(job)
            self.assertEqual((cp['fase'], cp['feitos']), ('sistema', 0))
            ctrl.pausa_pedida.side_effect = None
            ctrl.pausa_pedida.return_value = False
            self.assertIsNone(varredura._fase_sistema(ctrl, store, job, l, op, cp, l.tamanho))
            self.assertEqual(sorted(a['caminho'] for a in self.lista(job)), ['/f%d.txt' % i for i in range(6)])
            ctrl.cancelamento_pedido.return_value = True
            job2 = store.criar('varredura', caminho, 'varredura#mft2', {'modo': 'sistema'})['id']
            self.assertEqual(varredura._fase_sistema(ctrl, store, job2, l, op, None, l.tamanho), 'cancelado')


if __name__ == '__main__':
    unittest.main()
