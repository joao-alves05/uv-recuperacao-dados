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
import sys
import threading
import time
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
        if p == '/painel.js':
            return self._arquivo(PAINEL / 'painel.js', 'text/javascript; charset=utf-8')
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
                id_ = f'j-20261104-{E.contador:04d}'
                j = novo(id_, 'rodando', o, 0.0, vel=1024 ** 2, criado_em=agora() + 1)
                j['destino'] = d
                E.jobs[id_] = j
                E.eventos[id_] = []
                E.mudou(j)
                return self._json(201, j)
            partes = p.split('/')
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
