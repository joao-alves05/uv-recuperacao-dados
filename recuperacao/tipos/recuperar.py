# -*- coding: utf-8 -*-
"""Tipo `recuperar`: copia os achados escolhidos da origem para uma pasta de destino, organizados por grupo.

Nasce só pela rota `POST /api/jobs/<id>/recuperar` (rotas_achados.py), que grava a seleção (lista de ids de achado)
no checkpoint do job. A origem só é lida (Leitor); o destino fica em outro disco (`unidade.conferir`, quando a origem
é disco ou volume). Cada arquivo é gravado como `<nome>.parte` e renomeado só quando termina; nada existente é
sobrescrito (nome repetido ganha " (2)"). A tabela `recuperacoes` guarda o que cada achado virou, então pausar, cair e
retomar pula o que já está pronto. No fim saem `relatorio.html` e `relatorio.csv` na pasta de destino.
"""
import csv
import hashlib
import html
import io
import os
import re
import time

from .. import achados, config, leitor, unidade
from ..executor import ErroJob
from ..leitor import ErroLeitor
from . import imagem

PASTAS = {'foto': 'Fotos', 'video': 'Videos', 'audio': 'Audios', 'documento': 'Documentos',
          'compactado': 'Compactados', 'banco': 'Bancos de dados', 'outro': 'Outros'}
BLOCO = 1024 * 1024
MAX_NOME = 120
_PROIBIDOS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESULTADOS = {'ok': 'ok', 'com_falhas': 'com trechos ilegíveis (zerados)', 'sumiu': 'achado não encontrado'}
CSS_RELATORIO = ('body{font-family:sans-serif;margin:2rem}table{border-collapse:collapse;width:100%}'
                 'th,td{border:1px solid #ccc;padding:4px 8px;text-align:left;font-size:14px}th{background:#f0f0f0}'
                 '.num{text-align:right}.ruim{color:#a00}.parcial{color:#a60}')
ROTULO_ESTADO = {'bom': 'bom', 'parcial': 'parcial', 'ruim': 'ruim'}


def validar(origem, destino, opcoes=None, retomada=False):
    if not isinstance(origem, str) or not origem.strip() or '\x00' in origem:
        return 'Informe a origem.'
    if not isinstance(destino, str) or not destino.strip() or '\x00' in destino:
        return 'Informe a pasta de destino.'
    erro = imagem.caminho_proibido(destino)
    if erro:
        return erro
    if os.path.isfile(destino):
        return 'O destino é um arquivo. Escolha uma pasta.'
    if leitor.eh_dispositivo(origem):
        return unidade.conferir(origem, destino)           # nunca gravar no disco que está sendo recuperado
    o, d = imagem._normal(origem), imagem._normal(destino)
    if o == d:
        return 'O destino não pode ser a própria origem.'
    try:
        if os.path.commonpath([o, d]) == o:
            return 'O destino não pode ficar dentro da origem.'
    except ValueError:
        pass
    return None


def nome_seguro(nome):
    """Nome de arquivo que o Windows e o Linux aceitam, sem caminho."""
    nome = _PROIBIDOS.sub('_', str(nome).replace('\\', '/').rsplit('/', 1)[-1]).strip(' .') or 'arquivo'
    base, ext = os.path.splitext(nome)
    if base.split('.')[0].upper() in imagem.NOMES_RESERVADOS:
        base = '_' + base
    return base[:MAX_NOME] + ext[:12]


def nome_unico(pasta, nome, usados):
    base, ext = os.path.splitext(nome)
    cand, n = nome, 1
    while cand.casefold() in usados or os.path.exists(os.path.join(pasta, cand)):
        n += 1
        cand = '%s (%d)%s' % (base, n, ext)
    return cand


def _copiar(aberto, achado, tmp, ctrl):
    """Grava o achado em `tmp`. Devolve (resultado, bytes, sha256), ou 'pausado'/'cancelado'."""
    h = hashlib.sha256()
    lacunas = 0
    feitos = 0
    with open(tmp, 'wb') as saida:
        falta = achado['tamanho']
        for off, n in achados.extents_de(achado):
            while n > 0 and falta > 0:
                if ctrl.cancelamento_pedido():
                    return 'cancelado'
                if ctrl.pausa_pedida():
                    return 'pausado'
                pedaco = min(BLOCO, n, falta)
                try:
                    dados = aberto.ler(off, pedaco)
                    if len(dados) < pedaco:
                        raise OSError('leitura curta')
                except OSError:
                    dados, lacunas = bytes(pedaco), lacunas + 1
                saida.write(dados)
                h.update(dados)
                off, n, falta, feitos = off + pedaco, n - pedaco, falta - pedaco, feitos + pedaco
        saida.flush()
        os.fsync(saida.fileno())
    return ('com_falhas' if lacunas else 'ok'), feitos, h.hexdigest()


def _sha_arquivo(caminho):
    h = hashlib.sha256()
    with open(caminho, 'rb') as f:
        for b in iter(lambda: f.read(BLOCO), b''):
            h.update(b)
    return h.hexdigest()


def executar(job, ctrl):
    store, job_id = ctrl.store, job['id']
    origem, destino = job['origem'], job['destino']
    erro = validar(origem, destino, job.get('opcoes'), retomada=True)
    if erro:
        raise ErroJob(erro)
    cp = store.obter_checkpoint(job_id)
    ids = cp.get('ids') if cp else None
    if not isinstance(ids, list):
        raise ErroJob('A lista de arquivos a recuperar se perdeu. Peça a recuperação de novo.')
    try:
        os.makedirs(destino, exist_ok=True)
    except OSError as e:
        raise ErroJob('Não consegui criar a pasta de destino: %s' % e.strerror) from None
    con = store._con()
    linhas = {l['achado_id']: dict(l) for l in con.execute('SELECT * FROM recuperacoes WHERE job_id=?', (job_id,))}
    usados = {os.path.basename(l['arquivo']).casefold() for l in linhas.values() if l['arquivo']}
    total = 0
    for i in range(0, len(ids), 500):
        parte = ids[i:i + 500]
        total += con.execute('SELECT COALESCE(SUM(tamanho),0) FROM achados WHERE id IN (%s)' % ','.join('?' * len(parte)),
                             parte).fetchone()[0]
    feitos = sum(l['bytes'] for l in linhas.values() if l['resultado'] != 'copiando')
    try:
        aberto = leitor.abrir(origem)                            # única abertura da origem: só leitura
    except ErroLeitor as e:
        raise ErroJob(str(e)) from None
    pronto = lambda l: l is not None and l['resultado'] in RESULTADOS
    with aberto:
        ctrl.evento('Recuperando %d arquivos para %s.' % (len(ids), destino))
        ctrl.progresso(feitos, total, 'Recuperando: %d de %d' % (sum(pronto(l) for l in linhas.values()), len(ids)))
        for ordem, achado_id in enumerate(ids):
            if ctrl.cancelamento_pedido():
                return 'cancelado'
            if ctrl.pausa_pedida():
                return 'pausado'
            if pronto(linhas.get(achado_id)):
                continue
            achado = achados.obter(store, achado_id) if type(achado_id) is int else None
            if achado is None or achado['job_id'] != job['opcoes'].get('varredura'):
                con.execute('INSERT OR REPLACE INTO recuperacoes (job_id, achado_id, ordem, resultado, quando)'
                            ' VALUES (?,?,?,?,?)', (job_id, achado_id if type(achado_id) is int else -1, ordem, 'sumiu', time.time()))
                continue
            pasta_rel = PASTAS.get(achado['grupo'], PASTAS['outro'])
            previa = linhas.get(achado_id)
            if previa and previa['arquivo']:
                rel = previa['arquivo']
            else:
                nome = nome_unico(os.path.join(destino, pasta_rel), nome_seguro(achado['nome']), usados)
                rel = pasta_rel + '/' + nome
                usados.add(nome.casefold())
            final = os.path.join(destino, *rel.split('/'))
            con.execute('INSERT OR REPLACE INTO recuperacoes (job_id, achado_id, ordem, arquivo, resultado, quando)'
                        ' VALUES (?,?,?,?,?,?)', (job_id, achado_id, ordem, rel, 'copiando', time.time()))
            tmp = final + '.parte'
            try:
                os.makedirs(os.path.dirname(final), exist_ok=True)
                r = _copiar(aberto, achado, tmp, ctrl)
                if isinstance(r, str):
                    _remover(tmp)
                    return r
                resultado, n, sha = r
                if os.path.exists(final):
                    # sobrou de uma queda entre o rename e o registro: se for idêntico, vale; senão, outro nome
                    if _sha_arquivo(final) == sha:
                        _remover(tmp)
                    else:
                        nome = nome_unico(os.path.dirname(final), os.path.basename(final), usados)
                        usados.add(nome.casefold())
                        rel = pasta_rel + '/' + nome
                        final = os.path.join(destino, *rel.split('/'))
                        os.replace(tmp, final)
                else:
                    os.replace(tmp, final)
            except OSError as e:
                _remover(tmp)
                raise ErroJob('Não consegui gravar %s no destino: %s' % (rel, e.strerror or e)) from None
            agora = time.time()
            con.execute('BEGIN IMMEDIATE')
            try:
                con.execute('UPDATE recuperacoes SET arquivo=?, resultado=?, bytes=?, sha256=?, quando=? '
                            'WHERE job_id=? AND achado_id=?', (rel, resultado, n, sha, agora, job_id, achado_id))
                con.execute('UPDATE achados SET recuperado_em=? WHERE id=?', (agora, achado_id))
                con.execute('COMMIT')
            except BaseException:
                con.execute('ROLLBACK')
                raise
            feitos += n
            ctrl.progresso(feitos, total, 'Recuperando: %d de %d' % (ordem + 1, len(ids)))
            ctrl.checkpoint(None)
            if config.atraso_bloco():
                time.sleep(config.atraso_bloco())          # só para os testes
    nomes = escrever_relatorio(store, job)
    store.salvar_checkpoint(job_id, dict(store.obter_checkpoint(job_id) or {}, relatorio=nomes))
    ctrl.evento('Recuperação concluída. Relatório: %s.' % ', '.join(nomes))
    return 'concluido'


def _remover(caminho):
    try:
        os.remove(caminho)
    except OSError:
        pass


# --- relatório -------------------------------------------------------------------------------------------------
def _celula(v):
    """Texto seguro para planilha: célula que começa com = + - @ vira texto."""
    v = '' if v is None else str(v)
    return "'" + v if v[:1] in ('=', '+', '-', '@', '\t', '\r') else v


def linhas_do_relatorio(store, job):
    sql = ('SELECT r.ordem, r.arquivo, r.resultado, r.bytes, r.sha256, a.nome, a.caminho, a.grupo, a.tipo, a.estado, '
           'a.pontuacao, a.tamanho, a.data FROM recuperacoes r LEFT JOIN achados a ON a.id=r.achado_id '
           'WHERE r.job_id=? ORDER BY r.ordem')
    return [dict(l) for l in store._con().execute(sql, (job['id'],))]


def _criar_exclusivo(destino, base, ext, job_id):
    for sufixo in ('', '-' + job_id) + tuple('-%s-%d' % (job_id, n) for n in range(2, 50)):
        caminho = os.path.join(destino, base + sufixo + ext)
        try:
            return caminho, open(caminho, 'x', encoding='utf-8', newline='')
        except FileExistsError:
            continue
    raise ErroJob('Não consegui criar o relatório no destino.')


def escrever_relatorio(store, job):
    """Grava relatorio.html e relatorio.csv na pasta de destino (nunca por cima de um arquivo existente). Devolve os nomes."""
    linhas = linhas_do_relatorio(store, job)
    destino = job['destino']
    nomes = []
    caminho, f = _criar_exclusivo(destino, 'relatorio', '.csv', job['id'])
    with f:
        f.write('﻿')
        w = csv.writer(f)
        w.writerow(['arquivo_recuperado', 'nome', 'caminho_original', 'grupo', 'tipo', 'estado', 'pontuacao', 'tamanho',
                    'bytes_gravados', 'resultado', 'sha256', 'data'])
        for l in linhas:
            w.writerow([_celula(x) for x in (l['arquivo'], l['nome'], l['caminho'], l['grupo'], l['tipo'], l['estado'],
                                             l['pontuacao'], l['tamanho'], l['bytes'], RESULTADOS.get(l['resultado'], l['resultado']),
                                             l['sha256'], l['data'])])
    nomes.append(os.path.basename(caminho))
    caminho, f = _criar_exclusivo(destino, 'relatorio', '.html', job['id'])
    with f:
        f.write(_html(linhas, job))
    nomes.append(os.path.basename(caminho))
    return nomes


def _tam(n):
    n = float(n or 0)
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or u == 'TB':
            return ('%d %s' % (n, u)) if u == 'B' else ('%.1f %s' % (n, u))
        n /= 1024


def _html(linhas, job):
    e = html.escape
    por_estado = {}
    for l in linhas:
        por_estado[l['estado']] = por_estado.get(l['estado'], 0) + 1
    resumo = ', '.join('%d %s' % (n, ROTULO_ESTADO.get(k, k or '?')) for k, n in sorted(por_estado.items(), key=lambda x: str(x[0])))
    out = io.StringIO()
    out.write('<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Relatório de recuperação</title>'
              '<style>' + CSS_RELATORIO + '</style>')
    out.write('<h1>Relatório de recuperação</h1><p>%d arquivos (%s). Total gravado: %s. Trabalho %s.</p>'
              % (len(linhas), e(resumo or 'nenhum'), _tam(sum(l['bytes'] or 0 for l in linhas)), e(job['id'])))
    out.write('<table><tr><th>Arquivo</th><th>Tipo</th><th>Estado</th><th>Pontuação</th><th class="num">Tamanho</th>'
              '<th>Resultado</th><th>SHA-256</th></tr>')
    for l in linhas:
        out.write('<tr><td>%s</td><td>%s</td><td class="%s">%s</td><td>%s</td><td class="num">%s</td><td>%s</td><td>%s</td></tr>'
                  % (e(l['arquivo'] or ''), e(l['tipo'] or ''), e(l['estado'] or ''), e(ROTULO_ESTADO.get(l['estado'], l['estado'] or '')),
                     e('' if l['pontuacao'] is None else str(l['pontuacao'])), _tam(l['bytes'] or l['tamanho']),
                     e(RESULTADOS.get(l['resultado'], l['resultado'])), e(l['sha256'] or '')))
    out.write('</table><p>Estado: <b>bom</b> = o arquivo está inteiro e coerente; <b>parcial</b> = só o começo é confiável; '
              '<b>ruim</b> = inteiro, mas o conteúdo falhou numa conferência. Confira sempre abrindo os arquivos.</p>')
    return out.getvalue()
