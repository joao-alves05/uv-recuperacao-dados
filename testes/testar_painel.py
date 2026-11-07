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
sys.path.insert(0, str(AQUI))
from painel_falso import ACHADOS, XSS_ROTULO  # noqa: E402
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
        s.ev(f"document.querySelector({json.dumps(seletor)})?.scrollIntoView({{block:'nearest'}})")
        c = s.centro(seletor)
        if not c:
            raise RuntimeError('sem elemento ' + seletor)
        s.mouse('mouseMoved', *c)
        s.mouse('mousePressed', *c, button='left', clickCount=1)
        s.mouse('mouseReleased', *c, button='left', clickCount=1)
        time.sleep(depois)

    def tecla(s, key, vk):
        s.cmd('Input.dispatchKeyEvent', type='keyDown', key=key, code=key, windowsVirtualKeyCode=vk)
        s.cmd('Input.dispatchKeyEvent', type='keyUp', key=key, code=key, windowsVirtualKeyCode=vk)
        time.sleep(.3)

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
        conferir('nome muito comprido vira reticencias com dica', p.ev("(()=>{const n=document.querySelectorAll('.item')[3].querySelector('.item-nome');return getComputedStyle(n).webkitLineClamp==='2'&&document.querySelectorAll('.item')[3].title.includes('HD externo')})()"))
        p.foto('1366-lista-claro')

        # ---- XSS
        conferir('XSS: nenhum script do servico rodou', p.ev('window.__xss')is None, p.ev('String(window.__xss)'))
        conferir('XSS: nenhuma imagem criada, texto aparece literal', p.ev("document.querySelectorAll('img').length")==0 and '<img src=x' in p.ev("document.querySelector('.item').title"))
        p.clica('.item')
        conferir('abrir job: a URL mudou para /jobs/<id>', p.espera(f"location.pathname==='/jobs/{j(1)}'"), p.ev('location.pathname'))
        p.espera("!document.getElementById('j-conteudo').hidden")
        conferir('XSS: mensagem aparece como texto e nada executou',
                 p.ev("document.getElementById('j-msg').textContent.includes('<img src=x')&&document.querySelectorAll('img,#j-msg *,#j-origem :not(.origem-nome):not(.origem-det):not(.origem-tec)').length===0&&window.__xss===undefined"))
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

        # ---- tela inicial: os discos como em "Meu computador"
        p.tema('light')
        p.tamanho(1366, 768)
        p.ir(base + '/', 1)
        conferir('inicio: pergunta "Onde estavam os seus arquivos?"', p.espera("document.getElementById('t-inicio').textContent==='Onde estavam os seus arquivos?' && !document.getElementById('v-inicio').hidden") and p.ev("document.getElementById('v-nova').hidden&&document.getElementById('v-job').hidden"))
        conferir('inicio: 7 cartoes de volume', p.espera("document.querySelectorAll('#i-volumes .fonte').length===7"))
        titulos = p.ev("[...document.querySelectorAll('#i-volumes .fonte-nome')].map(n=>n.textContent)")
        conferir('inicio: ordem = removiveis, discos fixos, leitores vazios, disco do Windows por ultimo',
                 titulos == ['Pen drive (E:)', 'Cartão de memória (F:)', 'Dados (D:)', XSS_ROTULO + ' (G:)', 'Leitor de cartões USB', 'CD/DVD (H:)', 'Disco local (C:)'], titulos)
        conferir('inicio: so o disco do Windows leva o selo', p.ev("[...document.querySelectorAll('#i-volumes .fonte')].map(c=>c.querySelector('.tag')?c.querySelector('.tag').textContent:'').join('|')") == '||||||Disco do Windows')
        conferir('inicio: pen drive mostra tipo, sistema de arquivos, barra e espaco',
                 p.ev("(()=>{const c=document.querySelector('#i-volumes .fonte');return c.querySelector('.fonte-sub').textContent==='Pen drive · FAT32'&&c.querySelector('.fonte-meta').textContent==='12 GB livres de 29 GB'&&c.querySelector('.barra-cheia').style.width==='57%'&&c.querySelector('svg')!==null})()"))
        conferir('inicio: cada tipo tem seu icone (todos diferentes entre pen drive, cartao, DVD e disco)',
                 p.ev("new Set([...document.querySelectorAll('#i-volumes .fonte svg')].map(s=>s.innerHTML.length+s.querySelector('path').getAttribute('d'))).size")>=4)
        conferir('inicio: leitor sem cartao e DVD sem disco aparecem apagados e desabilitados',
                 p.ev("(()=>{const v=[...document.querySelectorAll('#i-volumes .fonte.sem-midia')];return v.length===2&&v.every(c=>c.disabled&&parseFloat(getComputedStyle(c).opacity)<1)&&v[0].querySelector('.fonte-sub').textContent==='Sem cartão'&&v[1].querySelector('.fonte-sub').textContent==='Sem disco'&&!v[0].querySelector('.barra')})()"))
        h0 = p.ev('location.hash')
        p.clica('#i-volumes .fonte.sem-midia', .2)
        conferir('inicio: clicar no leitor vazio nao faz nada', p.ev('location.hash') == h0)
        conferir('XSS no rotulo do volume: aparece como texto, nada executa',
                 p.ev("document.querySelectorAll('#i-volumes img,#i-volumes .fonte *[onerror]').length")==0 and p.ev('window.__xss')is None and '<img src=x' in p.ev("document.querySelectorAll('#i-volumes .fonte-nome')[3].textContent"))
        conferir('inicio: "Discos inteiros (avancado)" comeca recolhido', not p.ev("document.getElementById('i-discos').open") and 'Discos inteiros (avançado)' in p.ev("document.querySelector('#i-discos summary').textContent"))
        p.ev("document.getElementById('i-discos').open=true")
        conferir('discos inteiros: quantidade e letras', p.ev("document.querySelectorAll('#i-discos-grade .fonte').length")==4 and p.ev("[...document.querySelectorAll('#i-discos-grade .fonte-meta')].some(m=>m.textContent.includes('D:, G:'))"))
        p.foto('1366-inicio-claro-discos-abertos')
        p.ev("document.getElementById('i-discos').open=false")
        p.foto('1366-inicio-claro')
        conferir('inicio: botao "Atualizar" e link "Avancado: digitar caminhos"', p.ev("document.getElementById('i-atualizar').textContent")=='Atualizar' and p.ev("document.getElementById('avancado-link').textContent")=='Avançado: digitar caminhos')
        conferir('selo do topo diz "Original protegido" com a dica',
                 p.ev("document.querySelector('.selo span').textContent")=='Original protegido' and 'nunca grava no disco que você quer recuperar' in p.ev("document.querySelector('.selo').title")
                 and 'Os arquivos recuperados vão para outro disco.' in p.ev("document.querySelector('.selo').title") and 'Somente leitura' not in p.ev('document.body.textContent'))

        # painel do volume (pen drive)
        p.clica('#i-volumes .fonte')
        conferir('clicar no cartao abre o painel do volume (URL #volume/E)', p.espera("location.hash==='#volume/E' && !document.getElementById('v-volume').hidden && document.getElementById('v-inicio').hidden"), p.ev('location.hash'))
        conferir('painel: botao principal "Recuperar arquivos" e ao lado "Fazer copia de seguranca (imagem)"',
                 p.ev("(()=>{const a=[...document.querySelectorAll('#vol-corpo .acoes .btn')];return a.length===2&&a[0].textContent==='Recuperar arquivos'&&a[0].classList.contains('btn-primario')&&a[1].textContent==='Fazer cópia de segurança (imagem)'&&a.every(b=>!b.disabled)})()"))
        av = p.ev("document.getElementById('vol-corpo').querySelector('.avisos').textContent")
        conferir('painel do pen drive: avisos de TRIM e de parar de usar, sem aviso de administrador', 'TRIM' in av and 'Pare de usar o pen drive' in av and 'administrador' not in av, av)
        p.foto('1366-volume-claro')
        p.ev('history.back()')
        conferir('voltar do navegador volta aos discos', p.espera("location.hash==='' && !document.getElementById('v-inicio').hidden"))

        # administrador
        http(porta, '/_teste/admin?on=0')
        p.clica('#i-atualizar', .6)
        p.clica('#i-volumes .fonte:nth-child(2)')
        p.espera("location.hash==='#volume/F'")
        av = p.ev("document.getElementById('vol-corpo').querySelector('.avisos').textContent")
        conferir('sem administrador: aviso claro com o passo do iniciar-admin.cmd',
                 'Para ler o cartão, o programa precisa de permissão de administrador: feche e abra pelo atalho “iniciar-admin.cmd”.' in av, av)
        conferir('aviso de administrador vem primeiro e em destaque', p.ev("document.querySelector('#vol-corpo .avisos .alerta').dataset.tipo")=='aviso' and 'administrador' in p.ev("document.querySelector('#vol-corpo .avisos .alerta').textContent"))
        conferir('cartao: pede para parar de usar, sem TRIM', 'Pare de usar o cartão até terminar' in av and 'TRIM' not in av, av)
        p.foto('1366-volume-sem-admin-claro')
        http(porta, '/_teste/admin?on=1')

        # Recuperar arquivos: escolher como procurar
        p.clica('#b-recuperar')
        conferir('"Recuperar arquivos" leva a escolha Rapida / Profunda',
                 p.espera("location.hash==='#volume/F/recuperar' && !document.getElementById('v-buscar').hidden") and p.ev("document.querySelectorAll('#buscar-corpo .opcao').length")==2 and 'chega na próxima atualização' not in p.ev("document.getElementById('buscar-corpo').textContent"))
        conferir('escolha da busca: sem botao morto (Comecar a busca e copia antes)', p.ev("(()=>{const b=[...document.querySelectorAll('#v-buscar .btn')].filter(b=>b.getClientRects().length);return b.length===3&&b.every(x=>!x.disabled)&&b.map(x=>x.textContent).join('|')==='‹ Voltar|Começar a busca|Fazer cópia de segurança antes'})()"))
        p.foto('1366-buscar-claro')
        p.clica('#b-buscar-imagem')
        conferir('da escolha da busca da para ir direto a copia de seguranca', p.espera("location.hash==='#volume/F/imagem' && !document.getElementById('v-imagem').hidden"))

        # copia em 3 passos: pen drive E
        p.ir(base + '/?r=1#volume/E/imagem', 1)
        p.espera("document.getElementById('img-nome')")
        conferir('copia: passo 1 (origem) feito, passo 2 (onde salvar) atual, passo 3 pendente',
                 p.ev("[...document.querySelectorAll('#imagem-corpo .passos li')].map(l=>l.className+':'+l.querySelector('.passo-nome').textContent).join('|')")=='feito:Origem|atual:Onde salvar|:Confirmar')
        conferir('copia: origem ja escolhida aparece', 'Pen drive (E:)' in p.ev("document.querySelector('#imagem-corpo > .fonte').textContent"))
        conferir('copia: "Onde salvar?"', 'Onde salvar?' in p.ev("document.querySelector('#imagem-corpo h2').textContent"))
        opcs = p.ev("[...document.querySelectorAll('.opcao .opcao-nome')].map(n=>n.textContent)")
        conferir('copia: destinos sao so de OUTROS volumes (sem o E:, sem leitor vazio, sem DVD)', len(opcs) == 4 and not any('(E:)' in o or '(H:)' in o for o in opcs), opcs)
        sug = p.ev("(()=>{const l=document.querySelector('.opcao:has(input:checked)');return l.querySelector('.opcao-nome').textContent+'|'+(l.querySelector('.tag')?l.querySelector('.tag').textContent:'')})()")
        conferir('copia: destino sugerido = o volume com mais espaco livre que nao e a origem (G:), marcado "Sugerido"', sug == XSS_ROTULO + ' (G:)|Sugerido', sug)
        nome = p.ev("document.getElementById('img-nome').value")
        conferir('copia: nome automatico Recuperacao-E-AAAA-MM-DD.img', __import__('re').fullmatch(r'Recuperacao-E-\d{4}-\d{2}-\d{2}\.img', nome) is not None, nome)
        cam = p.ev("document.getElementById('img-caminho').textContent")
        conferir('copia: caminho mostrado = letra do destino + nome', cam == 'G:\\' + nome, cam)
        conferir('copia: sem aviso de espaco quando cabe e "Continuar" liberado', p.ev("document.getElementById('img-aviso').hidden && !document.getElementById('img-continuar').disabled"))
        p.foto('1366-copia-passo2-claro')
        p.ev("(()=>{const i=document.getElementById('img-nome');i.value='a/b.img';i.dispatchEvent(new Event('input'))})()")
        conferir('copia: nome com caractere proibido bloqueia o Continuar com aviso', p.ev("!document.getElementById('img-aviso').hidden&&document.getElementById('img-continuar').disabled&&document.getElementById('img-aviso').textContent.includes('O nome não pode ter')"))
        p.ev("(()=>{const i=document.getElementById('img-nome');i.value='minha-copia.img';i.dispatchEvent(new Event('input'))})()")
        p.ev("[...document.querySelectorAll('.opcao')].find(l=>l.textContent.includes('Dados')).querySelector('input').click()")
        p.espera("document.getElementById('img-caminho').textContent.startsWith('D:')")
        conferir('copia: trocar de destino muda o caminho e guarda o nome digitado', p.ev("document.getElementById('img-caminho').textContent")=='D:\\minha-copia.img' and p.ev("document.getElementById('img-nome').value")=='minha-copia.img', p.ev("document.getElementById('img-caminho').textContent"))
        p.clica('#img-continuar')
        conferir('copia: passo 3 (confirmar) com origem, destino e tamanho',
                 p.espera("location.hash==='#volume/E/imagem/confirmar' && !!document.getElementById('conf-destino')") and p.ev("document.getElementById('conf-destino').textContent")=='D:\\minha-copia.img'
                 and p.ev("[...document.querySelectorAll('#imagem-corpo .passos li')].map(l=>l.className).join('|')")=='feito|feito|atual' and 'Pen drive (E:)' in p.ev("document.querySelector('#imagem-corpo .dados').textContent") and p.ev("document.querySelector('#imagem-corpo .dados').textContent.includes('29 GB')"))
        p.foto('1366-copia-passo3-claro')
        p.clica('#conf-iniciar', .8)
        conferir('copia: "Iniciar copia" cria o job com a origem do volume e o destino escolhido',
                 p.espera("/^\\/jobs\\/j-20261104-01\\d\\d$/.test(location.pathname)") and http(porta, '/_teste/ultimo')['ultimo'] == {'tipo': 'imagem', 'origem': '\\\\.\\E:', 'destino': 'D:\\minha-copia.img'}, http(porta, '/_teste/ultimo'))

        # falta de espaco: copiar o D: (500 GB) para qualquer outro volume nao cabe
        p.ir(base + '/?r=2#volume/D/imagem', 1)
        p.espera("document.getElementById('img-nome')")
        conferir('copia: aviso quando falta espaco e Continuar bloqueado',
                 p.ev("!document.getElementById('img-aviso').hidden&&document.getElementById('img-aviso').textContent.includes('Não cabe: faltam')&&document.getElementById('img-continuar').disabled"))
        conferir('copia: destino que nao cabe vem marcado na lista', 'não cabe' in p.ev("document.querySelector('.opcao .fonte-meta.falta').textContent"))
        p.foto('1366-copia-sem-espaco-claro')

        # disco inteiro e disco do Windows
        p.ir(base + '/?r=3#disco/disco-1/imagem', 1)
        p.espera("document.getElementById('img-nome')")
        opcs = p.ev("[...document.querySelectorAll('.opcao .opcao-nome')].map(n=>n.textContent)")
        conferir('disco inteiro: destinos nao incluem partes do proprio disco (D:, G:)', len(opcs) == 3 and not any('(D:)' in o or '(G:)' in o for o in opcs), opcs)
        conferir('disco inteiro: nome automatico usa o disco', __import__('re').fullmatch(r'Recuperacao-disco1-\d{4}-\d{2}-\d{2}\.img', p.ev("document.getElementById('img-nome').value")) is not None)
        p.ir(base + '/?r=4#volume/C', 1)
        p.espera("document.getElementById('vol-corpo').textContent.length>10")
        av = p.ev("document.querySelector('#vol-corpo .avisos').textContent")
        conferir('disco do Windows: avisos de TRIM (SSD) e de uso minimo', 'TRIM' in av and 'disco do Windows' in av and 'Pare de usar' not in av, av)
        p.ir(base + '/?r=5#volume/Z', 1)
        conferir('volume que sumiu: mensagem clara e saida para o inicio', p.espera("document.getElementById('vol-corpo').textContent.includes('Não encontramos mais este disco')") and p.ev("document.querySelector('#vol-corpo .btn').textContent")=='Voltar ao início')

        # CD/DVD com disco: a leitura e lenta, mas libera
        http(porta, '/_teste/dvd?on=1')
        p.ir(base + '/?r=6#volume/H', 1)
        p.espera("document.querySelector('#vol-corpo .avisos')")
        conferir('CD/DVD: aviso de leitura lenta e botoes habilitados',
                 'lenta e pode levar horas' in p.ev("document.querySelector('#vol-corpo .avisos').textContent") and p.ev("[...document.querySelectorAll('#vol-corpo .acoes .btn')].every(b=>!b.disabled)"))
        p.foto('1366-dvd-claro')
        http(porta, '/_teste/dvd?on=0')

        # sem discos, erro e carregando
        http(porta, '/_teste/fontes_vazias?on=1')
        p.ir(base + '/?r=7', 1)
        conferir('nenhum disco: mensagem de vazio', p.espera("!document.getElementById('i-vazio').hidden") and p.ev("document.getElementById('i-conteudo').hidden"))
        http(porta, '/_teste/fontes_vazias?on=0')
        p.clica('#i-atualizar', .8)
        conferir('"Atualizar" traz os discos', p.espera("document.querySelectorAll('#i-volumes .fonte').length===7 && document.getElementById('i-vazio').hidden"))
        http(porta, '/_teste/quebrar?on=1')
        p.ir(base + '/?r=8', 1.2)
        conferir('listar discos falhou: erro claro com "Tentar de novo"', p.espera("!document.getElementById('i-erro').hidden") and 'Não consegui listar os discos' in p.ev("document.getElementById('i-erro-texto').textContent"))
        p.foto('1366-inicio-erro-claro')
        http(porta, '/_teste/quebrar?on=0')
        p.clica('#i-tentar')
        conferir('"Tentar de novo" mostra os discos', p.espera("document.querySelectorAll('#i-volumes .fonte').length===7", 12))
        http(porta, '/_teste/atraso?s=1.5')
        p.ir(base + '/?r=9', .5)
        conferir('procurando os discos: estado de carregando', p.ev("!document.getElementById('i-carregando').hidden&&document.getElementById('i-conteudo').hidden"))
        http(porta, '/_teste/atraso?s=0')
        p.espera("document.querySelectorAll('#i-volumes .fonte').length===7", 8)

        # avancado: digitar caminhos
        p.clica('#avancado-link')
        conferir('"Avancado: digitar caminhos" abre o formulario antigo', p.espera("location.hash==='#avancado' && !document.getElementById('v-nova').hidden && document.getElementById('v-inicio').hidden") and p.ev("document.getElementById('t-nova').textContent")=='Avançado: digitar caminhos')
        p.foto('1366-avancado-claro')
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

        # ---- medidas da tela de discos e do painel do volume
        def medidas_discos(nome, w, h, tema):
            p.tema(tema)
            p.tamanho(w, h)
            p.ir(base + '/', 1)
            p.espera("document.querySelectorAll('#i-volumes .fonte').length===7")
            r = p.ev("""(()=>{const d=document.documentElement,out={};
              out.semRolagemLateral=d.scrollWidth<=innerWidth;
              const m=document.querySelector('.principal').getBoundingClientRect();
              const cs=[...document.querySelectorAll('#i-volumes .fonte')];
              out.cartoesDentro=cs.every(e=>{const r=e.getBoundingClientRect();return r.left>=m.left-.5&&r.right<=m.right+.5});
              const linhas={};cs.forEach(c=>{const r=c.getBoundingClientRect();(linhas[Math.round(r.top)]=linhas[Math.round(r.top)]||[]).push(Math.round(r.height))});
              out.linhasIguais=Object.values(linhas).every(a=>a.every(x=>x===a[0]));
              const lefts=new Set(cs.map(c=>Math.round(c.getBoundingClientRect().left)));out.colunas=lefts.size;
              const bs=[...document.querySelectorAll('.principal .btn')].filter(b=>b.getClientRects().length);
              out.botoesMesmaAltura=bs.every(b=>Math.abs(b.getBoundingClientRect().height-bs[0].getBoundingClientRect().height)<=.5);
              out.alturaBotao=Math.round(bs[0].getBoundingClientRect().height);
              out.nomesCortados=cs.filter(c=>{const n=c.querySelector('.fonte-nome');return n.scrollWidth>n.clientWidth&&!n.title}).length;
              out.tituloMaior=Math.max(...[...document.querySelectorAll('.principal *')].filter(e=>e.getClientRects().length&&e.children.length===0&&e.textContent.trim()).map(e=>parseFloat(getComputedStyle(e).fontSize)))===parseFloat(getComputedStyle(document.getElementById('t-inicio')).fontSize);
              return out;})()""")
            p.foto(f'{w}x{h}-inicio-{"claro" if tema == "light" else "escuro"}')
            conferir(f'{nome} (discos): sem rolagem lateral', r['semRolagemLateral'])
            conferir(f'{nome} (discos): cartoes dentro da area principal', r['cartoesDentro'])
            conferir(f'{nome} (discos): cartoes da mesma linha com a mesma altura ({r["colunas"]} colunas)', r['linhasIguais'])
            conferir(f'{nome} (discos): botoes com a mesma altura ({r["alturaBotao"]} px)', r['botoesMesmaAltura'])
            conferir(f'{nome} (discos): nome cortado sempre tem dica', r['nomesCortados'] == 0)
            conferir(f'{nome} (discos): o titulo e o maior texto', r['tituloMaior'])
            return r
        medidas_discos('1366x768 claro', 1366, 768, 'light')
        medidas_discos('1366x768 escuro', 1366, 768, 'dark')
        medidas_discos('1280x720 claro', 1280, 720, 'light')
        medidas_discos('1500x900 claro', 1500, 900, 'light')
        medidas_discos('1920x1080 claro', 1920, 1080, 'light')
        r = medidas_discos('390x844 claro', 390, 844, 'light')
        conferir('390 (discos): uma coluna e botao de toque (>=40)', r['colunas'] == 1 and r['alturaBotao'] >= 40, r)
        medidas_discos('390x844 escuro', 390, 844, 'dark')
        for (w, h, tema) in ((1366, 768, 'dark'), (390, 844, 'light'), (1280, 720, 'light')):
            p.tema(tema)
            p.tamanho(w, h)
            p.ir(base + '/?m=%d#volume/F/imagem' % w, 1)
            p.espera("document.getElementById('img-nome')")
            ok = p.ev("""(()=>{const d=document.documentElement,m=document.querySelector('.principal').getBoundingClientRect();
              const bs=[...document.querySelectorAll('.principal .btn')].filter(b=>b.getClientRects().length);
              const fora=[...document.querySelectorAll('.principal .cartao,.principal .fonte,.principal .passos,.principal .opcao')].filter(e=>e.getClientRects().length).filter(e=>{const r=e.getBoundingClientRect();return !(r.left>=m.left-.5&&r.right<=m.right+.5)}).map(e=>e.className);
              const alt=bs.map(b=>Math.round(b.getBoundingClientRect().height));
              return d.scrollWidth<=innerWidth&&fora.length===0&&alt.every(x=>x===alt[0])||JSON.stringify([d.scrollWidth,innerWidth,fora,alt])})()""")
            p.foto(f'{w}x{h}-copia-{"claro" if tema == "light" else "escuro"}')
            conferir(f'{w}x{h} {tema} (copia): sem rolagem lateral, tudo dentro, botoes iguais', ok is True, ok)
        p.tema('dark')
        p.tamanho(1366, 768)
        p.ir(base + '/?m=9#volume/E', 1)
        p.espera("document.getElementById('b-recuperar')")
        p.foto('1366-volume-escuro')
        p.tema('light')
        p.tamanho(390, 844)
        p.ir(base + '/?m=10#volume/F', 1)
        p.espera("document.getElementById('b-recuperar')")
        p.foto('390x844-volume-claro')

        # ---- celular: uma coluna e navegacao
        p.tema('light')
        p.tamanho(390, 844)
        p.ir(base + '/#lista', 1)
        p.espera("document.querySelectorAll('.item').length>=7")
        conferir('celular: "Ver recuperacoes" mostra so a lista', p.ev("getComputedStyle(document.querySelector('.principal')).display==='none'&&document.getElementById('lista').getBoundingClientRect().width>=390"))
        conferir('celular: itens inteiros na tela', p.ev("[...document.querySelectorAll('.item')].every(a=>{const r=a.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth})"))
        conferir('celular: itens com altura de toque', p.ev("[...document.querySelectorAll('.item')].every(a=>a.getBoundingClientRect().height>=44)"))
        p.foto('390x844-lista-claro')
        p.clica('.item')
        conferir('celular: tocar abre o detalhe e esconde a lista', p.espera("getComputedStyle(document.getElementById('lista')).display==='none'&&!document.getElementById('v-job').hidden"))
        p.clica('#v-job [data-voltar-lista]')
        conferir('celular: "Recuperações" volta a lista', p.espera("location.pathname==='/'&&getComputedStyle(document.getElementById('lista')).display!=='none'"))
        p.clica('#nova-link')
        conferir('celular: "Nova recuperacao" abre a escolha de disco', p.espera("location.hash===''&&getComputedStyle(document.querySelector('.principal')).display!=='none'&&!document.getElementById('v-inicio').hidden&&document.querySelectorAll('#i-volumes .fonte').length===7"))
        p.foto('390x844-inicio-claro')
        p.ev('history.back()')
        conferir('celular: voltar do navegador tambem volta a lista', p.espera("getComputedStyle(document.getElementById('lista')).display!=='none'"))

        # ---- marca vem do servico; nada de marca no codigo
        p.tamanho(1366, 768)
        p.ir(base + '/', 1)
        conferir('marca: nome e empresa vem de /api/marca', p.espera("document.getElementById('marca-nome').textContent==='Marca de Teste'") and p.ev("document.getElementById('marca-sub').textContent")=='Empresa de Teste' and p.ev('document.title')=='Marca de Teste')
        p.ir(base + '/', .3)
        conferir('marca: cor do botao vem do servico', p.espera("getComputedStyle(document.getElementById('nova-link')).backgroundColor==='rgb(122, 63, 176)'", 3) and p.ev("getComputedStyle(document.getElementById('nova-link')).backgroundColor")=='rgb(122, 63, 176)')
        proibidos = ('uv' + 'informatica', 'Cachoeir' + 'inha', '@' + 'uv', '(5' + '1)', '+5' + '5')
        achados = [(f.name, t) for f in (AQUI.parent / 'recuperacao' / 'painel').glob('*') for t in proibidos if t in f.read_text(encoding='utf-8')]
        achados += [(f.name, t) for f in (AQUI / 'testar_painel.py', AQUI / 'painel_falso.py') for t in proibidos if t in f.read_text(encoding='utf-8')]
        conferir('nenhuma marca da empresa escrita no painel nem nos testes', not achados, achados)

        # ---- resultados da busca (fase 5.4)
        p.tema('light')
        p.tamanho(1366, 768)
        vid = http(porta, '/_teste/varredura?estado=concluido')['id']
        p.ir(base + f'/jobs/{vid}', 1)
        conferir('busca: pagina do job mostra o titulo "Busca em Pen drive (E:)" e o rotulo "Varrido"',
                 p.espera("document.getElementById('j-nome').textContent==='Busca em Pen drive (E:)'") and p.ev("document.getElementById('j-l-copiado').textContent")=='Varrido' and p.ev("document.getElementById('j-n-ruins').hidden"))
        conferir('resultados: 100 linhas na primeira pagina de 640', p.espera("document.querySelectorAll('.tabela tbody tr').length===100", 10) and p.ev("document.querySelector('.pg-txt').textContent")=='1 a 100 de 640')
        esperado = {}
        for a in ACHADOS:
            esperado[a['grupo']] = esperado.get(a['grupo'], 0) + 1
        abas = p.ev("Object.fromEntries([...document.querySelectorAll('.aba')].filter(b=>!b.hidden).map(b=>[b.dataset.grupo,b.querySelector('.aba-n').textContent]))")
        conferir('abas por grupo com a contagem certa', abas == dict({'': '640'}, **{g: str(n) for g, n in esperado.items()}), abas)
        conferir('abas: "Todos" marcada', p.ev("document.querySelector('.aba[data-grupo=\"\"]').getAttribute('aria-selected')")=='true')
        p.foto('1366-resultados-lista-claro')

        # ordenar clicando na coluna
        def nomes(n=5):
            return p.ev(f"[...document.querySelectorAll('.tabela tbody tr .col-nome .nome-txt')].slice(0,{n}).map(t=>t.textContent)")

        def esperados(chave, desc=False, n=5, base=ACHADOS):
            return [a['nome'] for a in sorted(base, key=lambda a: (chave(a), a['id']), reverse=desc)][:n]
        conferir('ordem inicial: nome crescente', nomes() == esperados(lambda a: a['nome'].lower()), nomes())
        p.clica('.tabela th.col-tamanho .ord')
        p.espera("document.querySelector('th.col-tamanho').getAttribute('aria-sort')==='ascending'")
        time.sleep(.3)
        conferir('clicar em Tamanho: crescente', nomes() == esperados(lambda a: a['tamanho']), nomes())
        p.clica('.tabela th.col-tamanho .ord')
        p.espera("document.querySelector('th.col-tamanho').getAttribute('aria-sort')==='descending'")
        time.sleep(.3)
        conferir('clicar de novo: decrescente (com seta)', nomes() == esperados(lambda a: a['tamanho'], True) and p.ev("document.querySelector('th.col-tamanho .seta').textContent")=='▼', nomes())
        p.clica('.tabela th.col-data .ord')
        p.clica('.tabela th.col-data .ord')
        p.espera("document.querySelector('th.col-data').getAttribute('aria-sort')==='descending'")
        time.sleep(.3)
        conferir('ordenar por Data decrescente', nomes() == esperados(lambda a: a['data'] or '', True), nomes())
        p.clica('.tabela th.col-tipo .ord')
        p.espera("document.querySelector('th.col-tipo').getAttribute('aria-sort')==='ascending'")
        time.sleep(.3)
        conferir('ordenar por Tipo crescente', nomes() == esperados(lambda a: a['tipo']), nomes())
        p.clica('.tabela th.col-nome .ord')
        p.espera("document.querySelector('th.col-nome').getAttribute('aria-sort')==='ascending'")
        time.sleep(.3)
        conferir('so uma coluna ordena por vez', p.ev("document.querySelectorAll('th[aria-sort=ascending],th[aria-sort=descending]').length")==1)

        # paginar
        p.clica('#r-prox')
        conferir('proxima pagina: 101 a 200 de 640', p.espera("document.querySelector('.pg-txt').textContent==='101 a 200 de 640'") and p.ev("document.getElementById('r-ant').disabled")==False)
        p.clica('#r-ant')
        conferir('pagina anterior volta ao inicio e bloqueia o "Anterior"', p.espera("document.querySelector('.pg-txt').textContent==='1 a 100 de 640'") and p.ev("document.getElementById('r-ant').disabled"))

        # miniaturas e estados
        conferir('fotos: miniatura carregada (img da rota propria)', p.espera("(()=>{const v=[...document.querySelectorAll('.mini img')].filter(i=>{const r=i.getBoundingClientRect(),b=document.querySelector('.tabela-box').getBoundingClientRect();return r.top>=b.top&&r.bottom<=b.bottom});return v.length>0&&v.every(i=>i.complete&&i.naturalWidth===64)})()") and p.ev("document.querySelector('.mini img').getAttribute('src').startsWith('/api/achados/')"))
        conferir('arquivos que nao sao foto mostram icone do grupo', p.ev("[...document.querySelectorAll('.tabela tbody tr')].filter(t=>!t.querySelector('img')).every(t=>t.querySelector('.mini svg'))"))
        conferir('estado de cada arquivo: Bom, Parcial ou Ruim, com dica', p.ev("[...document.querySelectorAll('.tabela .tag.q')].every(t=>['Bom','Parcial','Ruim'].includes(t.textContent)&&t.title.length>10)"))

        # filtros
        def total():
            return p.ev("document.querySelector('.pg-txt')?document.querySelector('.pg-txt').textContent:document.querySelector('.sel-txt').textContent")

        def aba(g):
            p.clica(f'.aba[data-grupo="{g}"]')
            time.sleep(.5)
        aba('foto')
        fotos = [a for a in ACHADOS if a['grupo'] == 'foto']
        conferir('aba Fotos: so fotos', p.espera(f"document.querySelector('.pg-txt').textContent==='1 a 100 de {len(fotos)}'") and p.ev("[...document.querySelectorAll('.tabela tbody tr .col-tipo')].every(t=>['JPG','PNG','GIF','HEIC','CR2'].includes(t.textContent))"))
        p.ev("(()=>{const s=document.querySelector('select[aria-label=Tipo]');s.value='PNG';s.dispatchEvent(new Event('change'))})()")
        pngs = [a for a in fotos if a['tipo'] == 'PNG']
        time.sleep(.6)
        conferir('filtro de tipo PNG', p.ev("document.querySelectorAll('.tabela tbody tr').length")==len(pngs) and p.ev("[...document.querySelectorAll('.tabela tbody tr .col-tipo')].every(t=>t.textContent==='PNG')"))
        p.ev("(()=>{const s=document.querySelector('select[aria-label=Estado]');s.value='parcial';s.dispatchEvent(new Event('change'))})()")
        time.sleep(.6)
        pp = [a for a in pngs if a['estado'] == 'parcial']
        conferir('filtros combinados: tipo + estado', p.ev("document.querySelectorAll('.tabela tbody tr').length")==len(pp) and p.ev("[...document.querySelectorAll('.tabela tbody tr .tag.q')].every(t=>t.textContent==='Parcial')"), len(pp))
        p.clica('#r-limpar')
        time.sleep(.6)
        conferir('"Limpar filtros" tira tipo e estado mas mantem a aba', p.ev("document.querySelector('.aba.ativa').dataset.grupo")=='foto' and p.ev("document.querySelector('.pg-txt').textContent")==f'1 a 100 de {len(fotos)}' and p.ev("document.getElementById('r-limpar').disabled"))
        p.ev("(()=>{const s=document.querySelector('select[aria-label=Tamanho]');s.value='xx';s.dispatchEvent(new Event('change'))})()")
        aba('video')
        time.sleep(.4)
        grandes = [a for a in ACHADOS if a['grupo'] == 'video' and a['tamanho'] > 104857600]
        conferir('filtro de tamanho (mais de 100 MB) nos videos', p.espera(f"document.querySelectorAll('.tabela tbody tr').length===({len(grandes)}||0)||document.querySelector('.vazio:not([hidden])')!==null") and (p.ev("document.querySelectorAll('.tabela tbody tr').length")==len(grandes)))
        p.clica('#r-limpar')
        aba('')
        p.ev("(()=>{const i=document.querySelector('input[type=search]');i.value='extremamente';i.dispatchEvent(new Event('input'))})()")
        conferir('busca por nome (com espera de digitacao)', p.espera("document.querySelectorAll('.tabela tbody tr').length===1", 5))
        conferir('nome muito comprido: reticencias e dica com o nome inteiro', p.ev("(()=>{const t=document.querySelector('.tabela tbody tr .col-nome .nome-txt');return t.scrollWidth>t.clientWidth&&getComputedStyle(t).textOverflow==='ellipsis'&&t.closest('td').title.includes('reticencias')})()"))
        p.ev("(()=>{const i=document.querySelector('input[type=search]');i.value='zzzz-nao-existe';i.dispatchEvent(new Event('input'))})()")
        conferir('nada combina: mensagem clara', p.espera("!document.querySelector('.res-area .vazio').hidden && document.querySelector('.res-area .vazio').textContent.includes('Nenhum arquivo combina')", 5))
        p.ev("(()=>{const i=document.querySelector('input[type=search]');i.value='<img';i.dispatchEvent(new Event('input'))})()")
        conferir('XSS no nome do arquivo: aparece como texto e nada executa', p.espera("document.querySelectorAll('.tabela tbody tr').length===1", 5) and '<img src=x onerror' in p.ev("document.querySelector('.tabela tbody tr .col-nome .nome-txt').textContent") and p.ev("document.querySelectorAll('.tabela img[onerror],.tabela tbody .col-nome .nome-txt *').length")==0 and p.ev('window.__xss')is None)
        p.clica('#r-limpar')
        p.ev("(()=>{const d=document.querySelectorAll('.filtros input[type=date]');d[0].value='2025-01-01';d[0].dispatchEvent(new Event('change'));d[1].value='2025-12-31';d[1].dispatchEvent(new Event('change'))})()")
        no_ano = [a for a in ACHADOS if a['data'] and '2025-01-01' <= a['data'][:10] <= '2025-12-31']
        time.sleep(.7)
        conferir('filtro de data (de / ate)', p.ev("document.querySelector('.pg-txt').textContent")==f'1 a {min(100, len(no_ano))} de {len(no_ano)}', p.ev("document.querySelector('.pg-txt').textContent"))
        p.clica('#r-limpar')
        time.sleep(.5)

        # selecao
        p.espera("document.querySelectorAll('.tabela tbody tr').length===100")
        p.clica('.tabela thead input[type=checkbox]')
        conferir('caixa do cabecalho marca todos desta pagina', p.espera("document.querySelector('.sel-txt').textContent.startsWith('100 selecionados')") and p.ev("[...document.querySelectorAll('.tabela tbody input[type=checkbox]')].every(c=>c.checked)"))
        conferir('oferece "Selecionar os 640 da lista"', p.ev("!document.getElementById('r-sel-filtro').hidden&&document.getElementById('r-sel-filtro').textContent")=='Selecionar os 640 da lista')
        p.clica('#r-sel-filtro')
        conferir('selecionar tudo (640) de todas as paginas', p.espera("document.querySelector('.sel-txt').textContent.startsWith('Todos os 640 arquivos da lista estão selecionados')") and p.ev("window.__painel.resultados().modo")=='filtro')
        p.clica('.tabela tbody tr:nth-child(3) input[type=checkbox]')
        conferir('desmarcar um tira so ele (639 selecionados)', p.espera("document.querySelector('.sel-txt').textContent.startsWith('639 selecionados')", 8) and p.ev("window.__painel.resultados().modo")=='ids')
        p.clica('#r-prox')
        p.espera("document.querySelector('.pg-txt').textContent==='101 a 200 de 640'")
        conferir('a selecao continua na pagina 2', p.ev("[...document.querySelectorAll('.tabela tbody input[type=checkbox]')].every(c=>c.checked)"))
        p.clica('#r-sel-limpar')
        conferir('"Limpar selecao" zera', p.espera("document.querySelector('.sel-txt').textContent==='640 arquivos na lista'") and p.ev("document.getElementById('r-recuperar').disabled"))
        p.clica('#r-ant')
        p.espera("document.querySelector('.pg-txt').textContent==='1 a 100 de 640'")
        aba('foto')
        docs = [a for a in ACHADOS if a['grupo'] == 'foto']
        p.espera(f"document.querySelector('.pg-txt').textContent==='1 a 100 de {len(docs)}'")
        p.clica('.tabela thead input[type=checkbox]')
        p.clica('#r-sel-filtro')
        conferir('selecionar por grupo: so as fotos', p.espera(f"document.querySelector('.sel-txt').textContent.startsWith('Todos os {len(docs)} arquivos')") and p.ev("window.__painel.resultados().modo")=='filtro')
        aba('documento')
        conferir('mudar de aba desfaz a selecao "todos" (nao recupera o que nao esta a vista)', p.espera("window.__painel.resultados().modo==='ids'&&window.__painel.resultados().selecionados===0"))
        aba('')

        # grade
        p.clica('#r-v-grade')
        conferir('visao em grade: 100 cartoes com miniatura e estado', p.espera("document.querySelectorAll('.grade .item-grade').length===100&&document.querySelector('.tabela-box').hidden") and p.ev("document.querySelectorAll('.grade .mini img').length")>=1)
        conferir('grade: escolha de ordem aparece', p.ev("!document.querySelector('.ordenar-grade').hidden"))
        p.foto('1366-resultados-grade-claro')
        p.clica('.grade .item-grade:nth-child(2) .ig-nome')
        conferir('grade: clicar no cartao marca o arquivo', p.espera("document.querySelector('.sel-txt').textContent.startsWith('1 selecionado')") and p.ev("document.querySelector('.grade .item-grade:nth-child(2)').classList.contains('marcada')"))
        recarregar(p)
        conferir('a visao escolhida (grade) fica guardada', p.espera("document.querySelectorAll('.grade .item-grade').length>0"))
        p.clica('#r-v-lista')
        p.espera("!document.querySelector('.tabela-box').hidden")

        # arvore de pastas
        conferir('arvore de pastas aparece (achados com caminho)', p.espera("!document.querySelector('.arvore').hidden") and p.ev("document.querySelectorAll('.arvore .no-linha').length")>=1)
        p.espera("document.querySelectorAll('.arvore .no-linha').length>=5")
        nomes_pastas = p.ev("[...document.querySelectorAll('.arvore .no-nome .no-txt')].map(n=>n.textContent)")
        conferir('arvore: raiz e as pastas do original', nomes_pastas[:5] == ['Todas as pastas', 'DCIM', 'Documentos', 'Musicas', 'Videos'], nomes_pastas)
        p.ev("[...document.querySelectorAll('.arvore .no-nome')].find(b=>b.textContent.startsWith('DCIM')).click()")
        dcim = [a for a in ACHADOS if a.get('caminho', '').startswith('DCIM')]
        time.sleep(.7)
        conferir('clicar numa pasta filtra os achados dela', p.ev("document.querySelector('.pg-txt').textContent")==f'1 a 100 de {len(dcim)}' and p.ev("document.querySelector('.no-linha.atual').dataset.caminho")=='DCIM')
        conferir('achados do sistema de arquivos: caminho original e selo "Apagado"', p.ev("(()=>{const r=[...document.querySelectorAll('.tabela tbody tr')];return r.length>0&&r.every(t=>t.querySelector('.nome-cam')&&t.querySelector('.nome-cam').textContent.startsWith('DCIM')&&t.querySelector('.tag.apagado')&&t.querySelector('.tag.apagado').textContent==='Apagado')})()"))
        p.ev("[...document.querySelectorAll('.arvore .no-linha')].find(l=>l.dataset.caminho==='DCIM').querySelector('.no-seta').click()")
        conferir('abrir a pasta mostra as subpastas', p.espera("[...document.querySelectorAll('.arvore .no-linha')].some(l=>l.dataset.caminho==='DCIM/100CANON')"))
        p.foto('1366-resultados-arvore-claro')
        larg = lambda: p.ev("Math.round(document.querySelector('.arvore').getBoundingClientRect().width)")
        p.ev("document.querySelector('.alca-arvore').scrollIntoView({block:'center'})")
        c = p.centro('.alca-arvore')
        l0 = larg()
        p.mouse('mousePressed', *c, button='left', clickCount=1)
        p.mouse('mouseMoved', c[0] + 60, c[1], button='left')
        p.mouse('mouseReleased', c[0] + 60, c[1], button='left', clickCount=1)
        time.sleep(.3)
        conferir('arrastar a alca muda a largura da arvore e guarda', larg() > l0 + 40 and json.loads(p.ev("localStorage.getItem('painel.arvore')"))['l'] > l0 + 40, (l0, larg()))
        p.clica('.arvore-cab .btn')
        conferir('"Recolher" esconde a arvore e "Mostrar pastas" traz de volta', p.ev("document.querySelector('.arvore').getBoundingClientRect().width")==0 and p.ev("!document.querySelector('.resultados > .btn').hidden"))
        p.clica('.resultados > .btn')
        conferir('arvore volta', larg() > 100)
        p.ev("[...document.querySelectorAll('.arvore .no-nome')].find(b=>b.textContent.startsWith('Todas')).click()")
        time.sleep(.5)
        conferir('achados pelo conteudo (sem caminho) nao levam selo "Apagado"', p.ev("(()=>{const r=[...document.querySelectorAll('.tabela tbody tr')].filter(t=>!t.querySelector('.nome-cam'));return r.length>0&&r.every(t=>!t.querySelector('.tag.apagado'))})()"))

        # recuperar selecionados
        p.clica('.tabela tbody tr:nth-child(1) input[type=checkbox]')
        p.clica('.tabela tbody tr:nth-child(2) input[type=checkbox]')
        p.clica('.tabela tbody tr:nth-child(3) input[type=checkbox]')
        conferir('"Recuperar selecionados" so vale com algo marcado, e mostra a quantidade', p.espera("document.getElementById('r-recuperar').textContent==='Recuperar selecionados (3)'") and not p.ev("document.getElementById('r-recuperar').disabled"))
        sug = p.ev("(()=>{const s=document.querySelector('select[aria-label=\"Disco de destino\"]');return s.options[s.selectedIndex].textContent})()")
        conferir('destino sugerido: outro disco, o de mais espaco (G:), nunca o da origem (E:)', sug.endswith('(G:) (sugerido)') and p.ev("[...document.querySelector('select[aria-label=\"Disco de destino\"]').options].every(o=>!o.textContent.includes('(E:)'))"), sug)
        conferir('caminho de destino mostrado', 'G:\\Recuperados' in p.ev("document.querySelector('.recuperar .dica').textContent"))
        p.ev("(()=>{const i=document.querySelector('input[aria-label=\"Nome da pasta de destino\"]');i.value='a/b';i.dispatchEvent(new Event('input'))})()")
        conferir('nome de pasta invalido bloqueia o botao', p.ev("document.getElementById('r-recuperar').disabled&&!document.querySelector('.recuperar .alerta').hidden"))
        p.ev("(()=>{const i=document.querySelector('input[aria-label=\"Nome da pasta de destino\"]');i.value='Recuperados';i.dispatchEvent(new Event('input'))})()")
        p.foto('1366-resultados-recuperar-claro')
        p.ev("document.getElementById('r-recuperar').scrollIntoView({block:'center'})"); time.sleep(.5); p.clica('#r-recuperar', .8)
        conferir('recuperar: abre o job de recuperacao e o servico recebeu os 3 arquivos e o destino',
                 p.espera("/^\\/jobs\\/j-20261104-09\\d\\d$/.test(location.pathname) && document.getElementById('j-nome').textContent==='Arquivos recuperados'") and http(porta, '/_teste/ultimo_rec')['rec']['n'] == 3 and http(porta, '/_teste/ultimo_rec')['rec']['destino'] == 'G:\\Recuperados', http(porta, '/_teste/ultimo_rec'))
        rid = p.ev('location.pathname').split('/')[-1]
        conferir('job de recuperacao: "Salvando em" e rotulo "Recuperado"', p.ev("document.getElementById('j-dt-destino').textContent")=='Salvando em' and p.ev("document.getElementById('j-l-copiado').textContent")=='Recuperado' and p.ev("document.getElementById('j-pos').hidden"))
        http(porta, f'/_teste/mexer?id={rid}&estado=concluido&progresso=1')
        conferir('concluido: aparecem "Abrir pasta" e "Ver relatorio"', p.espera("!document.getElementById('j-pos').hidden") and p.ev("document.getElementById('j-relatorio').getAttribute('href')")==f'/api/jobs/{rid}/relatorio')
        p.clica('#j-abrir-pasta', .5)
        conferir('"Abrir pasta" pede ao servico para abrir o destino', http(porta, '/_teste/ultimo_rec')['abriu'] == 'G:\\Recuperados')
        http(porta, '/_teste/abrir_falha?on=1')
        p.clica('#j-abrir-pasta', .5)
        conferir('se nao abrir, o erro vem escrito com o caminho da pasta', p.espera("!document.getElementById('j-pos-erro').hidden") and 'G:\\Recuperados' in p.ev("document.getElementById('j-pos-erro').textContent"))
        http(porta, '/_teste/abrir_falha?on=0')
        p.foto('1366-recuperacao-pronta-claro')
        p.ir(base + f'/jobs/{vid}', 1)
        p.espera("document.querySelectorAll('.tabela tbody tr').length===100")
        conferir('na lista, o que ja foi recuperado vem marcado', p.ev("document.querySelectorAll('.tabela .recuperado').length")>=1)
        http(porta, '/_teste/zerar_recuperados')

        # busca em andamento: os achados aparecem enquanto varre
        vid2 = http(porta, '/_teste/varredura?estado=rodando&progresso=0')['id']
        p.ir(base + f'/jobs/{vid2}', 1)
        conferir('busca sem achados ainda: "Procurando…"', p.espera("document.querySelector('.res-area .vazio') && document.querySelector('.res-area .vazio').textContent.includes('Procurando')"))
        http(porta, f'/_teste/mexer?id={vid2}&progresso=0.25')
        conferir('achados aparecem enquanto varre (25% = 160)', p.espera("window.__painel.resultados()&&window.__painel.resultados().total===160", 8), p.ev('JSON.stringify(window.__painel.resultados())'))
        p.foto('1366-resultados-andando-claro')
        http(porta, f'/_teste/mexer?id={vid2}&progresso=0.5')
        conferir('e continuam chegando (50% = 320)', p.espera("window.__painel.resultados().total===320", 8))
        http(porta, f'/_teste/mexer?id={vid2}&estado=concluido&progresso=1')
        conferir('ao terminar: 640 e aviso de busca terminada', p.espera("window.__painel.resultados().total===640", 8) and p.espera("document.getElementById('j-alerta').textContent.includes('Busca terminada')"))

        # servico com defeito na lista de achados
        http(porta, '/_teste/quebrar?on=1')
        p.ir(base + f'/jobs/{vid}', 1.5)
        conferir('lista de achados com erro: mensagem clara e "Tentar de novo"', p.espera("!!document.querySelector('.res-area .vazio-erro .btn') && document.querySelector('.res-area .vazio-erro').textContent.includes('Não consegui ler os resultados')", 8))
        http(porta, '/_teste/quebrar?on=0')

        # escolher o modo e comecar a busca
        p.ir(base + '/?r=20#volume/E/recuperar', 1)
        p.espera("document.getElementById('b-iniciar-busca')")
        conferir('"Recuperar arquivos": escolher Rapida ou Profunda, cada uma explicada',
                 p.ev("[...document.querySelectorAll('#buscar-corpo .opcao')].map(o=>o.querySelector('.opcao-nome').textContent).join('|')")=='Rápida|Profunda' and p.ev("document.querySelector('#buscar-corpo .opcao input').checked")
                 and p.ev("[...document.querySelectorAll('#buscar-corpo .opcao-explica')].every(e=>e.textContent.length>30)"))
        conferir('modo: avisos do pen drive tambem aparecem aqui', 'TRIM' in p.ev("document.querySelector('#buscar-corpo .avisos').textContent"))
        p.foto('1366-buscar-modo-claro')
        p.ev("document.querySelectorAll('#buscar-corpo .opcao input')[1].click()")
        p.espera("document.querySelectorAll('#buscar-corpo .opcao input')[1].checked")
        p.clica('#b-iniciar-busca', .8)
        conferir('"Comecar a busca": cria o job de varredura com o modo escolhido',
                 p.espera("/^\\/jobs\\/j-20261104-01\\d\\d$/.test(location.pathname) && document.getElementById('j-nome').textContent==='Busca em Pen drive (E:)'") and http(porta, '/_teste/ultimo_rec')['opcoes'] == {'modo': 'profunda', 'sistema_antes': True} and http(porta, '/_teste/ultimo')['ultimo'] == {'tipo': 'varredura', 'origem': '\\\\.\\E:', 'destino': ''}, http(porta, '/_teste/ultimo'))

        # medidas das telas de resultados
        def medidas_res(nome, w, h, tema, visao):
            p.tema(tema)
            p.tamanho(w, h)
            p.ir(base + f'/jobs/{vid}', 1)
            p.espera("document.querySelectorAll('.tabela tbody tr,.grade .item-grade').length>0")
            p.ev(f"(()=>{{const b=document.getElementById('r-v-{visao}');b.click()}})()")
            time.sleep(.6)
            r = p.ev("""(()=>{const d=document.documentElement,m=document.querySelector('.principal').getBoundingClientRect();
              const vis=[...document.querySelectorAll('.resultados .btn,.resultados .aba')].filter(b=>b.getClientRects().length);
              const alt=vis.map(b=>Math.round(b.getBoundingClientRect().height));
              const fora=[...document.querySelectorAll('.resultados > *,.res-area > *,.cartao')].filter(e=>e.getClientRects().length).filter(e=>{const r=e.getBoundingClientRect();return r.right>m.right+.5||r.left<m.left-.5}).map(e=>e.className);
              return {lateral:d.scrollWidth<=innerWidth,fora,alturas:[...new Set(alt)],maior:Math.max(...[...document.querySelectorAll('.resultados *')].filter(e=>e.getClientRects().length&&e.children.length===0&&e.textContent.trim()).map(e=>parseFloat(getComputedStyle(e).fontSize)))}})()""")
            p.foto(f'{w}x{h}-resultados-{visao}-{"claro" if tema == "light" else "escuro"}')
            conferir(f'{nome} (resultados, {visao}): sem rolagem lateral e nada fora da area', r['lateral'] and not r['fora'], r)
            conferir(f'{nome} (resultados, {visao}): botoes e abas com a mesma altura', len(r['alturas']) == 1, r['alturas'])
            conferir(f'{nome} (resultados, {visao}): fonte maxima 14', r['maior'] <= 14, r['maior'])
        medidas_res('1366x768 claro', 1366, 768, 'light', 'lista')
        medidas_res('1366x768 escuro', 1366, 768, 'dark', 'lista')
        medidas_res('1366x768 claro', 1366, 768, 'light', 'grade')
        medidas_res('1280x720 claro', 1280, 720, 'light', 'lista')
        medidas_res('1500x900 claro', 1500, 900, 'light', 'lista')
        medidas_res('1920x1080 claro', 1920, 1080, 'light', 'lista')
        medidas_res('390x844 claro', 390, 844, 'light', 'lista')
        medidas_res('390x844 escuro', 390, 844, 'dark', 'grade')
        p.tema('light')
        p.tamanho(1366, 768)

        # ---- fase 7: etapas, buscas anteriores, prévia, partições e CD/DVD
        p.tema('light')
        p.tamanho(1366, 768)

        def etapas():
            return p.ev("(()=>{const n=document.getElementById('passos-rec');return n.hidden?'':[...n.querySelectorAll('li')].map(l=>(l.className||'-')+':'+l.querySelector('.passo-nome').textContent).join('|')})()")
        p.ir(base + '/?f=1', 1)
        p.espera("document.querySelectorAll('#i-volumes .fonte').length===7")
        conferir('etapas: no inicio, "Escolher local" e a atual', etapas() == 'atual:Escolher local|-:Buscar|-:Ver e recuperar', etapas())
        p.ir(base + '/?f=2#volume/E/recuperar', 1)
        p.espera("document.getElementById('b-iniciar-busca')")
        conferir('etapas: na escolha Rapida/Profunda, "Buscar" e a atual', etapas() == 'feito:Escolher local|atual:Buscar|-:Ver e recuperar', etapas())
        p.ir(base + '/?f=3#volume/E/imagem', 1)
        p.espera("document.getElementById('img-nome')")
        conferir('etapas: a copia de seguranca tem as suas proprias etapas (as da recuperacao somem)', etapas() == '')
        p.ir(base + '/?f=4#avancado', 1)
        conferir('etapas: no "Avancado: digitar caminhos" tambem somem', etapas() == '')
        vid3 = http(porta, '/_teste/varredura?estado=rodando&progresso=0.3')['id']
        p.ir(base + f'/jobs/{vid3}', 1)
        conferir('etapas: busca andando = "Buscar"', p.espera("document.getElementById('passos-rec').hidden===false") and etapas() == 'feito:Escolher local|atual:Buscar|-:Ver e recuperar', etapas())
        http(porta, f'/_teste/mexer?id={vid3}&estado=concluido&progresso=1')
        conferir('etapas: busca concluida = "Ver e recuperar"', p.espera("[...document.querySelectorAll('#passos-rec li')].map(l=>l.className||'-').join('|')==='feito|feito|atual'"), etapas())

        # topo compacto: a lista comeca acima da dobra
        p.ev("localStorage.setItem('painel.visao','lista')")
        p.ir(base + f'/jobs/{vid}', 1)
        p.espera("document.querySelectorAll('.tabela tbody tr').length===100")
        for (w, h) in ((1366, 768), (1280, 720)):
            p.tamanho(w, h)
            time.sleep(.4)
            vis = p.ev("(()=>{document.getElementById('principal').scrollTop=0;return [...document.querySelectorAll('.tabela tbody tr')].filter(r=>{const x=r.getBoundingClientRect();return x.top>=0&&x.bottom<=innerHeight}).length})()")
            conferir(f'{w}x{h}: depois da busca, a lista ja mostra linhas inteiras sem rolar ({vis})', vis >= (4 if h == 768 else 3), vis)
        p.tamanho(1366, 768)
        p.foto('1366-resultados-compacto-claro')

        # previa ampliada
        p.clica('.tabela tbody tr:nth-child(1) .mini')
        conferir('previa: clicar na miniatura abre a foto ampliada com o nome', p.espera("document.querySelector('.previa').open") and '<img src=x onerror' in p.ev("document.getElementById('pv-titulo').textContent") and p.espera("(()=>{const i=document.querySelector('.pv-img img');return !!i&&i.complete&&i.naturalWidth===64})()"))
        conferir('previa: XSS no nome nao vira elemento', p.ev("document.querySelectorAll('.previa [onerror]').length")==0 and p.ev('window.__xss')is None)
        det = p.ev("document.querySelector('.pv-det').textContent")
        conferir('previa: detalhes (tipo, tamanho, data, pasta, estado, por que)', all(x in det for x in ('Tipo', 'Tamanho', 'Data', 'Pasta de origem', 'Estado', 'Por quê', 'estrutura do arquivo')), det)
        p.foto('1366-previa-claro')
        t1 = p.ev("document.getElementById('pv-titulo').textContent")
        p.clica('#pv-prox')
        conferir('previa: "Proximo" mostra o arquivo seguinte (aviso sem previa para video/documento)', p.espera(f"document.getElementById('pv-titulo').textContent!=={json.dumps(t1)}") and p.ev("document.getElementById('pv-ant').disabled")==False and p.ev("!!document.querySelector('.pv-sem')") and 'não tem prévia' in p.ev("document.querySelector('.pv-sem').textContent"))
        p.tecla('ArrowRight', 39)
        conferir('previa: seta direita avanca', p.espera("document.querySelector('.previa .pg-txt').textContent.startsWith('3 de 100')"))
        p.tecla('ArrowLeft', 37)
        conferir('previa: seta esquerda volta', p.espera("document.querySelector('.previa .pg-txt').textContent.startsWith('2 de 100')"))
        p.clica('#pv-marcar')
        conferir('previa: "Marcar para recuperar" seleciona o arquivo', p.espera("document.getElementById('pv-marcar').textContent==='Tirar da seleção'") and p.ev("window.__painel.resultados().selecionados")==1)
        p.tecla('Escape', 27)
        conferir('previa: Esc fecha e o foco volta para a miniatura', p.espera("!document.querySelector('.previa').open") and p.ev("document.activeElement.classList.contains('mini')"))
        p.clica('#r-sel-limpar')
        p.clica('#r-v-grade')
        p.espera("document.querySelectorAll('.grade .item-grade').length===100")
        p.clica('.grade .item-grade:nth-child(1) .mini')
        conferir('previa: tambem abre pela grade', p.espera("document.querySelector('.previa').open"))
        p.tecla('Escape', 27)
        p.clica('#r-v-lista')

        # buscas anteriores
        p.ir(base + '/?f=5', 1)
        p.espera("document.querySelectorAll('#i-volumes .fonte').length===7")
        conferir('inicio: "Buscas anteriores" lista as buscas com o numero de achados', p.espera("!document.getElementById('i-buscas').hidden && [...document.querySelectorAll('#i-buscas-lista .item')].some(i=>i.textContent.includes('640 arquivos achados'))", 8), p.ev("document.getElementById('i-buscas-lista').textContent"))
        n_busca = p.ev("fetch('/api/jobs').then(r=>r.json()).then(l=>l.filter(j=>j.tipo==='varredura').length)", True)
        conferir('buscas anteriores: uma linha por busca (ate 8)', p.ev("document.querySelectorAll('#i-buscas-lista .item').length")==min(8, n_busca), n_busca)
        p.foto('1366-inicio-buscas-claro')
        antes = http(porta, '/_teste/estado')['jobs']
        p.clica('#i-buscas-lista .item')
        conferir('reabrir uma busca mostra os resultados sem criar nada novo', p.espera("/^\\/jobs\\/j-20261104-09\\d\\d$/.test(location.pathname)&&!document.getElementById('j-resultados').hidden") and http(porta, '/_teste/estado')['jobs'] == antes)

        # particoes perdidas
        p.ir(base + '/?f=6', 1)
        p.ev("document.getElementById('i-discos').open=true")
        conferir('discos inteiros: o texto ensina a "Procurar particoes perdidas"', 'Procurar partições perdidas' in p.ev("document.querySelector('#i-discos > .dica').textContent"))
        p.ev("[...document.querySelectorAll('#i-discos-grade .fonte')].find(c=>c.textContent.includes('ST1000DM010')).click()")
        conferir('painel do disco inteiro tem o botao "Procurar particoes perdidas"; o do volume nao', p.espera("location.hash==='#disco/disco-1'&&!!document.getElementById('b-particoes')") and not p.ev("document.getElementById('b-particoes').disabled"))
        p.foto('1366-disco-claro')
        p.clica('#b-particoes')
        conferir('particoes: tela de preparo com destino sugerido em outro disco', p.espera("location.hash==='#disco/disco-1/particoes'&&!!document.getElementById('part-dest')") and '(sugerido)' in p.ev("document.getElementById('part-dest').options[document.getElementById('part-dest').selectedIndex].textContent") and p.ev("[...document.getElementById('part-dest').options].every(o=>!o.textContent.includes('(D:)')&&!o.textContent.includes('(G:)'))"))
        p.foto('1366-particoes-preparo-claro')
        p.clica('#b-iniciar-particoes', .8)
        ult = http(porta, '/_teste/ultimo')['ultimo']
        conferir('particoes: cria o job `particoes` com a origem do disco e o relatorio em outro disco',
                 p.espera("/^\\/jobs\\/j-20261104-01\\d\\d$/.test(location.pathname)&&document.getElementById('j-nome').textContent==='Partições em ST1000DM010-2EP102'") and ult['tipo'] == 'particoes' and ult['origem'] == '\\\\.\\PhysicalDrive1' and __import__('re').fullmatch(r'[A-Z]:\\Particoes-disco1-\d{4}-\d{2}-\d{2}\.json', ult['destino']) is not None, ult)
        pid = p.ev('location.pathname').split('/')[-1]
        conferir('particoes: enquanto procura, nada de lista de candidatos', p.ev("document.getElementById('j-particoes').hidden"))
        http(porta, f'/_teste/mexer?id={pid}&estado=concluido&progresso=1')
        conferir('particoes: terminada, lista os 3 candidatos pela confianca', p.espera("document.querySelectorAll('#j-particoes .parte').length===3") and p.ev("[...document.querySelectorAll('#j-particoes .parte-nome')].map(n=>n.textContent).join('|')")=='NTFS|FAT32|ext4')
        conferir('particoes: nivel e % de confianca, "Ja esta na tabela" e "Sugerida"', p.ev("[...document.querySelectorAll('#j-particoes .parte')].map(l=>[...l.querySelectorAll('.tag')].map(t=>t.textContent).join('+')).join('|')")=='Confiança alta (95 %)+Já está na tabela|Confiança média (62 %)+Sugerida|Confiança baixa (30 %)')
        conferir('particoes: tamanho e inicio de cada uma', '500 GB · começa em 1,0 MB' in p.ev("document.querySelector('#j-particoes .parte-meta').textContent"), p.ev("document.querySelector('#j-particoes .parte-meta').textContent"))
        p.ev("document.querySelectorAll('#j-particoes .parte-ev')[1].open=true")
        conferir('particoes: XSS na evidencia aparece como texto', p.ev("document.querySelectorAll('#j-particoes .parte-ev b').length")==0 and '<b onmouseover' in p.ev("document.querySelectorAll('#j-particoes .parte-ev')[1].textContent") and p.ev('window.__xss')is None)
        conferir('particoes: aviso da candidata aparece', 'passaria do fim do disco' in p.ev("document.querySelectorAll('#j-particoes .parte')[2].textContent"))
        p.foto('1366-particoes-lista-claro')
        p.ev("document.querySelectorAll('#j-particoes .parte')[1].querySelector('.btn').click()")
        ini2 = 500 * 1024 ** 3 + 2 * 1024 ** 2
        conferir('"Ver arquivos desta particao" leva a busca com a origem e o inicio', p.espera(f"location.hash==='#disco/disco-1/recuperar/{ini2}/{50 * 1024 ** 3}'") and 'só a partição que começa em' in p.ev("document.getElementById('buscar-corpo').textContent"))
        p.foto('1366-particoes-busca-claro')
        p.clica('#b-iniciar-busca', .8)
        conferir('a busca da particao manda offset e tamanho', p.espera("location.pathname.startsWith('/jobs/')") and http(porta, '/_teste/ultimo_rec')['opcoes'] == {'modo': 'rapida', 'sistema_antes': True, 'offset': ini2, 'tamanho': 50 * 1024 ** 3}, http(porta, '/_teste/ultimo_rec'))

        # recuperar: aviso de espaco, erro de "nao cabe" e arquivos que nao gravaram
        p.tema('light')
        p.tamanho(1366, 768)
        p.ev("localStorage.setItem('painel.visao','lista')")
        p.ir(base + f'/jobs/{vid}', 1)
        p.espera("document.querySelectorAll('.tabela tbody tr').length===100")

        def destino_rec(letra):
            p.ev("(()=>{const s=document.querySelector('select[aria-label=\"Disco de destino\"]');const o=[...s.options].find(o=>o.textContent.includes('(" + letra + ")'));s.value=o.value;s.dispatchEvent(new Event('change'))})()")
        p.clica('.tabela tbody tr:nth-child(2) input[type=checkbox]')
        destino_rec('F:')
        p.ev("document.getElementById('r-recuperar').scrollIntoView({block:'center'})"); time.sleep(.5); p.clica('#r-recuperar', .8)
        conferir('recuperar: "Nao cabe" do servico aparece escrito e nada e criado', p.espera("!document.querySelector('.recuperar .alerta').hidden && document.querySelector('.recuperar .alerta').textContent.includes('Não cabe')") and p.ev("location.pathname")==f'/jobs/{vid}')
        destino_rec('C:')
        p.ev("document.getElementById('r-recuperar').scrollIntoView({block:'center'})"); time.sleep(.5); p.clica('#r-recuperar', .8)
        conferir('recuperar: aviso do servico (cabe so parte) aparece na pagina da recuperacao', p.espera("/^\\/jobs\\/j-20261104-09\\d\\d$/.test(location.pathname)&&!document.getElementById('j-aviso-rec').hidden") and 'podem falhar por falta de espaço' in p.ev("document.getElementById('j-aviso-rec').textContent"))
        p.foto('1366-recuperar-aviso-claro')
        rid2 = p.ev('location.pathname').split('/')[-1]
        import urllib.parse as up
        http(porta, f'/_teste/evento?id={rid2}&texto=' + up.quote('Não consegui gravar Fotos/IMG_0001.jpg: Sem espaço no disco'))
        http(porta, f'/_teste/evento?id={rid2}&texto=' + up.quote('Não consegui gravar Documentos/Relatorio <b>final</b>.pdf: Nome inválido'))
        http(porta, f'/_teste/evento?id={rid2}&texto=' + up.quote('2 arquivos não puderam ser gravados (veja o motivo no relatório). Peça de novo para tentar só esses.'))
        conferir('recuperar: sem falhas ainda, nada de lista de falhas', p.ev("document.getElementById('j-falhas').hidden"))
        http(porta, f'/_teste/mexer?id={rid2}&estado=concluido&progresso=1')
        conferir('recuperar: no fim, "2 arquivos nao foram gravados: veja o relatorio" e a lista com o motivo de cada um',
                 p.espera("!document.getElementById('j-falhas').hidden") and p.ev("document.getElementById('j-falhas-txt').textContent")=='2 arquivos não foram gravados: veja o relatório.'
                 and p.ev("document.querySelectorAll('#j-falhas-lista li').length")==2 and 'Sem espaço no disco' in p.ev("document.getElementById('j-falhas-lista').textContent"))
        conferir('recuperar: motivo de terceiros em texto puro', p.ev("document.querySelectorAll('#j-falhas-lista b').length")==0 and '<b>final</b>' in p.ev("document.getElementById('j-falhas-lista').textContent"))
        p.foto('1366-recuperar-falhas-claro')

        # CD/DVD: imagem liberada, e "copiar o disco e depois buscar" num passo so
        http(porta, '/_teste/dvd?on=1')
        p.ir(base + '/?f=7#volume/H', 1)
        p.espera("document.querySelector('#vol-corpo .avisos')")
        conferir('CD/DVD: botoes liberados, com o aviso de leitura lenta', p.ev("[...document.querySelectorAll('#vol-corpo .acoes .btn')].every(b=>!b.disabled)") and 'lenta e pode levar horas' in p.ev("document.querySelector('#vol-corpo .avisos').textContent") and 'próxima versão' not in p.ev("document.querySelector('#vol-corpo .avisos').textContent"))
        http(porta, '/_teste/optico_bloqueado?on=1')
        p.ir(base + '/?f=8#volume/H', 1)
        p.espera("document.querySelector('#vol-corpo .avisos')")
        conferir('CD/DVD: se o servico mandar motivos de bloqueio, botoes desabilitados e o motivo escrito', p.ev("[...document.querySelectorAll('#vol-corpo .acoes .btn')].every(b=>b.disabled)") and 'em uso por outro programa' in p.ev("document.querySelector('#vol-corpo .avisos').textContent"))
        http(porta, '/_teste/optico_bloqueado?on=0')
        p.ir(base + '/?f=9#volume/H', 1)
        p.espera("document.getElementById('b-recuperar')")
        p.clica('#b-recuperar')
        conferir('CD/DVD: "Recuperar arquivos" oferece "Copiar o disco e depois buscar"', p.espera("!!document.getElementById('b-copiar-buscar')") and not p.ev("document.getElementById('b-copiar-buscar').disabled") and 'insistindo nos trechos difíceis' in p.ev("document.getElementById('buscar-corpo').textContent"))
        p.foto('1366-optico-copiar-buscar-claro')
        p.ev("document.querySelectorAll('#buscar-corpo .opcao input')[1].click()")
        p.espera("document.querySelectorAll('#buscar-corpo .opcao input')[1].checked")
        p.clica('#b-copiar-buscar', .8)
        ult = http(porta, '/_teste/ultimo')['ultimo']
        conferir('copiar e buscar: cria primeiro o job de IMAGEM do disco, guardando a copia em outro disco',
                 p.espera("/^\\/jobs\\/j-20261104-01\\d\\d$/.test(location.pathname)") and ult['tipo'] == 'imagem' and ult['origem'] == '\\\\.\\CdRom0' and __import__('re').fullmatch(r'[A-Z]:\\Copia-H-\d{4}-\d{2}-\d{2}\.img', ult['destino']) is not None, ult)
        cid = p.ev('location.pathname').split('/')[-1]
        conferir('copiar e buscar: a pagina avisa que a busca comeca sozinha depois da copia', p.espera("!document.getElementById('j-encadeada').hidden") and 'começa sozinha (Profunda)' in p.ev("document.getElementById('j-encadeada').textContent"))
        http(porta, f'/_teste/mexer?id={cid}&estado=concluido&progresso=1')
        conferir('copia pronta: a busca dentro da copia comeca sozinha, com o modo escolhido e a lista de arquivos antes',
                 p.espera("/^\\/jobs\\/j-20261104-01\\d\\d$/.test(location.pathname)&&location.pathname!=='/jobs/" + cid + "'&&document.getElementById('j-nome').textContent.startsWith('Busca em Copia-H-')", 8)
                 and http(porta, '/_teste/ultimo_rec')['opcoes'] == {'modo': 'profunda', 'sistema_antes': True} and http(porta, '/_teste/ultimo')['ultimo']['origem'] == ult['destino'] and http(porta, '/_teste/ultimo')['ultimo']['tipo'] == 'varredura', http(porta, '/_teste/ultimo'))
        p.ir(base + f'/jobs/{cid}', 1)
        conferir('qualquer copia pronta tem o botao "Buscar arquivos nesta copia"', p.espera("!document.getElementById('j-pos-img').hidden"))
        http(porta, '/_teste/dvd?on=0')

        # medidas das telas novas
        for (w, h, tema) in ((1366, 768, 'dark'), (390, 844, 'light')):
            p.tema(tema)
            p.tamanho(w, h)
            p.ir(base + f'/jobs/{pid}', 1)
            p.espera("document.querySelectorAll('#j-particoes .parte').length===3")
            ok = p.ev("""(()=>{const d=document.documentElement,m=document.querySelector('.principal').getBoundingClientRect();
              const fora=[...document.querySelectorAll('#j-particoes .parte,#passos-rec,.cartao')].filter(e=>e.getClientRects().length).filter(e=>{const r=e.getBoundingClientRect();return r.left<m.left-.5||r.right>m.right+.5}).map(e=>e.className);
              return d.scrollWidth<=innerWidth&&fora.length===0||JSON.stringify([d.scrollWidth,innerWidth,fora])})()""")
            p.foto(f'{w}x{h}-particoes-lista-{"claro" if tema == "light" else "escuro"}')
            conferir(f'{w}x{h} {tema} (particoes): sem rolagem lateral e nada fora da area', ok is True, ok)
            p.ir(base + f'/jobs/{vid}', 1)
            p.espera("document.querySelectorAll('.tabela tbody tr,.grade .item-grade').length>0")
            p.clica('.mini')
            p.espera("document.querySelector('.previa').open")
            ok = p.ev("""(()=>{const r=document.querySelector('.previa').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight})()""")
            p.foto(f'{w}x{h}-previa-{"claro" if tema == "light" else "escuro"}')
            conferir(f'{w}x{h} {tema} (previa): a janela cabe inteira na tela', ok)
            p.tecla('Escape', 27)
            p.ir(base + '/?f=9', 1)
            p.espera("document.querySelectorAll('#i-volumes .fonte').length===7")
            p.espera("document.querySelectorAll('#i-buscas-lista .item').length>0")
            ok = p.ev("""(()=>{const d=document.documentElement,m=document.querySelector('.principal').getBoundingClientRect();return d.scrollWidth<=innerWidth&&[...document.querySelectorAll('#i-buscas-lista .item,#passos-rec')].every(e=>{const r=e.getBoundingClientRect();return r.left>=m.left-.5&&r.right<=m.right+.5})})()""")
            p.foto(f'{w}x{h}-inicio-buscas-{"claro" if tema == "light" else "escuro"}')
            conferir(f'{w}x{h} {tema} (inicio com buscas): nada fora da area', ok)
        p.tema('light')
        p.tamanho(1366, 768)

        # ---- tela de resultados mais clara: nome do disco, lista distinta, destino, "disco mudou", consultas leves
        import urllib.parse as up
        DISCO1 = chr(92) * 2 + '.' + chr(92) + 'PhysicalDrive1'
        p.tema('light')
        p.tamanho(1366, 768)
        va = http(porta, '/_teste/varredura?estado=concluido&origem=' + up.quote(DISCO1))['id']
        vb = http(porta, '/_teste/varredura?estado=falhou&modo=profunda&progresso=0&origem=' + up.quote(DISCO1) + '&erro=' + up.quote('Não consegui ler o disco. Detalhe técnico longo que não deve aparecer inteiro na lista.'))['id']
        p.ir(base + f'/jobs/{va}', 1)
        p.espera("document.querySelectorAll('.tabela tbody tr').length>0", 10)
        p.espera("document.getElementById('j-nome').textContent==='Busca em ST1000DM010-2EP102'", 8)
        conferir('origem: o titulo mostra o nome do disco, nao o caminho tecnico', p.ev("document.getElementById('j-nome').textContent")=='Busca em ST1000DM010-2EP102', (p.ev("document.getElementById('j-nome').textContent"), p.ev('JSON.stringify(window.__painel.estado())')))
        conferir('origem: o campo mostra nome, tamanho e letras, e o caminho tecnico fica so na dica (title)',
                 p.ev("(()=>{const d=document.getElementById('j-origem');return d.querySelector('.origem-nome').textContent==='ST1000DM010-2EP102'&&/931 GB/.test(d.textContent)&&d.textContent.includes('D:, G:')&&!d.textContent.includes('PhysicalDrive')&&d.title.endsWith('PhysicalDrive1')})()"))
        conferir('origem: nenhum caminho tecnico no titulo nem na lista da esquerda',
                 p.ev("![...document.querySelectorAll('#lista-itens .item-nome,#j-nome')].some(e=>e.textContent.includes('PhysicalDrive'))"))
        itens = p.ev(f"(()=>{{const o={{}};for(const id of ['{va}','{vb}']){{const a=document.querySelector('a.item[href=\"/jobs/'+id+'\"]');o[id]=a?{{sub:a.querySelector('.item-sub').textContent,mot:a.querySelector('.item-motivo').textContent,motOculto:a.querySelector('.item-motivo').hidden}}:null}}return o}})()")
        conferir('lista: cada item tem tipo e data com hora (dois da mesma origem se distinguem)',
                 itens[va] and itens[vb] and itens[va]['sub'].startswith('Busca rápida · ') and itens[vb]['sub'].startswith('Busca profunda · ') and p.ev(f"/\\d{{2}}\\/\\d{{2}}\\/\\d{{4}} \\d{{2}}:\\d{{2}}$/.test(document.querySelector('a.item[href=\"/jobs/{va}\"] .item-sub').textContent)"), itens)
        conferir('lista: o que falhou mostra o motivo em uma linha; o que deu certo nao',
                 itens[vb]['mot']=='Não consegui ler o disco.' and itens[va]['motOculto'], itens)
        p.foto('1366-resultados-origem-claro')
        # destino: nunca o disco da origem, nunca a pasta do programa
        p.espera("document.querySelector('select[aria-label=\"Disco de destino\"]').options.length>0", 8)
        opcoes = p.ev("[...document.querySelector('select[aria-label=\"Disco de destino\"]').options].map(o=>o.textContent)")
        conferir('destino: disco inteiro como origem nao oferece as letras dele (D: e G:)', opcoes and not any('(D:)' in o or '(G:)' in o for o in opcoes), opcoes)
        conferir('destino: pasta padrao e "Recuperados", sem pasta do programa',
                 p.ev("document.querySelector('input[aria-label=\"Nome da pasta de destino\"]').value")=='Recuperados' and not p.ev("/uv-recuperacao|Desktop/i.test(document.querySelector('.recuperar').textContent)"))
        conferir('destino: o campo "Pasta" ocupa o resto da linha do cartao',
                 p.ev("(()=>{const i=document.querySelector('input[aria-label=\"Nome da pasta de destino\"]').getBoundingClientRect();const r=document.querySelector('.rec-dest').getBoundingClientRect();return i.width>216&&Math.abs(i.right-r.right)<=1})()"))
        # sem outro disco: campo livre vazio, com explicacao, largura total
        http(porta, '/_teste/fontes_vazias?on=1')
        p.ir(base + f'/jobs/{va}', 1)
        p.espera("document.querySelectorAll('.tabela tbody tr').length>0", 10)
        conferir('sem outro disco: campo vazio (nao inventa pasta), com explicacao em portugues claro',
                 p.ev("(()=>{const i=document.querySelector('input[aria-label=\"Pasta de destino\"]');return i.value===''&&!i.parentElement.hidden&&i.placeholder.startsWith('Ex.')&&/Não achei outro disco/.test(document.querySelector('.recuperar').textContent)})()"))
        conferir('sem outro disco: o campo livre ocupa a largura do cartao',
                 p.ev("(()=>{const i=document.querySelector('input[aria-label=\"Pasta de destino\"]').getBoundingClientRect();const r=document.querySelector('.rec-dest').getBoundingClientRect();return Math.abs(i.right-r.right)<=1&&Math.abs(i.left-r.left)<=1})()"))
        http(porta, '/_teste/fontes_vazias?on=0')
        # origem mudou (409): botao para refazer, sem "Recuperar" ativo, aviso perto do botao
        http(porta, '/_teste/origem_mudou?on=1')
        p.ir(base + f'/jobs/{va}', 1)
        p.espera("document.querySelectorAll('.tabela tbody tr').length>0", 10)
        p.clica('.tabela tbody tr:nth-child(1) input[type=checkbox]')
        p.clica('#r-ir-recuperar', .8)
        p.ev("document.getElementById('r-recuperar').scrollIntoView({block:'center'})"); time.sleep(.5); p.clica('#r-recuperar', .8)
        conferir('409: aparece "Buscar de novo neste disco" e some o "Recuperar selecionados"',
                 p.espera("!document.getElementById('r-refazer').hidden", 6) and p.ev("document.getElementById('r-refazer').textContent")=='Buscar de novo neste disco' and p.ev("document.getElementById('r-recuperar').hidden"))
        conferir('409: aviso em tom calmo (nao vermelho de perigo), em portugues claro, logo acima do botao',
                 p.ev("(()=>{const a=document.querySelector('.recuperar .alerta');const b=document.getElementById('r-refazer').getBoundingClientRect();const r=a.getBoundingClientRect();return a.dataset.tipo==='aviso'&&!a.classList.contains('alerta-perigo')&&/não está igual/.test(a.textContent)&&r.bottom<=b.top&&b.top-r.bottom<=24})()"))
        conferir('409: com o disco mudado some o bloco de destino: fica so o aviso e o botao',
                 p.ev("['.rec-dest','.rec-opcoes','.rec-resumo','.aviso-destino'].every(q=>{const e=document.querySelector('.recuperar '+q);return !e||e.getBoundingClientRect().height===0})"))
        VISIVEL = "(()=>{const f=(id)=>{const r=document.getElementById(id).getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight};return f('r-refazer')&&(()=>{const r=document.querySelector('.recuperar .alerta').getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight})()})()"
        conferir('409: o aviso e o botao aparecem inteiros na tela, sem precisar rolar', p.espera(VISIVEL, 3), p.ev("JSON.stringify([scrollY,innerHeight,document.getElementById('r-refazer').getBoundingClientRect().bottom])"))
        p.foto('1366-resultados-disco-mudou-claro')
        p.tema('dark')
        time.sleep(.6)
        conferir('tema escuro: os cartoes da lista da esquerda tem fundo escuro (letra clara legivel)',
                 p.ev("(()=>{const c=getComputedStyle(document.querySelector('#lista-itens .item')).backgroundColor.match(/[0-9.]+/g).map(Number);return c[0]+c[1]+c[2]<330})()"), p.ev("getComputedStyle(document.querySelector('#lista-itens .item')).backgroundColor"))
        p.foto('1366-resultados-disco-mudou-escuro')
        p.tema('light')
        p.tamanho(390, 844)
        p.ev("localStorage.removeItem('painel.arvore')")
        p.ir(base + f'/jobs/{va}', 1)
        p.espera("document.querySelectorAll('.tabela tbody tr').length>0", 10)
        conferir('celular: as pastas comecam recolhidas e nao ha rolagem dentro de rolagem',
                 p.ev("document.querySelector('.arvore').getBoundingClientRect().width===0||document.querySelector('.arvore').hidden||document.querySelector('.res-corpo').classList.contains('arvore-recolhida')") and p.ev("(()=>{const t=document.querySelector('.tabela-box');return t.scrollHeight<=t.clientHeight+1})()"))
        p.clica('.tabela tbody tr:nth-child(1) input[type=checkbox]')
        conferir('celular: com algo marcado aparece a barra "Recuperar…" colada no rodapé da tela',
                 p.espera("!document.querySelector('.sel-fixa').hidden", 4) and p.ev("(()=>{const r=document.querySelector('.sel-fixa').getBoundingClientRect();return r.bottom<=innerHeight+1&&r.bottom>=innerHeight-24&&r.left>=0&&r.right<=innerWidth})()"))
        p.foto('390-resultados-barra-fixa-claro')
        p.clica('#r-ir-recuperar', .8)
        p.ev("document.getElementById('r-recuperar').scrollIntoView({block:'center'})"); time.sleep(.5); p.clica('#r-recuperar', .8)
        conferir('celular, 409: o aviso e o botao aparecem inteiros na tela, sem rolar',
                 p.espera("!document.getElementById('r-refazer').hidden", 4) and p.espera(VISIVEL, 3) and p.ev("document.documentElement.scrollWidth<=innerWidth"))
        p.foto('390-resultados-disco-mudou-claro')
        p.tema('dark')
        time.sleep(.6)
        p.foto('390-resultados-disco-mudou-escuro')
        p.tema('light')
        p.tamanho(1366, 768)
        time.sleep(.5)
        p.ev("document.getElementById('r-refazer').scrollIntoView({block:'center'})")
        http(porta, '/_teste/origem_mudou?on=0')
        p.clica('#r-refazer', .8)
        conferir('409: o botao cria a mesma busca (mesmo disco, mesmo modo) e abre a nova',
                 p.espera(f"/^\\/jobs\\/j-/.test(location.pathname)&&location.pathname!=='/jobs/{va}'", 8) and http(porta, '/_teste/ultimo_rec')['opcoes'].get('modo')=='rapida', http(porta, '/_teste/ultimo_rec'))

        # ---- estado com tres selos, larguras 1280/1500/1920 e celular escuro com barra fixa
        id3 = http(porta, '/_teste/tres_selos?on=1')['id']
        CONTRASTE = """(()=>{const lum=(c)=>{const f=(v)=>{v/=255;return v<=.03928?v/12.92:Math.pow((v+.055)/1.055,2.4)};return .2126*f(c[0])+.7152*f(c[1])+.0722*f(c[2])};
          const cor=(s)=>s.match(/[0-9.]+/g).map(Number);
          const fundo=(e)=>{for(;e;e=e.parentElement){const c=cor(getComputedStyle(e).backgroundColor);if(c.length<4||c[3]>.95)return c}return [255,255,255]};
          const cr=(e)=>{const a=lum(cor(getComputedStyle(e).color)),b=lum(fundo(e));return (Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
          return Math.min(...SEL.map(q=>cr(document.querySelector(q))))})()"""
        NOME_TEMA = {'light': 'claro', 'dark': 'escuro'}
        NAO_COBRE = "(()=>{const b=document.getElementById('r-recuperar').getBoundingClientRect();const f=document.querySelector('.sel-fixa');if(f.hidden)return true;const r=f.getBoundingClientRect();return r.height===0||r.top>=b.bottom||r.bottom<=b.top||r.left>=b.right||r.right<=b.left})()"
        for (w, h, tema) in ((1280, 720, 'light'), (1280, 720, 'dark'), (1500, 900, 'light'), (1500, 900, 'dark'), (1920, 1080, 'light'), (1920, 1080, 'dark')):
            nome = f'{w}x{h} {tema}'
            p.tema(tema)
            p.tamanho(w, h)
            p.ir(base + f'/jobs/{va}', 1)
            p.espera("document.querySelectorAll('.tabela tbody tr').length>0", 10)
            p.clica(f'.tabela tbody tr[data-linha="{id3}"] input[type=checkbox]')
            p.espera("!document.querySelector('.sel-fixa').hidden", 4)
            time.sleep(.4)
            alt = p.ev("[...document.querySelectorAll('.tabela tbody tr')].map(t=>Math.round(t.getBoundingClientRect().height))")
            conferir(f'{nome}: todas as linhas da lista tem a mesma altura', max(alt) - min(alt) <= 1, sorted(set(alt)))
            conferir(f'{nome}: a linha com tres selos (Ruim, Apagado, Recuperado) mostra os tres numa linha so',
                     p.ev(f"(()=>{{const t=[...document.querySelectorAll('.tabela tbody tr[data-linha=\"{id3}\"] .estados > *')];const td=document.querySelector('.tabela tbody tr[data-linha=\"{id3}\"] .col-estado').getBoundingClientRect();return t.length===3&&new Set(t.map(e=>{{const r=e.getBoundingClientRect();return Math.round(r.top+r.height/2)}})).size===1&&t.every(e=>e.getBoundingClientRect().right<=td.right+0.5)}})()"))
            conferir(f'{nome}: nada passa da largura da janela e nao ha rolagem horizontal',
                     p.ev("(()=>{const W=document.documentElement.clientWidth;const f=[...document.querySelectorAll('body *')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&(r.right>W+1||r.left<-1)&&!e.closest('.tabela-box,.abas,.sr-only')});return document.documentElement.scrollWidth<=W&&document.body.scrollWidth<=W&&f.length===0})()"), p.ev("document.documentElement.scrollWidth+' de '+document.documentElement.clientWidth"))
            conferir(f'{nome}: a tabela cabe sem rolagem horizontal',
                     p.ev("(()=>{const t=document.querySelector('.tabela-box');return t.scrollWidth<=t.clientWidth+1})()"))
            conferir(f'{nome}: botoes, selos e cabecalhos nao tem texto cortado',
                     p.ev("[...document.querySelectorAll('.btn,.tag,.recuperado,.tabela th,.sel-fixa .btn')].filter(e=>e.getBoundingClientRect().width>0&&e.scrollWidth>e.clientWidth+1&&getComputedStyle(e).textOverflow!=='ellipsis').map(e=>e.textContent.trim()).join('|')")=='')
            conferir(f'{nome}: colunas da tabela sem espremer (nome >=140, tamanho >=70, data >=80)',
                     p.ev("(()=>{const r=(q)=>document.querySelector('.tabela tbody tr '+q).getBoundingClientRect().width;return r('.col-nome')>=140&&r('.col-tamanho')>=70&&r('.col-data')>=80})()"))
            p.foto(f'{w}x{h}-resultados-barra-fixa-{NOME_TEMA[tema]}')
            p.ev("document.getElementById('r-recuperar').scrollIntoView({block:'center'})"); time.sleep(.5)
            conferir(f'{nome}: com o cartao a vista a barra fixa nao cobre o botao Recuperar', p.ev(NAO_COBRE))
            p.foto(f'{w}x{h}-resultados-cartao-{NOME_TEMA[tema]}')
        for tema in ('light', 'dark'):
            p.tema(tema)
            p.tamanho(390, 844)
            p.ir(base + f'/jobs/{va}', 1)
            p.espera("document.querySelectorAll('.tabela tbody tr').length>0", 10)
            p.clica(f'.tabela tbody tr[data-linha="{id3}"] input[type=checkbox]')
            p.espera("!document.querySelector('.sel-fixa').hidden", 4)
            time.sleep(.4)
            alt = p.ev("[...document.querySelectorAll('.tabela tbody tr')].map(t=>Math.round(t.getBoundingClientRect().height))")
            conferir(f'390 {tema}: todas as linhas da lista tem a mesma altura', max(alt) - min(alt) <= 1, sorted(set(alt)))
            conferir(f'390 {tema}: nada passa da largura da janela', p.ev("document.documentElement.scrollWidth<=document.documentElement.clientWidth"))
            c = p.ev(CONTRASTE.replace('SEL', "['.sel-fixa-txt','.sel-fixa .btn-primario']"))
            conferir(f'390 {tema}: texto e botao da barra fixa tem contraste >= 4,5', c >= 4.5, c)
            p.foto(f'390-resultados-barra-fixa-{NOME_TEMA[tema]}-3selos')
            p.clica('#r-ir-recuperar', .8)
            p.ev("document.getElementById('r-recuperar').scrollIntoView({block:'center'})"); time.sleep(.5)
            conferir(f'390 {tema}: a barra fixa nao cobre o botao Recuperar do cartao', p.ev(NAO_COBRE))
            c = p.ev(CONTRASTE.replace('SEL', "['#r-recuperar']"))
            conferir(f'390 {tema}: o botao Recuperar esta inteiro na tela e o texto dele tem contraste >= 4,5',
                     p.ev("(()=>{const b=document.getElementById('r-recuperar').getBoundingClientRect();return b.top>=0&&b.bottom<=innerHeight})()") and c >= 4.5, c)
            p.foto(f'390-resultados-cartao-{NOME_TEMA[tema]}')
        http(porta, '/_teste/tres_selos?on=0')
        p.tema('light')
        p.tamanho(1366, 768)

        # destino vindo do servico (/api/destinos): ja preenchido, botoes de escolha rapida, aviso e sugestao
        CAMPO = "document.querySelector('input[aria-label=\"Pasta de destino\"]')"
        http(porta, '/_teste/destinos?on=1')
        p.ir(base + f'/jobs/{va}', 1)
        p.espera("document.querySelectorAll('.tabela tbody tr').length>0", 10)
        conferir('destino do servico: o campo ja vem preenchido com o sugerido (mais espaco, fora do disco da origem)',
                 p.espera(CAMPO + ".value==='C:\\\\Recuperados'", 6), p.ev(CAMPO + '.value'))
        ops = p.ev("[...document.querySelectorAll('.rec-opcoes .btn')].map(b=>b.textContent)")
        conferir('destino do servico: botoes de escolha rapida com nome do disco e espaco livre, sem as letras do disco da origem',
                 ops == ['Disco C: · 120 GB livres', 'Disco F: · 41 GB livres', 'Disco E: · 1,0 KB livres'], ops)
        conferir('destino do servico: o escolhido fica marcado e o campo ocupa a largura do cartao',
                 p.ev("document.querySelector('.rec-opcoes .btn').getAttribute('aria-pressed')")=='true' and p.ev("(()=>{const i=" + CAMPO + ".getBoundingClientRect();const r=document.querySelector('.rec-dest').getBoundingClientRect();return Math.abs(i.right-r.right)<=1&&Math.abs(i.left-r.left)<=1})()"))
        p.foto('1366-resultados-destino-claro')
        p.ev("[...document.querySelectorAll('.rec-opcoes .btn')].find(b=>b.textContent.startsWith('Disco F:')).click()")
        conferir('destino do servico: clicar num botao troca o campo', p.ev(CAMPO + '.value')=='F:\\Recuperados')
        p.clica('.tabela tbody tr:nth-child(1) input[type=checkbox]')
        p.ev('scrollTo(0,0)')
        time.sleep(.4)
        conferir('barra fixa: com algo marcado mostra "1 selecionado · tamanho" colada no rodape da tela',
                 p.espera("!document.querySelector('.sel-fixa').hidden", 4) and p.ev("document.querySelector('.sel-fixa-txt').textContent").startswith('1 selecionado · ') and p.ev("(()=>{const r=document.querySelector('.sel-fixa').getBoundingClientRect();return r.bottom<=innerHeight+1&&r.bottom>=innerHeight-24})()"), p.ev("document.querySelector('.sel-fixa').outerHTML.length"))
        conferir('barra fixa: a lista ganha no fim um espaco da altura da barra, para ela nao cobrir a ultima linha',
                 p.ev("parseFloat(getComputedStyle(document.querySelector('.res-area')).paddingBottom)>=document.querySelector('.sel-fixa').getBoundingClientRect().height"))
        p.foto('1366-resultados-barra-fixa-claro')
        p.clica('#r-ir-recuperar', .8)
        conferir('barra fixa: "Recuperar…" leva ao cartao e a barra some enquanto o cartao esta a vista',
                 p.espera("document.querySelector('.sel-fixa').hidden", 4) and p.ev("(()=>{const r=document.querySelector('.recuperar').getBoundingClientRect();return r.top<innerHeight&&r.bottom>0})()"))
        p.ev("[...document.querySelectorAll('.rec-opcoes .btn')].find(b=>b.textContent.startsWith('Disco E:')).click()")
        conferir('"Nao cabe": diz quanto falta e qual disco escolher',
                 p.espera("/^Faltam .*Escolha o Disco C: ou outro com mais espaço\\./.test(document.querySelector('.recuperar .alerta').textContent)", 3), p.ev("document.querySelector('.recuperar .alerta').textContent"))
        conferir('destino do servico: o escolhido tem marca de selecionado (visto) e borda forte',
                 p.ev("(()=>{const b=document.querySelector('.rec-opcoes .btn[aria-pressed=true]');const s=getComputedStyle(b,'::before').content;return b.textContent.startsWith('Disco E:')&&s.includes('\u2713')&&getComputedStyle(b).boxShadow!=='none'})()"))
        p.foto('1366-resultados-nao-cabe-claro')
        p.ev("[...document.querySelectorAll('.rec-opcoes .btn')].find(b=>b.textContent.startsWith('Disco F:')).click()")
        p.ev("document.getElementById('r-recuperar').scrollIntoView({block:'center'})"); time.sleep(.5); p.clica('#r-recuperar', .8)
        conferir('destino recusado com sugestao: frase curta em tom calmo e botao "Usar C:\\Recuperados"',
                 p.espera("!document.getElementById('r-usar-sugestao').hidden", 6) and p.ev("document.getElementById('r-usar-sugestao').textContent")=='Usar C:\\Recuperados' and p.ev("document.querySelector('.recuperar .alerta').dataset.tipo")=='aviso')
        conferir('destino recusado: a frase diz o quanto nao cabe e o que fazer (nao so "nao cabe")',
                 p.ev("(()=>{const t=document.querySelector('.recuperar .alerta').textContent;return /^Não cabe em F:.*marcados passam do espaço livre[.] Use C:.*marque menos arquivos/.test(t)||/^Faltam/.test(t)})()"), p.ev("document.querySelector('.recuperar .alerta').textContent"))
        conferir('destino recusado: so o "Usar ..." fica em destaque; "Recuperar selecionados" vira secundario',
                 p.ev("document.getElementById('r-usar-sugestao').classList.contains('btn-primario')&&!document.getElementById('r-recuperar').classList.contains('btn-primario')"))
        p.foto('1366-resultados-sugestao-claro')
        p.clica('#r-usar-sugestao', .8)
        conferir('"Usar ...": troca o destino e tenta de novo (abre a recuperacao)',
                 p.espera("/^\\/jobs\\/j-/.test(location.pathname)&&location.pathname!=='/jobs/" + va + "'", 8) and http(porta, '/_teste/ultimo_rec')['rec']['destino'] == 'C:\\Recuperados', http(porta, '/_teste/ultimo_rec'))
        http(porta, '/_teste/destinos?on=1&sem=1')
        p.ir(base + f'/jobs/{va}', 1)
        p.espera("document.querySelectorAll('.tabela tbody tr').length>0", 10)
        conferir('sem outro disco: o aviso aparece no lugar do campo, com "Procurar de novo"',
                 p.espera("!document.querySelector('.aviso-destino').hidden", 6) and p.ev("document.querySelector('.aviso-destino').textContent.includes('Ligue um pendrive ou HD externo')") and p.ev(CAMPO + ".parentElement.hidden") and p.ev("document.getElementById('r-procurar').textContent")=='Procurar de novo' and p.ev("document.getElementById('r-recuperar').disabled"))
        p.ev("document.querySelector('.recuperar').scrollIntoView({block:'center'})")
        time.sleep(.4)
        p.foto('1366-resultados-sem-disco-claro')
        p.tema('dark')
        time.sleep(.5)
        p.foto('1366-resultados-sem-disco-escuro')
        p.tema('light')
        http(porta, '/_teste/destinos?on=1&sem=0')
        p.clica('#r-procurar', .8)
        conferir('"Procurar de novo": ao achar um disco, o campo se preenche', p.espera(CAMPO + ".value==='C:\\\\Recuperados'", 6), p.ev(CAMPO + '.value'))
        http(porta, '/_teste/destinos?on=0')

        # consultas leves: aba oculta para tudo; discos so a cada 30 s; miniaturas so as que aparecem
        codigo_r = (AQUI.parent / 'recuperacao' / 'painel' / 'resultados.js').read_text(encoding='utf-8')
        codigo_p = (AQUI.parent / 'recuperacao' / 'painel' / 'painel.js').read_text(encoding='utf-8')
        conferir('discos: intervalo minimo de 30 s entre consultas, sem relogio que consulte', 'FONTES_MS = 30000' in codigo_p and '> 5000' not in codigo_p)
        conferir('miniaturas: fila de no maximo 4 ao mesmo tempo, so as que aparecem na tela', 'MAX_MINI = 4' in codigo_r and 'IntersectionObserver' in codigo_r)
        p.ir(base + f'/jobs/{va}', 1)
        p.espera("document.querySelectorAll('.tabela tbody tr').length>0", 10)
        time.sleep(1)
        ims = p.ev("(()=>{const t=[...document.querySelectorAll('.tabela img')];const c=t.filter(i=>i.getAttribute('src'));return {tot:t.length,com:c.length,foraDaVista:c.filter(i=>{const r=i.getBoundingClientRect();return r.top>innerHeight+200||r.bottom<-200}).length,esperando:t.length-c.length}})()")
        conferir('miniaturas: so as da vista carregam (as outras esperam)', ims['tot'] >= 3 and ims['com'] > 0 and ims['foraDaVista'] == 0 and ims['esperando'] > 0, ims)
        antes = http(porta, '/_teste/contadores')
        p.ev("Object.defineProperty(document,'hidden',{get:()=>true,configurable:true});document.dispatchEvent(new Event('visibilitychange'))")
        conferir('aba oculta: o painel fecha a conexao ao vivo e para as consultas', p.espera("window.__painel.estado().abaOculta===true&&window.__painel.estado().sse===false&&window.__painel.estado().emConsulta===false", 4), p.ev('JSON.stringify(window.__painel.estado())'))
        time.sleep(3.5)
        meio = http(porta, '/_teste/contadores')
        conferir('aba oculta: nenhum pedido de jobs, discos ou achados enquanto escondida', meio['consultas'] == antes['consultas'] and meio['fontes'] == antes['fontes'], (antes, meio))
        p.ev("Object.defineProperty(document,'hidden',{get:()=>false,configurable:true});document.dispatchEvent(new Event('visibilitychange'))")
        conferir('aba de volta: reconecta sozinha', p.espera("window.__painel.estado().abaOculta===false&&window.__painel.estado().sse===true", 6), p.ev('JSON.stringify(window.__painel.estado())'))
        depois = http(porta, '/_teste/contadores')
        conferir('aba de volta em menos de 30 s: nao pergunta os discos de novo (o Windows e lento)', depois['fontes'] == antes['fontes'], (antes, depois))

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
