# -*- coding: utf-8 -*-
"""Mesma unidade: o destino da imagem não pode ficar no mesmo disco físico da origem.

Gravar no disco que está sendo recuperado sobrescreve justamente o que ainda poderia ser salvo. Só vale quando a
origem é um disco ou volume (arquivo comum pode ficar no mesmo disco do destino). Se não der para saber em que disco
o destino está, a cópia é recusada (mais seguro do que arriscar).

Os discos vêm como conjunto de textos ('0', '1' no Windows; 'sda', 'nvme0n1' no Linux). O mapeamento real fica em
`Sistema`; os testes trocam `MAPA` por um falso.
"""
import os
import re

from . import leitor

MSG_MESMO_DISCO = ('O destino está no mesmo disco da origem. Escolha uma pasta em outro disco: gravar a imagem no disco '
                   'que você quer recuperar pode destruir o que ainda dá para salvar.')
MSG_SEM_CERTEZA = ('Não consegui confirmar em que disco fica o destino. Escolha uma pasta de outro disco, '
                   'com letra de unidade própria.')
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
_DISCO_WIN = re.compile(r'\\\\\.\\PhysicalDrive(\d+)', re.I)
_VOLUME_WIN = re.compile(r'\\\\\.\\([A-Za-z]:)', re.I)


class Sistema:
    """O mapeamento de verdade. Cada método devolve um set de discos, ou None se não souber."""

    def __init__(self, api=None, raiz_sys='/sys'):
        self.api = api
        self.raiz_sys = raiz_sys

    # --- Windows ---
    def _extensoes(self, letra):
        """Discos físicos de um volume. Abre o VOLUME DO DESTINO ou da origem com acesso zero (só para o IOCTL)."""
        api = self.api or leitor.ApiWindows()
        h = api.CreateFileW('\\\\.\\' + letra, 0, leitor.FILE_SHARE_READ | leitor.FILE_SHARE_WRITE, leitor.OPEN_EXISTING, 0)
        try:
            return {str(n) for n in api.extensoes(h)}
        finally:
            api.fechar(h)

    # --- Linux ---
    def _disco_de_bloco(self, nome, fundo=0):
        base = os.path.join(self.raiz_sys, 'class', 'block', nome)
        real = os.path.realpath(base)
        if not os.path.isdir(real):
            return None
        if os.path.exists(os.path.join(real, 'partition')):
            return self._disco_de_bloco(os.path.basename(os.path.dirname(real)), fundo + 1) if fundo < 4 else None
        escravos = os.path.join(real, 'slaves')                      # LVM/dm: os discos de baixo
        if os.path.isdir(escravos) and os.listdir(escravos) and fundo < 4:
            achados = set()
            for e in os.listdir(escravos):
                sub = self._disco_de_bloco(e, fundo + 1)
                if sub is None:
                    return None
                achados |= sub
            return achados
        return {os.path.basename(real)}

    def discos_da_origem(self, origem):
        if os.name == 'nt' or _DISCO_WIN.fullmatch(origem) or _VOLUME_WIN.fullmatch(origem):
            m = _DISCO_WIN.fullmatch(origem)
            if m:
                return {str(int(m.group(1)))}
            m = _VOLUME_WIN.fullmatch(origem)
            return self._extensoes(m.group(1).upper()) if m else None
        return self._disco_de_bloco(os.path.basename(origem))

    def discos_do_destino(self, destino):
        pasta = os.path.realpath(os.path.dirname(os.path.abspath(destino)))
        if os.name == 'nt':
            letra = os.path.splitdrive(pasta)[0]
            return self._extensoes(letra.upper()) if re.fullmatch(r'[A-Za-z]:', letra) else None
        dev = os.stat(pasta).st_dev
        ref = os.path.join(self.raiz_sys, 'dev', 'block', '%d:%d' % (os.major(dev), os.minor(dev)))
        return self._disco_de_bloco(os.path.basename(os.path.realpath(ref))) if os.path.exists(ref) else None


MAPA = Sistema()


def conferir(origem, destino):
    """Frase de erro se o destino está no mesmo disco físico da origem (ou se não dá para saber); None se está ok."""
    try:
        d_origem = MAPA.discos_da_origem(origem)
        d_destino = MAPA.discos_do_destino(destino)
    except (OSError, ValueError, AttributeError):
        return MSG_SEM_CERTEZA
    if not d_origem or not d_destino:
        return MSG_SEM_CERTEZA
    if set(d_origem) & set(d_destino):
        return MSG_MESMO_DISCO
    return None
