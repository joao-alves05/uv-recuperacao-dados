# -*- coding: utf-8 -*-
"""Um comando: sobe o serviço se não estiver no ar e abre o navegador já com o token.

    python iniciar.py

Se o serviço já responde em /api/saude, só abre o navegador. Se não, sobe destacado (sem janela no
Windows), espera a porta (até 10 s) e abre. Dois cliques seguidos não sobem duas cópias (trava O_EXCL).
"""
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request
import webbrowser

PASTA = os.path.dirname(os.path.abspath(__file__))
if PASTA not in sys.path:
    sys.path.insert(0, PASTA)

from recuperacao import config  # noqa: E402

PRAZO_SUBIDA_S = 10.0
VALIDADE_TRAVA_S = 10.0
NO_WINDOW = 0x08000000
DETACHED = 0x00000008
NEW_GROUP = 0x00000200
BREAKAWAY = 0x01000000


def servico_no_ar(porta, timeout=1.0):
    """True só se quem responde na porta é o nosso serviço (ok + versao em /api/saude)."""
    try:
        with urllib.request.urlopen('http://%s:%d/api/saude' % (config.HOST, porta), timeout=timeout) as r:
            dados = json.loads(r.read(4096))
        return isinstance(dados, dict) and dados.get('ok') is True and 'versao' in dados
    except (OSError, ValueError):
        return False


def identidade_confere(porta, timeout=2.0):
    """Quem responde na porta sabe o token do arquivo? Manda um desafio aleatório e confere o HMAC.
    Só depois disso o token é usado para pedir o ticket (nunca vai para um programa desconhecido)."""
    desafio = secrets.token_hex(16)
    try:
        with urllib.request.urlopen('http://%s:%d/api/saude?desafio=%s' % (config.HOST, porta, desafio),
                                    timeout=timeout) as r:
            dados = json.loads(r.read(4096))
        prova = dados.get('prova') if isinstance(dados, dict) else None
    except (OSError, ValueError):
        return False
    esperado = hmac.new(config.obter_token().encode(), desafio.encode(), hashlib.sha256).hexdigest()
    return isinstance(prova, str) and hmac.compare_digest(prova, esperado)


def pedir_ticket(porta, timeout=5.0):
    """Ticket de uso único (60 s) para o ?t= da URL: o token do arquivo nunca aparece na URL."""
    req = urllib.request.Request('http://%s:%d/api/ticket' % (config.HOST, porta), data=b'{}', method='POST',
                                 headers={'X-RecDados-Token': config.obter_token(), 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(4096))['ticket']


def servico_admin(porta, timeout=2.0):
    """O serviço que está no ar roda como administrador? (/api/saude informa; não mostra nada do usuário.)"""
    try:
        with urllib.request.urlopen('http://%s:%d/api/saude' % (config.HOST, porta), timeout=timeout) as r:
            return json.loads(r.read(4096)).get('admin') is True
    except (OSError, ValueError, AttributeError):
        return False


def desligar_servico(porta, espera=20.0):
    """Pede o encerramento limpo (jobs ficam pausados) e espera a porta liberar."""
    req = urllib.request.Request('http://%s:%d/api/desligar' % (config.HOST, porta), data=b'{}', method='POST',
                                 headers={'X-RecDados-Token': config.obter_token(), 'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except OSError:
        return False
    fim = time.monotonic() + espera
    while time.monotonic() < fim:
        if not servico_no_ar(porta, 0.5):
            return True
        time.sleep(0.2)
    return False


def pegar_trava(caminho, agora=time.time):
    """Cria o arquivo com O_EXCL. Trava com mais de 10 s é de uma subida que morreu e é refeita."""
    for _ in range(2):
        try:
            fd = os.open(caminho, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if agora() - os.path.getmtime(caminho) < VALIDADE_TRAVA_S:
                    return False
                os.remove(caminho)
            except OSError:
                return False
    return False


def subir_servico():
    config.garantir_pastas()
    args = [sys.executable, '-m', 'recuperacao.servico']
    registro = open(os.path.join(config.pasta_logs(), 'servico.log'), 'ab')
    kw = dict(cwd=PASTA, close_fds=True, stdin=subprocess.DEVNULL, stdout=registro, stderr=registro)
    try:
        if os.name == 'nt':
            base = DETACHED | NO_WINDOW | NEW_GROUP
            try:
                subprocess.Popen(args, creationflags=base | BREAKAWAY, **kw)
            except OSError:
                subprocess.Popen(args, creationflags=base, **kw)
        else:
            subprocess.Popen(args, start_new_session=True, **kw)
    finally:
        registro.close()


def esperar_servico(porta, prazo=PRAZO_SUBIDA_S):
    fim = time.monotonic() + prazo
    while time.monotonic() < fim:
        if servico_no_ar(porta, 0.5):
            return True
        time.sleep(0.2)
    return False


def principal(no_ar=servico_no_ar, subir=subir_servico, esperar=esperar_servico, abrir=webbrowser.open,
              trava=None, identidade=identidade_confere, ticket=pedir_ticket, exigir_admin=False,
              admin_no_ar=servico_admin, desligar=desligar_servico):
    porta = config.porta()
    if exigir_admin and no_ar(porta) and identidade(porta) and not admin_no_ar(porta):
        # o serviço velho roda sem privilégio e não consegue ler o disco inteiro: troca por um elevado (jobs ficam pausados)
        if not desligar(porta):
            print('Não consegui trocar o serviço em andamento por um com administrador. Feche-o e tente de novo.',
                  file=sys.stderr)
            return 1
    if not no_ar(porta):
        config.garantir_pastas()
        trava = trava or (lambda: pegar_trava(config.caminho_lock()))
        if trava():
            subir()
        if not esperar(porta):
            print('Não consegui iniciar o serviço. Veja %s' % os.path.join(config.pasta_logs(), 'servico.log'),
                  file=sys.stderr)
            return 1
    if not identidade(porta):
        print('Outro programa está usando a porta %d (ou é o serviço de outro usuário). Nada foi aberto.' % porta,
              file=sys.stderr)
        return 2
    try:
        t = ticket(porta)
    except (OSError, ValueError, KeyError):
        print('O serviço não deu a chave de acesso. Nada foi aberto.', file=sys.stderr)
        return 1
    abrir('http://%s:%d/?t=%s' % (config.HOST, porta, t))
    return 0


if __name__ == '__main__':
    sys.exit(principal(exigir_admin='--admin' in sys.argv[1:]))
