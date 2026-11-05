# -*- coding: utf-8 -*-
"""Arquivos pequenos montados em Python para os testes de carving (nenhum disco real, nenhum arquivo de cliente).

Cada função devolve os bytes de um arquivo válido do tipo. O conteúdo "comprimido" é aleatório com semente fixa:
o que importa para o carving é a estrutura (marcadores, tamanhos, CRCs, caixas), não a imagem.
"""
import hashlib
import io
import os
import random
import sqlite3
import struct
import tempfile
import zipfile
import zlib


def aleatorio(n, semente=1):
    return random.Random(semente).randbytes(n)


# --- fotos -----------------------------------------------------------------------------------------------------
def _exif(data='2021:05:17 10:20:30'):
    texto = data.encode() + b'\x00'
    ifd0 = struct.pack('<HHHII', 1, 0x8769, 4, 1, 26) + struct.pack('<I', 0)
    exif = struct.pack('<HHHII', 1, 0x9003, 2, 20, 44) + struct.pack('<I', 0)
    return b'Exif\x00\x00' + b'II*\x00' + struct.pack('<I', 8) + ifd0 + exif + texto


def jpeg(scan=3000, data=None, largura=64, altura=48, semente=1):
    """JPEG com estrutura completa (SOI, APP0, [APP1 EXIF], DQT, SOF0, DHT, SOS, dados, EOI)."""
    corpo = aleatorio(scan, semente).replace(b'\xff', b'\xff\x00')
    partes = [b'\xff\xd8', b'\xff\xe0' + struct.pack('>H', 16) + b'JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00']
    if data:
        ex = _exif(data)
        partes.append(b'\xff\xe1' + struct.pack('>H', 2 + len(ex)) + ex)
    partes.append(b'\xff\xdb' + struct.pack('>H', 67) + b'\x00' + bytes(range(1, 65)))
    partes.append(b'\xff\xc0' + struct.pack('>H', 17) + b'\x08' + struct.pack('>HH', altura, largura)
                  + b'\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01')
    partes.append(b'\xff\xc4' + struct.pack('>H', 31) + b'\x00' + bytes(16) + bytes(12))
    partes.append(b'\xff\xda' + struct.pack('>H', 12) + b'\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00')
    partes.append(corpo)
    partes.append(b'\xff\xd9')
    return b''.join(partes)


def _chunk(tp, dados):
    return struct.pack('>I', len(dados)) + tp + dados + struct.pack('>I', zlib.crc32(tp + dados) & 0xFFFFFFFF)


def png(largura=8, altura=8, semente=2):
    linhas = b''.join(b'\x00' + aleatorio(largura * 3, semente + k) for k in range(altura))
    return (b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', struct.pack('>IIBBBBB', largura, altura, 8, 2, 0, 0, 0))
            + _chunk(b'IDAT', zlib.compress(linhas)) + _chunk(b'IEND', b''))


def gif():
    return (b'GIF89a' + struct.pack('<HHBBB', 2, 2, 0x80, 0, 0) + b'\x00\x00\x00\xff\xff\xff'
            + b'\x21\xf9\x04\x00\x00\x00\x00\x00' + b'\x2c' + struct.pack('<HHHHB', 0, 0, 2, 2, 0)
            + b'\x02\x02\x44\x01\x00' + b'\x3b')


def bmp(largura=2, altura=2):
    linha = (largura * 3 + 3) // 4 * 4
    pixels = aleatorio(linha * altura, 3)
    return (b'BM' + struct.pack('<IHHI', 54 + len(pixels), 0, 0, 54)
            + struct.pack('<IiiHHIIiiII', 40, largura, altura, 1, 24, 0, len(pixels), 2835, 2835, 0, 0) + pixels)


def tiff(cr2=False, marca=b'Cam', data=None, largura=16, altura=12, strip=600, dng=False):
    """TIFF little-endian com uma faixa de pixels no fim. cr2=True põe 'CR' no byte 8; `marca` vai na tag Make."""
    ifd = 16 if cr2 else 8
    n = 5 + (1 if data else 0) + (1 if dng else 0)
    pos_make = ifd + 2 + n * 12 + 4
    make = marca + b'\x00'
    pos = pos_make + len(make)
    texto = None
    if data:
        texto = data.encode() + b'\x00'
        pos_data = pos
        pos += len(texto)
    pos_strip = pos
    campos = [(256, 3, 1, largura), (257, 3, 1, altura), (271, 2, len(make), pos_make), (273, 4, 1, pos_strip),
              (279, 4, 1, strip)]
    if data:
        campos.append((306, 2, 20, pos_data))
    if dng:
        campos.append((50706, 1, 4, 0x00000401))
    campos.sort()
    ifd_b = struct.pack('<H', len(campos))
    for tag, tp, cnt, val in campos:
        ifd_b += struct.pack('<HHI', tag, tp, cnt) + (struct.pack('<H', val) + b'\x00\x00' if tp == 3 and cnt == 1
                                                      else struct.pack('<I', val))
    ifd_b += struct.pack('<I', 0)
    cab = b'II*\x00' + struct.pack('<I', ifd) + (b'CR\x02\x00\x00\x00\x00\x00' if cr2 else b'')
    corpo = cab + ifd_b + make + (texto or b'') + aleatorio(strip, 9)
    assert len(cab) + len(ifd_b) == pos_make, (len(cab) + len(ifd_b), pos_make)
    return corpo


# --- vídeo e áudio -----------------------------------------------------------------------------------------------
def _caixa(tp, conteudo):
    return struct.pack('>I', 8 + len(conteudo)) + tp + conteudo


def mp4(mdat=2000, marca=b'isom', criacao=3786825600 + 86400 * 400, com_moov=True):
    ftyp = _caixa(b'ftyp', marca + b'\x00\x00\x02\x00' + b'isomiso2mp41')
    mvhd = _caixa(b'mvhd', b'\x00\x00\x00\x00' + struct.pack('>II', criacao, criacao) + bytes(80))
    moov = _caixa(b'moov', mvhd) if com_moov else b''
    return ftyp + moov + _caixa(b'mdat', aleatorio(mdat, 4))


def heic(mdat=1500):
    return (_caixa(b'ftyp', b'heic\x00\x00\x00\x00mif1heic') + _caixa(b'meta', b'\x00\x00\x00\x00' + _caixa(b'hdlr', bytes(24)))
            + _caixa(b'mdat', aleatorio(mdat, 5)))


def wav(dados=1000):
    fmt = struct.pack('<4sIHHIIHH', b'fmt ', 16, 1, 1, 8000, 8000, 1, 8)
    data = b'data' + struct.pack('<I', dados) + aleatorio(dados, 6)
    corpo = b'WAVE' + fmt + data
    return b'RIFF' + struct.pack('<I', len(corpo)) + corpo


def avi(dados=1000):
    lista = b'LIST' + struct.pack('<I', 4 + 8) + b'hdrl' + b'avih' + struct.pack('<I', 0)
    corpo = b'AVI ' + lista + b'LIST' + struct.pack('<I', 4 + dados) + b'movi' + aleatorio(dados, 7)
    return b'RIFF' + struct.pack('<I', len(corpo)) + corpo


def mp3(quadros=20):
    """ID3v2.3 vazio e quadros MPEG-1 camada III, 128 kbps, 44,1 kHz (417 bytes cada)."""
    id3 = b'ID3\x03\x00\x00\x00\x00\x00\x00'
    quadro = b'\xff\xfb\x90\x00' + aleatorio(413, 8)
    return id3 + quadro * quadros


# --- documentos e pacotes -----------------------------------------------------------------------------------------
def pdf(paginas=1, data='20210517102030'):
    objs = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
            b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>',
            ('<< /CreationDate (D:%s) >>' % data).encode()]
    out = b'%PDF-1.4\n'
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b'%d 0 obj\n' % i + o + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objs) + 1)
    for o in offs:
        out += b'%010d 00000 n \n' % o
    out += b'trailer\n<< /Size %d /Root 1 0 R /Info 4 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objs) + 1, xref)
    return out


def _zip(arquivos, comp=zipfile.ZIP_DEFLATED, sem_busca=False):
    if sem_busca:
        class Fluxo:                       # saída sem seek: o zipfile usa descritores de dados (bit 3)
            def __init__(self):
                self.b = io.BytesIO()
            def write(self, d):
                return self.b.write(d)
            def flush(self):
                pass
        f = Fluxo()
        with zipfile.ZipFile(f, 'w', comp) as z:
            for nome, dados in arquivos:
                with z.open(nome, 'w') as e:
                    e.write(dados)
        return f.b.getvalue()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', comp) as z:
        for nome, dados in arquivos:
            z.writestr(zipfile.ZipInfo(nome, (2021, 5, 17, 10, 20, 30)), dados, compress_type=comp)
    return buf.getvalue()


def zip_simples(sem_busca=False):
    return _zip([('a.txt', b'ola ' * 200), ('pasta/b.bin', aleatorio(500, 10))], sem_busca=sem_busca)


def docx():
    return _zip([('[Content_Types].xml', b'<Types/>'), ('_rels/.rels', b'<Relationships/>'),
                 ('word/document.xml', b'<w:document>' + b'texto ' * 100 + b'</w:document>')])


def xlsx():
    return _zip([('[Content_Types].xml', b'<Types/>'), ('xl/workbook.xml', b'<workbook/>')])


def odt():
    return _zip([('mimetype', b'application/vnd.oasis.opendocument.text'), ('content.xml', b'<office/>' * 50)],
                comp=zipfile.ZIP_STORED)


def rtf():
    return b'{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}Texto com \\{ chave escapada \\} e mais.\\par}'


def _vint(n):
    out = b''
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def rar4(dados=300):
    principal = b'\x73' + struct.pack('<H', 0) + struct.pack('<H', 13) + bytes(6)
    principal = struct.pack('<H', zlib.crc32(principal) & 0xFFFF) + principal
    cab = b'\x74' + struct.pack('<H', 0x8000) + struct.pack('<H', 32) + struct.pack('<I', dados) + bytes(32 - 11)
    cab = struct.pack('<H', zlib.crc32(cab) & 0xFFFF) + cab
    fim = struct.pack('<H', 0x3DC4) + b'\x7b' + struct.pack('<HH', 0x4000, 7)
    return b'Rar!\x1a\x07\x00' + principal + cab + aleatorio(dados, 11) + fim


def rar5(dados=300):
    def bloco(corpo):
        h = _vint(len(corpo)) + corpo
        return struct.pack('<I', zlib.crc32(h) & 0xFFFFFFFF) + h
    principal = bloco(_vint(1) + _vint(0) + _vint(0))
    arquivo = bloco(_vint(2) + _vint(2) + _vint(dados) + bytes(6)) + aleatorio(dados, 12)
    fim = bloco(_vint(5) + _vint(0) + _vint(0))
    return b'Rar!\x1a\x07\x01\x00' + principal + arquivo + fim


def sete_z(dados=400):
    prox = aleatorio(60, 13)
    inicio = struct.pack('<QQI', dados, len(prox), zlib.crc32(prox) & 0xFFFFFFFF)
    return b'7z\xbc\xaf\x27\x1c\x00\x04' + struct.pack('<I', zlib.crc32(inicio) & 0xFFFFFFFF) + inicio + aleatorio(dados, 14) + prox


def sqlite_bytes(linhas=300):
    pasta = tempfile.mkdtemp(prefix='recdados-amostra-')
    caminho = os.path.join(pasta, 'a.db')
    con = sqlite3.connect(caminho)
    con.execute('PRAGMA page_size=4096')
    con.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)')
    con.executemany('INSERT INTO t (v) VALUES (?)', [('linha %d ' % i * 5,) for i in range(linhas)])
    con.commit()
    con.close()
    with open(caminho, 'rb') as f:
        dados = f.read()
    for nome in os.listdir(pasta):
        os.remove(os.path.join(pasta, nome))
    os.rmdir(pasta)
    return dados


def ole2(nome=u'WordDocument'):
    """OLE2 mínimo: cabeçalho, 1 setor de FAT, 1 de diretório e 1 de dados (2048 bytes)."""
    livre = 0xFFFFFFFF
    cab = bytearray(512)
    cab[0:8] = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
    struct.pack_into('<HHHHH', cab, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into('<IIIIII', cab, 40, 0, 1, 1, 0, 4096, 0xFFFFFFFE)
    struct.pack_into('<II', cab, 64, 0, 0xFFFFFFFE)
    struct.pack_into('<I', cab, 72, 0)
    struct.pack_into('<109I', cab, 76, 0, *([livre] * 108))
    fat = struct.pack('<128I', 0xFFFFFFFD, 0xFFFFFFFE, 0xFFFFFFFE, *([livre] * 125))

    def entrada(n, tipo):
        e = bytearray(128)
        nb = n.encode('utf-16-le') + b'\x00\x00'
        e[0:len(nb)] = nb
        struct.pack_into('<H', e, 64, len(nb))
        e[66] = tipo
        return bytes(e)
    dire = (entrada(u'Root Entry', 5) + entrada(nome, 2) + bytes(256))
    return bytes(cab) + fat * 1 + dire + aleatorio(512, 15)


# --- ajudantes --------------------------------------------------------------------------------------------------
def sha256(dados):
    return hashlib.sha256(dados).hexdigest()


def monta_imagem(tamanho, pecas, fundo=b'\x00', semente=99):
    """Imagem de `tamanho` bytes com cada (offset, bytes) de `pecas` gravado. `fundo` b'\\x00' = zeros; None = aleatório."""
    img = bytearray(aleatorio(tamanho, semente) if fundo is None else fundo * tamanho)
    for off, dados in pecas:
        img[off:off + len(dados)] = dados
    return bytes(img)
