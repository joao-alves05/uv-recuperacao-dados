# -*- coding: utf-8 -*-
"""Só para teste: sobe o serviço de verdade (outro processo) com um "disco" defeituoso e/ou lento.

A configuração vem em RECDADOS_TESTE_DEFEITOS (JSON):
  ruins:          [[setor inicial, setor final]]  setores de 512 B que a leitura da imagem nunca consegue ler
  lento_falha:    segundos de espera em cada leitura que falha (deixa a bisseção demorar)
  lote_sistema:   troca varredura.LOTE_SISTEMA (lotes menores = mais pontos de retomada)
  lento_sistema:  segundos de espera por arquivo na lista pelo sistema de arquivos
Nada do código de produção muda: o desvio é feito aqui, no processo de teste, antes de subir o serviço.
"""
import json
import os
import sys
import time

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

from recuperacao import arquivos_sa, servico                      # noqa: E402
from recuperacao.tipos import imagem, varredura                  # noqa: E402

S = 512


def instalar(cfg):
    ruins = [(int(a), int(b)) for a, b in cfg.get('ruins', [])]
    if ruins:
        original = imagem._ler_bloco

        def lendo(f, pos, n):
            primeiro, ultimo = pos // S, (pos + n - 1) // S
            if any(a <= ultimo and primeiro < b for a, b in ruins):
                time.sleep(cfg.get('lento_falha', 0))
                raise OSError('setor ruim de mentira')
            return original(f, pos, n)
        imagem._ler_bloco = lendo
    if cfg.get('lote_sistema'):
        varredura.LOTE_SISTEMA = int(cfg['lote_sistema'])
    if cfg.get('lento_sistema'):
        achado_de = arquivos_sa.achado_de

        def devagar(e, prefixo=''):
            time.sleep(cfg['lento_sistema'])
            return achado_de(e, prefixo)
        arquivos_sa.achado_de = devagar


if __name__ == '__main__':
    instalar(json.loads(os.environ.get('RECDADOS_TESTE_DEFEITOS') or '{}'))
    sys.exit(servico.principal())
