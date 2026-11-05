# -*- coding: utf-8 -*-
"""Servico de mentira para testar o painel sem o servico de verdade.

Implementa o contrato de docs/FASE1.md com dados falsos: token por cookie (?t=teste),
lista, detalhe, acoes com 409, criacao de job e SSE com varios clientes.
Rotas /_teste/* so existem aqui: servem para o teste de tela derrubar o SSE,
recusar conexoes, mexer num job ou contar consultas.

    python painel_falso.py --porta 8780            # para olhar a tela
    python painel_falso.py --porta 8780 --vivo     # o job em andamento avanca sozinho

Nunca escreve em disco e escuta so em 127.0.0.1.
"""
import argparse
import json
import queue
import random
import struct
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PAINEL = Path(__file__).resolve().parent.parent / 'recuperacao' / 'painel'
TOKEN = 'teste'
XSS_CAMINHO = 'C:\\Clientes\\<img src=x onerror="window.__xss=1">\\disco.img'
XSS_ROTULO = '<img src=x onerror="window.__xss=5">Fotos'
GB = 1024 ** 3
BARRAS = chr(92) * 2 + '.' + chr(92)      # prefixo de disco do Windows


def fontes_falsas(dvd=False):
    """Como o Windows mostraria: SSD do sistema, HD com duas partições, pen drive, cartão, leitor vazio e DVD vazio."""
    def vol(letra, rotulo, fs, tam, livre):
        return {'letra': letra, 'rotulo': rotulo, 'sistema_arquivos': fs, 'tamanho': tam, 'livre': livre}
    def disco(id_, caminho, nome, tam, tipo, rem, sistema, vols):
        return {'id': id_, 'caminho': caminho, 'nome': nome, 'tamanho': tam, 'tipo': tipo, 'removivel': rem, 'setor': 512, 'sistema': sistema, 'volumes': vols}
    return [
        disco('disco-0', BARRAS + 'PhysicalDrive0', 'Samsung SSD 860 EVO 500GB', 465 * GB, 'ssd', False, True, [vol('C:', '', 'NTFS', 465 * GB, 120 * GB)]),
        disco('disco-1', BARRAS + 'PhysicalDrive1', 'ST1000DM010-2EP102', 931 * GB, 'hd', False, False,
              [vol('D:', 'Dados', 'NTFS', 500 * GB, 310 * GB), vol('G:', XSS_ROTULO, 'NTFS', 431 * GB, 400 * GB)]),
        disco('disco-2', BARRAS + 'PhysicalDrive2', 'Kingston DataTraveler 3.0', int(28.8 * GB), 'usb', True, False, [vol('E:', '', 'FAT32', int(28.8 * GB), int(12.4 * GB))]),
        disco('disco-3', BARRAS + 'PhysicalDrive3', 'Generic SD Card', 59 * GB, 'cartao', True, False, [vol('F:', '', 'exFAT', 59 * GB, 41 * GB)]),
        disco('disco-4', BARRAS + 'PhysicalDrive4', 'Leitor de cartões USB', 0, 'usb', True, False, []),
        disco('optico-0', BARRAS + 'CdRom0', 'HL-DT-ST DVDRAM GH24NSD1', 4 * GB if dvd else 0, 'optico', True, False, [vol('H:', 'FILMES', 'UDF', 4 * GB, 0) if dvd else vol('H:', '', '', 0, 0)]),
    ]


XSS_MSG = '<img src=x onerror="window.__xss=2"> lendo o setor <b>42</b>'


def agora():
    return time.time()


def novo(id_, estado, origem, prog, total=10 * 1024 ** 3, vel=0, ruins=0, msg='', erro='', **extra):
    j = {'id': id_, 'tipo': 'imagem', 'origem': origem, 'destino': origem + '.copia', 'opcoes': {'bloco': 1048576},
         'estado': estado, 'progresso': prog, 'bytes_total': total, 'bytes_feitos': int(total * prog), 'velocidade': vel,
         'setores_ruins': ruins, 'mensagem': msg, 'criado_em': agora() - 1000, 'atualizado_em': agora(),
         'iniciado_em': None, 'terminado_em': None, 'erro': erro}
    j.update(extra)
    return j


def dados_iniciais():
    t = agora()
    return {
        'j-20261104-0001': novo('j-20261104-0001', 'rodando', XSS_CAMINHO, 0.42, vel=38 * 1024 ** 2, ruins=2, msg=XSS_MSG, criado_em=t - 10),
        'j-20261104-0002': novo('j-20261104-0002', 'interrompido', 'D:\\Fotos do casamento\\cartao-sd.img', 0.31, criado_em=t - 20, msg='Parou no setor 9120'),
        'j-20261104-0003': novo('j-20261104-0003', 'concluido', 'E:\\pendrive.bin', 1.0, total=512 * 1024 ** 2, criado_em=t - 30),
        'j-20261104-0004': novo('j-20261104-0004', 'pausado', 'F:\\HD externo com um nome extremamente comprido para testar as reticencias do painel.img', 0.77, criado_em=t - 40),
        'j-20261104-0005': novo('j-20261104-0005', 'falhou', 'G:\\disco-ruim.img', 0.05, criado_em=t - 50, erro='<b onmouseover="window.__xss=3">Disco desconectado</b>'),
        'j-20261104-0006': novo('j-20261104-0006', 'na_fila', 'H:\\tabela.img', 0.0, criado_em=t - 60),
        'j-20261104-0007': novo('j-20261104-0007', 'cancelado', 'I:\\antigo.img', 0.12, criado_em=t - 70),
    }


# ---------------------------------------------------------------- achados (fase 5)
GRUPOS_FALSOS = {
    'foto': ['JPG', 'JPG', 'JPG', 'JPG', 'PNG', 'GIF', 'HEIC', 'CR2'],
    'video': ['MP4', 'MOV', 'AVI'],
    'audio': ['MP3', 'WAV'],
    'documento': ['PDF', 'PDF', 'DOCX', 'XLSX', 'PPTX', 'DOC'],
    'compactado': ['ZIP', 'RAR', '7Z'],
    'banco': ['SQLITE'],
    'outro': ['RTF'],
}
PESOS_GRUPO = [('foto', 380), ('video', 40), ('audio', 60), ('documento', 110), ('compactado', 30), ('banco', 6), ('outro', 14)]
PASTAS_FALSAS = {'foto': ['DCIM/100CANON', 'DCIM/101CANON', 'DCIM/Viagem'], 'video': ['Videos/Viagem', 'DCIM/100CANON'],
                 'audio': ['Musicas/Albuns'], 'documento': ['Documentos/Trabalho', 'Documentos/Escola'], 'compactado': ['Documentos/Trabalho']}
KB = 1024
TAMANHOS_FALSOS = {'foto': (300 * KB, 6 * 1024 * KB), 'video': (10 * 1024 * KB, 400 * 1024 * KB), 'audio': (2 * 1024 * KB, 12 * 1024 * KB),
                   'documento': (20 * KB, 5 * 1024 * KB), 'compactado': (1024 * KB, 200 * 1024 * KB), 'banco': (100 * KB, 50 * 1024 * KB),
                   'outro': (2 * KB, 300 * KB)}


def png_pequeno(w, h, cor):
    """PNG de verdade, sem biblioteca: degradê simples da cor dada."""
    linhas = b''
    for y in range(h):
        linhas += b'\x00' + b''.join(bytes((min(255, cor[0] + y * 2), min(255, cor[1] + x), min(255, cor[2] + (x + y)))) for x in range(w))

    def bloco(tipo, dados):
        c = struct.pack('>I', zlib.crc32(tipo + dados) & 0xffffffff)
        return struct.pack('>I', len(dados)) + tipo + dados + c
    return b'\x89PNG\r\n\x1a\n' + bloco(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) + bloco(b'IDAT', zlib.compress(linhas)) + bloco(b'IEND', b'')


def gerar_achados(n=640):
    r = random.Random(7)
    grupos = [g for g, p in PESOS_GRUPO for _ in range(p)]
    out = []
    for i in range(1, n + 1):
        g = r.choice(grupos)
        tipo = r.choice(GRUPOS_FALSOS[g])
        lo, hi = TAMANHOS_FALSOS[g]
        sorte = r.random()
        estado = 'bom' if sorte < .8 else 'parcial' if sorte < .92 else 'ruim'
        data = None
        if r.random() < .85:
            data = '%04d-%02d-%02d %02d:%02d:00' % (r.randint(2021, 2026), r.randint(1, 10), r.randint(1, 28), r.randint(0, 23), r.randint(0, 59))
        caminho = ''
        nome = 'f%07d.%s' % (i, tipo.lower())
        if g in PASTAS_FALSAS and r.random() < .4:
            caminho = r.choice(PASTAS_FALSAS[g])
            nome = ('IMG_%04d.%s' % (r.randint(1000, 9999), tipo) if g == 'foto' else 'Arquivo %d.%s' % (r.randint(1, 99), tipo.lower()))
        a = {'id': i, 'nome': nome, 'tipo': tipo, 'grupo': g, 'tamanho': r.randint(lo, hi), 'offset': i * 4 * 1024 * 1024, 'estado': estado,
             'data': data, 'pontuacao': {'bom': 90, 'parcial': 55, 'ruim': 15}[estado] + r.randint(0, 9), 'recuperado_em': None}
        if g == 'foto':
            a['largura'], a['altura'] = r.choice([(4000, 3000), (1920, 1080), (3024, 4032), (800, 600)])
        if caminho:
            a['caminho'] = caminho
            a['apagado'] = True
        out.append(a)
    # alguns nomes longos e com marcas para provar reticências e texto puro
    out[0]['nome'] = 'Um nome de arquivo extremamente comprido para provar as reticencias da lista de resultados.jpg'
    out[0]['tipo'], out[0]['grupo'] = 'JPG', 'foto'
    out[1]['nome'] = '<img src=x onerror="window.__xss=6">.jpg'
    out[1]['tipo'], out[1]['grupo'] = 'JPG', 'foto'
    out[1]['estado'] = 'bom'
    return out


ACHADOS = gerar_achados()


def visiveis(job):
    if job['estado'] == 'concluido' or job['progresso'] >= 1:
        return ACHADOS
    return ACHADOS[:int(len(ACHADOS) * job['progresso'])]


def filtrar(itens, q, sem_grupo=False):
    def um(k, padrao=''):
        return (q.get(k) or [padrao])[0]
    g, t, e, b, pasta = um('grupo'), um('tipo').upper(), um('estado'), um('busca').lower(), um('pasta')
    mn = int(um('min', '0') or 0)
    mx = int(um('max', '0') or 0)
    de, ate = um('de'), um('ate')
    r = []
    for a in itens:
        if g and not sem_grupo and a['grupo'] != g:
            continue
        if t and a['tipo'] != t:
            continue
        if e and a['estado'] != e:
            continue
        if b and b not in a['nome'].lower():
            continue
        if mn and a['tamanho'] < mn:
            continue
        if mx and a['tamanho'] > mx:
            continue
        if (de or ate) and not a['data']:
            continue
        if de and a['data'][:10] < de:
            continue
        if ate and a['data'][:10] > ate:
            continue
        if pasta and not (a.get('caminho') == pasta or a.get('caminho', '').startswith(pasta + '/')):
            continue
        r.append(a)
    return r


def resposta_achados(job, q):
    base = visiveis(job)
    itens = filtrar(base, q)
    por_grupo = {}
    for a in filtrar(base, q, sem_grupo=True):
        por_grupo[a['grupo']] = por_grupo.get(a['grupo'], 0) + 1
    ordem = (q.get('ordem') or ['nome'])[0]
    chave = {'nome': lambda a: a['nome'].lower(), 'tamanho': lambda a: a['tamanho'], 'data': lambda a: a['data'] or '', 'tipo': lambda a: a['tipo']}.get(ordem, lambda a: a['nome'].lower())
    itens = sorted(itens, key=lambda a: (chave(a), a['id']), reverse=(q.get('sentido') or ['asc'])[0] == 'desc')
    por = max(1, min(500, int((q.get('por_pagina') or ['100'])[0] or 100)))
    pag = max(1, int((q.get('pagina') or ['1'])[0] or 1))
    return {'total': len(itens), 'pagina': pag, 'itens': itens[(pag - 1) * por:pag * por],
            'resumo': {'por_grupo': por_grupo, 'tamanho_total': sum(a['tamanho'] for a in itens)}}


def arvore(job, caminho):
    filhos = {}
    for a in visiveis(job):
        c = a.get('caminho')
        if not c:
            continue
        if caminho:
            if not c.startswith(caminho + '/'):
                continue
            resto = c[len(caminho) + 1:]
        else:
            resto = c
        nome = resto.split('/')[0]
        d = filhos.setdefault(nome, {'nome': nome, 'caminho': (caminho + '/' if caminho else '') + nome, 'total': 0})
        d['total'] += 1
    return {'caminho': caminho, 'pastas': sorted(filhos.values(), key=lambda d: d['nome'])}


class Estado:
    def __init__(s):
        s.trava = threading.RLock()
        s.jobs = dados_iniciais()
        s.eventos = {i: [{'id': n, 'job_id': i, 'quando': agora() - 100 + n, 'texto': f'Passo {n} <i>de teste</i> em {i}'} for n in range(1, 6)] for i in s.jobs}
        s.filas = []
        s.recusar = 0
        s.atraso = 0.0
        s.quebrado = False
        s.revogado = False
        s.consultas = 0
        s.sse_abertos = 0
        s.sse_total = 0
        s.contador = 100
        s.admin = True
        s.dvd = False
        s.fontes_vazias = False
        s.ultimo = None
        s.ultimo_opcoes = None
        s.ultimo_rec = None
        s.abriu = None
        s.abrir_falha = False
        s.contador2 = 900

    def lista(s):
        return sorted(s.jobs.values(), key=lambda j: j['criado_em'], reverse=True)

    def avisar(s, nome, dado):
        msg = f'event: {nome}\ndata: {json.dumps(dado, ensure_ascii=False)}\n\n'
        for q in list(s.filas):
            q.put(msg)

    def mudou(s, j):
        j['atualizado_em'] = agora()
        s.avisar('job', j)


E = Estado()
PERMITE = {'pausar': {'rodando'}, 'retomar': {'pausado', 'interrompido'}, 'cancelar': {'na_fila', 'rodando', 'pausado', 'interrompido'}}
NOVO_ESTADO = {'pausar': 'pausado', 'retomar': 'rodando', 'cancelar': 'cancelado'}


class H(BaseHTTPRequestHandler):
    server_version = 'PainelFalso'

    def log_message(self, *a):
        pass

    def _json(self, codigo, dado, extra=None):
        corpo = json.dumps(dado, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(corpo)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def _arquivo(self, caminho, tipo):
        try:
            corpo = caminho.read_bytes()
        except OSError:
            return self._json(404, {'erro': 'Arquivo do painel não encontrado.'})
        self.send_response(200)
        self.send_header('Content-Type', tipo)
        self.send_header('Content-Length', str(len(corpo)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(corpo)

    def _autorizado(self):
        return f'recdados={TOKEN}' in (self.headers.get('Cookie') or '') or self.headers.get('X-RecDados-Token') == TOKEN

    def _corpo(self):
        n = int(self.headers.get('Content-Length') or 0)
        try:
            return json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        if p == '/api/saude':
            return self._json(200, {'ok': True, 'versao': 'falso', 'pid': 0, 'admin': E.admin})
        if p.startswith('/_teste/'):
            return self._teste(p, q)
        if p == '/' or p.startswith('/jobs/'):
            if 't' in q:
                self.send_response(302)
                self.send_header('Set-Cookie', f'recdados={q["t"][0]}; HttpOnly; SameSite=Strict; Path=/')
                self.send_header('Location', p)
                self.end_headers()
                return
            if not self._autorizado():
                corpo = '<!doctype html><meta charset=utf-8><p>Abra pelo atalho do programa'.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)
                return
            return self._arquivo(PAINEL / 'index.html', 'text/html; charset=utf-8')
        if p in ('/painel.js', '/resultados.js'):
            return self._arquivo(PAINEL / p[1:], 'text/javascript; charset=utf-8')
        if p == '/painel.css':
            return self._arquivo(PAINEL / 'painel.css', 'text/css; charset=utf-8')
        if p.startswith('/api/'):
            if not self._autorizado() or E.revogado:
                return self._json(401, {'erro': 'abra pelo atalho do programa'})
            if p == '/api/marca':
                return self._json(200, {'app': 'Marca de Teste', 'empresa': 'Empresa de Teste', 'site': '', 'cor': '#7a3fb0'})
            if p == '/api/fontes':
                with E.trava:
                    atraso, quebrado, vazias = E.atraso, E.quebrado, E.fontes_vazias
                time.sleep(atraso)
                if quebrado:
                    return self._json(500, {'erro': 'Não consegui listar os discos.'})
                return self._json(200, [] if vazias else fontes_falsas(E.dvd))
            if p == '/api/jobs':
                with E.trava:
                    E.consultas += 1
                    atraso, quebrado = E.atraso, E.quebrado
                    dado = E.lista()
                time.sleep(atraso)
                if quebrado:
                    return self._json(500, {'erro': 'O serviço não conseguiu responder.'})
                return self._json(200, dado)
            partes = p.split('/')
            if len(partes) == 5 and partes[2] == 'jobs' and partes[4] in ('achados', 'arvore', 'relatorio'):
                with E.trava:
                    j = E.jobs.get(partes[3])
                    atraso, quebrado = E.atraso, E.quebrado
                    if not j:
                        return self._json(404, {'erro': 'Recuperação não encontrada.'})
                    if partes[4] == 'relatorio':
                        corpo = '<!doctype html><meta charset=utf-8><h1>Relatório</h1><p>Arquivos recuperados.</p>'.encode()
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.send_header('Content-Length', str(len(corpo)))
                        self.end_headers()
                        self.wfile.write(corpo)
                        return
                    dado = resposta_achados(j, q) if partes[4] == 'achados' else arvore(j, (q.get('caminho') or [''])[0])
                time.sleep(atraso)
                if quebrado:
                    return self._json(500, {'erro': 'Não consegui ler os resultados.'})
                return self._json(200, dado)
            if len(partes) == 5 and partes[2] == 'achados' and partes[4] == 'previa':
                try:
                    a = ACHADOS[int(partes[3]) - 1]
                except (ValueError, IndexError):
                    return self._json(404, {'erro': 'Arquivo não encontrado.'})
                if a['grupo'] != 'foto' or a['estado'] == 'ruim':
                    return self._json(404, {'erro': 'Sem prévia.'})
                corpo = png_pequeno(64, 48, ((a['id'] * 37) % 120, (a['id'] * 91) % 120, (a['id'] * 53) % 120))
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', str(len(corpo)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(corpo)
                return
            if p.startswith('/api/jobs/'):
                with E.trava:
                    j = E.jobs.get(p.split('/')[3])
                    if not j:
                        return self._json(404, {'erro': 'Recuperação não encontrada.'})
                    return self._json(200, dict(j, eventos=E.eventos.get(j['id'], [])[-200:]))
            if p == '/api/eventos':
                return self._sse()
        self._json(404, {'erro': 'Não encontrado.'})

    def do_POST(self):
        p = urlparse(self.path).path
        if not p.startswith('/api/') and not p.startswith('/_teste/'):
            return self._json(404, {'erro': 'Não encontrado.'})
        if p.startswith('/_teste/'):
            return self._teste(p, parse_qs(urlparse(self.path).query))
        if not self._autorizado():
            return self._json(401, {'erro': 'abra pelo atalho do programa'})
        corpo = self._corpo()
        with E.trava:
            if p == '/api/jobs':
                o, d = str(corpo.get('origem', '')), str(corpo.get('destino', ''))
                if 'html' in o:
                    return self._json(400, {'erro': '<b onmouseover="window.__xss=4">A origem</b> não existe.'})
                if o == d:
                    return self._json(400, {'erro': 'O destino não pode ser a própria origem.'})
                E.contador += 1
                E.ultimo = {'tipo': corpo.get('tipo'), 'origem': o, 'destino': d}
                E.ultimo_opcoes = corpo.get('opcoes')
                id_ = f'j-20261104-{E.contador:04d}'
                j = novo(id_, 'rodando', o, 0.0, vel=1024 ** 2, criado_em=agora() + 1)
                j['destino'] = d
                if corpo.get('tipo') == 'varredura':
                    j['tipo'], j['destino'], j['opcoes'] = 'varredura', '', corpo.get('opcoes') or {}
                E.jobs[id_] = j
                E.eventos[id_] = []
                E.mudou(j)
                return self._json(201, j)
            partes = p.split('/')
            if len(partes) == 5 and partes[2] == 'jobs' and partes[4] == 'recuperar':
                j = E.jobs.get(partes[3])
                if not j or j.get('tipo') != 'varredura':
                    return self._json(404, {'erro': 'Busca não encontrada.'})
                destino = str(corpo.get('destino', '')).strip()
                if not destino:
                    return self._json(400, {'erro': 'Escolha onde salvar os arquivos.'})
                if destino[:2].upper() == j['origem'][-2:].upper():
                    return self._json(400, {'erro': 'O destino não pode ficar no mesmo disco da origem.'})
                if corpo.get('ids') == 'todos':
                    alvo = filtrar(visiveis(j), {k: [str(v)] for k, v in (corpo.get('filtros') or {}).items()})
                else:
                    ids = {int(i) for i in (corpo.get('ids') or [])}
                    alvo = [a for a in visiveis(j) if a['id'] in ids]
                if not alvo:
                    return self._json(400, {'erro': 'Nenhum arquivo para recuperar.'})
                E.contador2 += 1
                id_ = f'j-20261104-{E.contador2:04d}'
                n = novo(id_, 'rodando', j['origem'], 0.3, total=sum(a['tamanho'] for a in alvo), vel=20 * 1024 ** 2, criado_em=agora() + 1)
                n['tipo'], n['destino'] = 'recuperar', destino
                E.jobs[id_] = n
                E.eventos[id_] = []
                for a in alvo:
                    a['recuperado_em'] = agora()
                E.ultimo_rec = {'ids': 'todos' if corpo.get('ids') == 'todos' else sorted(a['id'] for a in alvo), 'n': len(alvo), 'filtros': corpo.get('filtros'), 'destino': destino}
                E.mudou(n)
                return self._json(201, n)
            if len(partes) == 5 and partes[2] == 'jobs' and partes[4] == 'abrir':
                j = E.jobs.get(partes[3])
                if not j or E.abrir_falha:
                    return self._json(500, {'erro': 'Não consegui abrir a pasta.'})
                E.abriu = j['destino']
                return self._json(200, {'ok': True})
            if len(partes) == 5 and partes[2] == 'jobs' and partes[4] in PERMITE:
                j = E.jobs.get(partes[3])
                if not j:
                    return self._json(404, {'erro': 'Recuperação não encontrada.'})
                if j['estado'] not in PERMITE[partes[4]]:
                    return self._json(409, {'erro': f'Não dá para {partes[4]} agora: a recuperação está "{j["estado"]}".'})
                j['estado'] = NOVO_ESTADO[partes[4]]
                E.eventos[j['id']].append({'id': len(E.eventos[j['id']]) + 1, 'job_id': j['id'], 'quando': agora(), 'texto': 'Você pediu: ' + partes[4]})
                E.mudou(j)
                return self._json(200, j)
            if p == '/api/desligar':
                return self._json(200, {'ok': True})
        self._json(404, {'erro': 'Não encontrado.'})

    def _sse(self):
        with E.trava:
            if E.recusar > 0 or E.quebrado:
                E.recusar = max(0, E.recusar - 1)
                return self._json(503, {'erro': 'Serviço ocupado.'})
            q = queue.Queue()
            E.filas.append(q)
            E.sse_abertos += 1
            E.sse_total += 1
            inicial = f'retry: 500\nevent: jobs\ndata: {json.dumps(E.lista(), ensure_ascii=False)}\n\n'
        try:
            time.sleep(E.atraso)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(inicial.encode())
            self.wfile.flush()
            while True:
                try:
                    m = q.get(timeout=15)
                except queue.Empty:
                    m = ': ping\n\n'
                if m is None:
                    return
                self.wfile.write(m.encode())
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with E.trava:
                if q in E.filas:
                    E.filas.remove(q)
                E.sse_abertos -= 1

    def _teste(self, p, q):
        with E.trava:
            if p == '/_teste/derrubar':
                for f in list(E.filas):
                    f.put(None)
            elif p == '/_teste/recusar':
                E.recusar = int(q.get('n', ['0'])[0])
            elif p == '/_teste/revogar':
                E.revogado = q.get('on', ['1'])[0] == '1'
            elif p == '/_teste/quebrar':
                E.quebrado = q.get('on', ['1'])[0] == '1'
            elif p == '/_teste/atraso':
                E.atraso = float(q.get('s', ['0'])[0])
            elif p == '/_teste/admin':
                E.admin = q.get('on', ['1'])[0] == '1'
            elif p == '/_teste/varredura':
                E.contador2 += 1
                id_ = f'j-20261104-{E.contador2:04d}'
                prog = float(q.get('progresso', ['1'])[0])
                n = novo(id_, q.get('estado', ['concluido'])[0], '\\\\.\\E:', prog, total=int(28.8 * 1024 ** 3), criado_em=agora() + 1)
                n['tipo'], n['destino'], n['opcoes'] = 'varredura', '', {'modo': 'rapida'}
                E.jobs[id_] = n
                E.eventos[id_] = []
                E.mudou(n)
                return self._json(200, {'id': id_})
            elif p == '/_teste/abrir_falha':
                E.abrir_falha = q.get('on', ['1'])[0] == '1'
            elif p == '/_teste/ultimo_rec':
                return self._json(200, {'rec': E.ultimo_rec, 'abriu': E.abriu, 'opcoes': E.ultimo_opcoes})
            elif p == '/_teste/zerar_recuperados':
                for a in ACHADOS:
                    a['recuperado_em'] = None
            elif p == '/_teste/dvd':
                E.dvd = q.get('on', ['1'])[0] == '1'
            elif p == '/_teste/fontes_vazias':
                E.fontes_vazias = q.get('on', ['1'])[0] == '1'
            elif p == '/_teste/ultimo':
                return self._json(200, {'ultimo': E.ultimo})
            elif p == '/_teste/zerar':
                E.jobs.clear()
                E.avisar('jobs', [])
            elif p == '/_teste/restaurar':
                E.jobs.update(dados_iniciais())
                E.avisar('jobs', E.lista())
            elif p == '/_teste/mexer':
                j = E.jobs.get(q['id'][0])
                if j:
                    for k in ('estado', 'mensagem'):
                        if k in q:
                            j[k] = q[k][0]
                    if 'progresso' in q:
                        j['progresso'] = float(q['progresso'][0])
                        j['bytes_feitos'] = int(j['bytes_total'] * j['progresso'])
                    if 'avisar' not in q or q['avisar'][0] != '0':
                        E.mudou(j)
            elif p == '/_teste/estado':
                pass
            else:
                return self._json(404, {'erro': 'Não encontrado.'})
            return self._json(200, {'sse_abertos': E.sse_abertos, 'sse_total': E.sse_total, 'consultas': E.consultas, 'jobs': len(E.jobs)})


def vivo():
    while True:
        time.sleep(0.5)
        with E.trava:
            for j in E.jobs.values():
                if j['estado'] == 'rodando' and j['progresso'] < 1:
                    j['progresso'] = min(1.0, j['progresso'] + 0.01)
                    j['bytes_feitos'] = int(j['bytes_total'] * j['progresso'])
                    j['velocidade'] = 38 * 1024 ** 2
                    if j['progresso'] >= 1:
                        j['estado'] = 'concluido'
                    E.mudou(j)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--porta', type=int, default=8780)
    ap.add_argument('--vivo', action='store_true')
    a = ap.parse_args()
    if a.porta == 8765:
        sys.exit('A porta 8765 é do serviço de verdade.')
    if a.vivo:
        threading.Thread(target=vivo, daemon=True).start()
    srv = ThreadingHTTPServer(('127.0.0.1', a.porta), H)
    srv.daemon_threads = True
    print(f'Painel falso em http://127.0.0.1:{a.porta}/?t={TOKEN}', flush=True)
    srv.serve_forever()
