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

from .. import achados, carving, config, leitor
from ..executor import ErroJob
from ..leitor import ErroLeitor
from . import imagem

MODO_PADRAO = 'rapida'
NOMES_GRUPO = {'foto': 'fotos', 'video': 'vídeos', 'audio': 'áudios', 'documento': 'documentos',
               'compactado': 'compactados', 'banco': 'bancos de dados', 'outro': 'outros'}


def destino_de(origem, destino=None):
    if not isinstance(origem, str) or not origem.strip() or '\x00' in origem:
        return destino
    return 'varredura#' + hashlib.sha1(imagem._normal(origem).encode('utf-8', 'surrogatepass')).hexdigest()[:16]


def opcoes_de(opcoes):
    o = opcoes or {}
    return {'modo': o.get('modo', MODO_PADRAO), 'bloco': o.get('bloco', carving.BLOCO_PADRAO)}


def validar(origem, destino, opcoes, retomada=False):
    """Frase de erro (português) ou None se está tudo certo. O destino é ignorado."""
    if not isinstance(origem, str) or not origem.strip() or '\x00' in origem:
        return 'Informe a origem: o disco, o volume ou o arquivo onde procurar.'
    if opcoes is not None and not isinstance(opcoes, dict):
        return 'As opções precisam ser um objeto.'
    o = opcoes_de(opcoes)
    if o['modo'] not in carving.MODOS:
        return 'O modo precisa ser "rapida" ou "profunda".'
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
                'pos': est['pos'], 'tamanho': tamanho, 'modo': op['modo'], 'ruins': est['ruins']})
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
