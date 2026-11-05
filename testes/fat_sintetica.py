# -*- coding: utf-8 -*-
"""Imagem de 8 MiB com um sistema de arquivos no estilo FAT32, montada em Python (sem ferramenta externa).

Serve para provar que o carving acha, pelo conteúdo, arquivos cujas entradas de diretório foram apagadas (0xE5). O
carving não lê a FAT: a estrutura existe para a imagem parecer um pen drive de verdade (setor de boot, duas FATs,
raiz, clusters de 4 KiB, arquivos contíguos e alinhados ao setor). Com só 2048 clusters isto não é um FAT32 válido
para o Windows (o FAT32 exige 65525 clusters ou mais); a fase 4 monta as imagens de FAT que precisam ser válidas.
"""
import struct

import amostras

SETOR = 512
CLUSTER = 4096
RESERVADOS = 32
TAM_FAT = 16                       # setores por FAT (2048 clusters x 4 bytes)
TAMANHO = 8 * 1024 * 1024
INICIO_DADOS = (RESERVADOS + 2 * TAM_FAT) * SETOR


def _nome83(nome):
    base, _, ext = nome.upper().partition('.')
    return base[:8].ljust(8).encode('ascii') + ext[:3].ljust(3).encode('ascii')


def montar(arquivos, apagados=(), lixo=(), vazio=(), sementes=1):
    """arquivos: lista de (nome, bytes); lixo: clusters de bytes aleatórios antes de cada arquivo;
    vazio: clusters vazios (zeros) antes de cada arquivo, para espalhar os arquivos pela imagem.

    Devolve (imagem_bytes, info) onde info = [{'nome', 'dados', 'offset', 'apagado'}, ...] na ordem dos arquivos.
    Os arquivos listados em `apagados` (nomes) têm a entrada marcada 0xE5 e a cadeia da FAT liberada; o conteúdo fica."""
    img = bytearray(TAMANHO)
    boot = bytearray(SETOR)
    boot[0:3] = b'\xeb\x58\x90'
    boot[3:11] = b'MSDOS5.0'
    struct.pack_into('<HBHBHHBHHHII', boot, 11, SETOR, CLUSTER // SETOR, RESERVADOS, 2, 0, 0, 0xF8, 0, 63, 255, 0,
                     TAMANHO // SETOR)
    struct.pack_into('<IHHIHH', boot, 36, TAM_FAT, 0, 0, 2, 1, 6)
    boot[510:512] = b'\x55\xaa'
    img[0:SETOR] = boot
    img[6 * SETOR:7 * SETOR] = boot
    fat = bytearray(TAM_FAT * SETOR)
    struct.pack_into('<III', fat, 0, 0x0FFFFFF8, 0x0FFFFFFF, 0x0FFFFFFF)   # media, fim, raiz (cluster 2)
    entradas, info = bytearray(), []
    cluster = 3
    lixos, vazios = list(lixo), list(vazio)
    for n, (nome, dados) in enumerate(arquivos):
        cluster += vazios.pop(0) if vazios else 0
        for k in range(lixos.pop(0) if lixos else 0):
            off = INICIO_DADOS + (cluster - 2) * CLUSTER
            img[off:off + CLUSTER] = amostras.aleatorio(CLUSTER, 5000 + cluster + sementes)
            cluster += 1
        n_cl = max(1, -(-len(dados) // CLUSTER))
        off = INICIO_DADOS + (cluster - 2) * CLUSTER
        img[off:off + len(dados)] = dados
        apagado = nome in apagados
        if not apagado:
            for c in range(cluster, cluster + n_cl):
                struct.pack_into('<I', fat, c * 4, c + 1 if c < cluster + n_cl - 1 else 0x0FFFFFFF)
        e = bytearray(32)
        e[0:11] = _nome83(nome)
        e[11] = 0x20
        struct.pack_into('<HHI', e, 20, cluster >> 16, 0, 0)
        struct.pack_into('<HI', e, 26, cluster & 0xFFFF, len(dados))
        if apagado:
            e[0] = 0xE5
        entradas += e
        info.append({'nome': nome, 'dados': dados, 'offset': off, 'apagado': apagado})
        cluster += n_cl
    img[SETOR * RESERVADOS:SETOR * RESERVADOS + len(fat)] = fat
    img[SETOR * (RESERVADOS + TAM_FAT):SETOR * (RESERVADOS + TAM_FAT) + len(fat)] = fat
    raiz = INICIO_DADOS
    img[raiz:raiz + len(entradas)] = entradas
    return bytes(img), info
