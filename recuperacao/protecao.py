# -*- coding: utf-8 -*-
"""Pastas onde o programa nunca grava, nem a pedido (principalmente quando roda como administrador).

Com o serviço elevado, quem consegue ler o token da sessão poderia mandar gravar como administrador em qualquer
pasta (inclusive em pastas que carregam programas). O destino de imagem, de recuperação e de tabela proposta passa
por `destino_proibido`, que recusa o sistema operacional, as pastas de programas, a inicialização automática e a
própria pasta do programa.
"""
import os

PASTA_DO_PROGRAMA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VARS_WINDOWS = ('SystemRoot', 'windir', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432', 'ProgramData',
                 'ALLUSERSPROFILE')
_FIXAS_POSIX = ('/etc', '/usr', '/bin', '/sbin', '/lib', '/lib64', '/boot', '/proc', '/sys', '/dev', '/var/lib')
MSG = ('Essa pasta é protegida (sistema, programas, inicialização do Windows ou a pasta do programa). '
       'Escolha uma pasta sua, como Documentos, em outro disco.')


def _norm(caminho):
    return os.path.normcase(os.path.realpath(os.path.abspath(caminho)))


def _protegidas():
    pastas = [PASTA_DO_PROGRAMA]
    if os.name == 'nt':
        for v in _VARS_WINDOWS:
            if os.environ.get(v):
                pastas.append(os.environ[v])
        for base in (os.environ.get('APPDATA'), os.environ.get('ProgramData')):
            if base:
                pastas.append(os.path.join(base, 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup'))
    else:
        pastas.extend(_FIXAS_POSIX)
    return pastas


def destino_proibido(caminho):
    """Frase de erro se `caminho` é (ou fica dentro de) uma pasta protegida; None se pode."""
    if not isinstance(caminho, str) or not caminho.strip() or '\x00' in caminho:
        return None                                  # quem chama já recusa caminho vazio ou inválido
    try:
        alvo = _norm(caminho)
        for p in _protegidas():
            base = _norm(p)
            try:
                if os.path.commonpath([alvo, base]) == base:
                    return MSG
            except ValueError:
                continue                             # unidades diferentes
    except (OSError, ValueError):
        return MSG                                   # não deu para resolver o caminho: mais seguro recusar
    return None
