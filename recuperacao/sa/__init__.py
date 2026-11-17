# -*- coding: utf-8 -*-
"""Sistemas de arquivos em Python puro, só leitura (fase 4).

    from recuperacao.sa import detectar
    vol = detectar(leitor, inicio, tamanho)       # Volume ou None
    for e in vol.varrer(): ...                    # Entrada: nome, caminho, apagado, tamanho, estado, runs...
    for pedaco in vol.ler(e): ...
"""
from .base import ErroSA, Entrada, Volume


def detectar(leitor, inicio=0, tamanho=None, todas_sessoes=False):
    """Reconhece o sistema de arquivos que começa em `inicio` e devolve o Volume, ou None. `todas_sessoes` só vale
    para o UDF (procura âncoras no disco inteiro, para achar a última sessão)."""
    from . import exfat, ext, fat, hfs, iso, ntfs, outros, udf
    for modulo in (exfat, ntfs, fat, ext, udf, iso, hfs, outros):     # UDF antes da ISO: disco ponte lista o UDF
        vol = (modulo.reconhecer(leitor, inicio, tamanho, todas_sessoes) if modulo is udf
               else modulo.reconhecer(leitor, inicio, tamanho))
        if vol is not None:
            return vol
    return None
