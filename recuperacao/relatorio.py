# -*- coding: utf-8 -*-
"""Pedaços comuns dos relatórios finais (html e csv) dos trabalhos `recuperar` e `imagem`."""
import os

CSS = ('body{font-family:sans-serif;margin:2rem}table{border-collapse:collapse;width:100%}'
       'th,td{border:1px solid #ccc;padding:4px 8px;text-align:left;font-size:14px}th{background:#f0f0f0}'
       '.num{text-align:right}.ruim{color:#a00}.parcial{color:#a60}')


def tam(n):
    n = float(n or 0)
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or u == 'TB':
            return ('%d %s' % (n, u)) if u == 'B' else ('%.1f %s' % (n, u))
        n /= 1024


def celula(v):
    """Texto seguro para planilha: célula que começa com = + - @ vira texto."""
    v = '' if v is None else str(v)
    return "'" + v if v[:1] in ('=', '+', '-', '@', '\t', '\r') else v


def criar_exclusivo(base, ext, job_id):
    """Abre `<base><ext>` para escrita sem nunca sobrescrever; se existir, tenta `<base>-<id><ext>` e numerados.
    Devolve (caminho, arquivo) ou levanta OSError."""
    sufixos = ('', '-' + str(job_id)) + tuple('-%s-%d' % (job_id, n) for n in range(2, 50))
    for sufixo in sufixos:
        caminho = base + sufixo + ext
        try:
            return caminho, open(caminho, 'x', encoding='utf-8', newline='')
        except FileExistsError:
            continue
    raise FileExistsError('não achei nome livre para %s' % (base + ext))
