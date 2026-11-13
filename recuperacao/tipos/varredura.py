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
import struct
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
            'sistema_antes': o.get('sistema_antes', False), 'offset': o.get('offset', 0), 'tamanho': o.get('tamanho')}


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
        return _erro_fatia(o)                               # limites e setor: conferidos ao abrir o disco
    erro = imagem.caminho_proibido(origem)
    if erro:
        return erro
    if not os.path.isfile(origem):
        return 'A origem não existe ou não é um arquivo nem um disco.'
    return _erro_fatia(o, os.path.getsize(origem))


def _erro_fatia(o, total=None, setor=512):
    """Frase de erro se `offset`/`tamanho` (bytes, para varrer só uma partição) não servem; None se ok. Com `total`
    (tamanho da origem) confere os limites e o alinhamento ao setor."""
    off, tam = o['offset'], o['tamanho']
    for nome, v in (('offset', off), ('tamanho', tam)):
        if v is not None and (type(v) is not int or v < 0):
            return 'A opção "%s" precisa ser um número inteiro de bytes, 0 ou mais.' % nome
    if tam == 0:
        return 'A opção "tamanho" precisa ser maior que zero.'
    if off == 0 and tam is None:
        return None
    if total is None:
        return None
    fim = total if tam is None else off + tam
    if off >= total or fim > total:
        return 'O trecho pedido (offset %d, tamanho %s) não cabe na origem, que tem %d bytes.' % (
            off, 'até o fim' if tam is None else tam, total)
    if off % setor or (fim != total and fim % setor):
        return 'O offset e o tamanho precisam ser múltiplos do setor (%d bytes).' % setor
    return None


MOTIVOS = {'sem_conteudo': 'arquivos apagados só com o nome (o sistema de arquivos apagou os ponteiros do conteúdo, '
                           'como ext3 e ext4 fazem; a busca por assinatura pode achar o conteúdo)',
           'invalido': 'arquivos com posição inválida no disco (estrutura danificada)'}


def _offsets_com_nome(store, job_id):
    """Onde começam (na origem) os arquivos que a lista pelo sistema de arquivos já achou; o carving ignora esses."""
    return {l[0] for l in store._con().execute(
        'SELECT offset FROM achados WHERE job_id=? AND caminho IS NOT NULL AND tamanho > 0', (job_id,))}


def _aviso_lista_incompleta(motivo, op):
    texto = 'a lista pelo sistema de arquivos ficou incompleta: %s.' % motivo
    if op['modo'] == 'sistema':
        texto += ' Rode a busca por assinatura (rápida ou profunda) para achar o que faltou.'
    return texto


def _fase_sistema(ctrl, store, job_id, aberto, op, cp, tamanho, janela=None, resultado=None):
    """Lista os arquivos pelos sistemas de arquivos da origem (rápido, com nome e caminho, apagados incluídos) e grava
    em `achados`. Devolve 'pausado'/'cancelado' se parou no meio, ou None se terminou. O ponto de retomada é quantas
    entradas já foram consumidas (a ordem de `arquivos_sa.entradas` é fixa), então retomar não repete achado."""
    resultado = resultado if resultado is not None else {}
    retomando = bool(cp) and cp.get('fase') == 'sistema'
    ja = int(cp.get('feitos', 0)) if retomando else 0
    pulados = dict(cp.get('pulados') or {}) if retomando else {}
    try:
        # janela = (offset, tamanho) de uma partição já escolhida: só o sistema de arquivos dela, sem ler a tabela do disco
        lista = arquivos_sa.volumes(aberto, *janela) if janela else arquivos_sa.volumes(aberto)
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
            'ruins': 0, 'sistema_antes': op['sistema_antes'], 'offset': op.get('offset', 0),
            'janela': janela[1] if janela else tamanho, 'lista_incompleta': resultado.get('incompleta')})
        pendentes.clear()

    antes, soma = {}, 0
    for va in lista:
        antes[id(va)] = soma
        soma += va.volume.tamanho
    est['agora'] = time.monotonic()

    def lendo(va, fracao):
        """Chamado pelo volume enquanto lê as estruturas dele (NTFS: a cada pedaço da MFT), mesmo sem entrada nova."""
        ctrl.progresso(int(antes[id(va)] + fracao * va.volume.tamanho), total_bytes,
                       'Lendo a lista de arquivos: %d achados' % total)
        if ctrl.cancelamento_pedido() or ctrl.pausa_pedida():
            raise arquivos_sa.Parar()

    ctrl.progresso(0, total_bytes, 'Lendo a lista de arquivos: %d achados' % total)
    try:
        for ent, va in arquivos_sa.entradas(aberto, lista, ao_ler=lendo):
            est['n'] += 1
            if est['n'] <= ja:
                continue
            a, motivo = arquivos_sa.achado_de(ent, va.prefixo)
            if a is not None:
                pendentes.append(a)
                total += 1
            elif motivo != 'pasta':
                pulados[motivo] = pulados.get(motivo, 0) + 1
            if (len(pendentes) >= LOTE_SISTEMA or est['n'] % (4 * LOTE_SISTEMA) == 0
                    or (pendentes and time.monotonic() - est['agora'] >= 1.0)):
                est['agora'] = time.monotonic()
                gravar()
                ctrl.progresso(0, total_bytes, 'Lendo a lista de arquivos: %d achados' % total)
                ctrl.checkpoint(None, forcar=True)
                if ctrl.cancelamento_pedido() or ctrl.pausa_pedida():
                    return 'cancelado' if ctrl.cancelamento_pedido() else 'pausado'
    except arquivos_sa.Parar:
        gravar()
        ctrl.checkpoint(None, forcar=True)
        return 'cancelado' if ctrl.cancelamento_pedido() else 'pausado'
    except (arquivos_sa.sa.ErroSA, OSError, struct.error, IndexError, ValueError) as e:   # disco hostil/estrutura quebrada
        resultado['incompleta'] = 'a leitura parou no meio (%s)' % e
        ctrl.evento('A lista pelo sistema de arquivos parou: %s' % e)
    ilegiveis = sum(va.volume.ilegiveis for va in lista)
    resultado['ilegiveis'] = ilegiveis
    if ilegiveis and not resultado.get('incompleta'):
        resultado['incompleta'] = '%d setores do sistema de arquivos estavam ilegíveis' % ilegiveis
    gravar()
    if resultado.get('incompleta'):
        ctrl.aviso_final = _aviso_lista_incompleta(resultado['incompleta'], op)
        ctrl.evento('ATENÇÃO: ' + ctrl.aviso_final)
    ctrl.progresso(total_bytes, total_bytes, 'Lista de arquivos pronta: %d achados' % total, ilegiveis)
    ctrl.checkpoint(None, forcar=True)                      # o total de setores ilegíveis tem de chegar ao job
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
        erro = _erro_fatia(op, tamanho, max(1, getattr(aberto, 'setor', 512)))
        if erro:
            raise ErroJob(erro)
        fatia = None
        if op['offset'] or op['tamanho'] is not None:        # só uma partição: o resto da origem nem é lido
            fatia = carving.Fatia(aberto, op['offset'], tamanho - op['offset'] if op['tamanho'] is None else op['tamanho'])
        alvo = fatia.tamanho if fatia else tamanho            # quantos bytes esta varredura percorre
        cp = store.obter_checkpoint(job_id)
        inicio, ruins, incompleta = 0, 0, None
        if cp:
            if (cp.get('tamanho') != tamanho or cp.get('modo') != op['modo'] or cp.get('offset', 0) != op['offset']
                    or cp.get('janela', alvo) != alvo):
                raise ErroJob('A origem ou o modo mudaram desde que a varredura começou. Por segurança, nada foi alterado.')
        if op['modo'] == 'sistema' or op['sistema_antes']:
            if not cp or cp.get('fase') == 'sistema':        # a fase do sistema de arquivos vem antes do carving
                lista = {}
                resultado = _fase_sistema(ctrl, store, job_id, aberto, op, cp, tamanho,
                                          (fatia.deslocamento, fatia.tamanho) if fatia else None, lista)
                if resultado or op['modo'] == 'sistema':
                    return resultado or 'concluido'
                cp, ruins = None, lista.get('ilegiveis', 0)
                incompleta = lista.get('incompleta')
        if cp:
            inicio, ruins = int(cp.get('pos', 0)), int(cp.get('ruins', 0))
            incompleta = cp.get('lista_incompleta')
            if incompleta:
                ctrl.aviso_final = _aviso_lista_incompleta(incompleta, op)
            ctrl.evento('Retomando a varredura a partir do byte %d (%d arquivos já achados).' % (inicio, achados.contar(store, job_id)))
        else:
            ctrl.evento('Varredura %s: %d bytes%s, %d tipos de arquivo.'
                        % ('rápida' if op['modo'] == 'rapida' else 'profunda', alvo,
                           ' (a partir do byte %d da origem)' % op['offset'] if fatia else '', len(catalogo.assinaturas)))
        varredor = carving.Varredor(fatia or aberto, catalogo, op['modo'], op['bloco'])
        total = achados.contar(store, job_id)
        com_nome = _offsets_com_nome(store, job_id) if op['sistema_antes'] else set()
        repetidos = 0                                       # achados do carving que já estavam na lista com nome
        pendentes = []
        est = {'pos': inicio, 'ruins': ruins}
        atraso = config.atraso_bloco()

        def gravar():
            store.adicionar_achados(job_id, pendentes, checkpoint={
                'fase': 'carving', 'pos': est['pos'], 'tamanho': tamanho, 'modo': op['modo'], 'ruins': est['ruins'],
                'sistema_antes': op['sistema_antes'], 'offset': op.get('offset', 0), 'janela': alvo,
                'lista_incompleta': incompleta})
            pendentes.clear()

        def texto():
            return 'Procurando arquivos: %d achados' % total

        ctrl.progresso(inicio, alvo, texto(), ruins)
        for pos, novos, ruins_bloco in varredor.blocos(inicio):
            if com_nome and novos:
                restam = [a for a in novos if a.get('offset') not in com_nome]
                repetidos += len(novos) - len(restam)
                novos = restam
            pendentes.extend(novos)
            total += len(novos)
            for a in novos:
                ctrl.log('Achado %s em %d: %d bytes, %s (pontuação %s).' % (a['tipo'], a['offset'], a['tamanho'], a['estado'], a['pontuacao']))
            if ruins_bloco:
                ctrl.log('%d setores ilegíveis perto do byte %d: lidos como zeros.' % (ruins_bloco, pos))
            est['pos'], est['ruins'] = pos, est['ruins'] + ruins_bloco
            ctrl.progresso(pos, alvo, texto(), est['ruins'])
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
        est['pos'] = alvo
        gravar()
        ctrl.progresso(alvo, alvo, texto(), est['ruins'])
        ctrl.checkpoint(None, forcar=True)
    por_grupo = achados.contar_por_grupo(store, job_id)
    resumo = ', '.join('%d %s' % (n, NOMES_GRUPO.get(g, g)) for g, n in sorted(por_grupo.items(), key=lambda x: -x[1]))
    ctrl.evento('Varredura concluída: %d arquivos achados%s.' % (total, ' (%s)' % resumo if resumo else ''))
    if repetidos:
        ctrl.evento('%d achados da busca por assinatura eram arquivos que a lista pelo sistema de arquivos já tinha '
                    '(mesmo ponto de início) e ficaram só com o nome.' % repetidos)
    if varredor.erros_validador:
        ctrl.evento('%d candidatos foram descartados por dados fora do padrão.' % varredor.erros_validador)
    if est['ruins']:
        ctrl.evento('%d setores não puderam ser lidos e foram tratados como zeros.' % est['ruins'])
    return 'concluido'
