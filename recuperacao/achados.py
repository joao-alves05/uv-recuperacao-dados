# -*- coding: utf-8 -*-
"""Achados: o que a varredura (carving ou, na fase 4, o sistema de arquivos) encontrou.

Quem grava é `JobStore.adicionar_achados` (jobs.py); este módulo normaliza cada achado antes de gravar, lê os bytes
do achado da origem (só leitura) e consulta a tabela. O contrato completo está em docs/FASE5.md.
"""
import json
import os
import re

GRUPOS = ('foto', 'video', 'audio', 'documento', 'compactado', 'banco', 'outro')
ESTADOS = ('bom', 'parcial', 'ruim')
MAX_NOME = 255
SETOR_NOME = 512                      # o número no nome gerado (f0001234.jpg) é o setor de 512 bytes onde o achado começa

# coluna -> tipo SQL das colunas que a fase 5 acrescentou à tabela `achados` da fase 1 (migração em JobStore)
COLUNAS_NOVAS = (('nome', 'TEXT'), ('grupo', 'TEXT'), ('extensao', 'TEXT'), ('apagado', 'INTEGER'),
                 ('extents', 'TEXT'), ('pontuacao', 'INTEGER'), ('largura', 'INTEGER'), ('altura', 'INTEGER'),
                 ('recuperado_em', 'REAL'))
COLUNAS_GRAVAVEIS = ('job_id', 'nome', 'caminho', 'apagado', 'grupo', 'tipo', 'extensao', 'tamanho', 'offset',
                     'extents', 'estado', 'pontuacao', 'data', 'largura', 'altura')
_DATA = re.compile(r'\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?')


def nome_gerado(offset, extensao):
    return 'f%07d.%s' % (offset // SETOR_NOME, extensao.lower()) if extensao else 'f%07d' % (offset // SETOR_NOME)


def _int(valor, campo, minimo=0, maximo=None, opcional=False):
    if valor is None and opcional:
        return None
    if type(valor) is not int or valor < minimo or (maximo is not None and valor > maximo):
        raise ValueError('achado: "%s" inválido: %r' % (campo, valor))
    return valor


def normalizar(job_id, a):
    """Confere um achado (dict) e devolve a linha para gravar, na ordem de COLUNAS_GRAVAVEIS. ValueError se inválido."""
    if not isinstance(a, dict):
        raise ValueError('achado: precisa ser um objeto')
    tamanho = _int(a.get('tamanho'), 'tamanho')
    extents = a.get('extents')
    offset = a.get('offset')
    if extents is not None:
        if (not isinstance(extents, (list, tuple)) or not extents or len(extents) > 100000
                or not all(isinstance(e, (list, tuple)) and len(e) == 2 and type(e[0]) is int and type(e[1]) is int
                           and e[0] >= 0 and e[1] > 0 for e in extents)):
            raise ValueError('achado: "extents" precisa ser uma lista de [offset, comprimento]')
        if sum(e[1] for e in extents) != tamanho:
            raise ValueError('achado: a soma dos extents não bate com o tamanho')
        offset = extents[0][0]
        extents = json.dumps([list(e) for e in extents])
    else:
        offset = _int(offset, 'offset')
    caminho = a.get('caminho')
    if caminho is not None and (not isinstance(caminho, str) or '\x00' in caminho):
        raise ValueError('achado: "caminho" inválido')
    grupo = a.get('grupo') or 'outro'
    if grupo not in GRUPOS:
        raise ValueError('achado: grupo desconhecido: %r' % (grupo,))
    estado = a.get('estado') or 'bom'
    if estado not in ESTADOS:
        raise ValueError('achado: estado desconhecido: %r' % (estado,))
    extensao = a.get('extensao')
    if extensao is None and caminho:
        extensao = os.path.splitext(caminho.replace('\\', '/').rsplit('/', 1)[-1])[1].lstrip('.')
    if extensao is not None and not re.fullmatch(r'[A-Za-z0-9]{0,10}', str(extensao)):
        raise ValueError('achado: extensão inválida')
    nome = a.get('nome')
    if not nome:
        if caminho:
            nome = caminho.replace('\\', '/').rsplit('/', 1)[-1]
        else:
            nome = nome_gerado(offset, extensao or '')
    if not isinstance(nome, str) or not nome or len(nome) > MAX_NOME or '\x00' in nome:
        raise ValueError('achado: nome inválido')
    tipo = a.get('tipo') or (extensao or 'DESCONHECIDO').upper()
    if not isinstance(tipo, str) or len(tipo) > 40:
        raise ValueError('achado: tipo inválido')
    data = a.get('data')
    if data is not None and (not isinstance(data, str) or not _DATA.fullmatch(data)):
        raise ValueError('achado: "data" precisa ser AAAA-MM-DD ou AAAA-MM-DD HH:MM:SS')
    apagado = a.get('apagado')
    if apagado is not None:
        apagado = 1 if apagado else 0
    return (job_id, nome, caminho, apagado, grupo, tipo, extensao, tamanho, offset, extents, estado,
            _int(a.get('pontuacao'), 'pontuacao', 0, 100, True), data,
            _int(a.get('largura'), 'largura', 0, opcional=True), _int(a.get('altura'), 'altura', 0, opcional=True))


def como_dict(linha):
    """Linha do banco -> dict do contrato (extents já como lista, apagado como bool ou None)."""
    d = dict(linha)
    ext = d.get('extents')
    try:
        d['extents'] = json.loads(ext) if ext else None
    except ValueError:
        d['extents'] = None
    if d.get('apagado') is not None:
        d['apagado'] = bool(d['apagado'])
    return d


def obter(store, achado_id):
    linha = store._con().execute('SELECT * FROM achados WHERE id=?', (achado_id,)).fetchone()
    return como_dict(linha) if linha else None


def contar(store, job_id):
    return store._con().execute('SELECT COUNT(*) FROM achados WHERE job_id=?', (job_id,)).fetchone()[0]


def contar_por_grupo(store, job_id):
    return {l['grupo'] or 'outro': l['n'] for l in store._con().execute(
        'SELECT grupo, COUNT(*) AS n FROM achados WHERE job_id=? GROUP BY grupo', (job_id,))}


def extents_de(achado):
    """[(offset, comprimento), ...] dos bytes do achado na origem."""
    if achado.get('extents'):
        return [(int(o), int(n)) for o, n in achado['extents']]
    return [(int(achado['offset']), int(achado['tamanho']))]


def ler_achado(leitor, achado, maximo=None, bloco=1 << 20):
    """Gera os bytes do achado, lidos da origem (só leitura), em pedaços de até `bloco`. Para em `maximo` bytes."""
    falta = achado['tamanho'] if maximo is None else min(maximo, achado['tamanho'])
    for off, n in extents_de(achado):
        while n > 0 and falta > 0:
            pedaco = min(bloco, n, falta)
            dados = leitor.ler(off, pedaco)
            if len(dados) < pedaco:
                raise OSError('leitura curta em %d' % off)
            yield dados
            off, n, falta = off + pedaco, n - pedaco, falta - pedaco
        if falta <= 0:
            return


# --- consulta (lista de resultados) -----------------------------------------------------------------------------
ORDENS = {'nome': 'nome COLLATE NOCASE', 'tamanho': 'tamanho', 'data': 'data', 'tipo': 'tipo COLLATE NOCASE'}
MAX_POR_PAGINA = 500
POR_PAGINA_PADRAO = 50
_DIA = re.compile(r'\d{4}-\d{2}-\d{2}')


class ErroFiltro(ValueError):
    """Filtro inválido; a mensagem (em português) vai para o painel com 400."""


def _lista(valor, campo, permitidos=None):
    itens = [x.strip() for x in str(valor).split(',') if x.strip()]
    if not itens or len(itens) > 50 or any(len(x) > 40 for x in itens):
        raise ErroFiltro('Filtro "%s" inválido.' % campo)
    if permitidos and any(x not in permitidos for x in itens):
        raise ErroFiltro('Filtro "%s" inválido: use %s.' % (campo, ', '.join(permitidos)))
    return itens


def _bytes(valor, campo):
    try:
        n = int(str(valor))
    except ValueError:
        n = -1
    if n < 0:
        raise ErroFiltro('Filtro "%s" precisa ser um número de bytes (0 ou mais).' % campo)
    return n


def filtros_de(d):
    """Dict de texto (query string ou corpo do POST) -> filtros limpos. Campos vazios ou ausentes são ignorados."""
    if not isinstance(d, dict):
        raise ErroFiltro('Os filtros precisam ser um objeto.')
    d = {k: v for k, v in d.items() if v is not None and v != ''}
    f = {}
    if 'grupo' in d:
        f['grupo'] = _lista(d['grupo'], 'grupo', GRUPOS)
    if 'tipo' in d:
        f['tipo'] = _lista(d['tipo'], 'tipo')
    if 'estado' in d:
        f['estado'] = _lista(d['estado'], 'estado', ESTADOS)
    for c in ('min', 'max'):
        if c in d:
            f[c] = _bytes(d[c], c)
    if 'min' in f and 'max' in f and f['min'] > f['max']:
        raise ErroFiltro('O tamanho mínimo é maior que o máximo.')
    for c in ('de', 'ate'):
        if c in d:
            if not _DIA.fullmatch(str(d[c])):
                raise ErroFiltro('Filtro "%s" precisa ser uma data AAAA-MM-DD.' % c)
            f[c] = str(d[c])
    if 'pasta' in d:
        if len(str(d['pasta'])) > 1000 or '\x00' in str(d['pasta']):
            raise ErroFiltro('A pasta é longa demais.')
        f['pasta'] = str(d['pasta'])
    if 'busca' in d:
        if len(str(d['busca'])) > 100:
            raise ErroFiltro('A busca é longa demais.')
        f['busca'] = str(d['busca'])
    return f


def _onde(job_id, f, sem_grupo=False):
    sql, p = ['job_id=?'], [job_id]
    for campo in ('grupo', 'tipo', 'estado'):
        if campo in f and not (sem_grupo and campo == 'grupo'):
            sql.append('%s IN (%s)' % (campo, ','.join('?' * len(f[campo]))))
            p += f[campo]
    if 'min' in f:
        sql.append('tamanho>=?'), p.append(f['min'])
    if 'max' in f:
        sql.append('tamanho<=?'), p.append(f['max'])
    if 'de' in f:
        sql.append('data>=?'), p.append(f['de'])
    if 'ate' in f:
        sql.append('data<?'), p.append(f['ate'] + ' 99')        # o dia inteiro: "2021-05-17 23:59:59" < "2021-05-17 99"
    if 'pasta' in f:
        # a pasta e tudo dentro dela; \ e / valem igual, maiúsculas não importam (LIKE); só achados com caminho original
        base = f['pasta'].replace(chr(92), '/').rstrip('/')
        base = base.replace('!', '!!').replace('%', '!%').replace('_', '!_')
        sql.append("REPLACE(caminho, char(92), '/') LIKE ? ESCAPE '!'"), p.append(base + '/%')
    if 'busca' in f:
        achado = '%' + f['busca'].replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
        sql.append("(nome LIKE ? ESCAPE '!' OR caminho LIKE ? ESCAPE '!')"), p.extend([achado, achado])
    return ' AND '.join(sql), p


def listar(store, job_id, f, ordem='nome', sentido='asc', pagina=1, por_pagina=POR_PAGINA_PADRAO):
    """{total, pagina, por_pagina, itens, resumo:{por_grupo}}. O resumo ignora o filtro de grupo (são as abas)."""
    if ordem not in ORDENS:
        raise ErroFiltro('Ordem inválida: use %s.' % ', '.join(ORDENS))
    if sentido not in ('asc', 'desc'):
        raise ErroFiltro('Sentido inválido: use asc ou desc.')
    if type(pagina) is not int or pagina < 1:
        raise ErroFiltro('A página precisa ser um número a partir de 1.')
    if type(por_pagina) is not int or not 1 <= por_pagina <= MAX_POR_PAGINA:
        raise ErroFiltro('Itens por página: de 1 a %d.' % MAX_POR_PAGINA)
    con = store._con()
    onde, p = _onde(job_id, f)
    total = con.execute('SELECT COUNT(*) FROM achados WHERE ' + onde, p).fetchone()[0]
    d = sentido.upper()
    ordenar = ORDENS[ordem] + ' ' + d
    if ordem == 'data':
        ordenar = 'data IS NULL, ' + ordenar               # sem data vai sempre para o fim
    linhas = con.execute('SELECT * FROM achados WHERE %s ORDER BY %s, id %s LIMIT ? OFFSET ?' % (onde, ordenar, d),
                         p + [por_pagina, (pagina - 1) * por_pagina]).fetchall()
    onde_g, pg = _onde(job_id, f, sem_grupo=True)
    por_grupo = {l['grupo'] or 'outro': l['n'] for l in con.execute(
        'SELECT grupo, COUNT(*) AS n FROM achados WHERE %s GROUP BY grupo' % onde_g, pg)}
    itens = []
    for l in linhas:
        a = como_dict(l)
        a['previa'] = '/api/achados/%d/previa' % a['id']
        itens.append(a)
    tamanho_total = con.execute('SELECT COALESCE(SUM(tamanho),0) FROM achados WHERE ' + onde, p).fetchone()[0]
    return {'total': total, 'pagina': pagina, 'por_pagina': por_pagina, 'itens': itens,
            'resumo': {'por_grupo': por_grupo, 'tamanho_total': tamanho_total}}


def ids_filtrados(store, job_id, f):
    onde, p = _onde(job_id, f)
    return [l[0] for l in store._con().execute('SELECT id FROM achados WHERE %s ORDER BY id' % onde, p)]


def contar_do_job(store, job_id, ids):
    """Quantos dos ids existem e pertencem ao job (ids sem repetição)."""
    n = 0
    for i in range(0, len(ids), 500):
        parte = ids[i:i + 500]
        n += store._con().execute('SELECT COUNT(*) FROM achados WHERE job_id=? AND id IN (%s)' % ','.join('?' * len(parte)),
                                  [job_id] + parte).fetchone()[0]
    return n
