# -*- coding: utf-8 -*-
"""Rotas dos achados (fase 5). O servico.py só chama `registrar(app, ctx)`.

    GET /api/achados/<id>/previa    miniatura da foto (JPG, PNG, GIF, BMP até 2 MB, como estão) ou ícone do grupo
"""
import re
from xml.sax.saxutils import escape

from flask import Response, jsonify

from . import achados, leitor

MAX_PREVIA = 2 * 1024 * 1024
COR_GRUPO = {'foto': '#2f7d6d', 'video': '#7a4fa3', 'audio': '#b0701f', 'documento': '#2f5f9e',
             'compactado': '#8a6d1f', 'banco': '#5b6770', 'outro': '#6b6b6b'}
ROTULO_GRUPO = {'foto': 'FOTO', 'video': 'VIDEO', 'audio': 'AUDIO', 'documento': 'DOC', 'compactado': 'PACOTE',
                'banco': 'DADOS', 'outro': 'ARQ'}
IMAGENS = (('image/jpeg', b'\xff\xd8\xff'), ('image/png', b'\x89PNG\r\n\x1a\n'), ('image/gif', b'GIF8'),
           ('image/bmp', b'BM'))


def _erro(mensagem, codigo):
    return jsonify({'erro': mensagem}), codigo


def icone_svg(achado):
    """Ícone quadrado do grupo, com a extensão. Texto de terceiros só entra escapado e só com letras e números."""
    grupo = achado.get('grupo') or 'outro'
    ext = re.sub(r'[^A-Za-z0-9]', '', achado.get('extensao') or achado.get('tipo') or '')[:5].upper()
    cor = COR_GRUPO.get(grupo, COR_GRUPO['outro'])
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">'
            '<rect x="14" y="6" width="68" height="84" rx="6" fill="%s"/>'
            '<text x="48" y="44" font-family="sans-serif" font-size="13" font-weight="bold" text-anchor="middle" '
            'fill="#ffffff">%s</text>'
            '<text x="48" y="68" font-family="sans-serif" font-size="14" text-anchor="middle" fill="#ffffff">%s</text>'
            '</svg>' % (cor, escape(ROTULO_GRUPO.get(grupo, 'ARQ')), escape(ext)))


def _resposta(corpo, tipo):
    resp = Response(corpo, mimetype=tipo)
    resp.headers['Content-Disposition'] = 'inline'
    return resp


def registrar(app, ctx):
    store = ctx.store

    @app.get('/api/achados/<int:achado_id>/previa')
    def previa(achado_id):
        achado = achados.obter(store, achado_id)
        if achado is None:
            return _erro('Achado não encontrado.', 404)
        job = store.obter(achado['job_id'])
        if (achado.get('grupo') == 'foto' and 0 < achado['tamanho'] <= MAX_PREVIA
                and (achado.get('extensao') or '').lower() in ('jpg', 'jpeg', 'png', 'gif', 'bmp')):
            try:
                with leitor.abrir(job['origem']) as l:
                    dados = b''.join(achados.ler_achado(l, achado, MAX_PREVIA))
            except leitor.ErroPermissao as e:
                return _erro(str(e), 403)
            except (leitor.ErroLeitor, OSError):
                dados = b''
            # o tipo de imagem vem dos primeiros bytes, nunca do nome do arquivo
            tipo = next((m for m, magia in IMAGENS if dados.startswith(magia)), None)
            if tipo:
                return _resposta(dados, tipo)
        return _resposta(icone_svg(achado), 'image/svg+xml')
