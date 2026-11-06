# -*- coding: utf-8 -*-
"""Serviço local da Recuperação de Dados: app Flask, rotas, SSE e ciclo de vida.

    python -m recuperacao.servico      (de dentro de recuperacao-dados; normalmente quem sobe é o iniciar.py)

Só escuta 127.0.0.1. Toda rota /api/* (menos /api/saude) exige o cookie `recdados` ou o cabeçalho
X-RecDados-Token. O trabalho mora aqui, em jobs gravados em disco; a página é só painel.
"""
import ctypes
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, Response, abort, jsonify, make_response, redirect, request, send_from_directory
from werkzeug.serving import ThreadedWSGIServer

from . import config, fontes, identidade, leitor, unidade
from .executor import ErroAcao, Executor
from .jobs import DestinoEmUso, JobStore
from .tipos import imagem
from . import rotas_achados, rotas_arvore, rotas_particoes
from .tipos import recuperar as tipo_recuperar
from .tipos import varredura as tipo_varredura
from .tipos import particoes as tipo_particoes

TIPOS = {'imagem': imagem}
TIPOS['particoes'] = tipo_particoes
TIPOS['varredura'] = tipo_varredura
PAINEL = Path(__file__).resolve().parent / 'painel'
COOKIE = 'recdados'
ID_JOB = re.compile(r'j-\d{8}-[0-9a-f]{4}')
PING_S = 15
CAMINHO_INICIAR = Path(__file__).resolve().parent.parent / 'iniciar.py'
SW_SHOWNORMAL = 1
TICKET_S = 60                # o ?t= da URL vale 60 s e uma vez só
SESSAO_S = 12 * 3600         # o cookie vale 12 h
MAX_TICKETS = 100
MAX_SESSOES = 200
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'"
ENVIO_MIN_S = 0.5            # no máximo 2 eventos por segundo, por job, em cada cliente SSE

PAGINA_SEM_COOKIE = ('<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Recuperação de Dados</title>'
                     '<body style="font-family:sans-serif;margin:3rem"><h1>Abra pelo atalho do programa</h1>')
PAGINA_EM_CONSTRUCAO = ('<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Recuperação de Dados</title>'
                        '<body style="font-family:sans-serif;margin:3rem"><h1>Painel em construção</h1>')


class ServidorExclusivo(ThreadedWSGIServer):
    """No Windows o SO_REUSEADDR deixa uma 2ª cópia ligar na mesma porta; aqui o bind é exclusivo (como no Mapa)."""
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


FONTES_TTL_S = 5
PRIORIDADE_ABAIXO_DO_NORMAL = 0x00004000        # BELOW_NORMAL_PRIORITY_CLASS
PROCESSO_MODO_SEGUNDO_PLANO = 0x00100000        # PROCESS_MODE_BACKGROUND_BEGIN


class Contexto:
    """Tudo o que o serviço tem de vivo: banco, executor, token. O app Flask só guarda uma referência."""

    def __init__(self, provedor_fontes=None):
        config.garantir_pastas()
        self.porta = config.porta()
        self.token = config.obter_token()
        self.store = JobStore(config.caminho_db())
        self.executor = Executor(self.store, {**TIPOS, 'recuperar': tipo_recuperar})   # `recuperar` nasce só pela rota própria
        self.elevador = pedir_elevacao                   # injetável nos testes (ShellExecute falso)
        self.eh_windows = os.name == 'nt'
        self.provedor_fontes = provedor_fontes          # injetável nos testes; None = discos de verdade (só metadados)
        self._fontes_cache = None                       # (instante, lista): o PowerShell custa caro e o painel pergunta toda hora
        self._trava_fontes = threading.Lock()
        self.parar = threading.Event()
        self.servidor = None
        self.relogio = time.monotonic          # os testes trocam por um relógio falso
        self._trava_auth = threading.Lock()
        self._tickets = {}                     # ticket -> quando expira
        self._sessoes = {}                     # valor do cookie -> quando expira (só em memória: reiniciar o serviço exige reabrir pelo atalho)
        self._trava_desligar = threading.Lock()
        self._desligado = False

    def fontes(self, novo=False):
        """Lista de fontes com validade de FONTES_TTL_S; `novo` força reler (botão Atualizar)."""
        with self._trava_fontes:
            agora = self.relogio()
            if not novo and self._fontes_cache and agora - self._fontes_cache[0] < FONTES_TTL_S:
                return self._fontes_cache[1]
            lista = fontes.listar(self.provedor_fontes)
            self._fontes_cache = (agora, lista)
            return lista

    def _limpar(self, tabela, agora, limite):
        for k in [k for k, fim in tabela.items() if fim <= agora]:
            del tabela[k]
        while len(tabela) >= limite:
            del tabela[min(tabela, key=tabela.get)]

    def criar_ticket(self):
        """Ticket de uso único para o ?t= da URL. Quem o pede prova que lê a pasta de dados (token do arquivo)."""
        ticket = secrets.token_urlsafe(24)
        with self._trava_auth:
            agora = self.relogio()
            self._limpar(self._tickets, agora, MAX_TICKETS)
            self._tickets[ticket] = agora + TICKET_S
        return ticket

    def trocar_ticket(self, ticket):
        """Gasta o ticket e devolve o valor do cookie de sessão; None se não vale (inexistente, vencido ou já usado)."""
        with self._trava_auth:
            agora = self.relogio()
            fim = self._tickets.pop(ticket, None) if isinstance(ticket, str) else None
            if fim is None or fim <= agora:
                return None
            self._limpar(self._sessoes, agora, MAX_SESSOES)
            sessao = secrets.token_urlsafe(32)
            self._sessoes[sessao] = agora + SESSAO_S
            return sessao

    def sessao_valida(self, valor):
        if not isinstance(valor, str) or not valor:
            return False
        with self._trava_auth:
            fim = self._sessoes.get(valor)
            if fim is None:
                return False
            if fim <= self.relogio():
                del self._sessoes[valor]
                return False
            return True

    def iniciar(self):
        """Só depois de ter a porta: uma 2ª cópia que não conseguiu ligar não pode mexer nos jobs da 1ª."""
        self.store.marcar_interrompidos()
        self.executor.iniciar()

    def desligar(self):
        """Encerramento limpo: pausa tudo, espera os checkpoints (até 10 s) e para o servidor. Pode ser chamado de novo."""
        with self._trava_desligar:
            if self._desligado:
                return
            self._desligado = True
        self.executor.encerrar(10.0)
        self.parar.set()
        if self.servidor is not None:
            self.servidor.shutdown()


def pedir_elevacao(arquivo, parametros, pasta):
    """Pede ao Windows para abrir o programa como administrador (o próprio Windows mostra a confirmação do UAC).
    Devolve o código do ShellExecute: maior que 32 = pedido aceito; 5 (ou menos) = recusado/cancelado."""
    exe = sys.executable
    janela = os.path.join(os.path.dirname(exe), 'pythonw.exe')
    return int(ctypes.windll.shell32.ShellExecuteW(None, 'runas', janela if os.path.isfile(janela) else exe,
                                                   '"%s" %s' % (arquivo, parametros), pasta, SW_SHOWNORMAL))


def _erro(mensagem, codigo):
    return jsonify({'erro': mensagem}), codigo


def _evento(nome, dados):
    return 'event: %s\ndata: %s\n\n' % (nome, json.dumps(dados, ensure_ascii=False))


AVISO_PASTA_DE_DADOS = ('ATENÇÃO: a pasta de dados deste programa (banco de trabalhos e logs) está no mesmo disco que você vai '
                        'recuperar. Tudo o que o programa grava ali pode sobrescrever justamente o que você quer salvar. Feche o '
                        'programa e abra de novo com RECDADOS_DADOS apontando para outro disco, ou recupere só depois de mudar.')


def aviso_pasta_de_dados(origem):
    """Frase de alerta se `origem` é um disco/volume e a pasta de dados do serviço está nesse mesmo disco; senão None.
    É só aviso: o trabalho segue (quem sabe o que faz pode seguir; a recusa seria pior que o risco para quem não tem outro disco)."""
    if not leitor.eh_dispositivo(origem):
        return None
    try:
        config.garantir_pastas()
        mesma = unidade.conferir(origem, config.caminho_db()) == unidade.MSG_MESMO_DISCO
    except OSError:
        return None
    return AVISO_PASTA_DE_DADOS if mesma else None


def criar_app(ctx=None):
    ctx = ctx or Contexto()
    app = Flask(__name__, static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = 64 * 1024
    app.json.ensure_ascii = False
    app.json.sort_keys = False
    app.extensions['recdados'] = ctx
    store, executor = ctx.store, ctx.executor
    hosts = {'127.0.0.1:%d' % ctx.porta, 'localhost:%d' % ctx.porta}
    origens = {'http://' + h for h in hosts}

    def token_valido(valor):
        return bool(valor) and hmac.compare_digest(str(valor).encode(), ctx.token.encode())

    def tem_cookie():
        return ctx.sessao_valida(request.cookies.get(COOKIE))

    @app.before_request
    def proteger():
        if request.host not in hosts:
            return _erro('Endereço não permitido.', 403)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origem = request.headers.get('Origin')
            if origem is not None and origem not in origens:
                return _erro('Pedido de outro site recusado.', 403)
        if request.path.startswith('/api/') and request.path != '/api/saude':
            if not (tem_cookie() or token_valido(request.headers.get('X-RecDados-Token'))):
                return _erro('abra pelo atalho do programa', 401)
        return None

    @app.after_request
    def cabecalhos(resp):
        resp.headers['Cache-Control'] = 'no-store'
        resp.headers['Referrer-Policy'] = 'no-referrer'
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        resp.headers['X-Frame-Options'] = 'DENY'
        resp.headers['Content-Security-Policy'] = CSP
        return resp

    @app.teardown_request
    def fechar_conexao(_):
        store.fechar_conexao_da_thread()      # thread de requisição some depois; a conexão dela não pode ficar aberta

    @app.errorhandler(404)
    def _404(_):
        return _erro('Não encontrado.', 404)

    @app.errorhandler(405)
    def _405(_):
        return _erro('Método não permitido.', 405)

    @app.errorhandler(413)
    def _413(_):
        return _erro('Pedido grande demais.', 413)

    @app.errorhandler(Exception)
    def _500(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return _erro(e.description or 'Erro.', e.code or 500)
        logging.getLogger('recdados').exception('erro numa rota')
        return _erro('Erro interno do serviço. Veja o registro em logs/.', 500)

    # --- API -------------------------------------------------------------
    @app.get('/api/saude')
    def saude():
        dados = {'ok': True, 'versao': config.VERSAO, 'pid': os.getpid(), 'admin': leitor.eh_administrador()}
        desafio = request.args.get('desafio')
        if desafio is not None and 0 < len(desafio) <= 100:
            # prova de identidade: só quem tem o token do arquivo calcula isto; o iniciar confere antes de entregar o token
            dados['prova'] = hmac.new(ctx.token.encode(), desafio.encode(), hashlib.sha256).hexdigest()
        return jsonify(dados)

    @app.post('/api/ticket')
    def ticket():
        if not token_valido(request.headers.get('X-RecDados-Token')):
            return _erro('abra pelo atalho do programa', 401)       # o cookie de sessão não gera ticket
        return jsonify({'ticket': ctx.criar_ticket(), 'expira_em': TICKET_S})

    @app.get('/api/fontes')
    def listar_fontes():
        return jsonify(ctx.fontes(novo=request.args.get('novo') == '1'))

    @app.post('/api/elevar')
    def elevar():
        if not ctx.eh_windows:
            return _erro('Só o Windows tem o pedido de administrador. Neste sistema, abra o programa com sudo (root).', 409)
        if leitor.eh_administrador():
            return _erro('O programa já está rodando como administrador.', 409)
        codigo = ctx.elevador(str(CAMINHO_INICIAR), '--admin', str(CAMINHO_INICIAR.parent))
        if codigo <= 32:
            return _erro('O Windows não abriu o programa como administrador (a confirmação foi recusada ou cancelada).', 409)
        return jsonify({'ok': True, 'mensagem': 'Confirme a janela do Windows. O programa reabre como administrador.'}), 202

    @app.get('/api/marca')
    def marca():
        return jsonify(config.marca())

    @app.get('/api/jobs')
    def listar():
        return jsonify(store.listar())

    @app.post('/api/jobs')
    def criar():
        dados = request.get_json(silent=True, force=True)
        if not isinstance(dados, dict):
            return _erro('Pedido inválido: mande um JSON com tipo, origem e destino.', 400)
        tipo = dados.get('tipo')
        modulo = TIPOS.get(tipo) if isinstance(tipo, str) else None
        if modulo is None:
            return _erro('Tipo de trabalho desconhecido. Tipos disponíveis: %s.' % ', '.join(TIPOS), 400)
        origem, destino, opcoes = dados.get('origem'), dados.get('destino'), dados.get('opcoes') or {}
        if hasattr(modulo, 'destino_de'):               # tipo sem arquivo de saída (ex.: varredura) tem destino próprio
            destino = modulo.destino_de(origem, destino)
        em_uso = 'Já existe um trabalho em andamento para esse destino.'
        if isinstance(destino, str) and destino.strip() and chr(0) not in destino and store.job_com_destino(destino):
            return _erro(em_uso, 409)
        erro = modulo.validar(origem, destino, opcoes)
        if erro:
            return _erro(erro, 400)
        if tipo in ('imagem', 'varredura'):
            impressao = identidade.impressao(origem)          # para saber depois se a origem ainda é esta
            opcoes = dict(opcoes, impressao_origem=impressao) if impressao else {k: v for k, v in opcoes.items() if k != 'impressao_origem'}
            sem_identidade = not identidade.completa(impressao)
        try:
            job = store.criar(tipo, origem, destino, opcoes)
        except DestinoEmUso:
            return _erro(em_uso, 409)
        store.evento(job['id'], 'Pedido recebido: %s para %s.' % (origem, destino))
        if tipo in ('imagem', 'varredura') and sem_identidade:
            store.evento(job['id'], identidade.MSG_SEM_IDENTIDADE)
        aviso = aviso_pasta_de_dados(origem)
        if aviso:
            store.evento(job['id'], aviso)
        executor.submeter(job['id'])
        return jsonify(dict(store.obter(job['id']), **({'aviso': aviso} if aviso else {}))), 201

    def job_ou_404(job_id):
        job = store.obter(job_id) if ID_JOB.fullmatch(job_id) else None
        if job is None:
            abort(404, 'Job não encontrado.')
        return job

    @app.get('/api/jobs/<job_id>')
    def detalhe(job_id):
        job = job_ou_404(job_id)
        return jsonify({**job, 'eventos': store.eventos(job_id, 200)})

    @app.get('/api/jobs/<job_id>/mapa')
    def mapa_do_job(job_id):
        job = job_ou_404(job_id)
        resumo = imagem.resumir_mapa(job['destino'])
        if resumo is None:
            return _erro('Ainda não há mapa para este trabalho.', 404)
        return jsonify(resumo)

    def acao(nome):
        def rota(job_id):
            job_ou_404(job_id)
            try:
                return jsonify(getattr(executor, nome)(job_id))
            except ErroAcao as e:
                return _erro(e.mensagem, e.codigo)
        rota.__name__ = 'acao_' + nome
        return rota

    for nome in ('pausar', 'retomar', 'cancelar'):
        app.post('/api/jobs/<job_id>/' + nome)(acao(nome))

    @app.get('/api/eventos')
    def eventos():
        ass = store.assinar()      # assina antes de ler a lista: nenhuma mudança se perde entre uma e outra
        inicial = store.listar()

        def gerar():
            yield _evento('jobs', inicial)
            enviados, pendentes = {}, set()
            ultimo = time.monotonic()
            while not ctx.parar.is_set():
                pendentes |= ass.esperar(0.25 if pendentes else 1.0)
                agora = time.monotonic()
                for job_id in sorted(pendentes):
                    if agora - enviados.get(job_id, -1e9) >= ENVIO_MIN_S:
                        pendentes.discard(job_id)
                        job = store.obter(job_id)
                        if job:
                            enviados[job_id] = agora
                            ultimo = agora
                            yield _evento('job', job)
                if agora - ultimo >= PING_S:
                    ultimo = agora
                    yield ': ping\n\n'

        resp = Response(gerar(), mimetype='text/event-stream')
        resp.headers['X-Accel-Buffering'] = 'no'
        def ao_fechar():
            store.cancelar_assinatura(ass)
            store.fechar_conexao_da_thread()       # o gerador leu o banco depois do fim da requisição

        resp.call_on_close(ao_fechar)
        return resp

    @app.post('/api/desligar')
    def desligar():
        threading.Thread(target=ctx.desligar, name='recdados-desligar', daemon=True).start()
        return jsonify({'ok': True, 'mensagem': 'Desligando: os jobs em andamento ficam pausados.'})

    # --- painel ----------------------------------------------------------
    def pagina():
        """Troca ?t=<token> por cookie e redireciona sem o t; sem cookie, só o aviso; com cookie, o painel."""
        t = request.args.get('t')
        sessao = ctx.trocar_ticket(t) if t is not None else None
        if sessao is not None:
            resto = [(k, v) for k, v in request.args.items(multi=True) if k != 't']
            resp = make_response(redirect(request.path + ('?' + urlencode(resto) if resto else ''), 302))
            resp.set_cookie(COOKIE, sessao, httponly=True, samesite='Strict', path='/', max_age=SESSAO_S)
            return resp
        if not tem_cookie():
            return Response(PAGINA_SEM_COOKIE, 401, mimetype='text/html')
        index = PAINEL / 'index.html'
        if index.is_file():
            return send_from_directory(PAINEL, 'index.html')
        return Response(PAGINA_EM_CONSTRUCAO, mimetype='text/html')

    @app.get('/')
    def raiz():
        return pagina()

    @app.get('/jobs/<job_id>')
    def pagina_job(job_id):
        return pagina()

    @app.get('/<path:arquivo>')
    def estatico(arquivo):
        """Arquivos do painel (painel.js, painel.css...), só com cookie e só de dentro da pasta painel/."""
        if arquivo.startswith('api/') or not tem_cookie():
            abort(404)
        return send_from_directory(PAINEL, arquivo)

    rotas_particoes.registrar(app, ctx)
    rotas_achados.registrar(app, ctx)
    rotas_arvore.registrar(app, ctx)
    return app


# --- ciclo de vida -------------------------------------------------------
def baixar_prioridade():
    """Windows: o serviço (e os processos filhos, que herdam) trabalha em prioridade baixa de CPU e de disco, para o resto do
    PC não travar. Falha aqui não impede nada."""
    if os.name != 'nt':
        return False
    try:
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        eu = k.GetCurrentProcess()
        ok = bool(k.SetPriorityClass(eu, PRIORIDADE_ABAIXO_DO_NORMAL))
        k.SetPriorityClass(eu, PROCESSO_MODO_SEGUNDO_PLANO)             # E/S e memória em segundo plano; pode falhar sem problema
        return ok
    except Exception:
        return False


def principal():
    baixar_prioridade()
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    try:
        ctx = Contexto()
    except (OSError, ValueError, RuntimeError) as e:
        print('Não consegui preparar o serviço: %s' % e, file=sys.stderr)
        return 1
    try:
        ctx.servidor = ServidorExclusivo(config.HOST, ctx.porta, criar_app(ctx))
    except (OSError, SystemExit):         # o werkzeug sai com SystemExit quando a porta está em uso
        print('O serviço de recuperação já está no ar, ou a porta %d está ocupada. Nada foi alterado.'
              % ctx.porta, file=sys.stderr)
        return 1
    ctx.iniciar()

    def sinal(*_):
        threading.Thread(target=ctx.desligar, daemon=True).start()

    for nome in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
        if hasattr(signal, nome):
            try:
                signal.signal(getattr(signal, nome), sinal)
            except (ValueError, OSError):
                pass
    print('Recuperação de Dados em http://%s:%d (pid %d)' % (config.HOST, ctx.porta, os.getpid()), flush=True)
    try:
        ctx.servidor.serve_forever()
    finally:
        ctx.desligar()
        ctx.store.fechar()
    return 0


if __name__ == '__main__':
    sys.exit(principal())
