# -*- coding: utf-8 -*-
"""Tipo `particoes`: procura partição perdida varrendo a origem atrás de assinaturas de sistema de arquivos.

A origem (arquivo, imagem, disco ou volume) é lida só pelo Leitor. O resultado vai para o destino, que é o caminho
de um arquivo `.json` em OUTRO disco:

    <destino>             a tabela atual, os candidatos (com confiança) e a tabela proposta
    <nome>.mbr            setor 0 de um MBR com a proposta (se couber), para o técnico aplicar à mão
    <nome>.gpt            setores 0.. de um GPT (MBR protetor, cabeçalho e entradas), idem
    <nome>.gpt-fim        entradas e cabeçalho reservas do GPT, para gravar no fim do disco
    <destino>.busca.json  checkpoint da varredura (some quando termina)

O programa nunca grava nada na origem: a tabela reconstruída só existe nesses arquivos.
"""
import json
import os
import time

from .. import config, leitor, particoes, protecao, unidade
from ..executor import ErroJob
from ..leitor import ErroLeitor
from . import imagem

BLOCO_MIN = 64 * 1024
BLOCO_MAX = 64 * 1024 * 1024
MAX_HITS = 50000                 # trava de segurança: dado cheio de falsos sinais não pode encher a memória
VERSAO = 1


def caminho_busca(destino):
    return destino + '.busca.json'


def _nomes(destino):
    base = destino[:-5]
    return {'mbr': base + '.mbr', 'gpt': base + '.gpt', 'gpt_fim': base + '.gpt-fim'}


def bloco_de(opcoes):
    return (opcoes or {}).get('bloco', particoes.BLOCO_BUSCA)


def validar(origem, destino, opcoes, retomada=False):
    """Devolve a frase de erro (português) ou None se está tudo certo."""
    if not isinstance(origem, str) or not origem.strip() or '\x00' in origem:
        return 'Informe a origem: o disco, a imagem ou o arquivo onde procurar partições.'
    if not isinstance(destino, str) or not destino.strip() or '\x00' in destino:
        return 'Informe o destino: o caminho do arquivo .json da tabela reconstruída.'
    if opcoes is not None and not isinstance(opcoes, dict):
        return 'As opções precisam ser um objeto.'
    bloco = bloco_de(opcoes)
    if type(bloco) is not int or not BLOCO_MIN <= bloco <= BLOCO_MAX or bloco % 4096:
        return 'O bloco da busca precisa ser um número entre %d e %d bytes, múltiplo de 4096.' % (BLOCO_MIN, BLOCO_MAX)
    if not destino.lower().endswith('.json') or len(destino) <= 5:
        return 'O destino precisa ser um arquivo com extensão .json.'
    erro = imagem.caminho_proibido(destino) or protecao.destino_proibido(destino)
    if erro:
        return erro
    pasta = os.path.dirname(os.path.abspath(destino))
    if not os.path.isdir(pasta):
        return 'A pasta do destino não existe.'
    if leitor.eh_dispositivo(origem):
        if leitor.eh_optico(origem):
            return 'A leitura de CD/DVD chega numa versão futura. Por enquanto só discos, volumes e arquivos.'
        erro = unidade.conferir(origem, destino)           # nunca gravar no disco que está sendo examinado
        if erro:
            return erro
    else:
        erro = imagem.caminho_proibido(origem)
        if erro:
            return erro
        if not os.path.isfile(origem):
            return 'A origem não existe ou não é um arquivo nem um disco.'
        o, d = imagem._normal(origem), imagem._normal(destino)
        if o == d or any(imagem._normal(p) == o for p in _nomes(destino).values()) or o == imagem._normal(caminho_busca(destino)):
            return 'O destino não pode ser a própria origem.'
    if not retomada:
        for p in [destino, caminho_busca(destino)] + list(_nomes(destino).values()):
            if os.path.exists(p):
                return 'Já existe um arquivo no destino (%s). Escolha outro nome para não sobrescrever nada.' % os.path.basename(p)
    return None


def _gravar(caminho, dados):
    """Grava por um arquivo temporário e troca de nome: ou fica o arquivo antigo, ou o novo inteiro."""
    tmp = caminho + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(dados)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, caminho)


def _gravar_json(caminho, obj):
    _gravar(caminho, json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8'))


def _carregar_busca(caminho, origem_real, tamanho):
    try:
        with open(caminho, encoding='utf-8') as f:
            d = json.load(f)
        ok = d['origem'] == origem_real and d['tamanho'] == tamanho and type(d['pos']) is int and 0 <= d['pos'] <= tamanho \
            and isinstance(d['hits'], list) and type(d['ruins']) is int
    except (OSError, ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        raise ErroJob('O ponto de retomada desta busca não confere com a origem (o arquivo mudou ou está danificado). '
                      'Escolha outro nome de destino e comece de novo.')
    return d


def montar_resultado(origem, l, setor, atual, candidatos, proposta, ruins, nomes):
    """O conteúdo do .json e os arquivos binários da tabela proposta: (dict, {chave: bytes})."""
    inicios = {p['inicio'] for p in atual['particoes']}
    for c in candidatos:
        c['na_tabela_atual'] = c['inicio'] in inicios
    binarios, aplicacao = {}, {}
    mbr, av_mbr = particoes.montar_mbr(proposta, l.tamanho, setor)
    if mbr:
        binarios['mbr'] = mbr
        aplicacao['mbr'] = {'arquivo': os.path.basename(nomes['mbr']), 'gravar_em_byte': 0, 'bytes': len(mbr),
                            'avisos': av_mbr + ['Sem código de boot: serve para disco de dados.']}
    else:
        aplicacao['mbr'] = {'arquivo': None, 'avisos': av_mbr}
    ini, fim, av_gpt = particoes.montar_gpt(proposta, l.tamanho, setor, semente=origem)
    if ini:
        n = len(fim) // setor - 1
        binarios['gpt'], binarios['gpt_fim'] = ini, fim
        aplicacao['gpt'] = {'arquivo': os.path.basename(nomes['gpt']), 'gravar_em_byte': 0, 'bytes': len(ini), 'avisos': av_gpt}
        aplicacao['gpt_fim'] = {'arquivo': os.path.basename(nomes['gpt_fim']),
                                'gravar_em_byte': (l.tamanho // setor - 1 - n) * setor, 'bytes': len(fim)}
    else:
        aplicacao['gpt'] = {'arquivo': None, 'avisos': av_gpt}
    resultado = {'versao': VERSAO, 'gerado_em': int(time.time()), 'origem': origem, 'tamanho': l.tamanho, 'setor': setor,
                 'aviso': ('Esta é só uma proposta. O programa nunca grava a tabela no disco: se quiser aplicar, '
                           'o técnico grava os arquivos .mbr ou .gpt à mão, depois de conferir os candidatos.'),
                 'tabela_atual': atual, 'candidatos': candidatos, 'proposta': proposta, 'ilegiveis': ruins,
                 'aplicacao': aplicacao}
    return resultado, binarios


def executar(job, ctrl):
    origem, destino = job['origem'], job['destino']
    busca = caminho_busca(destino)
    retomada = os.path.exists(busca)
    erro = validar(origem, destino, job.get('opcoes'), retomada=retomada)
    if erro:
        raise ErroJob(erro)
    bloco = bloco_de(job.get('opcoes'))
    try:
        l = leitor.abrir(origem)                           # única abertura da origem: só leitura
    except ErroLeitor as e:
        raise ErroJob(str(e)) from None
    with l:
        tamanho, setor = l.tamanho, max(512, getattr(l, 'setor', 512))
        origem_real = imagem._normal(origem)
        estado = {'pos': 0, 'hits': [], 'ruins': 0}
        if retomada:
            d = _carregar_busca(busca, origem_real, tamanho)
            estado = {'pos': d['pos'], 'hits': d['hits'], 'ruins': d['ruins']}
            ctrl.evento('Retomando a busca em %d de %d bytes.' % (estado['pos'], tamanho))

        def salvar():
            _gravar_json(busca, {'origem': origem_real, 'tamanho': tamanho, **estado})

        salvar()
        atraso = config.atraso_bloco()
        excedeu = False
        while estado['pos'] < tamanho:
            if ctrl.cancelamento_pedido():
                salvar()
                return 'cancelado'
            if ctrl.pausa_pedida():
                salvar()
                return 'pausado'
            novos, ruins = particoes.hits_do_bloco(l, estado['pos'], setor, bloco)
            if len(estado['hits']) + len(novos) > MAX_HITS:
                novos, excedeu = novos[:max(0, MAX_HITS - len(estado['hits']))], True
            estado['hits'] += novos
            estado['ruins'] += ruins
            feito = min(tamanho, estado['pos'] + bloco)
            estado['pos'] = feito
            ctrl.progresso(feito, tamanho, 'Procurando partições: %d sinais' % len(estado['hits']))
            ctrl.checkpoint(salvar, bloco)
            if atraso:
                time.sleep(atraso)
        if estado['ruins']:
            ctrl.evento('%d trechos de 64 KiB não puderam ser lidos e ficaram de fora da busca.' % estado['ruins'])
        if excedeu:
            ctrl.evento('Sinais demais: guardei só os primeiros %d.' % MAX_HITS)
        atual = particoes.analisar(l, setor)
        candidatos = particoes.consolidar(l, estado['hits'], setor)
        proposta = particoes.propor(candidatos)
        nomes = _nomes(destino)
        resultado, binarios = montar_resultado(origem, l, setor, atual, candidatos, proposta, estado['ruins'], nomes)
        for chave, dados in binarios.items():
            _gravar(nomes[chave], dados)
        _gravar_json(destino, resultado)
    try:
        os.remove(busca)
    except OSError:
        pass
    ctrl.evento('Busca concluída: %d candidatos, %d na tabela proposta.' % (len(candidatos), len(proposta)))
    return 'concluido'
