# -*- coding: utf-8 -*-
"""Prova o painel no navegador de verdade (Edge sem janela, protocolo de depuracao), contra o servico de mentira.

Cobre: lista, abrir job e a URL mudar, recarregar e voltar ao mesmo job, botoes por estado, "interrompido" com
Retomar, SSE caindo e reconectando (e caindo para consulta), aviso ao sair so com job rodando, XSS que nao executa,
estados de carregando / vazio / erro, largura da lista e medidas em 1366x768, 1920x1080, 390x844 e 360x640.

    powershell.exe -Command "python testes\\testar_painel.py"
    (o Edge nao abre pelo Bash; rode pelo PowerShell)

Fotos: defina PAINEL_FOTOS com uma pasta e o teste grava os PNGs la.
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

AQUI = Path(__file__).resolve().parent
EDGE = next((p for p in (r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                         r'C:\Program Files\Microsoft\Edge\Application\msedge.exe') if os.path.exists(p)), None)
if not EDGE:
    print('sem Edge neste PC: teste pulado')
    sys.exit(0)
FOTOS = Path(os.environ['PAINEL_FOTOS']) if os.environ.get('PAINEL_FOTOS') else None


class WS:
    def __init__(s, url):
        hp, _, caminho = url.split('://')[1].partition('/')
        host, _, porta = hp.partition(':')
        s.k = socket.create_connection((host, int(porta)), timeout=60)
        chave = base64.b64encode(os.urandom(16)).decode()
        s.k.sendall((f'GET /{caminho} HTTP/1.1\r\nHost: {hp}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                     f'Sec-WebSocket-Key: {chave}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        buf = b''
        while b'\r\n\r\n' not in buf:
            buf += s.k.recv(1)
        s.buf = b''

    def send(s, t):
        d = t.encode()
        n = len(d)
        m = os.urandom(4)
        cab = b'\x81' + (bytes([0x80 | n]) if n < 126 else b'\xfe' + struct.pack('>H', n) if n < 65536 else b'\xff' + struct.pack('>Q', n))
        s.k.sendall(cab + m + bytes(b ^ m[i % 4] for i, b in enumerate(d)))

    def _r(s, n):
        while len(s.buf) < n:
            c = s.k.recv(65536)
            if not c:
                raise EOFError
            s.buf += c
        r, s.buf = s.buf[:n], s.buf[n:]
        return r

    def recv(s):
        out = b''
        while True:
            b0, b1 = s._r(2)
            n = b1 & 0x7f
            if n == 126:
                n = struct.unpack('>H', s._r(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', s._r(8))[0]
            d = s._r(n)
            op = b0 & 0xf
            if op == 8:
                raise EOFError
            if op in (1, 0):
                out += d
            if b0 & 0x80 and op in (1, 0):
                return out.decode('utf-8', 'replace')


class Pag:
    def __init__(s, porta_cdp):
        alvo = None
        for _ in range(80):
            try:
                lista = json.load(urllib.request.urlopen(f'http://127.0.0.1:{porta_cdp}/json', timeout=2))
                alvo = next((x for x in lista if x.get('type') == 'page'), None)
                if alvo:
                    break
            except Exception:
                pass
            time.sleep(.3)
        s.ws = WS(alvo['webSocketDebuggerUrl'])
        s.dialogos = []
        s.i = 0
        s.cmd('Page.enable')
        s.cmd('Runtime.enable')

    def cmd(s, metodo, **p):
        s.i += 1
        i = s.i
        s.ws.send(json.dumps({'id': i, 'method': metodo, 'params': p}))
        while True:
            r = json.loads(s.ws.recv())
            if r.get('method') == 'Page.javascriptDialogOpening':      # aviso de sair: aceita e segue
                s.dialogos.append((r['params'].get('type'), r['params'].get('message')))
                s.i += 1
                s.ws.send(json.dumps({'id': s.i, 'method': 'Page.handleJavaScriptDialog', 'params': {'accept': True}}))
            if r.get('id') == i:
                if 'error' in r:
                    raise RuntimeError(metodo + ': ' + json.dumps(r['error']))
                return r.get('result')

    def ev(s, js, aguarda=False):
        r = s.cmd('Runtime.evaluate', expression=js, returnByValue=True, awaitPromise=aguarda)
        if 'exceptionDetails' in r:
            raise RuntimeError(json.dumps(r['exceptionDetails'], ensure_ascii=False)[:600])
        return r['result'].get('value')

    def tamanho(s, w, h):
        s.cmd('Emulation.setDeviceMetricsOverride', width=w, height=h, deviceScaleFactor=1, mobile=False)

    def tema(s, t):
        s.cmd('Emulation.setEmulatedMedia', features=[{'name': 'prefers-color-scheme', 'value': t}])

    def ir(s, url, espera=.8):
        s.cmd('Page.navigate', url=url)
        time.sleep(espera)

    def espera(s, js, tempo=8):
        fim = time.time() + tempo
        v = None
        while time.time() < fim:
            try:
                v = s.ev(js)
            except RuntimeError:
                v = None
            if v:
                return v
            time.sleep(.1)
        return v

    def mouse(s, tipo, x, y, **k):
        s.cmd('Input.dispatchMouseEvent', type=tipo, x=x, y=y, **k)

    def centro(s, seletor):
        return s.ev(f"(()=>{{const e=document.querySelector({json.dumps(seletor)});if(!e)return null;const r=e.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]}})()")

    def clica(s, seletor, depois=.35):
        c = s.centro(seletor)
        if not c:
            raise RuntimeError('sem elemento ' + seletor)
        s.mouse('mouseMoved', *c)
        s.mouse('mousePressed', *c, button='left', clickCount=1)
        s.mouse('mouseReleased', *c, button='left', clickCount=1)
        time.sleep(depois)

    def foto(s, nome):
        if FOTOS:
            FOTOS.mkdir(parents=True, exist_ok=True)
            r = s.cmd('Page.captureScreenshot', format='png')
            (FOTOS / f'{nome}.png').write_bytes(base64.b64decode(r['data']))


def porta_livre():
    k = socket.socket()
    k.bind(('127.0.0.1', 0))
    p = k.getsockname()[1]
    k.close()
    return p


def http(porta, caminho, metodo='GET'):
    req = urllib.request.Request(f'http://127.0.0.1:{porta}{caminho}', method=metodo, data=b'' if metodo == 'POST' else None)
    return json.load(urllib.request.urlopen(req, timeout=10))


RESULTADOS = []


def conferir(nome, cond, detalhe=''):
    RESULTADOS.append((nome, bool(cond), detalhe))
    print(('ok     ' if cond else 'FALHOU ') + nome + (('  -> ' + str(detalhe)) if (detalhe and not cond) else ''), flush=True)


def j(id_):
    return f'j-20261104-{id_:04d}'


def main():
    porta, cdp = porta_livre(), porta_livre()
    perfil = tempfile.mkdtemp(prefix='painel_edge_')
    srv = subprocess.Popen([sys.executable, str(AQUI / 'painel_falso.py'), '--porta', str(porta)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    edge = None
    try:
        for _ in range(60):
            try:
                http(porta, '/api/saude')
                break
            except OSError:
                time.sleep(.2)
        base = f'http://127.0.0.1:{porta}'
        edge = subprocess.Popen([EDGE, '--headless', '--disable-gpu', '--no-first-run', f'--remote-debugging-port={cdp}',
                                 f'--user-data-dir={perfil}', '--window-size=1366,768', 'about:blank'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p = Pag(cdp)
        p.tema('light')
        p.tamanho(1366, 768)
        p.cmd('Page.addScriptToEvaluateOnNewDocument', source="window.__erros=[];addEventListener('error',e=>__erros.push(e.message));addEventListener('unhandledrejection',e=>__erros.push('rej '+e.reason));")

        # ---- sem cookie: nada de dados
        p.ir(base + '/', 1)
        conferir('sem cookie a pagina nao mostra dados', 'Abra pelo atalho' in p.ev('document.body.textContent') and 'disco.img' not in p.ev('document.body.textContent'))
        http(porta, '/_teste/revogar?on=1')
        p.ir(base + '/?t=teste', 1)
        conferir('servico recusa o acesso (401): painel mostra "Abra pelo atalho" e nenhum dado', p.espera("!document.getElementById('sem-acesso').hidden") and p.ev("document.querySelectorAll('.item').length")==0)
        http(porta, '/_teste/revogar?on=0')

        # ---- carregando (servico lento) e depois a lista
        http(porta, '/_teste/atraso?s=1.5')
        p.ir(base + '/?t=teste', .4)
        conferir('estado carregando aparece enquanto o servico demora', p.ev("!document.getElementById('lista-carregando').hidden && document.querySelectorAll('.item').length===0"))
        conferir('conexao diz "Conectando" enquanto espera', 'Conectando' in p.ev("document.getElementById('conexao-texto').textContent"))
        conferir('lista chega depois do atraso', p.espera("document.querySelectorAll('.item').length===7", 8))
        http(porta, '/_teste/atraso?s=0')
        conferir('carregando some quando a lista chega', p.ev("document.getElementById('lista-carregando').hidden"))
        conferir('conexao fica "Ao vivo"', p.espera("document.getElementById('conexao-texto').textContent==='Ao vivo'"))
        conferir('painel nao tem erro de script', p.ev('window.__erros.length')==0, p.ev('JSON.stringify(window.__erros)'))
        conferir('sem cookie na URL (o t= saiu)', 't=' not in p.ev('location.search+location.href'))

        # ---- lista
        conferir('7 recuperacoes na lista', p.ev("document.querySelectorAll('.item').length")==7)
        ordem = p.ev("[...document.querySelectorAll('.item')].map(a=>a.getAttribute('href'))")
        conferir('mais novo primeiro', ordem == [f'/jobs/{j(i)}' for i in range(1, 8)], ordem)
        conferir('nome na lista e o final do caminho', p.ev("document.querySelector('.item .item-nome').textContent")=='disco.img')
        conferir('estados em portugues', p.ev("[...document.querySelectorAll('.item .tag')].map(t=>t.textContent).join('|')")=='Em andamento|Interrompido|Concluído|Pausado|Com falha|Na fila|Cancelado')
        conferir('item em andamento mostra velocidade, resto e trechos ilegiveis',
                 p.ev("(()=>{const i=document.querySelectorAll('.item')[0];return i.querySelector('.esq').textContent.includes('MB/s')&&i.querySelector('.esq').textContent.includes('min')&&i.querySelector('.dir').textContent==='2 trechos ilegíveis'})()"))
        conferir('item interrompido em destaque (borda)', p.ev("getComputedStyle(document.querySelectorAll('.item')[1]).borderTopColor")!=p.ev("getComputedStyle(document.querySelectorAll('.item')[2]).borderTopColor"))
        conferir('nome muito comprido vira reticencias com dica', p.ev("(()=>{const n=document.querySelectorAll('.item')[3].querySelector('.item-nome');return n.scrollWidth>n.clientWidth&&getComputedStyle(n).textOverflow==='ellipsis'&&document.querySelectorAll('.item')[3].title.includes('HD externo')})()"))
        p.foto('1366-lista-claro')

        # ---- XSS
        conferir('XSS: nenhum script do servico rodou', p.ev('window.__xss')is None, p.ev('String(window.__xss)'))
        conferir('XSS: nenhuma imagem criada, texto aparece literal', p.ev("document.querySelectorAll('img').length")==0 and '<img src=x' in p.ev("document.querySelector('.item').title"))
        p.clica('.item')
        conferir('abrir job: a URL mudou para /jobs/<id>', p.espera(f"location.pathname==='/jobs/{j(1)}'"), p.ev('location.pathname'))
        p.espera("!document.getElementById('j-conteudo').hidden")
        conferir('XSS: mensagem aparece como texto e nada executou',
                 p.ev("document.getElementById('j-msg').textContent.includes('<img src=x')&&document.querySelectorAll('img,#j-msg *,#j-origem *').length===0&&window.__xss===undefined"))
        p.ev("document.getElementById('j-hist').open=true")
        p.espera("document.querySelectorAll('#j-eventos li').length>=5")
        conferir('XSS: historico em texto puro', p.ev("document.querySelectorAll('#j-eventos i').length")==0 and '<i>de teste</i>' in p.ev("document.getElementById('j-eventos').textContent"))
        p.ev("document.getElementById('j-hist').open=false")
        conferir('detalhe do job em andamento', p.ev("document.getElementById('j-pct').textContent")=='42 %' and 'disco.img' in p.ev("document.getElementById('j-nome').textContent"))
        conferir('historia do navegador cresceu (pushState)', p.ev('history.length')>=2)
        p.foto('1366-detalhe-andamento-claro')

        # ---- voltar e recarregar
        p.ev('history.back()')
        conferir('voltar do navegador volta para a lista', p.espera("location.pathname==='/' && document.getElementById('v-job').hidden"))
        p.ev('history.forward()')
        conferir('avancar volta ao job', p.espera(f"location.pathname==='/jobs/{j(1)}' && !document.getElementById('v-job').hidden"))
        recarregar(p)
        conferir('recarregar volta ao mesmo job', p.espera(f"location.pathname==='/jobs/{j(1)}' && document.getElementById('j-pct').textContent==='42 %'"))
        conferir('item do job aberto marcado na lista', p.ev("document.querySelector('.item[aria-current=page]')?.getAttribute('href')")==f'/jobs/{j(1)}')
        p.ir(base + '/jobs/j-nao-existe', 1)
        conferir('job que nao existe: mensagem clara', p.espera("!document.getElementById('j-naoachado').hidden")and p.ev("document.getElementById('j-conteudo').hidden"))
        p.ir(base + f'/jobs/{j(3)}', 1)
        conferir('abrir a URL direto (outra aba) mostra o job', p.espera("document.getElementById('j-tag').textContent==='Concluído'"))

        # ---- botoes por estado
        def botoes():
            return p.ev("[...document.querySelectorAll('#j-acoes [data-acao]')].filter(b=>!b.hidden&&!document.getElementById('j-acoes').hidden).map(b=>b.textContent).join('|')")

        def abrir(i):
            p.ev(f"history.pushState(null,'','/jobs/{j(i)}');dispatchEvent(new PopStateEvent('popstate'))")
            p.espera(f"document.getElementById('j-conteudo').hidden===false")
            time.sleep(.4)
        esperado = {1: 'Pausar|Cancelar', 2: 'Retomar|Cancelar', 3: '', 4: 'Retomar|Cancelar', 5: '', 6: 'Cancelar', 7: ''}
        for i, ex in esperado.items():
            abrir(i)
            conferir(f'botoes do estado {i}: "{ex or "nenhum"}"', botoes() == ex, botoes())
        abrir(2)
        conferir('interrompido: aviso em destaque com Retomar primario',
                 p.ev("(()=>{const a=document.getElementById('j-alerta');return !a.hidden&&a.dataset.tipo==='aviso'&&a.textContent.includes('Retomar')&&document.querySelector('[data-acao=retomar]').classList.contains('btn-primario')})()"))
        p.foto('1366-interrompido-claro')
        abrir(5)
        conferir('falhou: erro aparece como texto', p.ev("(()=>{const a=document.getElementById('j-alerta');return a.textContent.includes('<b onmouseover')&&a.querySelector('b')===null&&window.__xss===undefined})()"))
        abrir(2)
        p.clica('[data-acao=retomar]')
        conferir('Retomar: interrompido vira em andamento e botoes mudam', p.espera("document.getElementById('j-tag').textContent==='Em andamento'") and botoes() == 'Pausar|Cancelar', botoes())
        conferir('aviso de interrompido some', p.ev("document.getElementById('j-alerta').hidden"))
        p.clica('[data-acao=pausar]')
        conferir('Pausar: vira pausado', p.espera("document.getElementById('j-tag').textContent==='Pausado'") and botoes() == 'Retomar|Cancelar', botoes())
        p.clica('[data-acao=cancelar]')
        conferir('Cancelar pede confirmacao e esconde os outros botoes', p.ev("!document.getElementById('j-confirma').hidden&&document.getElementById('j-acoes').hidden"))
        p.clica('#j-confirma-nao')
        conferir('"Não, continuar" volta atras', p.ev("document.getElementById('j-confirma').hidden&&!document.getElementById('j-acoes').hidden")and p.ev("document.getElementById('j-tag').textContent")=='Pausado')
        p.clica('[data-acao=cancelar]')
        p.clica('#j-confirma-sim')
        conferir('"Sim, cancelar" cancela', p.espera("document.getElementById('j-tag').textContent==='Cancelado'") and botoes() == '')
        # 409: outra aba ja pausou
        abrir(6)
        http(porta, f'/_teste/mexer?id={j(6)}&estado=cancelado&avisar=0')
        p.clica('[data-acao=cancelar]')
        p.clica('#j-confirma-sim')
        conferir('409 do servico aparece escrito, sem quebrar', p.espera("!document.getElementById('j-acao-erro').hidden") and 'Não dá para' in p.ev("document.getElementById('j-acao-erro').textContent"), p.ev("document.getElementById('j-acao-erro').textContent"))
        conferir('depois do 409 a tela mostra o estado real', p.espera("document.getElementById('j-tag').textContent==='Cancelado'"))
        http(porta, '/_teste/restaurar')
        http(porta, f'/_teste/mexer?id={j(6)}&estado=na_fila')
        http(porta, f'/_teste/mexer?id={j(2)}&estado=interrompido')
        http(porta, f'/_teste/mexer?id={j(4)}&estado=pausado')
        http(porta, f'/_teste/mexer?id={j(7)}&estado=cancelado')

        # ---- progresso ao vivo
        p.ir(base + f'/jobs/{j(1)}', 1)
        http(porta, f'/_teste/mexer?id={j(1)}&estado=rodando&progresso=0.55')
        conferir('SSE atualiza o progresso do job aberto sem recarregar', p.espera("document.getElementById('j-pct').textContent==='55 %'"))
        conferir('SSE atualiza a barra da lista', p.ev("document.querySelector('.item .esq').textContent.startsWith('55 %')"))

        # ---- aviso ao sair
        def sair():
            return p.ev("(()=>{const e=new Event('beforeunload',{cancelable:true});window.dispatchEvent(e);return e.defaultPrevented})()")
        conferir('beforeunload avisa com job rodando', sair())
        conferir('aviso so liga o ouvinte de saida (o navegador decide; nada fica travado)', p.ev('window.__painel.estado().avisandoSaida'))
        http(porta, f'/_teste/mexer?id={j(1)}&estado=pausado')
        p.espera("window.__painel.estado().avisandoSaida===false")
        conferir('beforeunload nao avisa sem job rodando', not sair())
        http(porta, f'/_teste/mexer?id={j(1)}&estado=rodando')
        p.espera("window.__painel.estado().avisandoSaida===true")
        conferir('volta a avisar quando algum job roda', sair())

        # ---- SSE cai e reconecta
        antes = http(porta, '/_teste/estado')
        http(porta, '/_teste/derrubar')
        conferir('SSE caiu: painel diz "Reconectando"', p.espera("document.getElementById('conexao-texto').textContent.includes('Reconectando')", 3))
        conferir('SSE volta sozinho: "Ao vivo"', p.espera("document.getElementById('conexao-texto').textContent==='Ao vivo'", 10))
        depois = http(porta, '/_teste/estado')
        conferir('foi uma conexao nova, nao a mesma', depois['sse_total'] > antes['sse_total'] and depois['sse_abertos'] == 1, depois)
        http(porta, f'/_teste/mexer?id={j(1)}&progresso=0.61')
        conferir('depois de reconectar o progresso volta a andar', p.espera("document.getElementById('j-pct').textContent==='61 %'"))

        # ---- SSE falha 3 vezes: consulta a cada 2 s
        http(porta, '/_teste/recusar?n=999')
        http(porta, '/_teste/derrubar')
        conferir('SSE falhando: cai para consulta a cada 2 segundos', p.espera("window.__painel.estado().emConsulta", 15) and 'a cada 2 segundos' in p.ev("document.getElementById('conexao-texto').textContent"), p.ev('JSON.stringify(window.__painel.estado())'))
        c0 = http(porta, '/_teste/estado')['consultas']
        http(porta, f'/_teste/mexer?id={j(1)}&progresso=0.7&avisar=0')
        conferir('na consulta o progresso muda sem SSE', p.espera("document.getElementById('j-pct').textContent==='70 %'", 6))
        time.sleep(4.2)
        c1 = http(porta, '/_teste/estado')['consultas']
        conferir('consulta em ritmo de ~2 s', 1 <= c1 - c0 - 1 <= 5 and c1 - c0 >= 2, (c0, c1))
        p.foto('1366-consulta-claro')
        http(porta, '/_teste/recusar?n=0')
        conferir('quando o servico melhora o SSE volta (e a consulta para)', p.espera("document.getElementById('conexao-texto').textContent==='Ao vivo' && !window.__painel.estado().emConsulta", 16), p.ev('JSON.stringify(window.__painel.estado())'))
        c2 = http(porta, '/_teste/estado')['consultas']
        time.sleep(2.6)
        conferir('consulta realmente parou', http(porta, '/_teste/estado')['consultas'] == c2)

        # ---- vazio
        http(porta, '/_teste/zerar')
        conferir('lista vazia: mensagem de vazio', p.espera("!document.getElementById('lista-vazia').hidden && document.querySelectorAll('.item').length===0"))
        conferir('aviso de sair some quando a lista esvazia', p.espera("window.__painel.estado().avisandoSaida===false"))
        p.ir(base + '/', .8)
        p.foto('1366-vazio-claro')
        http(porta, '/_teste/restaurar')
        p.espera("document.querySelectorAll('.item').length===7")

        # ---- erro da lista
        http(porta, '/_teste/quebrar?on=1')
        p.ir(base + '/', 1.2)
        conferir('servico com defeito: estado de erro na lista', p.espera("!document.getElementById('lista-erro').hidden") and 'não conseguiu' in p.ev("document.getElementById('lista-erro-texto').textContent"), p.ev("document.getElementById('lista-erro-texto').textContent"))
        p.foto('1366-erro-claro')
        http(porta, '/_teste/quebrar?on=0')
        p.clica('#lista-tentar')
        conferir('"Tentar de novo" recupera a lista', p.espera("document.querySelectorAll('.item').length===7 && document.getElementById('lista-erro').hidden", 12))
        p.espera("document.getElementById('conexao-texto').textContent==='Ao vivo'", 20)

        # ---- formulario
        p.ir(base + '/', .8)
        conferir('inicio mostra o formulario de nova imagem (computador)', p.ev("!document.getElementById('v-nova').hidden&&document.getElementById('v-job').hidden"))
        p.clica('#f-enviar')
        conferir('campos vazios: pede para preencher', p.ev("document.getElementById('f-erro').textContent")=='Preencha os dois campos.')
        p.ev("document.getElementById('f-origem').value='html';document.getElementById('f-destino').value='D:\\\\x.img'")
        p.clica('#f-enviar')
        conferir('erro do servico no formulario em texto puro', p.espera("!document.getElementById('f-erro').hidden") and p.ev("document.getElementById('f-erro').querySelector('b')")is None and window_xss_ok(p))
        conferir('o que foi digitado fica depois do erro', p.ev("document.getElementById('f-origem').value")=='html')
        p.ev("document.getElementById('f-origem').value='C:\\\\a.img';document.getElementById('f-destino').value='D:\\\\a-copia.img'")
        p.clica('#f-enviar')
        conferir('criar: abre o novo job na URL', p.espera("/^\\/jobs\\/j-20261104-01\\d\\d$/.test(location.pathname)") and p.espera("document.getElementById('j-nome').textContent==='a.img'"), p.ev('location.pathname'))
        conferir('criar: job entra no topo da lista', p.ev("document.querySelector('.item .item-nome').textContent")=='a.img')
        conferir('formulario limpo depois de criar', p.ev("document.getElementById('f-origem').value")=='')

        # ---- largura da lista
        p.ir(base + '/', .8)
        larg = lambda: p.ev("Math.round(document.getElementById('lista').getBoundingClientRect().width)")
        conferir('lista comeca em 320 px', larg() == 320, larg())
        c = p.centro('#alca')
        p.mouse('mousePressed', *c, button='left', clickCount=1)
        p.mouse('mouseMoved', 400, c[1], button='left')
        p.mouse('mouseReleased', 400, c[1], button='left', clickCount=1)
        time.sleep(.3)
        conferir('arrastar a alca muda a largura', larg() == 400, larg())
        conferir('largura fica guardada', json.loads(p.ev("localStorage.getItem('painel.lista')"))['l'] == 400)
        recarregar(p)
        conferir('recarregar mantem a largura', larg() == 400, larg())
        c = p.centro('#alca')
        p.mouse('mousePressed', *c, button='left', clickCount=1)
        p.mouse('mouseMoved', 20, c[1], button='left')
        p.mouse('mouseReleased', 20, c[1], button='left', clickCount=1)
        time.sleep(.3)
        conferir('arrastar ate o canto recolhe a lista', larg() == 0 and p.ev("document.body.classList.contains('recolhida')"))
        p.clica('#alternar-lista')
        conferir('botao do topo reabre', larg() == 400, larg())
        p.ev("localStorage.removeItem('painel.lista')")

        # ---- medidas em varios tamanhos
        def medidas(nome, w, h, tema):
            p.tema(tema)
            p.tamanho(w, h)
            p.ir(base + f'/jobs/{j(1)}', 1)
            p.espera("document.getElementById('j-conteudo').hidden===false")
            p.ev("document.getElementById('j-hist').open=false")
            r = p.ev("""(()=>{const d=document.documentElement,out={};
              out.semRolagemLateral=d.scrollWidth<=innerWidth;
              out.tudoDentro=[...document.querySelectorAll('body *')].filter(e=>e.getClientRects().length&&!['SCRIPT','STYLE','path'].includes(e.tagName)&&!e.closest('svg')&&!e.closest('.principal,.lista-rolagem')).every(e=>{const r=e.getBoundingClientRect();return r.left>=-.5&&r.right<=innerWidth+.5});
              const bs=[...document.querySelectorAll('.principal .btn')].filter(b=>b.getClientRects().length);
              out.botoesMesmaAltura=bs.length>0&&bs.every(b=>Math.abs(b.getBoundingClientRect().height-bs[0].getBoundingClientRect().height)<=.5);
              out.alturaBotao=Math.round(bs[0].getBoundingClientRect().height);
              const m=document.querySelector('.principal').getBoundingClientRect();
              out.principalDentro=[...document.querySelectorAll('.principal .cartao,.principal .dados,.principal .historico,.principal .acoes')].filter(e=>e.getClientRects().length).every(e=>{const r=e.getBoundingClientRect();return r.left>=m.left-.5&&r.right<=m.right+.5});
              out.maiorFonte=Math.max(...[...document.querySelectorAll('body *')].filter(e=>e.getClientRects().length&&e.children.length===0&&e.textContent.trim()).map(e=>parseFloat(getComputedStyle(e).fontSize)));
              out.pctEMaior=parseFloat(getComputedStyle(document.getElementById('j-pct')).fontSize)===out.maiorFonte;
              return out;})()""")
            p.foto(f'{w}x{h}-detalhe-{"claro" if tema == "light" else "escuro"}')
            conferir(f'{nome}: sem rolagem lateral', r['semRolagemLateral'])
            conferir(f'{nome}: nada fora da largura da tela', r['tudoDentro'])
            conferir(f'{nome}: cartoes dentro da area principal', r['principalDentro'])
            conferir(f'{nome}: botoes todos com a mesma altura ({r["alturaBotao"]} px)', r['botoesMesmaAltura'])
            conferir(f'{nome}: o numero que importa (porcentagem) e o maior da tela', r['pctEMaior'], r['maiorFonte'])
            return r
        medidas('1366x768 claro', 1366, 768, 'light')
        medidas('1366x768 escuro', 1366, 768, 'dark')
        medidas('1920x1080 claro', 1920, 1080, 'light')
        medidas('1500x900 claro', 1500, 900, 'light')
        medidas('1280x720 claro', 1280, 720, 'light')
        r = medidas('390x844 claro', 390, 844, 'light')
        conferir('390: botao com altura de toque (>=40)', r['alturaBotao'] >= 40, r['alturaBotao'])
        medidas('390x844 escuro', 390, 844, 'dark')
        medidas('360x640 claro', 360, 640, 'light')

        # ---- celular: uma coluna e navegacao
        p.tema('light')
        p.tamanho(390, 844)
        p.ir(base + '/', 1)
        p.espera("document.querySelectorAll('.item').length>=7")
        conferir('celular: o inicio mostra so a lista', p.ev("getComputedStyle(document.querySelector('.principal')).display==='none'&&document.getElementById('lista').getBoundingClientRect().width>=390"))
        conferir('celular: itens inteiros na tela', p.ev("[...document.querySelectorAll('.item')].every(a=>{const r=a.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth})"))
        conferir('celular: itens com altura de toque', p.ev("[...document.querySelectorAll('.item')].every(a=>a.getBoundingClientRect().height>=44)"))
        p.foto('390x844-lista-claro')
        p.clica('.item')
        conferir('celular: tocar abre o detalhe e esconde a lista', p.espera("getComputedStyle(document.getElementById('lista')).display==='none'&&!document.getElementById('v-job').hidden"))
        p.clica('#v-job [data-voltar]')
        conferir('celular: "Recuperações" volta a lista', p.espera("location.pathname==='/'&&getComputedStyle(document.getElementById('lista')).display!=='none'"))
        p.clica('#nova-link')
        conferir('celular: "Nova imagem" abre o formulario', p.espera("location.hash==='#nova'&&getComputedStyle(document.querySelector('.principal')).display!=='none'&&!document.getElementById('v-nova').hidden"))
        p.foto('390x844-nova-claro')
        p.ev('history.back()')
        conferir('celular: voltar do navegador tambem volta a lista', p.espera("getComputedStyle(document.getElementById('lista')).display!=='none'"))

        # ---- marca vem do servico; nada de marca no codigo
        p.tamanho(1366, 768)
        p.ir(base + '/', 1)
        conferir('marca: nome e empresa vem de /api/marca', p.espera("document.getElementById('marca-nome').textContent==='Marca de Teste'") and p.ev("document.getElementById('marca-sub').textContent")=='Empresa de Teste' and p.ev('document.title')=='Marca de Teste')
        p.ir(base + '/', .3)
        conferir('marca: cor do botao vem do servico', p.ev("getComputedStyle(document.getElementById('f-enviar')).backgroundColor")=='rgb(122, 63, 176)')
        proibidos = ('uv' + 'informatica', 'Cachoeir' + 'inha', '@' + 'uv', '(5' + '1)', '+5' + '5')
        achados = [(f.name, t) for f in (AQUI.parent / 'recuperacao' / 'painel').glob('*') for t in proibidos if t in f.read_text(encoding='utf-8')]
        achados += [(f.name, t) for f in (AQUI / 'testar_painel.py', AQUI / 'painel_falso.py') for t in proibidos if t in f.read_text(encoding='utf-8')]
        conferir('nenhuma marca da empresa escrita no painel nem nos testes', not achados, achados)

        # ---- foco, estados e acessibilidade basica
        p.tamanho(1366, 768)
        p.ir(base + f'/jobs/{j(4)}', 1)
        p.espera("document.getElementById('j-conteudo').hidden===false")
        regras = p.ev("[...document.styleSheets].flatMap(f=>[...f.cssRules]).flatMap(r=>[r,...(r.cssRules?[...r.cssRules]:[])]).map(r=>r.selectorText||'').join('|')")
        conferir('botoes tem hover, pressionado, foco e desabilitado',
                 [x for x in ('.btn-primario:not(:disabled):hover', '.btn-primario:not(:disabled):active', ':focus-visible', '.btn:disabled', '.btn-suave:not(:disabled):hover') if x not in regras] == [], regras[:2000])
        conferir('barra de progresso com papel e valor para leitor de tela', p.ev("document.getElementById('j-barra').getAttribute('role')==='progressbar'&&document.getElementById('j-barra').getAttribute('aria-valuenow')==='77'"))
        conferir('historico fica recolhido', not p.ev("document.getElementById('j-hist').open"))
        codigo = (AQUI.parent / 'recuperacao' / 'painel' / 'painel.js').read_text(encoding='utf-8')
        conferir('painel nunca manda ping nem desliga o servico', '/api/ping' not in codigo and '/api/desligar' not in codigo)
        conferir('painel nao usa innerHTML', 'innerHTML' not in codigo and 'outerHTML' not in codigo and 'insertAdjacentHTML' not in codigo)
        conferir('nenhum erro de script durante o teste todo', p.ev('window.__erros.length')==0, p.ev('JSON.stringify(window.__erros)'))
    finally:
        for q in (edge, srv):
            try:
                if q:
                    q.kill()
                    q.wait(timeout=10)
            except Exception:
                pass
        time.sleep(1)
        shutil.rmtree(perfil, ignore_errors=True)
    falhas = [r for r in RESULTADOS if not r[1]]
    print(f'\n{len(RESULTADOS) - len(falhas)} de {len(RESULTADOS)} conferencias passaram')
    return 1 if falhas else 0


def recarregar(p):
    p.ev('window.__velho=1')
    p.cmd('Page.reload')
    p.espera("window.__velho===undefined && document.readyState==='complete' && document.querySelectorAll('.item').length>0", 10)


def window_xss_ok(p):
    return p.ev('window.__xss')is None


if __name__ == '__main__':
    sys.exit(main())
