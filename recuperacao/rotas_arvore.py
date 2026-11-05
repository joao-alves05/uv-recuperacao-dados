# -*- coding: utf-8 -*-
"""Rota da árvore de pastas do original (fase 4). O servico.py só chama `registrar(app, ctx)`.

    GET /api/jobs/<id>/arvore?caminho=/pasta   ->   {"caminho": "/pasta", "pastas": [{"nome", "caminho", "total"}]}

As subpastas diretas de UMA pasta do sistema de arquivos original, montadas a partir do `caminho` dos achados (a
varredura "pelo sistema de arquivos" é quem preenche). `total` = achados dentro, contando as subpastas. Raiz = caminho
vazio ou "/". Os arquivos da pasta vêm da lista de achados. Achados do carving não têm caminho e não entram aqui.
"""
import re

from flask import jsonify, request

ID_JOB = re.compile(r'j-\d{8}-[0-9a-f]{4}')


def _erro(mensagem, codigo):
    return jsonify({'erro': mensagem}), codigo


def normalizar_caminho(texto):
    """'/a//b/' -> '/a/b'; '' -> '/'. None se tiver byte nulo."""
    if texto is None or '\x00' in texto:
        return None
    partes = [p for p in texto.replace('\\', '/').split('/') if p]
    return '/' + '/'.join(partes)


def montar(store, job_id, caminho):
    """Subpastas diretas de `caminho` com o total de achados dentro de cada uma (contando as subpastas)."""
    prefixo = caminho.rstrip('/') + '/'
    pastas = {}
    cursor = store._con().execute(
        'SELECT caminho FROM achados WHERE job_id=? AND caminho IS NOT NULL AND substr(caminho, 1, ?) = ?',
        (job_id, len(prefixo), prefixo))
    for l in cursor:
        resto = l['caminho'][len(prefixo):]
        if '/' in resto:
            nome = resto.split('/', 1)[0]
            p = pastas.setdefault(nome, {'nome': nome, 'caminho': prefixo + nome, 'total': 0})
            p['total'] += 1
    return {'caminho': caminho, 'pastas': sorted(pastas.values(), key=lambda x: x['nome'].lower())}


def registrar(app, ctx):
    store = ctx.store

    @app.get('/api/jobs/<job_id>/arvore')
    def arvore(job_id):
        job = store.obter(job_id) if ID_JOB.fullmatch(job_id) else None
        if job is None:
            return _erro('Job não encontrado.', 404)
        if job['tipo'] != 'varredura':
            return _erro('Este trabalho não é uma varredura.', 404)
        caminho = normalizar_caminho(request.args.get('caminho', '/'))
        if caminho is None:
            return _erro('Pedido inválido: o caminho tem caracteres que não são aceitos.', 400)
        return jsonify(montar(store, job_id, caminho))
