# -*- coding: utf-8 -*-
"""Monta imagens de disco óptico só em Python: UDF 1.02 (com várias sessões) e ISO 9660 com Rock Ridge."""
import struct

SETOR = 2048


def tag(ident, corpo, local):
    """Descritor UDF: etiqueta de 16 bytes (checksum certo) + corpo, no tamanho de um setor."""
    b = bytearray(SETOR)
    b[16:16 + len(corpo)] = corpo
    struct.pack_into('<HHBBHHHI', b, 0, ident, 2, 0, 0, 1, 0, max(0, len(corpo)), local)
    b[4] = (sum(b[:4]) + sum(b[5:16])) & 0xFF
    return bytes(b)


def dstring(texto, n):
    d = texto.encode('latin-1')
    return (b'\x08' + d).ljust(n - 1, b'\0') + bytes([len(d) + 1])


def timestamp(ano=2024, mes=3, dia=15, h=10, mi=20, s=30):
    return struct.pack('<HHBBBBBBBB', 0x1000, ano, mes, dia, h, mi, s, 0, 0, 0)       # tipo 1, fuso 0


def fid(nome, icb, carac=0):
    nb = (b'\x08' + nome.encode('latin-1')) if nome else b''
    corpo = bytearray(22 + len(nb))
    struct.pack_into('<HBB', corpo, 0, 1, carac, len(nb))
    struct.pack_into('<IIH', corpo, 4, SETOR, icb, 0)
    struct.pack_into('<H', corpo, 20, 0)
    corpo[22:] = nb
    b = bytearray(16 + len(corpo))
    b[16:] = corpo
    struct.pack_into('<HHBBHHHI', b, 0, 257, 2, 0, 0, 1, 0, len(corpo), 0)
    b[4] = (sum(b[:4]) + sum(b[5:16])) & 0xFF
    return bytes(b).ljust((len(b) + 3) & ~3, b'\0')


class Sessao:
    """Uma sessão UDF a partir do setor `base`. Arquivos: {caminho: bytes | ('apagado', bytes)}; pastas implícitas."""

    def __init__(self, arquivos, base=0, rotulo='UDFTESTE', alt_fim=None):
        self.arq, self.base, self.rotulo, self.alt_fim = arquivos, base, rotulo, alt_fim

    def gerar(self, img):
        base = self.base
        ps = base + 260                                    # início da partição (setor)
        proximo = [3]                                      # bloco 0 FSD, 1 raiz (FE), 2 dados da raiz

        def aloc(n=1):
            r = proximo[0]
            proximo[0] += n
            return r

        def poe(setor, dados):
            img[setor * SETOR: setor * SETOR + len(dados)] = dados

        # árvore
        raiz = {}
        for caminho, conteudo in self.arq.items():
            no = raiz
            partes = [p for p in caminho.split('/') if p]
            for p in partes[:-1]:
                no = no.setdefault(p, {})
            no[partes[-1]] = conteudo

        def fe(lbn, tipo, info, aloc_tipo, ads, ano=2024):
            corpo = bytearray(160 + len(ads))
            struct.pack_into('<IHHHBB', corpo, 0, 0, 4, 0, 1, 0, tipo)          # ICB tag (16..): arquivo
            struct.pack_into('<H', corpo, 18, aloc_tipo)                          # flags (34)
            struct.pack_into('<Q', corpo, 40, info)                               # info length (56)
            corpo[56:68] = timestamp(ano)                                         # acesso (72)
            corpo[68:80] = timestamp(ano)                                         # modificação (84)
            struct.pack_into('<II', corpo, 152, 0, len(ads))                      # L_EA (168), L_AD (172)
            corpo[160:] = ads
            poe(ps + lbn, tag(261, bytes(corpo), lbn))

        def gravar_pasta(no, lbn_fe, lbn_pai):
            fids = fid('', lbn_pai, 0x0A)
            for nome, v in no.items():
                if isinstance(v, dict):
                    l = aloc()
                    d = aloc()
                    gravar_pasta(v, l, lbn_fe)
                    fids += fid(nome, l, 0x02)
                else:
                    apagado = isinstance(v, tuple)
                    dados = v[1] if apagado else v
                    l = aloc()
                    if len(dados) < 100:                                # pequeno: guardado dentro do File Entry
                        fe(l, 5, len(dados), 3, dados)
                    else:
                        n = -(-len(dados) // SETOR)
                        ini = aloc(n)
                        img[(ps + ini) * SETOR: (ps + ini) * SETOR + len(dados)] = dados
                        fe(l, 5, len(dados), 0, struct.pack('<II', len(dados), ini))
                    fids += fid(nome, l, 0x04 if apagado else 0)
            d = aloc()
            img[(ps + d) * SETOR: (ps + d) * SETOR + len(fids)] = fids
            fe(lbn_fe, 4, len(fids), 0, struct.pack('<II', len(fids), d))

        gravar_pasta(raiz, 1, 1)
        total_blocos = proximo[0]
        poe(ps + 0, tag(256, bytes(384) + struct.pack('<IIH', SETOR, 1, 0), 0))
        # VRS
        for k, ident in enumerate((b'BEA01', b'NSR02', b'TEA01')):
            poe(base + 16 + k, b'\0' + ident + b'\x01')
        # conjunto de volume em base+32 (e cópia em base+48)
        for vds in (base + 32, base + 48):
            pvd = bytearray(496)
            struct.pack_into('<I', pvd, 0, 1)
            pvd[8:40] = dstring(self.rotulo, 32)
            poe(vds, tag(1, bytes(pvd), vds))
            pd = bytearray(496)
            struct.pack_into('<IHH', pd, 0, 2, 1, 1)
            struct.pack_into('<II', pd, 172, ps, total_blocos + 1)               # início (188) e tamanho (192)
            poe(vds + 1, tag(5, bytes(pd), vds + 1))
            lvd = bytearray(496)
            struct.pack_into('<I', lvd, 0, 3)
            struct.pack_into('<I', lvd, 196, SETOR)                               # bloco lógico (212)
            struct.pack_into('<II', lvd, 232, SETOR, 0)                           # FSD: tamanho (248), bloco (252)
            struct.pack_into('<II', lvd, 248, 6, 1)                               # tamanho dos mapas (264), quantidade (268)
            lvd[424:430] = struct.pack('<BBHH', 1, 6, 1, 1)                       # mapa tipo 1 (440)
            poe(vds + 2, tag(6, bytes(lvd), vds + 2))
            poe(vds + 3, tag(8, bytes(0), vds + 3))
        for a in (base + 256,) + ((self.alt_fim,) if self.alt_fim else ()):
            av = bytearray(480)
            struct.pack_into('<IIII', av, 0, 4 * SETOR, base + 32, 4 * SETOR, base + 48)
            poe(a, tag(2, bytes(av), a))
        return total_blocos


def disco_udf(sessoes, tamanho_setores):
    img = bytearray(tamanho_setores * SETOR)
    for s in sessoes:
        s.gerar(img)
    return img


# --- ISO 9660 + Rock Ridge --------------------------------------------------------------------------------------
def _su_nm(nome):
    b = nome.encode('utf-8')
    return b'NM' + bytes([5 + len(b), 1, 0]) + b


def _reg(lba, tam, flags, nome_iso, su=b''):
    n = 33 + len(nome_iso) + (1 if len(nome_iso) % 2 == 0 else 0)
    b = bytearray(n)
    b[0] = n
    struct.pack_into('<I', b, 2, lba)
    struct.pack_into('>I', b, 6, lba)
    struct.pack_into('<I', b, 10, tam)
    struct.pack_into('>I', b, 14, tam)
    b[18:25] = bytes([124, 3, 15, 10, 20, 30, 0])
    b[25] = flags
    struct.pack_into('<H', b, 28, 1)
    struct.pack_into('>H', b, 30, 1)
    b[32] = len(nome_iso)
    b[33:33 + len(nome_iso)] = nome_iso
    b = bytes(b) + su
    if len(b) % 2:
        b += b'\0'
    return b[:0] + bytes([len(b)]) + b[1:]


def iso_rock_ridge(arquivos, rock_ridge=True):
    """ISO simples: arquivos na raiz {nome_longo: (nome_8_3, bytes)}; com Rock Ridge, a entrada NM leva o nome longo."""
    regs = [_reg(20, SETOR, 2, b'\0', b'SP\x07\x01\xbe\xef\x00' if rock_ridge else b''), _reg(20, SETOR, 2, b'\1')]
    dados, lba = [], 21
    for longo, (curto, conteudo) in arquivos.items():
        regs.append(_reg(lba, len(conteudo), 0, curto.encode('ascii'), _su_nm(longo) if rock_ridge else b''))
        dados.append((lba, conteudo))
        lba += -(-len(conteudo) // SETOR)
    img = bytearray(lba * SETOR)
    d = bytearray(SETOR)
    d[0], d[1:6], d[6] = 1, b'CD001', 1
    d[40:72] = b'RRTESTE'.ljust(32)
    struct.pack_into('<I', d, 80, lba)
    struct.pack_into('<H', d, 128, SETOR)
    d[156:190] = _reg(20, SETOR, 2, b'\0')[:34]
    img[16 * SETOR:17 * SETOR] = d
    img[17 * SETOR:17 * SETOR + 7] = b'\xffCD001\x01'
    img[20 * SETOR:20 * SETOR + sum(len(r) for r in regs)] = b''.join(regs)
    for l, c in dados:
        img[l * SETOR: l * SETOR + len(c)] = c
    return img
