# -*- coding: utf-8 -*-
"""Sistemas de arquivos em Python puro, só leitura (fase 4).

    from recuperacao.sa import detectar
    vol = detectar(leitor, inicio, tamanho)       # Volume ou None
    for e in vol.varrer(): ...                    # Entrada: nome, caminho, apagado, tamanho, estado, runs...
    for pedaco in vol.ler(e): ...
"""
from .base import ErroSA, Entrada, Volume


def detectar(leitor, inicio=0, tamanho=None):
    """Reconhece o sistema de arquivos que começa em `inicio` e devolve o Volume, ou None."""
    from . import exfat, ext, fat, iso, ntfs, outros
    for modulo in (exfat, ntfs, fat, ext, iso, outros):
        vol = modulo.reconhecer(leitor, inicio, tamanho)
        if vol is not None:
            return vol
    return None
