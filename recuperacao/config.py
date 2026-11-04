# -*- coding: utf-8 -*-
"""Pasta de dados, porta e token do serviço.

Tudo é lido a cada chamada (não na importação), para os testes trocarem
RECDADOS_DADOS e RECDADOS_PORTA por variável de ambiente.
"""
import json
import os
import re
import secrets
import time

VERSAO = '1.0.0-fase1'
HOST = '127.0.0.1'
PORTA_PADRAO = 8765


def pasta_dados():
    env = os.environ.get('RECDADOS_DADOS')
    if env:
        return os.path.abspath(env)
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), 'AppData', 'Local')
        return os.path.join(base, 'RecuperacaoDados')
    return os.path.join(os.path.expanduser('~'), '.local', 'share', 'recuperacao-dados')


def porta():
    bruto = os.environ.get('RECDADOS_PORTA') or PORTA_PADRAO
    try:
        p = int(bruto)
    except ValueError:
        raise ValueError(f'RECDADOS_PORTA inválida: {bruto!r}') from None
    if not 1 <= p <= 65535:
        raise ValueError(f'RECDADOS_PORTA fora do intervalo: {p}')
    return p


def atraso_bloco():
    """Só para teste: segundos de espera depois de cada bloco copiado (RECDADOS_ATRASO_BLOCO)."""
    try:
        return max(0.0, float(os.environ.get('RECDADOS_ATRASO_BLOCO') or 0))
    except ValueError:
        return 0.0


def log_leituras():
    """Só para teste: arquivo onde o tipo `imagem` anota cada leitura da origem (RECDADOS_LOG_LEITURAS)."""
    return os.environ.get('RECDADOS_LOG_LEITURAS') or None


def caminho_db():
    return os.path.join(pasta_dados(), 'jobs.db')


def caminho_token():
    return os.path.join(pasta_dados(), 'token')


def caminho_lock():
    return os.path.join(pasta_dados(), 'servico.lock')


def pasta_logs():
    return os.path.join(pasta_dados(), 'logs')


def garantir_pastas():
    os.makedirs(pasta_logs(), exist_ok=True)


def obter_token():
    """Lê o token; na primeira subida cria um e grava (só o dono lê). Continua o mesmo entre reinícios."""
    garantir_pastas()
    caminho = caminho_token()
    for _ in range(50):
        try:
            with open(caminho, 'r', encoding='ascii') as f:
                token = f.read().strip()
            if token:
                return token
        except FileNotFoundError:
            novo = secrets.token_urlsafe(32)
            try:
                fd = os.open(caminho, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass            # outra cópia criou agora: lê de novo
            else:
                with os.fdopen(fd, 'w', encoding='ascii') as f:
                    f.write(novo)
                return novo
        time.sleep(0.05)       # arquivo recém-criado ainda vazio
    raise RuntimeError('Não consegui ler nem criar o token do serviço.')


# --- marca (nome, empresa, site e cor vêm de configuração, nunca do código) ---------------------
MARCA_PADRAO = {'app': 'UV Recuperação de Dados', 'empresa': '', 'site': '', 'cor': ''}
MAX_TEXTO_MARCA = 80
_COR = re.compile(r'#[0-9a-fA-F]{6}')


def _marca_valida(chave, valor):
    """Devolve o valor limpo ou None se não serve (o padrão neutro entra no lugar)."""
    if not isinstance(valor, str):
        return None
    valor = valor.strip()
    if chave == 'cor':
        return valor if _COR.fullmatch(valor) else None
    if len(valor) > MAX_TEXTO_MARCA or any(ord(c) < 32 for c in valor):
        return None
    if chave == 'site' and valor and not valor.startswith('https://'):
        return None
    if chave == 'app' and not valor:
        return None
    return valor


def marca():
    """{"app","empresa","site","cor"}: variável RECDADOS_MARCA_* vence o marca.json da pasta de dados, que vence o padrão neutro."""
    arquivo = {}
    try:
        with open(os.path.join(pasta_dados(), 'marca.json'), 'r', encoding='utf-8') as f:
            lido = json.load(f)
        if isinstance(lido, dict):
            arquivo = lido
    except (OSError, ValueError):
        pass
    resultado = {}
    for chave, padrao in MARCA_PADRAO.items():
        valor = _marca_valida(chave, os.environ.get('RECDADOS_MARCA_' + chave.upper()))
        if valor is None:
            valor = _marca_valida(chave, arquivo.get(chave))
        resultado[chave] = padrao if valor is None else valor
    return resultado
