# -*- coding: utf-8 -*-
"""Validadores do carving: dado um possível início de arquivo, descobrem o fim pelo melhor método do tipo
(tamanho no cabeçalho > estrutura interna > rodapé > tamanho máximo) e conferem se o conteúdo faz sentido.

Cada validador é `f(j, off, limite, assin)`:
  j       Janela de leitura (só leitura) sobre a origem;
  off     onde o arquivo começaria;
  limite  quantos bytes, no máximo, o arquivo pode ter a partir de off (tamanho_max e fim da mídia);
  assin   a linha da tabela de assinaturas.
Devolve None (não é um arquivo desse tipo) ou um dict com `tamanho`, `estado` (bom | parcial | ruim), `pontuacao`
(0 a 100) e, quando houver, `tipo`, `extensao`, `grupo`, `data` (AAAA-MM-DD HH:MM:SS), `largura`, `altura`.

  bom      estrutura inteira e coerente, do cabeçalho ao fim;
  parcial  o começo é válido mas o fim não está lá (cortado, ou o resto foi sobrescrito); `tamanho` é o trecho válido;
  ruim     achou o arquivo inteiro mas o conteúdo falha numa conferência (por exemplo CRC errado).

Tudo aqui lê bytes alheios: nenhum campo é confiável, todo tamanho é conferido contra `limite` antes de ser usado.
"""
import re
import struct
import zlib

ZEROS = 4096                      # sequência de zeros que marca o fim de um conteúdo sem estrutura (cortado)


class Janela:
    """Leitura com cache sobre o Leitor (os validadores leem pedaços pequenos, muitas vezes, em ordem)."""

    def __init__(self, leitor, bloco=256 * 1024):
        self.leitor = leitor
        self.tamanho = leitor.tamanho
        self.bloco = bloco
        self._ini = 0
        self._buf = b''
        self.erros = 0

    def ler(self, off, n):
        """Até n bytes a partir de off (menos no fim da mídia). Setor ilegível devolve vazio, nunca levanta."""
        if n <= 0 or off < 0 or off >= self.tamanho:
            return b''
        n = min(n, self.tamanho - off)
        if self._ini <= off and off + n <= self._ini + len(self._buf):
            return self._buf[off - self._ini: off - self._ini + n]
        pedido = n if n >= self.bloco else min(self.bloco, self.tamanho - off)
        try:
            dados = self.leitor.ler(off, pedido)
        except OSError:
            self.erros += 1
            if pedido == n:
                return b''
            try:
                dados = self.leitor.ler(off, n)
            except OSError:
                return b''
        if n < self.bloco:
            self._ini, self._buf = off, dados
        return dados[:n]


def res(tamanho, estado, pontuacao, **extra):
    d = {'tamanho': int(tamanho), 'estado': estado, 'pontuacao': int(pontuacao)}
    d.update({k: v for k, v in extra.items() if v is not None})
    return d


def u16(b, o=0, le=True):
    return struct.unpack_from('<H' if le else '>H', b, o)[0]


def u32(b, o=0, le=True):
    return struct.unpack_from('<I' if le else '>I', b, o)[0]


def u64(b, o=0, le=True):
    return struct.unpack_from('<Q' if le else '>Q', b, o)[0]


def _data(ano, mes, dia, h=0, mi=0, s=0):
    if 1970 <= ano <= 2100 and 1 <= mes <= 12 and 1 <= dia <= 31 and h < 24 and mi < 60 and s < 62:
        return '%04d-%02d-%02d %02d:%02d:%02d' % (ano, mes, dia, h, mi, min(s, 59))
    return None


def _data_exif(txt):
    m = re.fullmatch(rb'(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})', txt[:19])
    return _data(*(int(x) for x in m.groups())) if m else None


def _ate_zeros(j, ini, fim, corrida=ZEROS):
    """Posição onde começa a primeira corrida de `corrida` zeros em [ini, fim), ou `fim` (ou o fim da mídia)."""
    pos = ini
    alvo = bytes(corrida)
    while pos < fim:
        n = min(1 << 20, fim - pos)
        c = j.ler(pos, n)
        if not c:
            return pos
        k = c.find(alvo)
        if k >= 0:
            return pos + k
        if len(c) < n:
            return pos + len(c)
        pos += max(1, len(c) - corrida + 1)
    return fim


# --- TIFF (e o EXIF dentro do JPEG) ----------------------------------------------------------------------------
TAM_TIPO = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}


def _ifd_em_memoria(t, le, pos):
    """Entradas (tag, tipo, contagem, campo_valor) de um IFD dentro de `t` (bytes). Lista vazia se não cabe."""
    if pos < 8 or pos + 2 > len(t):
        return []
    n = u16(t, pos, le)
    if n == 0 or n > 2048 or pos + 2 + n * 12 > len(t):
        return []
    return [(u16(t, pos + 2 + k * 12, le), u16(t, pos + 4 + k * 12, le), u32(t, pos + 6 + k * 12, le),
             t[pos + 10 + k * 12: pos + 14 + k * 12]) for k in range(n)]


def _exif_data(t):
    """Data da foto (DateTimeOriginal, senão DateTime) num TIFF/EXIF em memória; None se não achar."""
    if len(t) < 16 or t[:2] not in (b'II', b'MM'):
        return None
    le = t[:2] == b'II'
    if u16(t, 2, le) != 42:
        return None
    ifd0 = _ifd_em_memoria(t, le, u32(t, 4, le))
    candidatos = {}
    for tag, tp, cnt, vf in ifd0:
        if tag == 0x0132 and tp == 2 and cnt == 20:
            candidatos[0x0132] = u32(vf, 0, le)
        if tag == 0x8769:
            for tag2, tp2, cnt2, vf2 in _ifd_em_memoria(t, le, u32(vf, 0, le)):
                if tag2 in (0x9003, 0x9004) and tp2 == 2 and cnt2 == 20:
                    candidatos[tag2] = u32(vf2, 0, le)
    for tag in (0x9003, 0x9004, 0x0132):
        if tag in candidatos:
            d = _data_exif(t[candidatos[tag]: candidatos[tag] + 19])
            if d:
                return d
    return None


# --- JPEG -------------------------------------------------------------------------------------------------------
_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
_PODE_APOS_VARREDURA = frozenset([0xC4, 0xDA, 0xD9, 0xDB, 0xDC, 0xDD, 0xFE] + list(range(0xE0, 0xF0)))


def _fim_da_varredura_jpeg(j, pos, fim):
    """Percorre os dados comprimidos (FF00 e RSTn fazem parte deles). Devolve (posição, motivo): o próximo marcador de
    verdade, ou onde acabaram os dados ('zeros' = corrida de zeros, 'fim' = fim da mídia ou do limite)."""
    while pos < fim:
        n = min(65536, fim - pos)
        c = j.ler(pos, n)
        if not c:
            return pos, 'fim'
        z = c.find(bytes(ZEROS))
        lim = len(c) if z < 0 else z
        i = 0
        while True:
            i = c.find(b'\xff', i, lim)
            if i < 0:
                break
            if i + 1 >= len(c):
                break
            b = c[i + 1]
            if b == 0 or 0xD0 <= b <= 0xD7:
                i += 2
            elif b == 0xFF:
                i += 1
            else:
                return pos + i, 'marcador'
        if z >= 0:
            return pos + z, 'zeros'
        if len(c) < n:
            return pos + len(c), 'fim'
        pos += max(1, len(c) - ZEROS)
    return fim, 'fim'


def jpg(j, off, limite, a):
    if j.ler(off, 3) != b'\xff\xd8\xff':
        return None
    fim = off + limite
    pos = off + 2
    largura = altura = data = None
    viu_sos = False
    while True:
        cab = j.ler(pos, 4) if pos + 2 <= fim else b''
        if len(cab) < 2:
            break                                                # acabou a mídia ou o limite no meio da estrutura
        if cab[0] != 0xFF:
            break
        m = cab[1]
        if m == 0xFF:
            pos += 1
            continue
        if m == 0xD9:
            if not viu_sos:
                return None
            return res(pos + 2 - off, 'bom', 100, largura=largura, altura=altura, data=data)
        if m == 0x01 or 0xD0 <= m <= 0xD7:
            pos += 2
            continue
        if m == 0xD8 or m == 0x00 or len(cab) < 4:
            break
        tam = u16(cab, 2, False)
        if tam < 2:
            break
        if m == 0xE1 and data is None:
            corpo = j.ler(pos + 4, min(tam - 2, 65536))
            if corpo[:6] == b'Exif\x00\x00':
                data = _exif_data(corpo[6:])
        if m in _SOF and largura is None:
            sof = j.ler(pos + 4, 6)
            if len(sof) == 6:
                altura, largura = u16(sof, 1, False), u16(sof, 3, False)
        pos += 2 + tam
        if m == 0xDA:
            viu_sos = True
            pos, motivo = _fim_da_varredura_jpeg(j, pos, fim)
            if motivo != 'marcador':
                break
            prox = j.ler(pos + 1, 1)
            if not prox or prox[0] not in _PODE_APOS_VARREDURA:
                break
    if not viu_sos:
        return None if largura is None else res(min(pos, fim) - off, 'ruim', 10, largura=largura, altura=altura, data=data)
    return res(min(pos, fim) - off, 'parcial', 50, largura=largura, altura=altura, data=data)


# --- PNG ----------------------------------------------------------------------------------------------------------
def png(j, off, limite, a):
    if j.ler(off, 8) != b'\x89PNG\r\n\x1a\n':
        return None
    fim = off + limite
    pos = off + 8
    largura = altura = None
    crc_ruins = 0
    primeiro = True
    while True:
        cab = j.ler(pos, 8) if pos + 12 <= fim else b''
        if len(cab) < 8:
            break
        n, tp = u32(cab, 0, False), cab[4:8]
        if not re.fullmatch(rb'[A-Za-z]{4}', tp) or n > 0x7FFFFFFF:
            break
        if primeiro:
            if tp != b'IHDR' or n != 13:
                return None
            primeiro = False
        fim_chunk = pos + 12 + n
        if fim_chunk > fim:
            break
        crc = zlib.crc32(tp)
        p, falta = pos + 8, n
        while falta:
            pedaco = j.ler(p, min(falta, 1 << 20))
            if not pedaco:
                break
            crc = zlib.crc32(pedaco, crc)
            p, falta = p + len(pedaco), falta - len(pedaco)
        guardado = j.ler(pos + 8 + n, 4)
        if falta or len(guardado) < 4:
            break
        if u32(guardado, 0, False) != crc & 0xFFFFFFFF:
            crc_ruins += 1
        if tp == b'IHDR':
            ih = j.ler(pos + 8, 8)
            largura, altura = u32(ih, 0, False), u32(ih, 4, False)
        pos = fim_chunk
        if tp == b'IEND':
            if crc_ruins:
                return res(pos - off, 'ruim', 20, largura=largura, altura=altura)
            return res(pos - off, 'bom', 100, largura=largura, altura=altura)
    if primeiro:
        return None
    return res(pos - off, 'parcial', 50, largura=largura, altura=altura)


# --- GIF ----------------------------------------------------------------------------------------------------------
def _subblocos(j, pos, fim):
    """Posição depois dos sub-blocos (terminados por um de tamanho 0); None se acabam antes."""
    while pos < fim:
        b = j.ler(pos, 1)
        if not b:
            return None
        if b[0] == 0:
            return pos + 1
        pos += 1 + b[0]
    return None


def gif(j, off, limite, a):
    h = j.ler(off, 13)
    if len(h) < 13 or h[:6] not in (b'GIF87a', b'GIF89a'):
        return None
    largura, altura = u16(h, 6), u16(h, 8)
    if largura == 0 or altura == 0:
        return None
    fim = off + limite
    pos = off + 13
    if h[10] & 0x80:
        pos += 3 * (2 << (h[10] & 7))
    quadros = 0
    while pos < fim:
        b = j.ler(pos, 1)
        if not b:
            break
        if b[0] == 0x3B:
            return res(pos + 1 - off, 'bom', 100, largura=largura, altura=altura)
        if b[0] == 0x21:
            novo = _subblocos(j, pos + 2, fim)
        elif b[0] == 0x2C:
            d = j.ler(pos, 10)
            if len(d) < 10:
                break
            p = pos + 10 + (3 * (2 << (d[9] & 7)) if d[9] & 0x80 else 0) + 1
            novo = _subblocos(j, p, fim)
            quadros += 1
        else:
            break
        if novo is None:
            break
        pos = novo
    if not quadros:
        return res(min(pos, fim) - off, 'ruim', 10, largura=largura, altura=altura)
    return res(min(pos, fim) - off, 'parcial', 40, largura=largura, altura=altura)


# --- BMP ----------------------------------------------------------------------------------------------------------
def bmp(j, off, limite, a):
    h = j.ler(off, 34)
    if len(h) < 30 or h[:2] != b'BM' or h[6:10] != b'\x00\x00\x00\x00':
        return None
    tam, ini_pixels, dib = u32(h, 2), u32(h, 10), u32(h, 14)
    if dib not in (12, 40, 52, 56, 64, 108, 124) or ini_pixels < 14 + dib or ini_pixels >= tam or tam > a['tamanho_max']:
        return None
    if dib == 12:
        largura, altura, bits = u16(h, 18), u16(h, 20), u16(h, 24)
    else:
        largura, altura, bits = u32(h, 18), abs(struct.unpack_from('<i', h, 22)[0]), u16(h, 28)
    if not 0 < largura <= 65535 or not 0 < altura <= 65535 or bits not in (1, 4, 8, 16, 24, 32):
        return None
    compressao = u32(h, 30) if dib >= 40 and len(h) >= 34 else 0
    if compressao not in (0, 3) and dib >= 40 and not (compressao <= 6):
        return None
    if compressao in (0, 3):
        # sem compressão: os pixels têm de caber no tamanho declarado (cabeçalho que mente = falso positivo)
        if ini_pixels + (largura * bits + 31) // 32 * 4 * altura > tam:
            return None
    if tam > limite:
        return res(limite, 'parcial', 50, largura=largura, altura=altura)
    return res(tam, 'bom', 100, largura=largura, altura=altura)


# --- TIFF e RAW de câmera ---------------------------------------------------------------------------------------
_RAW_POR_MARCA = (('NIKON', 'NEF'), ('SONY', 'ARW'))


def tiff(j, off, limite, a):
    cab = j.ler(off, 16)
    if len(cab) < 16 or cab[:4] not in (b'II*\x00', b'MM\x00*'):
        return None
    le = cab[:2] == b'II'
    ifd0 = u32(cab, 4, le)
    if ifd0 < 8 or ifd0 >= limite:
        return None
    maximo = 8
    visitados, fila = set(), [ifd0]
    largura = altura = marca = None
    dng = False
    truncado = False
    primeiro = True
    while fila and len(visitados) < 64:
        pos = fila.pop()
        if pos in visitados or pos < 8 or pos + 2 > limite:
            continue
        visitados.add(pos)
        cabn = j.ler(off + pos, 2)
        n = u16(cabn, 0, le) if len(cabn) == 2 else 0
        if n == 0 or n > 2048:
            if primeiro:
                return None
            continue
        ent = j.ler(off + pos + 2, n * 12 + 4)
        if len(ent) < n * 12 + 4:
            truncado = True
            if primeiro:
                return None
            continue
        primeiro = False
        maximo = max(maximo, pos + 2 + n * 12 + 4)
        offs = cnts = None
        for k in range(n):
            tag, tp, cnt = u16(ent, k * 12, le), u16(ent, k * 12 + 2, le), u32(ent, k * 12 + 4, le)
            vf = ent[k * 12 + 8: k * 12 + 12]
            tam = TAM_TIPO.get(tp)
            if not tam:
                continue
            total = tam * cnt
            voff = u32(vf, 0, le) if total > 4 else None
            if voff is not None:
                if voff + total > limite:
                    truncado = True
                    continue
                maximo = max(maximo, voff + total)

            def valores(maxn=1 << 16):
                if cnt > maxn or tp not in (3, 4):
                    return []
                if voff is None:
                    bruto, base = vf, 0
                else:
                    bruto, base = j.ler(off + voff, total), 0
                if len(bruto) < total:
                    return []
                f, w = ('H', 2) if tp == 3 else ('I', 4)
                return list(struct.unpack(('<' if le else '>') + f * cnt, bruto[:total]))
            if tag in (273, 324):
                offs = valores()
            elif tag in (279, 325):
                cnts = valores()
            elif tag in (330, 34665, 34853) and tp in (4, 13):
                fila.extend(valores() if tag == 330 else [u32(vf, 0, le)])
            elif tag == 256 and largura is None and tp in (3, 4):
                largura = u16(vf, 0, le) if tp == 3 else u32(vf, 0, le)
            elif tag == 257 and altura is None and tp in (3, 4):
                altura = u16(vf, 0, le) if tp == 3 else u32(vf, 0, le)
            elif tag == 271 and tp == 2 and marca is None:
                marca = (j.ler(off + voff, min(total, 32)) if voff is not None else vf).split(b'\x00')[0].decode('latin-1')
            elif tag == 50706:
                dng = True
        if offs and cnts and len(offs) == len(cnts):
            maximo = max(maximo, max(o + c for o, c in zip(offs, cnts)))
        prox = u32(ent, n * 12, le)
        if prox:
            fila.append(prox)
    if maximo > limite:
        truncado, maximo = True, limite
    data = _exif_data(j.ler(off, min(limite, 1 << 20)))
    tipo = 'TIFF'
    if cab[8:10] == b'CR':
        tipo = 'CR2'
    elif dng:
        tipo = 'DNG'
    elif marca:
        tipo = next((t for m, t in _RAW_POR_MARCA if marca.upper().startswith(m)), 'TIFF')
    extra = {'tipo': tipo, 'extensao': 'tif' if tipo == 'TIFF' else tipo.lower(), 'largura': largura,
             'altura': altura, 'data': data}
    if truncado:
        return res(maximo, 'parcial', 50, **extra)
    return res(maximo, 'bom', 100, **extra)


# --- caixas ISO (MP4, MOV, HEIC) ------------------------------------------------------------------------------------
_CAIXAS = frozenset(b.encode() for b in (
    'moov', 'mdat', 'free', 'skip', 'wide', 'pnot', 'uuid', 'meta', 'moof', 'mfra', 'sidx', 'styp', 'pdin', 'junk',
    'idat', 'iloc', 'iinf', 'iprp', 'ssix', 'prft', 'emsg', 'PICT', 'prfl', 'cmov'))
_MARCAS_HEIC = (b'heic', b'heix', b'hevc', b'hevx', b'heim', b'heis', b'mif1', b'msf1')
_EPOCA_1904 = 2082844800


def _data_mvhd(j, pos, tam_caixa):
    corpo = j.ler(pos + 8, min(tam_caixa - 8, 160))
    i = corpo.find(b'mvhd')
    if i < 0 or len(corpo) < i + 16:
        return None
    versao = corpo[i + 4]
    criacao = u64(corpo, i + 8, False) if versao == 1 and len(corpo) >= i + 16 else u32(corpo, i + 8, False)
    ts = criacao - _EPOCA_1904
    if not 0 <= ts <= 4102444800:                  # de 1970 a 2100; fora disso é lixo (gmtime estoura com 64 bits)
        return None
    import time
    t = time.gmtime(ts)
    return _data(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


def iso(j, off, limite, a):
    cab = j.ler(off, 12)
    if len(cab) < 12 or cab[4:8] != b'ftyp':
        return None
    if not 16 <= u32(cab, 0, False) <= 512:
        return None
    marca = cab[8:12]
    fim = off + limite
    pos = off
    vistos = set()
    data = None
    truncado = False
    primeira = True
    while pos < fim:
        h = j.ler(pos, 16)
        if len(h) < 8:
            break
        tam, tp = u32(h, 0, False), h[4:8]
        if not (tp == b'ftyp' if primeira else tp in _CAIXAS):
            break
        cab_tam = 8
        if tam == 1:
            if len(h) < 16:
                break
            tam, cab_tam = u64(h, 8, False), 16
        elif tam == 0:
            tam = j.tamanho - pos
            truncado = True
        if tam < cab_tam:
            break
        if tp == b'moov' and data is None:
            data = _data_mvhd(j, pos, tam)
        vistos.add(tp)
        primeira = False
        if pos + tam > fim:
            pos, truncado = fim, True
            break
        pos += tam
    if primeira:
        return None
    heic = marca in _MARCAS_HEIC
    if heic:
        tipo, ext, grupo = 'HEIC', 'heic', 'foto'
    elif marca == b'qt  ':
        tipo, ext, grupo = 'MOV', 'mov', 'video'
    elif marca in (b'M4A ', b'M4B ', b'M4P '):
        tipo, ext, grupo = 'M4A', 'm4a', 'audio'
    else:
        tipo, ext, grupo = 'MP4', 'mp4', 'video'
    completo = (b'meta' in vistos) if heic else (b'moov' in vistos and b'mdat' in vistos)
    extra = {'tipo': tipo, 'extensao': ext, 'grupo': grupo, 'data': data}
    if truncado or not completo:
        return res(pos - off, 'parcial', 40 if truncado else 30, **extra)
    return res(pos - off, 'bom', 100, **extra)


# --- RIFF (AVI, WAV) -------------------------------------------------------------------------------------------------
def riff(j, off, limite, a):
    forma = a.get('forma', 'WAVE').encode('latin-1')
    h = j.ler(off, 20)
    if len(h) < 20 or h[:4] != b'RIFF' or h[8:12] != forma:
        return None
    esperado = b'fmt ' if forma == b'WAVE' else b'LIST'
    if h[12:16] != esperado:
        return None
    tam = u32(h, 4) + 8
    if tam < 20 or tam > a['tamanho_max']:
        return None
    fim_riff = off + tam
    alcance = min(fim_riff, off + limite)
    # confere o corpo: os pedaços (chunks) têm de se encadear até o fim declarado, sem sair dele
    pos, vistos, inteiro = off + 12, set(), True
    for _ in range(1000000):
        if pos >= alcance - 1:
            break
        c = j.ler(pos, 12)
        if len(c) < 8 or not re.fullmatch(rb'[ -~]{4}', c[:4]):
            inteiro = False
            break
        tam_chunk = u32(c, 4)
        vistos.add(c[:4])
        if c[:4] == b'LIST' and len(c) >= 12:
            vistos.add(c[8:12])
        fim_chunk = pos + 8 + tam_chunk
        if fim_chunk > fim_riff:
            inteiro = False
            break
        pos = fim_chunk + (tam_chunk & 1)
    obrigatorios = (b'fmt ', b'data') if forma == b'WAVE' else (b'hdrl', b'movi')
    if fim_riff > off + limite:
        return res(limite, 'parcial', 50)                    # a mídia acaba antes do fim declarado
    if not inteiro:
        return res(max(20, min(pos, fim_riff) - off), 'parcial', 40)
    if any(o not in vistos for o in obrigatorios):
        return res(tam, 'parcial', 40)
    return res(tam, 'bom', 100)


# --- MP3 -------------------------------------------------------------------------------------------------------------
_BR = {(3, 3): [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
       (3, 2): [32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
       (3, 1): [32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
       (2, 1): [32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
       (2, 2): [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
       (2, 3): [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]}
_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _quadro_mpeg(f):
    """Tamanho do quadro MPEG de áudio que começa em f (4 bytes), ou None."""
    if len(f) < 4 or f[0] != 0xFF or (f[1] & 0xE0) != 0xE0:
        return None
    ver, camada = (f[1] >> 3) & 3, 4 - ((f[1] >> 1) & 3)      # ver: 3 = MPEG1, 2 = MPEG2, 0 = 2.5; camada 1..3
    br, sr, pad = f[2] >> 4, (f[2] >> 2) & 3, (f[2] >> 1) & 1
    if ver == 1 or camada == 4 or br in (0, 15) or sr == 3:
        return None
    kbps = _BR[(3 if ver == 3 else 2, camada)][br - 1] * 1000
    hz = _SR[ver][sr]
    if camada == 1:
        return (12 * kbps // hz + pad) * 4
    return (144 if ver == 3 or camada == 2 else 72) * kbps // hz + pad


def mp3(j, off, limite, a):
    h = j.ler(off, 10)
    if len(h) < 10 or h[:3] != b'ID3' or h[3] not in (2, 3, 4) or h[4] == 0xFF or any(b & 0x80 for b in h[6:10]):
        return None
    pos = off + 10 + (h[6] << 21 | h[7] << 14 | h[8] << 7 | h[9]) + (10 if h[3] == 4 and h[5] & 0x10 else 0)
    fim = off + limite
    quadros = 0
    while pos + 4 <= fim:
        n = _quadro_mpeg(j.ler(pos, 4))
        if not n:
            break
        pos += n
        quadros += 1
    if quadros < 3:
        return None
    parou_por_falta = pos + 4 > fim
    if not parou_por_falta and j.ler(pos, 3) == b'TAG':
        pos += 128
    if pos > fim:
        return res(fim - off, 'parcial', 50)
    if parou_por_falta:
        return res(pos - off, 'parcial', 60)
    return res(pos - off, 'bom', 90)


# --- PDF -------------------------------------------------------------------------------------------------------------
_CONTINUA_PDF = re.compile(rb'\d+\s+\d+\s+obj|xref|trailer|startxref')
_PDF_DATA = re.compile(rb'/CreationDate\s*\(D:(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})')


def pdf(j, off, limite, a):
    cabeca = j.ler(off, 1024)
    if not re.match(rb'%PDF-[12]\.\d', cabeca) or b'obj' not in cabeca:
        return None
    fim = off + limite
    pos = off + 8
    fim_pdf = None
    while pos < fim:
        n = min(1 << 20, fim - pos)
        c = j.ler(pos, n)
        if not c:
            break
        i = c.find(b'%%EOF')
        if i < 0:
            if len(c) < n:
                break
            pos += max(1, len(c) - 4)
            continue
        e = pos + i + 5
        cauda = j.ler(e, 48)
        k = 0
        while k < len(cauda) and cauda[k] in b'\r\n \t':
            k += 1
        fim_pdf = e + (2 if cauda[:2] == b'\r\n' else 1 if cauda[:1] in (b'\r', b'\n') else 0)
        if _CONTINUA_PDF.match(cauda[k:]):
            pos = e
            continue
        break
    # a data: dicionário Info costuma estar no começo (arquivo pequeno) ou no fim
    data = None
    for ini, tam in ((off, 16384), (max(off, (fim_pdf or off) - 4096), 4096)):
        m = _PDF_DATA.search(j.ler(ini, tam))
        if m:
            data = _data(*(int(x) for x in m.groups()))
            if data:
                break
    if fim_pdf is None:
        return res(_ate_zeros(j, off + 8, fim) - off, 'parcial', 30, data=data)
    tail = j.ler(max(off, fim_pdf - 1100), min(1100, fim_pdf - off))
    k = tail.rfind(b'startxref')
    ok = False
    if k >= 0:
        m = re.match(rb'startxref\s+(\d+)', tail[k:])
        if m:
            alvo = j.ler(off + int(m.group(1)), 40) if int(m.group(1)) < limite else b''
            ok = bool(re.match(rb'xref|\d+\s+\d+\s+obj', alvo))
    if ok:
        return res(fim_pdf - off, 'bom', 100, data=data)
    return res(fim_pdf - off, 'parcial', 60, data=data)


# --- ZIP e derivados (DOCX, XLSX, PPTX, ODT, ODS, ODP, EPUB, JAR, APK) -------------------------------------------------
_PK_LOCAL, _PK_CENTRAL, _PK_FIM = b'PK\x03\x04', b'PK\x01\x02', b'PK\x05\x06'
_METODOS_ZIP = frozenset([0, 8, 9, 12, 14, 93, 95, 98])
_MIMES = {b'application/vnd.oasis.opendocument.text': ('ODT', 'documento'),
          b'application/vnd.oasis.opendocument.spreadsheet': ('ODS', 'documento'),
          b'application/vnd.oasis.opendocument.presentation': ('ODP', 'documento'),
          b'application/epub+zip': ('EPUB', 'documento')}


def _data_dos(d, t):
    return _data(1980 + (d >> 9), (d >> 5) & 15, d & 31, t >> 11, (t >> 5) & 63, (t & 31) * 2)


def _zip_caminhar(j, off, fim):
    """Anda pelos cabeçalhos locais enquanto o tamanho de cada entrada é conhecido. Devolve (pos, nomes, motivo): `pos` é
    o fim da última entrada inteira; motivo = 'fim' (o que vem depois não é cabeçalho local), 'desc' (entrada com
    descritor de dados ou zip64: tamanho desconhecido) ou 'cortado' (a próxima entrada não cabe)."""
    pos, nomes = off, []
    while pos + 30 <= fim:
        h = j.ler(pos, 30)
        if len(h) < 30 or h[:4] != _PK_LOCAL:
            return pos, nomes, 'fim'
        flags, tc = u16(h, 6), u32(h, 18)
        nlen, xlen = u16(h, 26), u16(h, 28)
        if flags & 8 or tc == 0xFFFFFFFF:
            return pos, nomes, 'desc'
        novo = pos + 30 + nlen + xlen + tc
        if novo > fim:
            return pos, nomes, 'cortado'
        nomes.append(j.ler(pos + 30, nlen))
        pos = novo
    return pos, nomes, 'cortado' if pos < fim else 'fim'


def _zip_fechar(j, off, ini_cd, fim):
    """Lê o diretório central a partir de ini_cd e confere o registro final. Devolve (fim_do_zip, nomes, data) ou None."""
    p = ini_cd
    nomes, data, n = [], None, 0
    while p + 46 <= fim:
        e = j.ler(p, 46)
        if len(e) < 46 or e[:4] != _PK_CENTRAL:
            break
        nlen, xlen, clen = u16(e, 28), u16(e, 30), u16(e, 32)
        nomes.append(j.ler(p + 46, nlen))
        if data is None:
            data = _data_dos(u16(e, 14), u16(e, 12))
        # o deslocamento do cabeçalho local tem de apontar para um cabeçalho local de verdade
        lo = u32(e, 42)
        if lo != 0xFFFFFFFF and j.ler(off + lo, 4) != _PK_LOCAL:
            return None
        p += 46 + nlen + xlen + clen
        n += 1
    if j.ler(p, 4) == b'PK\x05\x05':                      # assinatura digital: pula
        p += 6 + u16(j.ler(p + 4, 2), 0)
    f = j.ler(p, 22)
    if len(f) < 22 or f[:4] != _PK_FIM or p + 22 > fim:
        return None
    total, tam_cd, deslocamento, clen = u16(f, 10), u32(f, 12), u32(f, 16), u16(f, 20)
    if total != n or off + deslocamento != ini_cd or tam_cd != p - ini_cd or p + 22 + clen > fim:
        return None
    return p + 22 + clen, nomes, data


def _tipo_zip(j, off, nomes):
    nomes = [n.decode('utf-8', 'replace') for n in nomes]
    s = set(nomes)
    if '[Content_Types].xml' in s:
        for pref, t in (('word/', 'DOCX'), ('xl/', 'XLSX'), ('ppt/', 'PPTX')):
            if any(n.startswith(pref) for n in nomes):
                return t, 'documento'
    if 'mimetype' in s:
        h = j.ler(off, 30)
        if len(h) == 30 and h[:4] == _PK_LOCAL and u16(h, 8) == 0 and u32(h, 18) < 128:
            conteudo = j.ler(off + 30 + u16(h, 26) + u16(h, 28), u32(h, 18))
            if conteudo in _MIMES:
                return _MIMES[conteudo]
    if 'AndroidManifest.xml' in s:
        return 'APK', 'compactado'
    if 'META-INF/MANIFEST.MF' in s:
        return 'JAR', 'compactado'
    return 'ZIP', 'compactado'


def zip_(j, off, limite, a):
    h = j.ler(off, 30)
    if len(h) < 30 or h[:4] != _PK_LOCAL or u16(h, 4) > 100 or u16(h, 8) not in _METODOS_ZIP:
        return None
    nlen = u16(h, 26)
    nome = j.ler(off + 30, nlen)
    if nlen == 0 or len(nome) < nlen or any(b < 32 for b in nome):
        return None
    fim = off + limite
    pos, nomes, motivo = _zip_caminhar(j, off, fim)
    achou = None
    if motivo == 'fim' and j.ler(pos, 4) == _PK_CENTRAL:
        achou = _zip_fechar(j, off, pos, fim)
    elif motivo == 'desc':
        # entradas com descritor de dados: procura o registro final cujo diretório central termina exatamente nele
        # (isso descarta ZIP guardado dentro de outro e acasos)
        achou = _zip_procurar_fim(j, off, min(fim, off + (256 << 20)))
        if achou is None:
            return None
    if achou is not None:
        total, nomes_cd, data = achou
        tipo, grupo = _tipo_zip(j, off, nomes_cd)
        return res(total - off, 'bom', 100, tipo=tipo, extensao=tipo.lower(), grupo=grupo, data=data)
    if not nomes:
        return None
    # cortado, ou sem o diretório central: ficam as entradas inteiras
    tipo, grupo = _tipo_zip(j, off, nomes)
    return res(pos - off, 'parcial', 45, tipo=tipo, extensao=tipo.lower(), grupo=grupo)


def _zip_procurar_fim(j, off, fim):
    pos = off
    while pos < fim:
        n = min(1 << 20, fim - pos)
        c = j.ler(pos, n)
        if not c:
            return None
        i = c.find(_PK_FIM)
        while i >= 0:
            if i + 22 <= len(c):
                tam_cd, deslocamento = u32(c, i + 12), u32(c, i + 16)
                p = pos + i
                if p - off == deslocamento + tam_cd:
                    r = _zip_fechar(j, off, off + deslocamento, fim)
                    if r is not None:
                        return r
            i = c.find(_PK_FIM, i + 1)
        if len(c) < n:
            return None
        pos += max(1, len(c) - 21)
    return None


# --- RAR ---------------------------------------------------------------------------------------------------------------
def _vint(b, o=0):
    """(valor, bytes usados) de um inteiro de tamanho variável do RAR5; None se inválido."""
    v, s = 0, 0
    for k in range(10):
        if o + k >= len(b):
            return None
        x = b[o + k]
        v |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return v, k + 1
    return None


def rar(j, off, limite, a):
    h = j.ler(off, 8)
    fim = off + limite
    if h[:7] == b'Rar!\x1a\x07\x00':
        pos, blocos, completo = off + 7, 0, False
        while pos + 7 <= fim:
            b = j.ler(pos, 11)
            if len(b) < 7:
                break
            tp, flags, tam = b[2], u16(b, 3), u16(b, 5)
            if not 0x72 <= tp <= 0x7B or tam < 7:
                break
            if blocos == 0:
                if tp != 0x73 or zlib.crc32(j.ler(pos + 2, tam - 2)) & 0xFFFF != u16(b, 0):
                    return None
            add = u32(b, 7) if flags & 0x8000 and len(b) >= 11 else 0
            if pos + tam + add > fim:
                break
            pos += tam + add
            blocos += 1
            if tp == 0x7B:
                completo = True
                break
        if blocos < 2:
            return None
        return res(pos - off, 'bom' if completo else 'parcial', 100 if completo else 60, tipo='RAR', extensao='rar')
    if h[:8] == b'Rar!\x1a\x07\x01\x00':
        pos, blocos, completo = off + 8, 0, False
        while pos + 8 <= fim:
            b = j.ler(pos, 64)
            if len(b) < 8:
                break
            t = _vint(b, 4)
            if t is None:
                break
            tam, ul = t
            total_cab = 4 + ul + tam
            if tam < 2 or total_cab > (1 << 20) or pos + total_cab > fim:
                break
            cab = j.ler(pos, total_cab)
            if len(cab) < total_cab or zlib.crc32(cab[4:]) & 0xFFFFFFFF != u32(cab, 0):
                break
            q = 4 + ul
            tp = _vint(cab, q)
            if tp is None:
                break
            tipo, q = tp[0], q + tp[1]
            fl = _vint(cab, q)
            if fl is None:
                break
            flags, q = fl[0], q + fl[1]
            if flags & 1:
                ex = _vint(cab, q)
                if ex is None:
                    break
                q += ex[1]
            dados = 0
            if flags & 2:
                dd = _vint(cab, q)
                if dd is None:
                    break
                dados = dd[0]
            if blocos == 0 and tipo != 1:
                return None
            if pos + total_cab + dados > fim:
                break
            pos += total_cab + dados
            blocos += 1
            if tipo == 5:
                completo = True
                break
        if blocos < 2:
            return None
        return res(pos - off, 'bom' if completo else 'parcial', 100 if completo else 60, tipo='RAR', extensao='rar')
    return None


# --- 7z ---------------------------------------------------------------------------------------------------------------
def sete_z(j, off, limite, a):
    h = j.ler(off, 32)
    if len(h) < 32 or h[:6] != b'7z\xbc\xaf\x27\x1c' or h[6] != 0:
        return None
    if zlib.crc32(h[12:32]) & 0xFFFFFFFF != u32(h, 8):
        return None
    noff, ntam = u64(h, 12), u64(h, 20)
    total = 32 + noff + ntam
    if total > a['tamanho_max']:
        return None
    if total > limite:
        return res(limite, 'parcial', 50)
    if ntam:
        prox = j.ler(off + 32 + noff, ntam) if ntam <= (64 << 20) else b''
        if len(prox) < ntam:
            return res(32, 'parcial', 20)
        if zlib.crc32(prox) & 0xFFFFFFFF != u32(h, 28):
            return res(total, 'ruim', 20)
    return res(total, 'bom', 100)


# --- SQLite ------------------------------------------------------------------------------------------------------------
def sqlite(j, off, limite, a):
    h = j.ler(off, 100)
    if len(h) < 100 or h[:16] != b'SQLite format 3\x00':
        return None
    pagina = u16(h, 16, False)
    pagina = 65536 if pagina == 1 else pagina
    if pagina < 512 or pagina & (pagina - 1) or h[21:24] != b'\x40\x20\x20':
        return None
    paginas = u32(h, 28, False)
    if paginas == 0 or u32(h, 24, False) != u32(h, 92, False):
        return None                                     # tamanho em páginas não confiável: sem como saber o fim
    if u32(h, 56, False) not in (1, 2, 3) or not 1 <= u32(h, 44, False) <= 4:
        return None                                     # codificação do texto e formato do esquema fora do padrão
    total = paginas * pagina
    if total > a['tamanho_max']:
        return None
    primeira = j.ler(off + 100, 1)                      # a página do esquema é uma página de árvore (folha ou interna)
    if not primeira or primeira[0] not in (5, 13):
        return None
    if total > limite:
        return res(limite // pagina * pagina or pagina, 'parcial', 50)
    # confere o corpo por amostra: página inteira de zeros no meio do banco = trecho sobrescrito ou perdido
    passo = max(1, paginas // 64)
    for n in range(1, paginas, passo):
        if not any(j.ler(off + n * pagina, min(pagina, 4096))):
            return res(n * pagina, 'parcial', 40)
    return res(total, 'bom', 100)


# --- OLE2 (DOC, XLS, PPT antigos) ---------------------------------------------------------------------------------------
_OLE_LIVRE = 0xFFFFFFFF
_NOMES_OLE = (('WordDocument', 'DOC'), ('Workbook', 'XLS'), ('Book', 'XLS'), ('PowerPoint Document', 'PPT'))


def ole2(j, off, limite, a):
    h = j.ler(off, 512)
    if len(h) < 512 or h[:8] != b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        return None
    sh = u16(h, 30)
    if sh not in (9, 12) or u16(h, 32) != 6 or u16(h, 28) != 0xFFFE:
        return None
    setor = 1 << sh
    nfat, dir0, difat0, ndifat = u32(h, 44), u32(h, 48), u32(h, 68), u32(h, 72)
    if not 0 < nfat < 100000 or ndifat > 1000:
        return None
    fat_setores = [x for x in struct.unpack_from('<109I', h, 76) if x != _OLE_LIVRE][:nfat]
    prox, vistos = difat0, 0
    while len(fat_setores) < nfat and prox < 0xFFFFFFFA and vistos < ndifat:
        d = j.ler(off + (prox + 1) * setor, setor)
        if len(d) < setor:
            return None
        ent = struct.unpack_from('<%dI' % (setor // 4), d)
        fat_setores += [x for x in ent[:-1] if x != _OLE_LIVRE]
        prox, vistos = ent[-1], vistos + 1
    fat_setores = fat_setores[:nfat]
    fat = []
    for s in fat_setores:
        d = j.ler(off + (s + 1) * setor, setor)
        if len(d) < setor:
            return res(min(limite, (s + 2) * setor), 'parcial', 40)
        fat.extend(struct.unpack_from('<%dI' % (setor // 4), d))
    ultimo = max((i for i, v in enumerate(fat) if v != _OLE_LIVRE), default=-1)
    if ultimo < 0:
        return None
    total = (ultimo + 2) * setor
    # o tipo vem do diretório (cadeia a partir de dir0)
    tipo = 'DOC'
    achado, s, n = False, dir0, 0
    while s < len(fat) and s < 0xFFFFFFFA and n < 64:
        d = j.ler(off + (s + 1) * setor, setor)
        for k in range(0, len(d) - 127, 128):
            nl = u16(d, k + 64)
            if 2 <= nl <= 64:
                nm = d[k:k + nl - 2].decode('utf-16-le', 'replace')
                for chave, t in _NOMES_OLE:
                    if nm == chave:
                        tipo, achado = t, True
        s, n = fat[s], n + 1
    if not achado:
        tipo = 'DOC'
    if total > a['tamanho_max']:
        return None
    if total > limite:
        return res(limite, 'parcial', 50, tipo=tipo, extensao=tipo.lower())
    return res(total, 'bom', 100 if achado else 70, tipo=tipo, extensao=tipo.lower())


# --- RTF ---------------------------------------------------------------------------------------------------------------
_RTF_CONTROLE = re.compile(rb'\\(ansi|mac|pca?|deff\d|adeflang|fonttbl|uc\d)')
_RTF_TOKEN =re.compile(rb'\\.|[{}]', re.S)


def rtf(j, off, limite, a):
    cabeca = j.ler(off, 200)
    if cabeca[:6] != b'{\\rtf1' or not _RTF_CONTROLE.search(cabeca):
        return None
    fim = off + limite
    pos, prof = off, 0
    while pos < fim:
        n = min(1 << 20, fim - pos)
        c = j.ler(pos, n)
        if not c:
            break
        avanco = len(c)
        if avanco == n and pos + n < fim and (len(c) - len(c.rstrip(b'\\'))) % 2 == 1:
            c, avanco = c[:-1], avanco - 1             # barra solta no fim: o caractere escapado vem no próximo pedaço
        for m in _RTF_TOKEN.finditer(c):
            if m.group() == b'{':
                prof += 1
            elif m.group() == b'}':
                prof -= 1
                if prof == 0:
                    return res(pos + m.end() - off, 'bom', 90)
        pos += avanco
        if avanco < n:
            break
    util = _ate_zeros(j, off, fim) - off
    return res(util, 'parcial', 30) if util >= 64 else None


# --- tabela --------------------------------------------------------------------------------------------------------------
def generico(j, off, limite, a):
    """Só com `rodape` na tabela: o arquivo vai até o primeiro rodapé. Sem estrutura para conferir: pontuação baixa."""
    rod = bytes.fromhex(a['rodape'])
    cab = bytes.fromhex(a['cabecalho'])
    pos = off + len(cab)
    fim = off + limite
    while pos < fim:
        n = min(1 << 20, fim - pos)
        c = j.ler(pos, n)
        if not c:
            return None
        i = c.find(rod)
        if i >= 0:
            return res(pos + i + len(rod) - off, 'bom', 60)
        if len(c) < n:
            return None
        pos += max(1, len(c) - len(rod) + 1)
    return None


VALIDADORES = {'jpg': jpg, 'png': png, 'gif': gif, 'bmp': bmp, 'tiff': tiff, 'iso': iso, 'riff': riff, 'mp3': mp3,
               'pdf': pdf, 'zip': zip_, 'rar': rar, '7z': sete_z, 'sqlite': sqlite, 'ole2': ole2, 'rtf': rtf,
               'generico': generico}
