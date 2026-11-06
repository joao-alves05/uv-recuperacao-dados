# -*- coding: utf-8 -*-
"""Rotas dos achados (fase 5). O servico.py só chama `registrar(app, ctx)`.

    GET  /api/achados/<id>/previa    miniatura da foto (JPG, PNG, GIF, BMP até 2 MB, como estão) ou ícone do grupo
    GET  /api/jobs/<id>/achados      lista filtrada, ordenada e paginada, com o resumo por grupo
    POST /api/jobs/<id>/recuperar    cria o job `recuperar` com os achados escolhidos
    POST /api/jobs/<id>/abrir        abre a pasta de destino de um job `recuperar` no Explorer (só Windows)
    GET  /api/jobs/<id>/recuperados  o que cada arquivo do job `recuperar` virou (resultado, motivo, bytes), paginado
    GET  /api/jobs/<id>/relatorio    o relatório html do job `recuperar` ou `imagem`
"""
import os
import re
from xml.sax.saxutils import escape

from flask import Response, jsonify, request

import time

from . import achados, identidade, leitor
from .jobs import DestinoEmUso
from .tipos import recuperar as tipo_recuperar

MAX_PREVIA = 2 * 1024 * 1024
ID_JOB = re.compile(r'j-\d{8}-[0-9a-f]{4}')
MAX_SELECIONADOS = 200000
TTL_CONFERENCIA = 30.0        # segundos que a conferência da origem vale para a prévia (vem uma por miniatura)
RESULTADOS_VALIDOS = ('ok', 'com_falhas', 'falhou', 'sumiu', 'copiando')
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
    conferidas = {}                 # id da varredura -> (instante, frase de erro ou None)

    def origem_mudou(job):
        """Frase de erro se a origem deste job não é mais a de quando a busca começou (conferência com validade curta)."""
        guardada = (job.get('opcoes') or {}).get('impressao_origem')
        if not guardada:
            return None
        agora = time.monotonic()
        antiga = conferidas.get(job['id'])
        if antiga and agora - antiga[0] < TTL_CONFERENCIA:
            return antiga[1]
        erro = identidade.conferir(job['origem'], guardada, lambda m: store.evento(job['id'], m))
        conferidas[job['id']] = (agora, erro)
        return erro

    @app.get('/api/achados/<int:achado_id>/previa')
    def previa(achado_id):
        achado = achados.obter(store, achado_id)
        if achado is None:
            return _erro('Achado não encontrado.', 404)
        job = store.obter(achado['job_id'])
        if (achado.get('grupo') == 'foto' and 0 < achado['tamanho'] <= MAX_PREVIA
                and (achado.get('extensao') or '').lower() in ('jpg', 'jpeg', 'png', 'gif', 'bmp')):
            mudou = origem_mudou(job)
            if mudou:
                return _erro(mudou, 409)                 # nunca mostra bytes de outro disco como se fossem o achado
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

    def job_de_varredura(job_id):
        job = store.obter(job_id) if ID_JOB.fullmatch(job_id) else None
        if job is None:
            return None, _erro('Job não encontrado.', 404)
        if job['tipo'] != 'varredura':
            return None, _erro('Este trabalho não tem lista de arquivos achados.', 409)
        return job, None

    def inteiro(valor, padrao):
        if valor is None or valor == '':
            return padrao
        try:
            return int(valor)
        except ValueError:
            return None

    @app.get('/api/jobs/<job_id>/achados')
    def listar_achados(job_id):
        job, erro = job_de_varredura(job_id)
        if erro:
            return erro
        a = request.args
        pagina, por_pagina = inteiro(a.get('pagina'), 1), inteiro(a.get('por_pagina'), achados.POR_PAGINA_PADRAO)
        try:
            filtros = achados.filtros_de({k: a.get(k) for k in ('grupo', 'tipo', 'estado', 'min', 'max', 'de', 'ate', 'pasta', 'busca')})
            return jsonify(achados.listar(store, job_id, filtros, a.get('ordem') or 'nome', a.get('sentido') or 'asc',
                                          pagina if pagina is not None else 0, por_pagina if por_pagina is not None else 0))
        except achados.ErroFiltro as e:
            return _erro(str(e), 400)

    @app.post('/api/jobs/<job_id>/recuperar')
    def recuperar(job_id):
        job, erro = job_de_varredura(job_id)
        if erro:
            return erro
        corpo = request.get_json(silent=True, force=True)
        if not isinstance(corpo, dict):
            return _erro('Pedido inválido: mande um JSON com ids (ou "todos") e destino.', 400)
        destino = corpo.get('destino')
        msg = tipo_recuperar.validar(job['origem'], destino)
        if msg:
            return _erro(msg, 400)
        conferidas.pop(job_id, None)                      # recuperar sempre confere de novo
        mudou = origem_mudou(job)
        if mudou:
            return _erro(mudou, 409)
        escolha = corpo.get('ids')
        try:
            if escolha == 'todos':
                ids = achados.ids_filtrados(store, job_id, achados.filtros_de(corpo.get('filtros') or {}))
            elif isinstance(escolha, list) and all(type(i) is int for i in escolha):
                ids = list(dict.fromkeys(escolha))
                if len(ids) > MAX_SELECIONADOS:
                    return _erro('Arquivos demais num pedido só.', 400)
                if ids and achados.contar_do_job(store, job_id, ids) != len(ids):
                    return _erro('Algum arquivo escolhido não pertence a esta varredura.', 400)
            else:
                return _erro('Escolha os arquivos: ids (lista de números) ou "todos".', 400)
        except achados.ErroFiltro as e:
            return _erro(str(e), 400)
        if not ids:
            return _erro('Nenhum arquivo escolhido para recuperar.', 400)
        total, livre, aviso = achados.tamanho_dos_ids(store, ids), tipo_recuperar.espaco_livre(destino), None
        if livre is not None and livre < total:
            if livre < total // 2:
                return _erro('Não cabe: os arquivos somam %s e o destino tem só %s livres. Escolha outro disco ou menos arquivos.'
                             % (tipo_recuperar._tam(total), tipo_recuperar._tam(livre)), 400)
            aviso = ('Os arquivos somam %s e o destino tem %s livres: pode faltar espaço para alguns.'
                     % (tipo_recuperar._tam(total), tipo_recuperar._tam(livre)))
        try:
            novo = store.criar('recuperar', job['origem'], destino, {'varredura': job_id, 'total': len(ids)})
        except DestinoEmUso:
            return _erro('Já existe um trabalho em andamento para esse destino.', 409)
        store.salvar_checkpoint(novo['id'], {'ids': ids})          # antes de entrar na fila
        store.evento(novo['id'], 'Pedido recebido: recuperar %d arquivos para %s.' % (len(ids), destino))
        ctx.executor.submeter(novo['id'])
        return jsonify(dict(store.obter(novo['id']), **({'aviso': aviso} if aviso else {}))), 201

    def job_recuperar(job_id):
        job = store.obter(job_id) if ID_JOB.fullmatch(job_id) else None
        if job is None:
            return None, _erro('Job não encontrado.', 404)
        if job['tipo'] != 'recuperar':
            return None, _erro('Este trabalho não é uma recuperação.', 409)
        return job, None

    @app.post('/api/jobs/<job_id>/abrir')
    def abrir_pasta(job_id):
        job, erro = job_recuperar(job_id)
        if erro:
            return erro
        if not ctx.eh_windows:
            return _erro('Abrir a pasta pelo programa só funciona no Windows. Abra a pasta pelo gerenciador de arquivos.', 409)
        if not os.path.isdir(job['destino']):
            return _erro('A pasta de destino ainda não existe.', 409)
        try:
            os.startfile(job['destino'])                 # o caminho vem do job gravado, nunca do pedido
        except OSError as e:
            return _erro('Não consegui abrir a pasta: %s' % (e.strerror or e), 409)
        return jsonify({'ok': True})

    def job_com_relatorio(job_id):
        job = store.obter(job_id) if ID_JOB.fullmatch(job_id) else None
        if job is None:
            return None, _erro('Job não encontrado.', 404)
        if job['tipo'] not in ('recuperar', 'imagem'):
            return None, _erro('Este trabalho não gera relatório.', 409)
        return job, None

    def arquivo_relatorio(job):
        cp = store.obter_checkpoint(job['id']) or {}
        nome = next((n for n in cp.get('relatorio') or [] if n.endswith('.html')), None)
        pasta = job['destino'] if job['tipo'] == 'recuperar' else os.path.dirname(os.path.abspath(job['destino']))   # imagem: ao lado dela
        caminho = os.path.join(pasta, nome) if nome and nome == os.path.basename(nome) else None
        return caminho if caminho and os.path.isfile(caminho) else None

    @app.get('/api/jobs/<job_id>/relatorio')
    def relatorio(job_id):
        job, erro = job_com_relatorio(job_id)
        if erro:
            return erro
        caminho = arquivo_relatorio(job)
        if caminho is None:
            return _erro('O relatório ainda não foi gerado.', 404)
        with open(caminho, 'r', encoding='utf-8') as f:
            pagina = f.read()
        # a política do serviço não deixa estilo embutido: o CSS vem de uma rota própria
        pagina = pagina.replace('<style>' + tipo_recuperar.CSS_RELATORIO + '</style>',
                                '<link rel="stylesheet" href="/api/jobs/%s/relatorio.css">' % job_id)
        return Response(pagina, mimetype='text/html')

    @app.get('/api/jobs/<job_id>/relatorio.css')
    def relatorio_css(job_id):
        job, erro = job_com_relatorio(job_id)
        if erro:
            return erro
        return Response(tipo_recuperar.CSS_RELATORIO, mimetype='text/css')

    @app.get('/api/jobs/<job_id>/recuperados')
    def recuperados(job_id):
        job, erro = job_recuperar(job_id)
        if erro:
            return erro
        a = request.args
        escolhidos = [x for x in (a.get('resultado') or '').split(',') if x]
        if any(x not in RESULTADOS_VALIDOS for x in escolhidos):
            return _erro('Filtro "resultado" inválido: use %s.' % ', '.join(RESULTADOS_VALIDOS), 400)
        pagina, por_pagina = inteiro(a.get('pagina'), 1), inteiro(a.get('por_pagina'), achados.POR_PAGINA_PADRAO)
        if pagina is None or not 1 <= pagina <= achados.MAX_PAGINA:
            return _erro('A página precisa ser um número de 1 a %d.' % achados.MAX_PAGINA, 400)
        if por_pagina is None or not 1 <= por_pagina <= achados.MAX_POR_PAGINA:
            return _erro('Itens por página: de 1 a %d.' % achados.MAX_POR_PAGINA, 400)
        con = store._con()
        onde, p = 'job_id=?', [job_id]
        if escolhidos:
            onde += ' AND resultado IN (%s)' % ','.join('?' * len(escolhidos))
            p += escolhidos
        total = con.execute('SELECT COUNT(*) FROM recuperacoes WHERE ' + onde, p).fetchone()[0]
        itens = [{'arquivo': l['arquivo'], 'achado_id': l['achado_id'], 'resultado': l['resultado'], 'motivo': l['motivo'],
                  'bytes': l['bytes']} for l in con.execute(
            'SELECT arquivo, achado_id, resultado, motivo, bytes FROM recuperacoes WHERE %s ORDER BY ordem LIMIT ? OFFSET ?' % onde,
            p + [por_pagina, (pagina - 1) * por_pagina])]
        por_resultado = {l['resultado']: l['n'] for l in con.execute(
            'SELECT resultado, COUNT(*) AS n FROM recuperacoes WHERE job_id=? GROUP BY resultado', (job_id,))}
        return jsonify({'total': total, 'pagina': pagina, 'por_pagina': por_pagina, 'itens': itens,
                        'resumo': {'por_resultado': por_resultado}})
