# -*- coding: utf-8 -*-
"""Montadores de imagens sintéticas para os testes de partição: só os pedaços que as assinaturas olham
(setor de boot, superbloco, cópias reservas), escritos de forma independente do código que está sendo testado."""
import struct
import zlib

MIB = 1024 * 1024


def poe(img, off, dados):
    img[off: off + len(dados)] = dados


def fat32(img, ini, tam, oculto=None):
    total = tam // 512
    b = bytearray(512)
    b[0:3] = b'\xeb\x58\x90'
    b[3:11] = b'MSDOS5.0'
    struct.pack_into('<HBHBHHBHHHII', b, 11, 512, 8, 32, 2, 0, 0, 0xF8, 0, 63, 255, ini // 512 if oculto is None else oculto, total)
    struct.pack_into('<IHHI', b, 36, 200, 0, 0, 2)               # tamanho da FAT, flags, versão, cluster da raiz
    struct.pack_into('<HH', b, 48, 1, 6)                         # FSInfo, cópia do boot
    b[82:90] = b'FAT32   '
    b[510:512] = b'\x55\xaa'
    poe(img, ini, b)
    poe(img, ini + 6 * 512, b)
    poe(img, ini + 32 * 512, b'\xf8\xff\xff\x0f\xff\xff\xff\xff')


def ntfs(img, ini, tam, mft_cluster=4):
    total = tam // 512 - 1
    b = bytearray(512)
    b[0:3] = b'\xeb\x52\x90'
    b[3:11] = b'NTFS    '
    struct.pack_into('<HB', b, 11, 512, 8)
    b[21] = 0xF8
    struct.pack_into('<QQQ', b, 40, total, mft_cluster, total // 2)
    b[510:512] = b'\x55\xaa'
    poe(img, ini, b)
    poe(img, ini + total * 512, b)                               # cópia no último setor, fora da conta do BPB
    poe(img, ini + mft_cluster * 8 * 512, b'FILE')


def ext(img, ini, tam):
    """ext4 de blocos de 1 KiB, 8192 blocos por grupo, superbloco principal e cópia no grupo 1."""
    blocos = tam // 1024
    assert blocos > 8192
    sb = bytearray(1024)
    struct.pack_into('<IIIIIIII', sb, 0, 4096, blocos, 0, blocos - 100, 4000, 1, 0, 0)     # ... first_data_block em 20
    struct.pack_into('<II', sb, 32, 8192, 8192)
    struct.pack_into('<I', sb, 40, 512)
    struct.pack_into('<HHH', sb, 56, 0xEF53, 1, 1)
    struct.pack_into('<I', sb, 76, 1)
    struct.pack_into('<III', sb, 92, 0x24, 0x42, 1)             # compat (journal), incompat (extents+filetype), ro (esparso)
    poe(img, ini + 1024, sb)
    poe(img, ini + 2048, struct.pack('<III', 3, 4, 5))          # descritor do grupo 0 (bloco 2)
    cp = bytearray(sb)
    struct.pack_into('<H', cp, 90, 1)
    poe(img, ini + 8193 * 1024, cp)
    poe(img, ini + 8194 * 1024, struct.pack('<III', 8195, 8196, 8197))


def exfat(img, ini, tam):
    b = bytearray(512)
    b[0:3] = b'\xeb\x76\x90'
    b[3:11] = b'EXFAT   '
    struct.pack_into('<QQIIII', b, 64, ini // 512, tam // 512, 24, 8, 64, 1000)
    b[108], b[109] = 9, 3
    b[510:512] = b'\x55\xaa'
    poe(img, ini, b)
    poe(img, ini + 12 * 512, b)
    poe(img, ini + 24 * 512, b'\xf8\xff\xff\xff\xff\xff\xff\xff')


def hfs(img, ini, tam):
    h = bytearray(512)
    h[0:4] = b'H+\x00\x04'
    struct.pack_into('>II', h, 40, 4096, tam // 4096)
    poe(img, ini + 1024, h)
    poe(img, ini + tam - 1024, h)


def iso(img, ini, tam):
    d = bytearray(2048)
    d[0], d[1:6], d[6] = 1, b'CD001', 1
    struct.pack_into('<I', d, 80, tam // 2048)
    struct.pack_into('<H', d, 128, 2048)
    poe(img, ini + 32768, d)
    poe(img, ini + 32768 + 2048, b'\xffCD001\x01')


def fletcher64(dados):
    m = 0xFFFFFFFF
    s1 = s2 = 0
    for (w,) in struct.iter_unpack('<I', dados):
        s1 = (s1 + w) % m
        s2 = (s2 + s1) % m
    c1 = m - ((s1 + s2) % m)
    c2 = m - ((s1 + c1) % m)
    return (c2 << 32) | c1


def apfs(img, ini, tam):
    b = bytearray(4096)
    b[32:36] = b'NXSB'
    struct.pack_into('<IQ', b, 36, 4096, tam // 4096)
    struct.pack_into('<Q', b, 0, fletcher64(bytes(b[8:])))
    poe(img, ini, b)


# --- tabelas ------------------------------------------------------------------------------------------------
def entrada_mbr(status, tipo, lba, n):
    return struct.pack('<B3sB3sII', status, b'\0\0\0', tipo, b'\0\0\0', lba, n)


def mbr(img, entradas, assinatura=True, base=0):
    s = bytearray(512)
    for k, e in enumerate(entradas):
        s[446 + 16 * k: 462 + 16 * k] = entrada_mbr(*e)
    if assinatura:
        s[510:512] = b'\x55\xaa'
    poe(img, base, s)


def guid_bytes(texto):
    import uuid
    return uuid.UUID(texto).bytes_le


def gpt(img, entradas, setor=512):
    """entradas: [(guid_do_tipo, lba_ini, lba_fim, nome)]. Escreve o MBR protetor, o principal e a cópia reserva."""
    total = len(img) // setor
    n_sec = 16384 // setor
    arr = bytearray(16384)
    for k, (tipo, ini, fim, nome) in enumerate(entradas):
        struct.pack_into('<16s16sQQQ72s', arr, 128 * k, guid_bytes(tipo), bytes([k + 1]) * 16, ini, fim, 0, nome.encode('utf-16-le'))
    crc_arr = zlib.crc32(bytes(arr))

    def cab(meu, outro, ent):
        h = bytearray(92)
        h[:8] = b'EFI PART'
        struct.pack_into('<IIIIQQQQ16sQIII', h, 8, 0x10000, 92, 0, 0, meu, outro, 2 + n_sec, total - 2 - n_sec,
                         b'\x77' * 16, ent, 128, 128, crc_arr)
        struct.pack_into('<I', h, 16, zlib.crc32(bytes(h)))
        return h
    mbr(img, [(0, 0xEE, 1, min(total - 1, 0xFFFFFFFF))], base=0)
    poe(img, setor, cab(1, total - 1, 2))
    poe(img, 2 * setor, arr)
    poe(img, (total - 1 - n_sec) * setor, arr)
    poe(img, (total - 1) * setor, cab(total - 1, 1, total - 1 - n_sec))
    return n_sec


def disco_mbr_completo():
    """48 MiB: FAT32, NTFS, estendida com duas lógicas (ext4 e FAT32). Devolve (img, lista de (início, tamanho, sistema))."""
    img = bytearray(48 * MIB)
    fat32(img, 2048 * 512, 24576 * 512)
    ntfs(img, 28672 * 512, 24576 * 512)
    ext(img, 57344 * 512, 32768 * 512)
    fat32(img, 92160 * 512, 4096 * 512)
    mbr(img, [(0x80, 0x0C, 2048, 24576), (0, 0x07, 28672, 24576), (0, 0x05, 55296, 40960)])
    # EBR 1 em 55296: lógica em +2048, próximo EBR em +34816 (relativo ao início da estendida)
    mbr(img, [(0, 0x83, 2048, 32768), (0, 0x05, 34816, 6144)], base=55296 * 512)
    mbr(img, [(0, 0x0C, 2048, 4096)], base=(55296 + 34816) * 512)
    esperado = [(2048 * 512, 24576 * 512, 'fat32'), (28672 * 512, 24576 * 512, 'ntfs'), (55296 * 512, 40960 * 512, ''),
                (57344 * 512, 32768 * 512, 'ext4'), (92160 * 512, 4096 * 512, 'fat32')]
    return img, esperado


def disco_tabela_zerada():
    """48 MiB sem tabela: FAT32 em 1 MiB (12 MiB), NTFS em 14 MiB (12 MiB), ext4 em 27 MiB (16 MiB)."""
    img = bytearray(48 * MIB)
    fat32(img, 1 * MIB, 12 * MIB)
    ntfs(img, 14 * MIB, 12 * MIB)
    ext(img, 27 * MIB, 16 * MIB)
    esperado = [(1 * MIB, 12 * MIB, 'fat32'), (14 * MIB, 12 * MIB, 'ntfs'), (27 * MIB, 16 * MIB, 'ext4')]
    return img, esperado
