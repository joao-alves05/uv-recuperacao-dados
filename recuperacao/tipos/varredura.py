# -*- coding: utf-8 -*-
"""Tipo `varredura`: procura arquivos numa origem (arquivo, imagem, disco ou volume) pelo conteúdo (carving).

    POST /api/jobs {"tipo": "varredura", "origem": "...", "opcoes": {"modo": "rapida" | "profunda"}}

Não escreve nada: cada achado vai para a tabela `achados` assim que o bloco em que foi descoberto termina, e o ponto
de retomada (`checkpoints`) é gravado na mesma transação. Pausar, cair o serviço e retomar continua do último ponto,
sem repetir achado. A origem só é aberta pelo Leitor (somente leitura).

Este job não tem arquivo de saída; o `destino` do job é um identificador interno (`varredura#<hash da origem>`), que
só serve para impedir duas varreduras da mesma origem ao mesmo tempo.
"""
import hashlib
import os
import time

from .. import achados, arquivos_sa, carving, config, leitor
from ..executor import ErroJob
from ..leitor import ErroLeitor
from . import imagem

MODO_PADRAO = 'rapida'
MODOS = ('sistema',) + carving.MODOS     # `sistema` = pelo sistema de arquivos (rápido, com nomes); o resto é carving
LOTE_SISTEMA = 500
NOMES_GRUPO = {'foto': 'fotos', 'video': 'vídeos', 'audio': 'áudios', 'documento': 'documentos',
               'compactado': 'compactados', 'banco': 'bancos de dados', 'outro': 'outros'}


def destino_de(origem, destino=None):
    if not isinstance(origem, str) or not origem.strip() or '\x00' in origem:
        return destino
    return 'varredura#' + hashlib.sha1(imagem._normal(origem).encode('utf-8', 'surrogatepass')).hexdigest()[:16]


def opcoes_de(opcoes):
    o = opcoes or {}
    return {'modo': o.get('modo', MODO_PADRAO), 'bloco': o.get('bloco', carving.BLOCO_PADRAO),
            'sistema_antes': o.get('sistema_antes', False)}


def validar(origem, destino, opcoes, retomada=False):
    """Frase de erro (português) ou None se está tudo certo. O destino é ignorado."""
    if not isinstance(origem, str) or not origem.strip() or '\x00' in origem:
        return 'Informe a origem: o disco, o volume ou o arquivo onde procurar.'
    if opcoes is not None and not isinstance(opcoes, dict):
        return 'As opções precisam ser um objeto.'
    o = opcoes_de(opcoes)
    if o['modo'] not in MODOS:
        return 'O modo precisa ser "sistema", "rapida" ou "profunda".'
    if type(o['sistema_antes']) is not bool:
        return 'A opção "sistema_antes" precisa ser verdadeiro ou falso.'
    if type(o['bloco']) is not int or not imagem.BLOCO_MIN <= o['bloco'] <= imagem.BLOCO_MAX:
        return 'O tamanho do bloco precisa ser um número entre %d e %d bytes.' % (imagem.BLOCO_MIN, imagem.BLOCO_MAX)
    if leitor.eh_dispositivo(origem):
        if leitor.eh_optico(origem):
            return 'A leitura completa de CD/DVD chega numa versão futura. Por enquanto só discos, volumes e arquivos.'
        return None
    erro = imagem.caminho_proibido(origem)
    if erro:
        return erro
    if not os.path.isfile(origem):
        return 'A origem não existe ou não é um arquivo nem um disco.'
    return None


MOTIVOS = {'sem_conteudo': 'arquivos apagados só com o nome (o sistema de arquivos apagou os ponteiros do conteúdo, '
                           'como ext3 e ext4 fazem; a busca por assinatura pode achar o conteúdo)',
           'esparso': 'arquivos esparsos (com buracos), que a lista ainda não representa'}


def _fase_sistema(ctrl, store, job_id, aberto, op, cp, tamanho):
    """Lista os arquivos pelos sistemas de arquivos da origem (rápido, com nome e caminho, apagados incluídos) e grava
    em `achados`. Devolve 'pausado'/'cancelado' se parou no meio, ou None se terminou. O ponto de retomada é quantas
    entradas já foram consumidas (a ordem de `arquivos_sa.entradas` é fixa), então retomar não repete achado."""
    retomando = bool(cp) and cp.get('fase') == 'sistema'
    ja = int(cp.get('feitos', 0)) if retomando else 0
    pulados = dict(cp.get('pulados') or {}) if retomando else {}
    try:
        lista = arquivos_sa.volumes(aberto)
    except OSError as e:
        raise ErroJob('Não consegui ler a origem para reconhecer o sistema de arquivos: %s' % e) from None
    if not lista:
        if op['modo'] == 'sistema':
            raise ErroJob('Não reconheci nenhum sistema de arquivos nesta origem. Use a varredura rápida ou profunda '
                          '(por assinatura), que acha os arquivos sem os nomes.')
        ctrl.evento('Nenhum sistema de arquivos reconhecido; seguindo para a busca por assinatura.')
        return None
    if not retomando:
        for va in lista:
            ctrl.evento('Sistema de arquivos achado: %s%s.' % (va.descricao, (' (' + va.prefixo.strip('/') + ')') if va.prefixo else ''))
            if va.volume.limitacao:
                ctrl.evento(va.volume.limitacao)
    else:
        ctrl.evento('Retomando a lista pelo sistema de arquivos (%d entradas já vistas).' % ja)
    est = {'n': 0}
    total = achados.contar(store, job_id)
    pendentes = []
    total_bytes = sum(va.volume.tamanho for va in lista) or tamanho

    def gravar():
        store.adicionar_achados(job_id, pendentes, checkpoint={
            'fase': 'sistema', 'feitos': est['n'], 'pulados': pulados, 'tamanho': tamanho, 'modo': op['modo'],
            'ruins': 0, 'sistema_antes': op['sistema_antes']})
        pendentes.clear()

    ctrl.progresso(0, total_bytes, 'Lendo a lista de arquivos: %d achados' % total)
    try:
        for ent, va in arquivos_sa.entradas(aberto, lista):
            est['n'] += 1
            if est['n'] <= ja:
                continue
            a, motivo = arquivos_sa.achado_de(ent, va.prefixo)
            if a is not None:
                pendentes.append(a)
                total += 1
            elif motivo != 'pasta':
                pulados[motivo] = pulados.get(motivo, 0) + 1
            if len(pendentes) >= LOTE_SISTEMA or est['n'] % (4 * LOTE_SISTEMA) == 0:
                gravar()
                ctrl.progresso(0, total_bytes, 'Lendo a lista de arquivos: %d achados' % total)
                ctrl.checkpoint(None, forcar=True)
                if ctrl.cancelamento_pedido() or ctrl.pausa_pedida():
                    return 'cancelado' if ctrl.cancelamento_pedido() else 'pausado'
    except (arquivos_sa.sa.ErroSA, OSError) as e:
        gravar()
        ctrl.evento('A lista pelo sistema de arquivos parou: %s' % e)
    gravar()
    ctrl.progresso(total_bytes, total_bytes, 'Lista de arquivos pronta: %d achados' % total)
    por_grupo = achados.contar_por_grupo(store, job_id)
    resumo = ', '.join('%d %s' % (n, NOMES_GRUPO.get(g, g)) for g, n in sorted(por_grupo.items(), key=lambda x: -x[1]))
    ctrl.evento('Lista pelo sistema de arquivos concluída: %d arquivos%s.' % (total, ' (%s)' % resumo if resumo else ''))
    for motivo, n in sorted(pulados.items()):
        ctrl.evento('%d %s não entraram na lista.' % (n, MOTIVOS.get(motivo, motivo)))
    return None


def executar(job, ctrl):
    origem, store, job_id = job['origem'], ctrl.store, job['id']
    erro = validar(origem, job['destino'], job.get('opcoes'))
    if erro:
        raise ErroJob(erro)
    op = opcoes_de(job.get('opcoes'))
    try:
        catalogo = carving.carregar()
    except carving.ErroAssinaturas as e:
        raise ErroJob(str(e)) from None
    try:
        aberto = leitor.abrir(origem)                       # única abertura da origem: só leitura
    except ErroLeitor as e:
        raise ErroJob(str(e)) from None
    with aberto:
        tamanho = aberto.tamanho
        cp = store.obter_checkpoint(job_id)
        inicio, ruins = 0, 0
        if cp:
            if cp.get('tamanho') != tamanho or cp.get('modo') != op['modo']:
                raise ErroJob('A origem ou o modo mudaram desde que a varredura começou. Por segurança, nada foi alterado.')
        if op['modo'] == 'sistema' or op['sistema_antes']:
            if not cp or cp.get('fase') == 'sistema':        # a fase do sistema de arquivos vem antes do carving
                resultado = _fase_sistema(ctrl, store, job_id, aberto, op, cp, tamanho)
                if resultado or op['modo'] == 'sistema':
                    return resultado or 'concluido'
                cp = None
        if cp:
            inicio, ruins = int(cp.get('pos', 0)), int(cp.get('ruins', 0))
            ctrl.evento('Retomando a varredura a partir do byte %d (%d arquivos já achados).' % (inicio, achados.contar(store, job_id)))
        else:
            ctrl.evento('Varredura %s: %d bytes, %d tipos de arquivo.'
                        % ('rápida' if op['modo'] == 'rapida' else 'profunda', tamanho, len(catalogo.assinaturas)))
        varredor = carving.Varredor(aberto, catalogo, op['modo'], op['bloco'])
        total = achados.contar(store, job_id)
        pendentes = []
        est = {'pos': inicio, 'ruins': ruins}
        atraso = config.atraso_bloco()

        def gravar():
            store.adicionar_achados(job_id, pendentes, checkpoint={
                'fase': 'carving', 'pos': est['pos'], 'tamanho': tamanho, 'modo': op['modo'], 'ruins': est['ruins'],
                'sistema_antes': op['sistema_antes']})
            pendentes.clear()

        def texto():
            return 'Procurando arquivos: %d achados' % total

        ctrl.progresso(inicio, tamanho, texto(), ruins)
        for pos, novos, ruins_bloco in varredor.blocos(inicio):
            pendentes.extend(novos)
            total += len(novos)
            est['pos'], est['ruins'] = pos, est['ruins'] + ruins_bloco
            ctrl.progresso(pos, tamanho, texto(), est['ruins'])
            if novos:
                gravar()                                    # o painel vê os achados enquanto a varredura anda
                ctrl.checkpoint(None, forcar=True)
            else:
                ctrl.checkpoint(gravar, bytes_novos=0)
            if ctrl.cancelamento_pedido() or ctrl.pausa_pedida():
                gravar()
                return 'cancelado' if ctrl.cancelamento_pedido() else 'pausado'
            if atraso:
                time.sleep(atraso)
        est['pos'] = tamanho
        gravar()
    por_grupo = achados.contar_por_grupo(store, job_id)
    resumo = ', '.join('%d %s' % (n, NOMES_GRUPO.get(g, g)) for g, n in sorted(por_grupo.items(), key=lambda x: -x[1]))
    ctrl.evento('Varredura concluída: %d arquivos achados%s.' % (total, ' (%s)' % resumo if resumo else ''))
    if varredor.erros_validador:
        ctrl.evento('%d candidatos foram descartados por dados fora do padrão.' % varredor.erros_validador)
    if est['ruins']:
        ctrl.evento('%d setores não puderam ser lidos e foram tratados como zeros.' % est['ruins'])
    return 'concluido'
