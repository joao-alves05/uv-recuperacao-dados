# -*- coding: utf-8 -*-
"""Fase 3: partições. Ler as tabelas (MBR com estendidas e lógicas, GPT com CRC e cópia reserva, e reconhecer BSD
disklabel e Apple Partition Map), procurar partição perdida por assinaturas e montar a tabela reconstruída.

Só leitura: tudo passa pelo `Leitor` (recuperacao/leitor.py). Nenhuma função daqui grava em disco; montar_mbr e
montar_gpt só devolvem bytes, para o técnico aplicar à mão se quiser (o job `particoes` grava esses bytes em
arquivos num destino de outro disco).

    with leitor.abrir(caminho) as l:
        tabela = analisar(l)                         # partições da tabela, estado e avisos
        with abrir_volume(l, tabela['particoes'][0]) as vol:     # uma partição como se fosse um disco
            vol.ler(0, 512)
"""
import hashlib
import struct
import uuid
import zlib

from .leitor import Leitor

BLOCO_BUSCA = 4 * 1024 * 1024        # janela de leitura da busca
SOBRA = 64 * 1024                    # bytes lidos além da janela, para estruturas que passam do fim dela
PEDACO_RUIM = 64 * 1024              # leitura com falha é refeita em pedaços deste tamanho
MAX_LOGICAS = 128
MAX_ENTRADAS_GPT = 4096
MAX_BYTES_GPT = 4 * 1024 * 1024
ENTRADAS_GPT_PADRAO = 128

TIPOS_MBR = {
    0x01: 'FAT12', 0x04: 'FAT16 (<32 MB)', 0x05: 'Estendida', 0x06: 'FAT16', 0x07: 'NTFS / exFAT / HPFS',
    0x0B: 'FAT32', 0x0C: 'FAT32 (LBA)', 0x0E: 'FAT16 (LBA)', 0x0F: 'Estendida (LBA)', 0x11: 'FAT12 oculta',
    0x14: 'FAT16 oculta', 0x16: 'FAT16 oculta', 0x1B: 'FAT32 oculta', 0x1C: 'FAT32 (LBA) oculta', 0x27: 'Recuperação do Windows',
    0x42: 'Disco dinâmico do Windows', 0x82: 'Swap do Linux', 0x83: 'Linux', 0x85: 'Estendida do Linux',
    0x8E: 'LVM do Linux', 0xA5: 'FreeBSD', 0xA6: 'OpenBSD', 0xA8: 'Apple UFS', 0xA9: 'NetBSD', 0xAB: 'Apple boot',
    0xAF: 'Apple HFS+', 0xEE: 'GPT (protetora)', 0xEF: 'Sistema EFI', 0xFD: 'RAID do Linux',
}
ESTENDIDAS = (0x05, 0x0F, 0x85)
BSD_MBR = (0xA5, 0xA6, 0xA9)
TIPOS_GPT = {
    '00000000-0000-0000-0000-000000000000': '',
    'c12a7328-f81f-11d2-ba4b-00a0c93ec93b': 'Sistema EFI',
    'e3c9e316-0b5c-4db8-817d-f92df00215ae': 'Reservada da Microsoft',
    'ebd0a0a2-b9e5-4433-87c0-68b6b72699c7': 'Dados básicos (Windows)',
    'de94bba4-06d1-4d40-a16a-bfd50179d6ac': 'Recuperação do Windows',
    '5808c8aa-7e8f-42e0-85d2-e1e90434cfb3': 'Metadados de disco dinâmico',
    '0fc63daf-8483-4772-8e79-3d69d8477de4': 'Sistema de arquivos do Linux',
    '0657fd6d-a4ab-43c4-84e5-0933c84b4f4f': 'Swap do Linux',
    'e6d6d379-f507-44c2-a23c-238f2a3df928': 'LVM do Linux',
    'a19d880f-05fc-4d3b-a006-743f0f84911e': 'RAID do Linux',
    '48465300-0000-11aa-aa11-00306543ecac': 'Apple HFS+',
    '7c3457ef-0000-11aa-aa11-00306543ecac': 'Apple APFS',
    '55465300-0000-11aa-aa11-00306543ecac': 'Apple UFS',
    '516e7cb4-6ecf-11d6-8ff8-00022d09712b': 'FreeBSD UFS',
    '516e7cb5-6ecf-11d6-8ff8-00022d09712b': 'Swap do FreeBSD',
    '516e7cba-6ecf-11d6-8ff8-00022d09712b': 'FreeBSD ZFS',
}
GUID_DADOS_BASICOS = 'ebd0a0a2-b9e5-4433-87c0-68b6b72699c7'
GUID_POR_SISTEMA = {'fat12': GUID_DADOS_BASICOS, 'fat16': GUID_DADOS_BASICOS, 'fat32': GUID_DADOS_BASICOS,
                    'exfat': GUID_DADOS_BASICOS, 'ntfs': GUID_DADOS_BASICOS, 'iso9660': GUID_DADOS_BASICOS,
                    'ext2': '0fc63daf-8483-4772-8e79-3d69d8477de4', 'ext3': '0fc63daf-8483-4772-8e79-3d69d8477de4',
                    'ext4': '0fc63daf-8483-4772-8e79-3d69d8477de4', 'hfs+': '48465300-0000-11aa-aa11-00306543ecac',
                    'hfsx': '48465300-0000-11aa-aa11-00306543ecac', 'apfs': '7c3457ef-0000-11aa-aa11-00306543ecac'}
TIPO_MBR_POR_SISTEMA = {'fat12': 0x01, 'fat16': 0x0E, 'fat32': 0x0C, 'ntfs': 0x07, 'exfat': 0x07, 'ext2': 0x83,
                        'ext3': 0x83, 'ext4': 0x83, 'hfs+': 0xAF, 'hfsx': 0xAF, 'apfs': 0xAF}
FSTYPE_BSD = {1: 'swap', 2: 'Version 6', 3: 'Version 7', 4: 'System V', 5: '4.1BSD', 6: 'Eighth Edition', 7: 'UFS (4.2BSD)',
              8: 'MSDOS', 9: '4.4LFS', 10: 'desconhecido', 11: 'HPFS', 12: 'ISO9660', 13: 'boot', 14: 'vinum',
              15: 'RAID', 16: 'Filecore', 17: 'ext2', 27: 'ZFS'}
BSD_MAGIA = 0x82564557

FRASES = {
    'principal': 'setor de boot ou superbloco principal encontrado',
    'copia': 'cópia reserva encontrada no lugar esperado',
    'posicao': 'a posição gravada na própria estrutura confere com onde ela foi achada',
    'estrutura': 'estrutura interna coerente (tabela FAT, MFT ou descritores de grupo)',
    'fora': 'a partição passaria do fim do disco',
}
PESOS = {'principal': 0.45, 'copia': 0.35, 'posicao': 0.15, 'estrutura': 0.15, 'fora': -0.25}


def _u16(b, o):
    return struct.unpack_from('<H', b, o)[0]


def _u32(b, o):
    return struct.unpack_from('<I', b, o)[0]


def _u64(b, o):
    return struct.unpack_from('<Q', b, o)[0]


def _le(l, off, n):
    """Lê sem levantar: setor ilegível ou fora do disco devolve o que deu (possivelmente nada)."""
    try:
        return l.ler(off, n)
    except (OSError, ValueError):
        return b''


def _potencia2(n, minimo=1, maximo=1 << 40):
    return minimo <= n <= maximo and n & (n - 1) == 0


# --- uma partição como volume (para a fase 4) ---------------------------------
class Volume(Leitor):
    """Uma faixa [inicio, inicio+tamanho) do leitor de base, vista como se fosse um disco que começa em 0.
    Só leitura (herda do Leitor; não tem escrita). Fechar o volume NÃO fecha o leitor de base."""

    def __init__(self, base, inicio, tamanho=None):
        if inicio < 0 or inicio > base.tamanho:
            raise ValueError('o início da partição fica fora do disco')
        resto = base.tamanho - inicio
        t = resto if tamanho is None else min(max(0, tamanho), resto)
        super().__init__(t, base.setor)
        self.base = base
        self.inicio = inicio
        self.truncado = tamanho is not None and tamanho > resto      # a tabela promete mais do que o disco tem

    def _bruto(self, offset, n):
        return self.base.ler(self.inicio + offset, n)


def abrir_volume(base, inicio, tamanho=None):
    """Abre uma partição como volume. `inicio` pode ser o deslocamento em bytes ou um dicionário de partição
    (com `inicio` e `tamanho`) como os que `analisar` e a busca devolvem."""
    if isinstance(inicio, dict):
        inicio, tamanho = inicio['inicio'], inicio.get('tamanho', tamanho)
    return Volume(base, inicio, tamanho)


# --- detectores de sistema de arquivos (bytes da estrutura -> dicionário ou None) -------------------------
def det_ntfs(b):
    if len(b) < 512 or b[3:11] != b'NTFS    ' or b[510:512] != b'\x55\xaa':
        return None
    bps, spc, total, mft = _u16(b, 11), b[13], _u64(b, 40), _u64(b, 48)
    if spc > 0x80:
        spc = 1 << (256 - spc)
    if not _potencia2(bps, 512, 4096) or not _potencia2(spc, 1, 128) or total == 0 or mft * spc >= total:
        return None
    return {'sistema': 'ntfs', 'bps': bps, 'setores': total, 'tamanho': (total + 1) * bps,       # +1: a cópia do boot fica depois do último setor do BPB
            'mft': mft * spc * bps}


def det_exfat(b):
    if len(b) < 512 or b[3:11] != b'EXFAT   ' or b[510:512] != b'\x55\xaa' or any(b[11:64]):
        return None
    desloc, tam, fat = _u64(b, 64), _u64(b, 72), _u32(b, 80)
    bps_shift, spc_shift = b[108], b[109]
    if not 9 <= bps_shift <= 12 or bps_shift + spc_shift > 25 or tam == 0 or not 0 < fat < tam:
        return None
    bps = 1 << bps_shift
    return {'sistema': 'exfat', 'bps': bps, 'setores': tam, 'tamanho': tam * bps, 'inicio_decl': desloc, 'fat_off': fat}


def _fat_sem_rotulo(b, bps, spc, res, nf, raiz, f16, clusters, sistema):
    """Sem o rótulo "FAT12/16" o setor tem de provar que é FAT pelos números, senão qualquer setor com 55AA e um
    EB/E9 no início passaria: salto no padrão (EB xx 90 ou E9), raiz que ocupa setores inteiros, número de clusters
    abaixo do limite de FAT16 e uma FAT grande o bastante para endereçar todos os clusters."""
    if b[0] == 0xEB and b[2] != 0x90:
        return False
    if (raiz * 32) % bps or clusters >= 65525 or res + nf * f16 >= 65536:
        return False
    entradas = f16 * bps * 2 // 3 if sistema == 'fat12' else f16 * bps // 2
    return entradas >= clusters + 2


def det_fat(b):
    if len(b) < 512 or b[510:512] != b'\x55\xaa' or b[0] not in (0xEB, 0xE9):
        return None
    bps, spc, res, nf = _u16(b, 11), b[13], _u16(b, 14), b[16]
    raiz, t16, media, f16, t32 = _u16(b, 17), _u16(b, 19), b[21], _u16(b, 22), _u32(b, 32)
    if not _potencia2(bps, 512, 4096) or not _potencia2(spc, 1, 128) or res == 0 or nf not in (1, 2):
        return None
    if media != 0xF0 and media < 0xF8:
        return None
    total = t16 or t32
    if total == 0:
        return None
    if raiz == 0 and f16 == 0:                                       # FAT32
        f32 = _u32(b, 36)
        if f32 == 0 or _u16(b, 42) != 0 or b[82:90] != b'FAT32   ' or total <= res + nf * f32:
            return None
        sistema, fat_set, bk = 'fat32', f32, _u16(b, 50)
    else:
        if f16 == 0 or raiz == 0:
            return None
        dados = total - (res + nf * f16 + (raiz * 32 + bps - 1) // bps)
        clusters = dados // spc
        if clusters <= 0:
            return None
        sistema, fat_set, bk = ('fat12' if clusters < 4085 else 'fat16'), f16, 0
        if b[54:57] != b'FAT':                                       # rótulo em branco ou com lixo: só vale a estrutura
            if not _fat_sem_rotulo(b, bps, spc, res, nf, raiz, f16, clusters, sistema):
                return None
    return {'sistema': sistema, 'bps': bps, 'setores': total, 'tamanho': total * bps, 'oculto': _u32(b, 28), 'bk': bk,
            'reservado': res, 'media': media, 'fat_setores': fat_set}


def det_ext(b):
    """Superbloco ext2/3/4 (a estrutura começa em b[0]). Só aceita se os campos fazem sentido juntos."""
    if len(b) < 264 or _u16(b, 56) != 0xEF53:
        return None
    inodes, blocos, fdb, log_bs, bpg, ipg = _u32(b, 0), _u32(b, 4), _u32(b, 20), _u32(b, 24), _u32(b, 32), _u32(b, 40)
    estado, erros, rev = _u16(b, 58), _u16(b, 60), _u32(b, 76)
    compat, incompat, ro = _u32(b, 92), _u32(b, 96), _u32(b, 100)
    if log_bs > 6 or rev > 1 or inodes == 0 or ipg == 0 or blocos == 0 or fdb > 1 or not 1 <= estado <= 7 \
            or not 1 <= erros <= 3:
        return None
    bs = 1024 << log_bs
    if bpg == 0 or bpg > 8 * bs or (fdb == 1) != (bs == 1024):
        return None
    if incompat & 0x80 and len(b) >= 340:
        blocos |= _u32(b, 336) << 32
    sistema = 'ext4' if incompat & (0x40 | 0x80 | 0x200) else ('ext3' if compat & 0x4 else 'ext2')
    return {'sistema': sistema, 'bs': bs, 'bpg': bpg, 'fdb': fdb, 'grupo': _u16(b, 90), 'esparso': bool(ro & 1),
            'blocos': blocos, 'tamanho': blocos * bs}


def det_hfs(b):
    """Cabeçalho de volume do HFS+/HFSX (a estrutura começa em b[0])."""
    if len(b) < 48:
        return None
    mais = b[:4] == b'H+\x00\x04'
    if not (mais or b[:4] == b'HX\x00\x05'):
        return None
    bs, total = struct.unpack_from('>II', b, 40)
    if not _potencia2(bs, 512, 1 << 26) or total == 0:
        return None
    return {'sistema': 'hfs+' if mais else 'hfsx', 'bs': bs, 'blocos': total, 'tamanho': bs * total}


def det_apfs(b):
    if len(b) < 48 or b[32:36] != b'NXSB':
        return None
    bs, cont = _u32(b, 36), _u64(b, 40)
    if not _potencia2(bs, 4096, 65536) or cont == 0:
        return None
    return {'sistema': 'apfs', 'bs': bs, 'blocos': cont, 'tamanho': bs * cont}


def det_iso(b):
    if len(b) < 136 or b[0] != 1 or b[1:6] != b'CD001' or b[6] != 1:
        return None
    vss, lbs = _u32(b, 80), _u16(b, 128)
    if vss == 0 or not _potencia2(lbs, 512, 65536):
        return None
    return {'sistema': 'iso9660', 'bs': lbs, 'blocos': vss, 'tamanho': vss * lbs}


# tipo da busca -> (detector, bytes a ler a partir da estrutura)
_DET = {'ntfs': (det_ntfs, 512), 'exfat': (det_exfat, 512), 'fat': (det_fat, 512), 'ext': (det_ext, 1024),
        'hfs': (det_hfs, 64), 'apfs': (det_apfs, 64), 'iso': (det_iso, 2048)}


def _ver(l, tipo, pos):
    """Aplica o detector do tipo à estrutura que deveria estar em `pos`; None se não está lá (ou não dá para ler)."""
    if pos < 0 or pos >= l.tamanho:
        return None
    det, n = _DET[tipo]
    return det(_le(l, pos, n))


def identificar(l, inicio):
    """Sistema de arquivos que começa em `inicio` (início de partição), pelas assinaturas do próprio volume."""
    if inicio < 0 or inicio >= l.tamanho:
        return None
    b = _le(l, inicio, 512)
    for det in (det_apfs, det_ntfs, det_exfat, det_fat):
        r = det(b)
        if r:
            return r
    b = _le(l, inicio + 1024, 1024)
    r = det_hfs(b)
    if r:
        return r
    r = det_ext(b)
    if r and r['grupo'] == 0:
        return r
    return det_iso(_le(l, inicio + 32768, 2048))


# --- MBR --------------------------------------------------------------------------------------------------
def _entradas_mbr(b):
    return [struct.unpack_from('<B3xB3xII', b, 446 + 16 * i) for i in range(4)]       # (status, tipo, lba, setores)


def _mbr_plausivel(b):
    return len(b) >= 512 and all(e[0] in (0, 0x80) for e in _entradas_mbr(b))


def _item(l, origem, ini_set, n_set, setor, **extra):
    inicio, tam = ini_set * setor, n_set * setor
    d = {'origem_tabela': origem, 'inicio': inicio, 'tamanho': tam, 'fim': inicio + tam, 'inicio_setor': ini_set,
         'setores': n_set}
    d.update(extra)
    d['avisos'] = []
    if inicio + tam > l.tamanho:
        d['avisos'].append('A partição passa do fim do disco (a imagem está cortada ou a tabela está errada).')
    det = identificar(l, inicio) if inicio < l.tamanho else None
    d['sistema'] = det['sistema'] if det else ''
    return d


def _logicas(l, ext_ini, setor, avisos):
    """Percorre a cadeia de EBRs. Devolve (lista de (lba, setores, tipo), cadeia_ok)."""
    out, atual, vistos, ok = [], ext_ini, set(), True
    while len(out) < MAX_LOGICAS:
        if atual in vistos:
            avisos.append('A cadeia de partições lógicas dá a volta (EBR repetido).')
            return out, False
        vistos.add(atual)
        b = _le(l, atual * setor, 512)
        if len(b) < 512 or b[510:512] != b'\x55\xaa':
            avisos.append('Setor %d (partição lógica) sem a assinatura 55AA: a cadeia de lógicas foi cortada.' % atual)
            return out, False
        e = _entradas_mbr(b)
        if e[0][1] and e[0][3]:
            out.append((atual + e[0][2], e[0][3], e[0][1]))
        if e[1][1] and e[1][2]:
            atual = ext_ini + e[1][2]
        else:
            return out, True
    avisos.append('Partições lógicas demais: parei em %d.' % MAX_LOGICAS)
    return out, False


def _ler_mbr(l, b0, setor, res):
    total = l.tamanho // setor
    sig = b0[510:512] == b'\x55\xaa'
    res['esquema'] = 'mbr'
    if not sig:
        res['estado'] = 'tabela_danificada'
        res['avisos'].append('O primeiro setor não termina em 55AA: a tabela MBR pode ter sido danificada.')
    else:
        res['estado'] = 'ok'
    itens = []
    for status, tipo, lba, n in _entradas_mbr(b0):
        if tipo == 0 or n == 0:
            continue
        if tipo in ESTENDIDAS:
            logicas, cadeia = _logicas(l, lba, setor, res['avisos'])
            if not cadeia:
                res['estado'] = 'tabela_danificada'
            itens.append(_item(l, 'mbr', lba, n, setor, tipo='0x%02X' % tipo, tipo_nome=TIPOS_MBR.get(tipo, 'Estendida'),
                               ativa=status == 0x80, logica=False, estendida=True))
            for ini, cnt, t in logicas:
                itens.append(_item(l, 'mbr-logica', ini, cnt, setor, tipo='0x%02X' % t,
                                   tipo_nome=TIPOS_MBR.get(t, 'desconhecido'), ativa=False, logica=True))
            continue
        item = _item(l, 'mbr', lba, n, setor, tipo='0x%02X' % tipo, tipo_nome=TIPOS_MBR.get(tipo, 'desconhecido'),
                     ativa=status == 0x80, logica=False)
        if tipo in BSD_MBR:
            item['subparticoes'] = _bsd(l, lba * setor, setor, lba, res['avisos'])
        itens.append(item)
    res['particoes'] = itens
    if not itens and sig:
        res['avisos'].append('O MBR não tem nenhuma partição.')
    return res


# --- GPT --------------------------------------------------------------------------------------------------
def _guid(b):
    return str(uuid.UUID(bytes_le=bytes(b)))


def _cabecalho_gpt(l, lba, setor):
    """Lê o cabeçalho GPT que deveria estar no setor `lba`. Devolve dict com `erros` (vazio = válido) ou None se
    não há assinatura."""
    b = _le(l, lba * setor, setor)
    if len(b) < 92 or b[:8] != b'EFI PART':
        return None
    h = {'lba': lba, 'erros': []}
    tam = _u32(b, 12)
    if not 92 <= tam <= len(b):
        h['erros'].append('tamanho do cabeçalho inválido')
        return h
    crc = _u32(b, 16)
    if zlib.crc32(b[:16] + b'\0\0\0\0' + b[20:tam]) & 0xFFFFFFFF != crc:
        h['erros'].append('CRC do cabeçalho não confere')
    h.update(meu=_u64(b, 24), outro=_u64(b, 32), primeiro=_u64(b, 40), ultimo=_u64(b, 48), guid=_guid(b[56:72]),
             ent_lba=_u64(b, 72), n=_u32(b, 80), tam_ent=_u32(b, 84), crc_ent=_u32(b, 88))
    if h['meu'] != lba:
        h['erros'].append('o cabeçalho diz estar em outro setor')
    if not 0 < h['n'] <= MAX_ENTRADAS_GPT or h['tam_ent'] < 128 or h['tam_ent'] % 8 or h['n'] * h['tam_ent'] > MAX_BYTES_GPT:
        h['erros'].append('tamanho da tabela de entradas inválido')
        return h
    dados = _le(l, h['ent_lba'] * setor, h['n'] * h['tam_ent'])
    if len(dados) < h['n'] * h['tam_ent']:
        h['erros'].append('não consegui ler a tabela de entradas')
    elif zlib.crc32(dados) & 0xFFFFFFFF != h['crc_ent']:
        h['erros'].append('CRC da tabela de entradas não confere')
    h['entradas'] = dados
    return h


def _entradas_gpt(l, h, setor, avisos):
    itens = []
    dados, tam = h.get('entradas', b''), h['tam_ent']
    for i in range(min(h['n'], len(dados) // tam)):
        e = dados[i * tam: (i + 1) * tam]
        tipo = _guid(e[:16])
        if not any(e[:16]):
            continue
        ini, fim = _u64(e, 32), _u64(e, 40)
        nome = e[56:128].decode('utf-16-le', 'replace').split('\0')[0]
        if fim < ini:
            avisos.append('Entrada %d do GPT com fim antes do início: ignorada.' % (i + 1))
            continue
        it = _item(l, 'gpt', ini, fim - ini + 1, setor, tipo=tipo, tipo_nome=TIPOS_GPT.get(tipo, 'desconhecido'),
                   nome=nome, guid=_guid(e[16:32]), ativa=False, logica=False)
        itens.append(it)
    return itens


def _ler_gpt(l, setor, res, prot):
    """Escolhe o cabeçalho principal (LBA 1) ou a reserva. `prot`: o MBR protetor existe."""
    total = l.tamanho // setor
    p = _cabecalho_gpt(l, 1, setor)
    alt = p['outro'] if p and 'outro' in p and 1 < p['outro'] < total else total - 1
    r = _cabecalho_gpt(l, alt, setor)
    if r is None and alt != total - 1:
        r = _cabecalho_gpt(l, total - 1, setor)
    p_ok = p is not None and not p['erros']
    r_ok = r is not None and not r['erros']
    res['esquema'] = 'gpt'
    av = res['avisos']
    if p_ok:
        res['estado'] = 'ok'
        usado = p
        if r is None:
            av.append('O GPT não tem cópia reserva no fim do disco.')
        elif not r_ok:
            av.append('A cópia reserva do GPT está danificada (%s).' % '; '.join(r['erros']))
        elif r['crc_ent'] != p['crc_ent']:
            av.append('O GPT principal e a cópia reserva têm listas de partições diferentes.')
        if r_ok and r['meu'] != p['outro']:
            av.append('A cópia reserva do GPT não está onde o cabeçalho principal diz.')
    elif r_ok:
        res['estado'] = 'gpt_reserva_usada'
        usado = r
        av.append('O GPT principal %s: usei a cópia reserva do fim do disco.' %
                  ('está danificado (%s)' % '; '.join(p['erros']) if p else 'não existe'))
    else:
        res['estado'] = 'tabela_danificada'
        usado = None
        for nome, h in (('principal', p), ('reserva', r)):
            if h is None:
                av.append('GPT %s: sem assinatura.' % nome)
            else:
                av.append('GPT %s inválido: %s.' % (nome, '; '.join(h['erros'])))
    if usado is not None:
        res['guid_disco'] = usado['guid']
        res['particoes'] = _entradas_gpt(l, usado, setor, av)
    return res


def _achar_gpt(l, setor):
    """O GPT pode estar em setores de 512 ou de 4096, e uma imagem de arquivo não diz qual. Devolve o setor que tem
    a assinatura (o do leitor primeiro), ou None."""
    for s in dict.fromkeys([setor, 512, 4096]):
        if l.tamanho >= 3 * s:
            for lba in (1, l.tamanho // s - 1):
                b = _le(l, lba * s, 8)
                if b == b'EFI PART':
                    return s
    return None


# --- BSD disklabel e Apple Partition Map (só listar) --------------------------------------------------------
def _bsd(l, base, setor, base_setor, avisos):
    """Sub-partições de um disklabel BSD no setor 1 da fatia (a 512 bytes de `base`). Vazio se não há rótulo."""
    b = _le(l, base + 512, 512)
    if len(b) < 148 or _u32(b, 0) != BSD_MAGIA or _u32(b, 132) != BSD_MAGIA:
        return []
    n, secsize = min(_u16(b, 138), 16), _u32(b, 40) or 512
    bruto = []
    for i in range(n):
        if 148 + 16 * i + 16 > len(b):
            break
        tam, ini, _, fstype = struct.unpack_from('<IIIB', b, 148 + 16 * i)
        if fstype and tam:
            bruto.append((i, ini, tam, fstype))
    c = [x for x in bruto if x[0] == 2]
    absoluto = bool(base_setor) and bool(c) and c[0][1] == base_setor          # 'c' = fatia inteira
    out = []
    for i, ini, tam, fstype in bruto:
        inicio = (ini if absoluto or not base_setor else ini + base_setor) * secsize
        out.append({'origem_tabela': 'bsd', 'letra': chr(ord('a') + i), 'inicio': inicio, 'tamanho': tam * secsize,
                    'fim': inicio + tam * secsize, 'tipo': str(fstype), 'tipo_nome': FSTYPE_BSD.get(fstype, 'desconhecido'),
                    'somente_listar': True})
    avisos.append('Rótulo BSD (disklabel) encontrado: as sub-partições só são listadas.')
    return out


def _ler_apm(l, b0):
    """Apple Partition Map: bloco 0 'ER' (ou só o mapa a partir do bloco 1) e entradas 'PM' em big endian."""
    bloco = struct.unpack_from('>H', b0, 2)[0] if b0[:2] == b'ER' else 512
    if not _potencia2(bloco, 512, 4096):
        return None
    e1 = _le(l, bloco, 512)
    if len(e1) < 512 or e1[:2] != b'PM':
        return None
    n = min(struct.unpack_from('>I', e1, 4)[0], 128)
    itens = []
    for i in range(1, n + 1):
        e = _le(l, i * bloco, 512)
        if len(e) < 512 or e[:2] != b'PM':
            break
        ini, cnt = struct.unpack_from('>II', e, 8)
        nome = e[16:48].split(b'\0')[0].decode('ascii', 'replace')
        tipo = e[48:80].split(b'\0')[0].decode('ascii', 'replace')
        if tipo in ('Apple_partition_map', 'Apple_Free') or cnt == 0:
            continue
        itens.append({'origem_tabela': 'apm', 'inicio': ini * bloco, 'tamanho': cnt * bloco, 'fim': (ini + cnt) * bloco,
                      'inicio_setor': ini, 'setores': cnt, 'tipo': tipo, 'tipo_nome': tipo, 'nome': nome,
                      'somente_listar': True, 'avisos': []})
    return itens


# --- análise geral ------------------------------------------------------------------------------------------
def analisar(l, setor=None):
    """Lê a tabela de partições do disco, imagem ou arquivo.

    Devolve {esquema: mbr|gpt|apm|bsd|volume|nenhum, estado: ok|tabela_danificada|gpt_reserva_usada|sem_tabela,
    setor, tamanho, particoes: [...], avisos: [...]}. Cada partição tem inicio, tamanho, fim (bytes), tipo, tipo_nome,
    sistema (o que o conteúdo mostra: fat32, ntfs, ext4... ou '') e avisos."""
    setor = setor or getattr(l, 'setor', 512) or 512
    res = {'esquema': 'nenhum', 'estado': 'sem_tabela', 'setor': setor, 'tamanho': l.tamanho, 'particoes': [], 'avisos': []}
    if l.tamanho < 512:
        res['avisos'].append('A origem tem menos de um setor.')
        return res
    b0 = _le(l, 0, 512)
    if len(b0) < 512:
        res['estado'] = 'tabela_danificada'
        res['avisos'].append('Não consegui ler o primeiro setor.')
        return res
    protetor = b0[510:512] == b'\x55\xaa' and _mbr_plausivel(b0) and any(e[1] == 0xEE for e in _entradas_mbr(b0))
    s_gpt = _achar_gpt(l, setor)
    if s_gpt is not None or protetor:
        res['setor'] = s_gpt or setor
        _ler_gpt(l, s_gpt or setor, res, protetor)
        _marcar_sobreposicao(res)
        return res
    for det in (det_apfs, det_ntfs, det_exfat, det_fat):
        r = det(b0)
        if r:                                              # setor 0 é o início de um volume: pen drive sem tabela
            res.update(esquema='volume', estado='sem_tabela')
            res['particoes'] = [_item(l, 'volume', 0, l.tamanho // setor, setor, tipo='', tipo_nome='Volume sem tabela',
                                      ativa=False, logica=False)]
            res['avisos'].append('Não há tabela de partições: o disco inteiro é um único volume.')
            return res
    if b0[510:512] == b'\x55\xaa' and _mbr_plausivel(b0) and any(e[1] for e in _entradas_mbr(b0)):
        _ler_mbr(l, b0, setor, res)
        _marcar_sobreposicao(res)
        return res
    apm = _ler_apm(l, b0)
    if apm:
        res.update(esquema='apm', estado='ok', particoes=apm)
        res['avisos'].append('Apple Partition Map: as partições só são listadas.')
        return res
    bsd = _bsd(l, 0, setor, 0, res['avisos'])
    if bsd:
        res.update(esquema='bsd', estado='ok', particoes=bsd)
        return res
    det = identificar(l, 0)
    if det:
        res.update(esquema='volume', estado='sem_tabela')
        res['particoes'] = [_item(l, 'volume', 0, l.tamanho // setor, setor, tipo='', tipo_nome='Volume sem tabela',
                                  ativa=False, logica=False)]
        res['avisos'].append('Não há tabela de partições: o disco inteiro é um único volume.')
        return res
    if b0[510:512] == b'\x55\xaa' and _mbr_plausivel(b0):
        res.update(esquema='mbr', estado='ok')
        res['avisos'].append('O MBR não tem nenhuma partição.')
    elif _mbr_plausivel(b0) and any(e[1] and e[3] for e in _entradas_mbr(b0)):
        _ler_mbr(l, b0, setor, res)
        _marcar_sobreposicao(res)
        return res
    elif not any(b0):
        res['avisos'].append('Os primeiros setores estão zerados: a tabela pode ter sido apagada. Use a busca de partições.')
    else:
        res['avisos'].append('Não reconheci nenhuma tabela de partições. Use a busca de partições.')
    return res


def _marcar_sobreposicao(res):
    ps = [p for p in res['particoes'] if not p.get('estendida')]
    for i, a in enumerate(ps):
        for b in ps[i + 1:]:
            if a['inicio'] < b['fim'] and b['inicio'] < a['fim']:
                for x, y in ((a, b), (b, a)):
                    x['avisos'].append('Sobrepõe a partição que começa em %d.' % y['inicio'])


# --- busca de partição perdida ------------------------------------------------------------------------------
# padrão, deslocamento do padrão dentro da estrutura, tipo da busca
PADROES = [(b'NTFS    ', 3, 'ntfs'), (b'EXFAT   ', 3, 'exfat'), (b'FAT32   ', 82, 'fat'), (b'FAT16   ', 54, 'fat'),
           (b'FAT12   ', 54, 'fat'), (b'FAT     ', 54, 'fat'), (b'\x53\xef', 56, 'ext'), (b'H+\x00\x04', 0, 'hfs'),
           (b'HX\x00\x05', 0, 'hfs'), (b'NXSB', 32, 'apfs'), (b'\x01CD001\x01', 0, 'iso')]
_ALINHADOS = ('ntfs', 'exfat', 'fat', 'apfs')


def _ler_tolerante(l, off, n):
    """Lê a janela; se falhar, refaz em pedaços e põe zeros no que não leu. Devolve (bytes, pedaços ruins)."""
    try:
        return l.ler(off, n), 0
    except OSError:
        partes, ruins = [], 0
        for p in range(off, off + n, PEDACO_RUIM):
            q = min(PEDACO_RUIM, off + n - p)
            try:
                partes.append(l.ler(p, q))
            except OSError:
                partes.append(bytes(q))
                ruins += 1
        return b''.join(partes), ruins


def hits_do_bloco(l, base, setor=512, bloco=BLOCO_BUSCA):
    """Sinais de início de sistema de arquivos com a estrutura começando em [base, base+bloco). Devolve (hits, ruins).
    Cada hit é um dicionário simples ({t, x, i}), próprio para gravar em JSON no checkpoint."""
    n = min(bloco + SOBRA, l.tamanho - base)
    if n <= 0:
        return [], 0
    janela, ruins = _ler_tolerante(l, base, n)
    hits = []
    for padrao, off, tipo in PADROES:
        p = janela.find(padrao)
        while p != -1:
            xs = p - off
            if 0 <= xs < bloco and not (tipo in _ALINHADOS and (base + xs) % setor):
                det, tam = _DET[tipo]
                info = det(janela[xs: xs + tam])
                if info:
                    hits.append({'t': tipo, 'x': base + xs, 'i': info})
            p = janela.find(padrao, p + 1)
    hits.sort(key=lambda h: h['x'])
    return hits, ruins


def varrer(l, setor=512, bloco=BLOCO_BUSCA, inicio=0):
    """Varre tudo de uma vez (para quem não precisa de pausa nem checkpoint). Devolve (hits, pedaços ruins)."""
    hits, ruins, pos = [], 0, inicio
    while pos < l.tamanho:
        h, r = hits_do_bloco(l, pos, setor, bloco)
        hits += h
        ruins += r
        pos += bloco
    return hits, ruins


def _cabe(c, l):
    return c['inicio'] + c['tamanho'] <= l.tamanho


def _fat_media(l, ini, i):
    b = _le(l, ini + i['reservado'] * i['bps'], 4)
    return len(b) >= 3 and b[0] == i['media'] and b[1:3] == b'\xff\xff'


def _gdt_ext(l, ini, i, grupo):
    """Primeiro descritor de grupo (na cópia do grupo dado): bitmaps e tabela de inodes dentro do sistema de arquivos."""
    bloco_gdt = i['fdb'] + grupo * i['bpg'] + 1 if grupo else i['fdb'] + 1
    b = _le(l, ini + bloco_gdt * i['bs'], 12)
    if len(b) < 12:
        return False
    return all(0 < _u32(b, o) < i['blocos'] for o in (0, 4, 8))


def _cons_ntfs(l, x, i, setor, add):
    fim = i['setores'] * i['bps']                                   # a cópia fica no setor depois do último do volume
    c = _ver(l, 'ntfs', x + fim)
    if c and c['setores'] == i['setores']:
        add(x, 'ntfs', i, 'principal', 'copia', *(('estrutura',) if _le(l, x + i['mft'], 4) == b'FILE' else ()))
        return
    ini = x - fim
    if ini >= 0 and ini % setor == 0 and _le(l, x + i['mft'], 4) != b'FILE':
        if _ver(l, 'ntfs', ini):
            return                                                  # é a cópia de um volume cujo início será achado por si
        add(ini, 'ntfs', i, 'copia', *(('estrutura',) if _le(l, ini + i['mft'], 4) == b'FILE' else ()))
        return
    add(x, 'ntfs', i, 'principal', *(('estrutura',) if _le(l, x + i['mft'], 4) == b'FILE' else ()))


def _cons_fat(l, x, i, setor, add):
    bps, oc, bk = i['bps'], i['oculto'] * i['bps'], i['bk']
    ev = ['principal']
    ini = x
    if bk:
        copia = _ver(l, 'fat', x + bk * bps)
        tem_copia = bool(copia and copia['setores'] == i['setores'])
        c_ini = x - bk * bps
        if oc == x:
            ev.append('posicao')
        elif oc == c_ini and c_ini >= 0:
            if _ver(l, 'fat', c_ini):
                return
            ev, ini = ['copia', 'posicao'], c_ini
        elif not tem_copia and c_ini >= 0 and c_ini % setor == 0 and _ver(l, 'fat', c_ini):
            return
        if tem_copia and ini == x:
            ev.append('copia')
    elif oc == x:
        ev.append('posicao')
    if ini % setor:
        return
    if _fat_media(l, ini, i):
        ev.append('estrutura')
    add(ini, 'fat', i, *ev)


def _cons_exfat(l, x, i, setor, add):
    bps, po = i['bps'], i['inicio_decl'] * i['bps']
    copia = _ver(l, 'exfat', x + 12 * bps)
    tem_copia = bool(copia and copia['setores'] == i['setores'])
    c_ini = x - 12 * bps

    def fat_ok(ini):
        return _le(l, ini + i['fat_off'] * bps, 4) == b'\xf8\xff\xff\xff'
    if po == x or (po == 0 and (tem_copia or fat_ok(x))):
        ini, ev = x, ['principal'] + (['posicao'] if po == x else []) + (['copia'] if tem_copia else [])
    elif c_ini >= 0 and c_ini % setor == 0 and (po == c_ini or (po == 0 and fat_ok(c_ini))):
        if _ver(l, 'exfat', c_ini):
            return
        ini, ev = c_ini, ['copia'] + (['posicao'] if po == c_ini else [])
    else:
        ini, ev = x, ['principal']
    if fat_ok(ini):
        ev.append('estrutura')
    add(ini, 'exfat', i, *ev)


def _cons_ext(l, x, i, setor, add):
    g = i['grupo']
    ini = x - 1024 if g == 0 else x - (i['fdb'] + g * i['bpg']) * i['bs']
    if ini < 0 or ini % setor or (g and not _grupo_com_copia(g, i['esparso'])):
        return
    ev = ['principal' if g == 0 else 'copia']
    if g == 0 and i['blocos'] > i['fdb'] + i['bpg']:
        c = _ver(l, 'ext', ini + (i['fdb'] + i['bpg']) * i['bs'])
        if c and c['grupo'] == 1 and c['blocos'] == i['blocos']:
            ev.append('copia')
    if _gdt_ext(l, ini, i, g):
        ev.append('estrutura')
    add(ini, 'ext', i, *ev)


def _grupo_com_copia(g, esparso):
    if not esparso or g == 1:
        return True
    for base in (3, 5, 7):
        n = base
        while n < g:
            n *= base
        if n == g:
            return True
    return False


def _cons_hfs(l, x, i, setor, add):
    ini = x - 1024
    alt = _ver(l, 'hfs', ini + i['tamanho'] - 1024) if ini >= 0 else None
    if alt and alt['blocos'] == i['blocos'] and ini % setor == 0:
        add(ini, 'hfs', i, 'principal', 'copia')
        return
    c_ini = x + 1024 - i['tamanho']
    if c_ini >= 0 and c_ini % setor == 0:
        if _ver(l, 'hfs', c_ini + 1024):
            return                                                  # é a cópia reserva de um volume que tem principal
        add(c_ini, 'hfs', i, 'copia')
    if ini >= 0 and ini % setor == 0:
        add(ini, 'hfs', i, 'principal')


def _fletcher64(dados):
    m = 0xFFFFFFFF
    s1 = s2 = 0
    for (w,) in struct.iter_unpack('<I', dados):
        s1 = (s1 + w) % m
        s2 = (s2 + s1) % m
    c1 = m - ((s1 + s2) % m)
    c2 = m - ((s1 + c1) % m)
    return (c2 << 32) | c1


def _cons_apfs(l, x, i, setor, add):
    bloco = _le(l, x, i['bs'])
    ev = ['principal']
    if len(bloco) == i['bs'] and _u64(bloco, 0) == _fletcher64(bloco[8:]):
        ev.append('estrutura')
    add(x, 'apfs', i, *ev)


def _cons_iso(l, x, i, setor, add):
    ini = x - 32768
    if ini < 0 or ini % setor:
        return
    ev = ['principal']
    b = _le(l, x, 16 * 2048)
    for k in range(0, len(b), 2048):
        if b[k: k + 7] == b'\xffCD001\x01':
            ev.append('estrutura')
            break
    add(ini, 'iso', i, *ev)


_CONS = {'ntfs': _cons_ntfs, 'fat': _cons_fat, 'exfat': _cons_exfat, 'ext': _cons_ext, 'hfs': _cons_hfs,
         'apfs': _cons_apfs, 'iso': _cons_iso}


def _nivel(p):
    return 'alta' if p >= 0.8 else ('media' if p >= 0.5 else 'baixa')


def consolidar(l, hits, setor=512):
    """Transforma os sinais em candidatos a partição (início, tamanho, sistema, confiança, evidências), juntando os
    sinais do mesmo volume (setor de boot + cópia reserva, superbloco + cópias). Só lê. Devolve a lista por início."""
    cands = {}

    def add(inicio, fam, info, *evid):
        c = cands.setdefault((inicio, fam), {'inicio': inicio, 'tamanho': info['tamanho'], 'sistema': info['sistema'],
                                             'evid': set(), 'avisos': []})
        c['evid'].update(evid)

    for h in hits:
        _CONS[h['t']](l, h['x'], h['i'], setor, add)
    saida = []
    for c in cands.values():
        if not _cabe(c, l):
            c['evid'].add('fora')
        pontos = max(0.0, min(1.0, round(sum(PESOS[e] for e in c['evid']), 2)))
        frases = [FRASES[e] for e in ('principal', 'copia', 'posicao', 'estrutura', 'fora') if e in c['evid']]
        if 'copia' in c['evid'] and 'principal' not in c['evid']:
            frases.append('só a cópia reserva sobrou: o início foi calculado a partir dela')
        if 'fora' in c['evid']:
            c['avisos'].append('A partição passaria do fim do disco (imagem cortada ou tamanho errado).')
        saida.append({'inicio': c['inicio'], 'tamanho': c['tamanho'], 'fim': c['inicio'] + c['tamanho'],
                      'sistema': c['sistema'], 'confianca': pontos, 'nivel': _nivel(pontos), 'evidencias': frases,
                      'avisos': c['avisos']})
    saida.sort(key=lambda c: (c['inicio'], -c['confianca']))
    return saida


def propor(candidatos, minimo=0.5):
    """A tabela proposta: dos candidatos com confiança >= minimo, os de maior confiança que não se sobrepõem."""
    escolhidos = []
    for c in sorted(candidatos, key=lambda c: (-c['confianca'], c['inicio'])):
        if c['confianca'] < minimo:
            break
        if all(c['inicio'] >= e['fim'] or e['inicio'] >= c['fim'] for e in escolhidos):
            escolhidos.append(c)
    return sorted(escolhidos, key=lambda c: c['inicio'])


def buscar(l, setor=512, bloco=BLOCO_BUSCA, minimo=0.5):
    """Busca completa de uma vez: devolve {candidatos, proposta, ruins}. O job `particoes` faz o mesmo por etapas."""
    hits, ruins = varrer(l, setor, bloco)
    cands = consolidar(l, hits, setor)
    return {'candidatos': cands, 'proposta': propor(cands, minimo), 'ruins': ruins}


# --- tabela reconstruída para aplicar à mão -------------------------------------------------------------------
def _chs(lba):
    if lba >= 1024 * 255 * 63:
        return b'\xfe\xff\xff'
    c, h, s = lba // (255 * 63), (lba // 63) % 255, lba % 63 + 1
    return bytes([h, ((c >> 2) & 0xC0) | s, c & 0xFF])


def montar_mbr(proposta, tamanho, setor=512):
    """Setor 0 de um MBR com a tabela proposta (sem código de boot). Devolve (512 bytes ou None, avisos)."""
    avisos, ents = [], []
    for p in proposta:
        t = TIPO_MBR_POR_SISTEMA.get(p.get('sistema'))
        ini, n = p['inicio'] // setor, p['tamanho'] // setor
        if t is None:
            avisos.append('A partição em %d (%s) não tem tipo de MBR conhecido: ficou de fora.' % (p['inicio'], p.get('sistema') or '?'))
        elif ini >= 1 << 32 or n >= 1 << 32 or ini + n > 1 << 32:
            avisos.append('A partição em %d passa de 2 TiB: não cabe em MBR.' % p['inicio'])
        else:
            ents.append((t, ini, n))
    if len(ents) > 4:
        avisos.append('MBR só tem 4 entradas primárias: as %d partições seguintes ficaram de fora.' % (len(ents) - 4))
        ents = ents[:4]
    if not ents:
        return None, avisos + ['Nenhuma partição cabe num MBR.']
    b = bytearray(512)
    for k, (t, ini, n) in enumerate(ents):
        struct.pack_into('<B3sB3sII', b, 446 + 16 * k, 0, _chs(ini), t, _chs(ini + n - 1), ini, n)
    b[510:512] = b'\x55\xaa'
    return bytes(b), avisos


def _guid_estavel(*partes):
    return uuid.UUID(bytes=hashlib.sha256('|'.join(map(str, partes)).encode()).digest()[:16], version=4)


def _cab_gpt(meu, outro, primeiro, ultimo, guid, ent_lba, crc_ent, setor):
    h = bytearray(92)
    h[:8] = b'EFI PART'
    struct.pack_into('<IIIIQQQQ16sQIII', h, 8, 0x00010000, 92, 0, 0, meu, outro, primeiro, ultimo, guid.bytes_le,
                     ent_lba, ENTRADAS_GPT_PADRAO, 128, crc_ent)
    struct.pack_into('<I', h, 16, zlib.crc32(bytes(h)) & 0xFFFFFFFF)
    return bytes(h).ljust(setor, b'\0')


def montar_gpt(proposta, tamanho, setor=512, semente=''):
    """GPT completo com a tabela proposta. Devolve (inicio, fim, avisos):
    `inicio` = setores 0..(1+n): MBR protetor, cabeçalho e entradas; vai no setor 0 do disco;
    `fim` = entradas reservas + cabeçalho reserva; vai no setor (total - n - 1).
    Devolve (None, None, avisos) se o disco é pequeno demais."""
    total = tamanho // setor
    n = -(-ENTRADAS_GPT_PADRAO * 128 // setor)                       # setores ocupados pelas entradas
    if total < 2 * n + 8:
        return None, None, ['O disco é pequeno demais para um GPT.']
    primeiro, ultimo = 2 + n, total - 2 - n
    guid_disco = _guid_estavel('disco', semente, tamanho)
    avisos, ents = [], bytearray(ENTRADAS_GPT_PADRAO * 128)
    k = 0
    for p in proposta:
        ini, fim = p['inicio'] // setor, p['fim'] // setor - 1
        if k >= ENTRADAS_GPT_PADRAO:
            avisos.append('Partições demais para o GPT: as seguintes ficaram de fora.')
            break
        if ini < primeiro or fim > ultimo or fim < ini:
            avisos.append('A partição em %d fica fora da área usável do GPT: ficou de fora.' % p['inicio'])
            continue
        tipo = uuid.UUID(GUID_POR_SISTEMA.get(p.get('sistema'), GUID_DADOS_BASICOS))
        nome = ('Recuperada %d' % (k + 1)).encode('utf-16-le')
        struct.pack_into('<16s16sQQQ72s', ents, 128 * k, tipo.bytes_le, _guid_estavel('part', semente, ini, fim).bytes_le,
                         ini, fim, 0, nome)
        k += 1
    ents = bytes(ents)
    crc_ent = zlib.crc32(ents) & 0xFFFFFFFF
    protetor = bytearray(setor)
    struct.pack_into('<B3sB3sII', protetor, 446, 0, _chs(1), 0xEE, _chs(min(total - 1, 1 << 24)), 1, min(total - 1, 0xFFFFFFFF))
    protetor[510:512] = b'\x55\xaa'
    inicio = (bytes(protetor) + _cab_gpt(1, total - 1, primeiro, ultimo, guid_disco, 2, crc_ent, setor)
              + ents.ljust(n * setor, b'\0'))
    fim = ents.ljust(n * setor, b'\0') + _cab_gpt(total - 1, 1, primeiro, ultimo, guid_disco, total - 1 - n, crc_ent, setor)
    return inicio, fim, avisos
