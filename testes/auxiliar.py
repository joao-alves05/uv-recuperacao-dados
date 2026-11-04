# -*- coding: utf-8 -*-
"""Apoio dos testes: pasta temporária, origem aleatória, serviço em processo à parte.

Nenhum teste toca em disco real nem na porta 8765: tudo vai para pasta temporária e porta livre.
"""
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)


def _proibir_mapa_real():
    from recuperacao import unidade

    class Proibido:
        def _nao(self, *a):
            raise AssertionError('teste consultou o mapeamento de discos de verdade: troque unidade.MAPA por um falso')
        discos_da_origem = discos_do_destino = _nao
    unidade.MAPA = Proibido()


_proibir_mapa_real()


def porta_livre():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def sha256_de(caminho):
    h = hashlib.sha256()
    with open(caminho, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def criar_origem(pasta, tamanho, nome='origem.bin'):
    caminho = os.path.join(pasta, nome)
    with open(caminho, 'wb') as f:
        f.write(os.urandom(tamanho))
    return caminho


def esperar(condicao, prazo=20.0, passo=0.05, msg='condição não chegou'):
    fim = time.monotonic() + prazo
    while time.monotonic() < fim:
        v = condicao()
        if v:
            return v
        time.sleep(passo)
    raise AssertionError(msg)


class CasoComPasta(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.mkdtemp(prefix='recdados-teste-')
        self.addCleanup(shutil.rmtree, self.pasta, True)
        self.dados = os.path.join(self.pasta, 'dados')
        os.makedirs(self.dados)
        self.trabalho = os.path.join(self.pasta, 'trabalho')
        os.makedirs(self.trabalho)


class Servico:
    """O serviço de verdade, em outro processo, com pasta de dados e porta temporárias."""

    def __init__(self, dados, porta=None, atraso=0.0, log_leituras=None):
        self.dados = dados
        self.porta = porta or porta_livre()
        self.atraso = atraso
        self.log_leituras = log_leituras
        self.proc = None

    def env(self):
        e = dict(os.environ, RECDADOS_DADOS=self.dados, RECDADOS_PORTA=str(self.porta), PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8')
        if self.log_leituras:
            e['RECDADOS_LOG_LEITURAS'] = self.log_leituras
        else:
            e.pop('RECDADOS_LOG_LEITURAS', None)
        if self.atraso:
            e['RECDADOS_ATRASO_BLOCO'] = str(self.atraso)
        else:
            e.pop('RECDADOS_ATRASO_BLOCO', None)
        return e

    def iniciar(self, esperar_saude=True, prazo=20.0):
        self.proc = subprocess.Popen([sys.executable, '-m', 'recuperacao.servico'], cwd=RAIZ, env=self.env(),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if esperar_saude:
            def no_ar():
                if self.proc.poll() is not None:
                    raise AssertionError('o serviço saiu: %s' % self.proc.stderr.read().decode('utf-8', 'replace'))
                try:
                    return self.pedir('GET', '/api/saude', token=False)[0] == 200
                except OSError:
                    return False
            esperar(no_ar, prazo, 0.1, 'o serviço não subiu')
        return self

    @property
    def token(self):
        with open(os.path.join(self.dados, 'token'), encoding='ascii') as f:
            return f.read().strip()

    def pedir(self, metodo, caminho, corpo=None, token=True, cabecalhos=None, host=None):
        con = http.client.HTTPConnection('127.0.0.1', self.porta, timeout=30)
        try:
            h = dict(cabecalhos or {})
            if token:
                h['X-RecDados-Token'] = self.token
            if host:
                h['Host'] = host
            dados = None
            if corpo is not None:
                dados = json.dumps(corpo).encode('utf-8')
                h['Content-Type'] = 'application/json'
            con.request(metodo, caminho, body=dados, headers=h)
            r = con.getresponse()
            bruto = r.read()
            try:
                return r.status, json.loads(bruto), r
            except ValueError:
                return r.status, bruto, r
        finally:
            con.close()

    def criar_imagem(self, origem, destino, bloco=65536):
        st, job, _ = self.pedir('POST', '/api/jobs', {'tipo': 'imagem', 'origem': origem, 'destino': destino,
                                                      'opcoes': {'bloco': bloco}})
        assert st == 201, (st, job)
        return job

    def job(self, job_id):
        st, dados, _ = self.pedir('GET', '/api/jobs/' + job_id)
        assert st == 200, (st, dados)
        return dados

    def esperar_estado(self, job_id, estados, prazo=30.0):
        if isinstance(estados, str):
            estados = (estados,)
        return esperar(lambda: (j := self.job(job_id))['estado'] in estados and j, prazo, 0.05,
                       'job não chegou a %s' % (estados,))

    def matar(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.fechar()

    def fechar(self):
        if self.proc:
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            for f in (self.proc.stdout, self.proc.stderr):
                if f:
                    f.close()
