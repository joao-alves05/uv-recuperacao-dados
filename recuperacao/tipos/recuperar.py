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
import shutil
import time

from .. import achados, config, leitor, protecao, relatorio, setores_ruins, unidade
from ..executor import ErroJob
from ..leitor import ErroLeitor
from . import imagem

PASTAS = {'foto': 'Fotos', 'video': 'Videos', 'audio': 'Audios', 'documento': 'Documentos',
          'compactado': 'Compactados', 'banco': 'Bancos de dados', 'outro': 'Outros'}
BLOCO = 1024 * 1024
MAX_NOME = 120
_PROIBIDOS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESULTADOS = {'ok': 'ok', 'com_falhas': 'com trechos ilegíveis (zerados)', 'sumiu': 'achado não encontrado',
              'falhou': 'falhou ao gravar'}
CSS_RELATORIO = relatorio.CSS
ROTULO_ESTADO = {'bom': 'bom', 'parcial': 'parcial', 'ruim': 'ruim'}


def espaco_livre(destino):
    """Bytes livres no disco onde o destino ficará (olha a pasta existente mais próxima); None se não der para saber."""
    pasta = os.path.abspath(destino)
    for _ in range(64):
        if os.path.isdir(pasta):
            break
        pai = os.path.dirname(pasta)
        if pai == pasta:
            break
        pasta = pai
    try:
        return shutil.disk_usage(pasta).free
    except OSError:
        return None


def validar(origem, destino, opcoes=None, retomada=False):
    if not isinstance(origem, str) or not origem.strip() or '\x00' in origem:
        return 'Informe a origem.'
    if not isinstance(destino, str) or not destino.strip() or '\x00' in destino:
        return 'Informe a pasta de destino.'
    erro = imagem.caminho_proibido(destino) or protecao.destino_proibido(destino)
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
    ruins = getattr(aberto, 'ruins', None) or []         # faixas não lidas do mapa da imagem (fase 6)
    aberto.lacunas_do_arquivo = perdidos = []            # [(deslocamento no arquivo, bytes)] que são zeros por falta de leitura
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
                    perdidos.append((feitos, pedaco))
                else:
                    for a, b in setores_ruins.cortes(ruins, off, pedaco):
                        lacunas += 1
                        perdidos.append((feitos + a - off, b - a))
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
    pronto = lambda l: l is not None and l['resultado'] in RESULTADOS and l['resultado'] != 'falhou'     # falhou: tenta de novo ao retomar
    with aberto:
        aberto.ruins = setores_ruins.carregar(origem)           # origem que é imagem com mapa: setores ruins viram lacunas
        ctrl.evento('Recuperando %d arquivos para %s.' % (len(ids), destino))
        livre = espaco_livre(destino)
        if livre is not None and livre < total - feitos:
            ctrl.evento('Atenção: o destino tem %s livres e faltam gravar %s; alguns arquivos podem falhar por falta de espaço.'
                        % (_tam(livre), _tam(total - feitos)))
        falhas = 0
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
                ctrl.log('Achado %s não encontrado: pulado.' % (achado_id,))
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
                # um arquivo que não grava (disco cheio, nome inválido, permissão) não derruba o trabalho
                _remover(tmp)
                motivo = str(e.strerror or e)[:200]
                ctrl.log('Falhou ao gravar %s (achado %d): %s' % (rel, achado_id, motivo))
                con.execute("UPDATE recuperacoes SET resultado='falhou', motivo=?, bytes=0, quando=? "
                            "WHERE job_id=? AND achado_id=?", (motivo, time.time(), job_id, achado_id))
                ctrl.evento('Não consegui gravar %s: %s' % (rel, motivo))
                falhas += 1
                ctrl.progresso(feitos, total, 'Recuperando: %d de %d' % (ordem + 1, len(ids)))
                continue
            agora = time.time()
            con.execute('BEGIN IMMEDIATE')
            try:
                con.execute('UPDATE recuperacoes SET arquivo=?, resultado=?, bytes=?, sha256=?, quando=? '
                            'WHERE job_id=? AND achado_id=?', (rel, resultado, n, sha, agora, job_id, achado_id))
                con.execute('UPDATE achados SET recuperado_em=? WHERE id=?', (agora, achado_id))
                if resultado == 'com_falhas':
                    con.execute("UPDATE achados SET estado='parcial' WHERE id=? AND estado='bom'", (achado_id,))
                con.execute('COMMIT')
            except BaseException:
                con.execute('ROLLBACK')
                raise
            if aberto.lacunas_do_arquivo:
                try:
                    setores_ruins.anotar(destino, rel, aberto.lacunas_do_arquivo)
                except OSError:
                    ctrl.evento('Não consegui gravar lacunas.csv no destino.')
            ctrl.log('%s: %s, %d bytes, achado %d (bytes %s da origem), sha256 %s.' % (
                rel, resultado, n, achado_id, ' + '.join('%d..%d' % (o, o + c) for o, c in achados.extents_de(achado)[:20]), sha))
            feitos += n
            ctrl.progresso(feitos, total, 'Recuperando: %d de %d' % (ordem + 1, len(ids)))
            ctrl.checkpoint(None)
            if config.atraso_bloco():
                time.sleep(config.atraso_bloco())          # só para os testes
    nomes = escrever_relatorio(store, job)
    store.salvar_checkpoint(job_id, dict(store.obter_checkpoint(job_id) or {}, relatorio=nomes))
    if falhas:
        ctrl.evento('%d arquivos não puderam ser gravados (veja o motivo no relatório). Peça de novo para tentar só esses.' % falhas)
    ctrl.evento('Recuperação concluída. Relatório: %s.' % ', '.join(nomes))
    return 'concluido'


def _remover(caminho):
    try:
        os.remove(caminho)
    except OSError:
        pass


# --- relatório -------------------------------------------------------------------------------------------------
_celula = relatorio.celula
_tam = relatorio.tam


def linhas_do_relatorio(store, job):
    sql = ('SELECT r.ordem, r.arquivo, r.resultado, r.bytes, r.sha256, r.motivo, a.nome, a.caminho, a.grupo, a.tipo, a.estado, '
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
                    'bytes_gravados', 'resultado', 'motivo', 'sha256', 'data'])
        for l in linhas:
            w.writerow([_celula(x) for x in (l['arquivo'], l['nome'], l['caminho'], l['grupo'], l['tipo'], l['estado'],
                                             l['pontuacao'], l['tamanho'], l['bytes'], RESULTADOS.get(l['resultado'], l['resultado']),
                                             l['motivo'], l['sha256'], l['data'])])
    nomes.append(os.path.basename(caminho))
    caminho, f = _criar_exclusivo(destino, 'relatorio', '.html', job['id'])
    with f:
        f.write(_html(linhas, job))
    nomes.append(os.path.basename(caminho))
    return nomes


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
                     e(RESULTADOS.get(l['resultado'], l['resultado']) + (': ' + l['motivo'] if l['motivo'] else '')),
                     e(l['sha256'] or '')))
    out.write('</table><p>Estado: <b>bom</b> = o arquivo está inteiro e coerente; <b>parcial</b> = só o começo é confiável; '
              '<b>ruim</b> = inteiro, mas o conteúdo falhou numa conferência. Confira sempre abrindo os arquivos.</p>')
    return out.getvalue()
