# -*- coding: utf-8 -*-
"""APFS (sempre) e HFS+/HFSX/UDF (quando o leitor próprio não abre): só reconhecer, sem listar nem recuperar.
APFS fica assim de propósito: checkpoints do contêiner e B-trees com cópia na escrita (ver docs/FASE3-4.md)."""
import struct

from .base import Volume

LIMITACAO = ('%s foi reconhecido, mas esta versão ainda não lê a estrutura de arquivos dele. '
             'Use a busca profunda (carving) para achar os arquivos.')


class Reconhecido(Volume):
    def __init__(self, leitor, inicio, tamanho, tipo):
        super().__init__(leitor, inicio, tamanho)
        self.tipo = tipo
        self.limitacao = LIMITACAO % tipo

    def varrer(self):
        return iter(())


def reconhecer(leitor, inicio=0, tamanho=None):
    try:
        v = leitor.ler(inicio + 1024, 4)
        if len(v) == 4 and v[:2] in (b'H+', b'HX') and struct.unpack('>H', v[2:4])[0] in (4, 5):
            return Reconhecido(leitor, inicio, tamanho, 'HFS+' if v[:2] == b'H+' else 'HFSX')
        if leitor.ler(inicio + 32, 4) == b'NXSB':
            return Reconhecido(leitor, inicio, tamanho, 'APFS')
        for k in range(16, 16 + 8):                          # UDF: BEA01 ... NSR02/NSR03 ... TEA01
            d = leitor.ler(inicio + k * 2048 + 1, 5)
            if d == b'NSR02' or d == b'NSR03':
                return Reconhecido(leitor, inicio, tamanho, 'UDF')
    except (OSError, struct.error):
        pass
    return None
