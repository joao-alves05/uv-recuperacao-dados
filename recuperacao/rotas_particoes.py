# -*- coding: utf-8 -*-
"""Rotas da fase 3 (partições). O servico.py só chama `registrar(app, ctx)`.

    GET /api/fontes/<id>/particoes   tabela de partições da fonte (só leitura, na hora)
    GET /api/jobs/<id>/particoes     resultado de um job `particoes` pronto (candidatos e tabela proposta)
"""
import json
import re

from flask import jsonify

from . import fontes, leitor, particoes

ID_JOB = re.compile(r'j-\d{8}-[0-9a-f]{4}')


def _erro(mensagem, codigo):
    return jsonify({'erro': mensagem}), codigo


def registrar(app, ctx):
    @app.get('/api/fontes/<fonte_id>/particoes')
    def particoes_da_fonte(fonte_id):
        fonte = next((f for f in fontes.listar(ctx.provedor_fontes) if f['id'] == fonte_id), None)
        if fonte is None:
            return _erro('Fonte não encontrada.', 404)
        try:
            with leitor.abrir(fonte['caminho']) as l:
                tabela = particoes.analisar(l)
        except leitor.ErroPermissao as e:
            return _erro(str(e), 403)
        except leitor.ErroLeitor as e:
            return _erro(str(e), 409)
        return jsonify({'fonte': fonte['id'], **tabela})

    @app.get('/api/jobs/<job_id>/particoes')
    def particoes_do_job(job_id):
        job = ctx.store.obter(job_id) if ID_JOB.fullmatch(job_id) else None
        if job is None:
            return _erro('Job não encontrado.', 404)
        if job['tipo'] != 'particoes':
            return _erro('Este job não é uma busca de partições.', 404)
        try:
            with open(job['destino'], encoding='utf-8') as f:
                return jsonify(json.load(f))
        except (OSError, ValueError):
            return _erro('Ainda não há resultado para este trabalho.', 404)
