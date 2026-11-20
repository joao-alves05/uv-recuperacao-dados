# -*- coding: utf-8 -*-
"""Monta imagens de disco óptico só em Python: UDF 1.02 (com várias sessões) e ISO 9660 com Rock Ridge."""
import binascii
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


def fid(nome, icb, carac=0, ref=0):
    nb = (b'\x08' + nome.encode('latin-1')) if nome else b''
    corpo = bytearray(22 + len(nb))
    struct.pack_into('<HBB', corpo, 0, 1, carac, len(nb))
    struct.pack_into('<IIH', corpo, 4, SETOR, icb, ref)
    struct.pack_into('<H', corpo, 20, 0)
    corpo[22:] = nb
    b = bytearray(16 + len(corpo))
    b[16:] = corpo
    struct.pack_into('<HHBBHHHI', b, 0, 257, 2, 0, 0, 1, 0, len(corpo), 0)
    b[4] = (sum(b[:4]) + sum(b[5:16])) & 0xFF
    return bytes(b).ljust((len(b) + 3) & ~3, b'\0')


DESLOCA_VIRTUAL = 100


class Sessao:
    """Uma sessão UDF a partir do setor `base`. Arquivos: {caminho: bytes | ('apagado', bytes)}; pastas implícitas."""

    def __init__(self, arquivos, base=0, rotulo='UDFTESTE', alt_fim=None, indireto=None, vat=None, esparsa=None, metadados=None):
        # indireto: {caminho: n} = n Indirect Entries (tag 259) antes do File Entry; {caminho: 'laco'} = 2 que apontam uma para a outra
        # vat: None = partição física; '2.00' ou '1.50' = partição virtual com a VAT no último setor da imagem; 'ruim' = VAT 2.00 com
        # cabeçalho corrompido. Na virtual, o bloco virtual é o físico + DESLOCA_VIRTUAL (nunca batem).
        self.arq, self.base, self.rotulo, self.alt_fim, self.indireto = arquivos, base, rotulo, alt_fim, indireto or {}
        self.vat = vat
        # esparsa: None = partição física; {'pacote': P, 'remapa': caminho, 'copias': ['ruim', 'ok', ...]} = partição esparsa
        # (1 a 4 cópias da Sparing Table em base+200..; 'ruim' corrompe a cópia sem refazer o CRC). Os pacotes do arquivo
        # `remapa` são gravados no pacote de reposição e o original fica com lixo.
        self.esparsa = esparsa
        # metadados: None = partição física; {'arquivo': 'ok'|'ruim'|'dados', 'espelho': 'ok'|'ruim'} = UDF 2.50 com partição de
        # metadados. FSD, File Entries e diretórios vão para o Metadata File (em 2 extents separados por um buraco) e uma cópia
        # no Mirror; só os dados dos arquivos ficam na física. 'ruim' zera o File Entry (tag inválida); 'dados' deixa o File
        # Entry certo mas estraga o bloco do FSD dentro do arquivo.
        self.metadados = metadados

    def gerar(self, img):
        base = self.base
        virt = (lambda p: p + DESLOCA_VIRTUAL) if self.vat else (lambda p: p)    # bloco físico -> o que se grava nas referências
        ps = base + 260                                    # início da partição (setor)
        proximo = [3]                                      # bloco 0 FSD, 1 raiz (FE), 2 dados da raiz
        meta = self.metadados
        mref = 1 if meta else 0                            # partition reference dos blocos de metadados
        fis_prox = [0]                                     # só com metadados: contador da partição física
        meta_buf = bytearray()

        def aloc_d(n=1):
            if not meta:
                return aloc(n)
            r = fis_prox[0]
            fis_prox[0] += n
            return r

        def pb(lbn, dados, fis=False):
            """Grava no bloco `lbn` da partição de metadados (ou da física, se `fis` ou se não há metadados)."""
            if meta and not fis:
                if len(meta_buf) < (lbn + 1) * SETOR + len(dados):
                    meta_buf.extend(bytes((lbn + 1) * SETOR + len(dados) - len(meta_buf)))
                meta_buf[lbn * SETOR: lbn * SETOR + len(dados)] = dados
            else:
                poe(ps + lbn, dados)

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

        def fe(lbn, tipo, info, aloc_tipo, ads, ano=2024, fis=False):
            corpo = bytearray(160 + len(ads))
            struct.pack_into('<IHHHBB', corpo, 0, 0, 4, 0, 1, 0, tipo)          # ICB tag (16..): arquivo
            struct.pack_into('<H', corpo, 18, aloc_tipo)                          # flags (34)
            struct.pack_into('<Q', corpo, 40, info)                               # info length (56)
            corpo[56:68] = timestamp(ano)                                         # acesso (72)
            corpo[68:80] = timestamp(ano)                                         # modificação (84)
            struct.pack_into('<II', corpo, 152, 0, len(ads))                      # L_EA (168), L_AD (172)
            corpo[160:] = ads
            pb(lbn, tag(261, bytes(corpo), lbn), fis)

        def indireta(lbn, destino):
            corpo = bytearray(36)
            struct.pack_into('<IHHHBB', corpo, 0, 0, 4, 0, 1, 0, 12)            # ICB tag: tipo 12 = Indirect Entry
            struct.pack_into('<IIH', corpo, 20, SETOR, virt(destino), 0)              # long_ad do próximo ICB (36)
            pb(lbn, tag(259, bytes(corpo), lbn))

        def cadeia(caminho, real):
            """Bloco que a pasta deve apontar para o arquivo: `real`, ou o início da cadeia de Indirect Entries."""
            n = self.indireto.get(caminho)
            if not n:
                return real
            if n == 'laco':
                a, b = aloc(), aloc()
                indireta(a, b)
                indireta(b, a)
                return a
            blocos = [aloc() for _ in range(n)]
            for i, bl in enumerate(blocos):
                indireta(bl, blocos[i + 1] if i + 1 < n else real)
            return blocos[0]

        dados_em = {}

        def gravar_pasta(no, lbn_fe, lbn_pai, prefixo=''):
            fids = fid('', virt(lbn_pai), 0x0A, mref)
            for nome, v in no.items():
                if isinstance(v, dict):
                    l = aloc()
                    d = aloc()
                    gravar_pasta(v, l, lbn_fe, prefixo + nome + '/')
                    fids += fid(nome, virt(l), 0x02, mref)
                else:
                    apagado = isinstance(v, tuple)
                    dados = v[1] if apagado else v
                    l = aloc()
                    if len(dados) < 100:                                # pequeno: guardado dentro do File Entry
                        fe(l, 5, len(dados), 3, dados)
                    else:
                        n = -(-len(dados) // SETOR)
                        if self.esparsa:                               # dados alinhados em pacote, sozinhos nele
                            P = self.esparsa['pacote']
                            proximo[0] = -(-proximo[0] // P) * P
                            n = -(-n // P) * P
                        ini = aloc_d(n)
                        dados_em[prefixo + nome] = (ini, n)
                        img[(ps + ini) * SETOR: (ps + ini) * SETOR + len(dados)] = dados
                        if meta:                                       # dado na física: long_ad com partition reference 0
                            fe(l, 5, len(dados), 1, struct.pack('<IIH', len(dados), ini, 0) + bytes(6))
                        else:
                            fe(l, 5, len(dados), 0, struct.pack('<II', len(dados), virt(ini)))
                    fids += fid(nome, virt(cadeia(prefixo + nome, l)), 0x04 if apagado else 0, mref)
            d = aloc()
            pb(d, fids)
            fe(lbn_fe, 4, len(fids), 0, struct.pack('<II', len(fids), virt(d)))

        gravar_pasta(raiz, 1, 1)
        spar_map = b''
        meta_map = b''
        if meta:
            pb(0, tag(256, bytes(384) + struct.pack('<IIH', SETOR, 1, mref), 0))   # FSD: raiz no bloco 1 da partição de metadados
            nm = proximo[0]
            h = nm // 2
            fe1, fe2 = aloc_d(), aloc_d()
            p1 = aloc_d(h)
            aloc_d(3)                                      # buraco: o Metadata File tem 2 extents que não se tocam
            p2 = aloc_d(nm - h)
            mir = aloc_d(nm)
            poe(ps + p1, bytes(meta_buf[:h * SETOR]))
            poe(ps + p2, bytes(meta_buf[h * SETOR: nm * SETOR]))
            poe(ps + mir, bytes(meta_buf[:nm * SETOR]))
            fe(fe1, 250, nm * SETOR, 0, struct.pack('<IIII', h * SETOR, p1, (nm - h) * SETOR, p2), fis=True)
            fe(fe2, 251, nm * SETOR, 0, struct.pack('<II', nm * SETOR, mir), fis=True)
            if meta.get('arquivo') == 'ruim':
                poe(ps + fe1, bytes(SETOR))
            elif meta.get('arquivo') == 'dados':
                poe(ps + p1, bytes([0xEE]) * SETOR)            # o bloco 0 do Metadata File é o FSD
            if meta.get('espelho') == 'ruim':
                poe(ps + fe2, bytes(SETOR))
            meta_map = (bytes([2, 64, 0, 0, 0]) + b'*UDF Metadata Partition' + bytes(8)
                        + struct.pack('<HHIIIIH', 1, 1, fe1, fe2, 0xFFFFFFFF, 1, 1)).ljust(64, bytes(1))
        if self.esparsa:
            P = self.esparsa['pacote']
            proximo[0] = -(-proximo[0] // P) * P
            ini, n = dados_em[self.esparsa['remapa']]
            entradas = []
            for orig in range(ini, ini + n, P):
                novo = aloc(P)
                img[(ps + novo) * SETOR:(ps + novo + P) * SETOR] = img[(ps + orig) * SETOR:(ps + orig + P) * SETOR]
                img[(ps + orig) * SETOR:(ps + orig + P) * SETOR] = bytes([0xEE]) * (P * SETOR)         # pacote defeituoso: lixo
                entradas.append((orig, novo))
            corpo = bytearray(40 + 8 * len(entradas))
            corpo[1:19] = b'*UDF Sparing Table'
            struct.pack_into('<HHI', corpo, 32, len(entradas), 0, 1)
            for i, (o, nv) in enumerate(entradas):
                struct.pack_into('<II', corpo, 40 + 8 * i, o, nv)
            locs = []
            for k, estado in enumerate(self.esparsa['copias']):
                loc = base + 200 + k
                t = bytearray(tag(0, bytes(corpo), loc))
                struct.pack_into('<H', t, 8, binascii.crc_hqx(bytes(t[16:16 + len(corpo)]), 0))
                t[4] = (sum(t[:4]) + sum(t[5:16])) & 0xFF
                if estado == 'ruim':
                    t[16 + 40] ^= 0xFF                                    # corrompe a 1ª entrada; o CRC deixa de bater
                poe(loc, bytes(t))
                locs.append(loc)
            spar_map = (bytes([2, 64, 0, 0, 0]) + b'*UDF Sparable Partition'.ljust(23, bytes(1)) + bytes(8)
                        + struct.pack('<HHHBBI', 1, 1, P, len(locs), 0, len(corpo) + 16)
                        + b''.join(struct.pack('<I', l) for l in locs)).ljust(64, bytes(1))
        total_blocos = fis_prox[0] if meta else proximo[0]
        tam_part = total_blocos + 1
        if self.vat:                                       # VAT: dados no penúltimo setor da imagem e File Entry no último
            ultimo = len(img) // SETOR - 1
            fe_vat, dados_vat = ultimo - ps, ultimo - 1 - ps
            tam_part = fe_vat + 1
            tab = [0xFFFFFFFF] * (total_blocos + DESLOCA_VIRTUAL)
            for p in range(total_blocos):
                tab[virt(p)] = p
            corpo = struct.pack('<%dI' % len(tab), *tab)
            if self.vat == '1.50':
                corpo += bytes(1) + b'*UDF Virtual Alloc Tbl'.ljust(23, bytes(1)) + bytes(8) + bytes(4)
                tipo_vat = 0
            else:
                hl = 60000 if self.vat == 'ruim' else 152
                corpo = struct.pack('<HH', hl, 0) + bytes(148) + corpo
                tipo_vat = 248
            assert len(corpo) <= SETOR
            poe(ps + dados_vat, corpo)
            fe(fe_vat, tipo_vat, len(corpo), 0, struct.pack('<II', len(corpo), dados_vat))
        if not meta:
            poe(ps + 0, tag(256, bytes(384) + struct.pack('<IIH', SETOR, virt(1), mref), 0))
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
            struct.pack_into('<II', pd, 172, ps, tam_part)               # início (188) e tamanho (192)
            poe(vds + 1, tag(5, bytes(pd), vds + 1))
            lvd = bytearray(496)
            struct.pack_into('<I', lvd, 0, 3)
            struct.pack_into('<I', lvd, 196, SETOR)                               # bloco lógico (212)
            struct.pack_into('<II', lvd, 232, SETOR, virt(0))                           # FSD: tamanho (248), bloco (252)
            struct.pack_into('<II', lvd, 248, 6 + (64 if self.vat else 0), 2 if self.vat else 1)   # tamanho dos mapas (264), quantidade (268)
            lvd[424:430] = struct.pack('<BBHH', 1, 6, 1, 1)                       # mapa tipo 1 (440)
            if meta_map:                                                          # mapa tipo 2 de metadados depois do tipo 1
                struct.pack_into('<II', lvd, 248, 6 + 64, 2)
                lvd[430:494] = meta_map
                struct.pack_into('<H', lvd, 240, mref)                            # partition reference do FSD
            if spar_map:                                                          # mapa tipo 2 esparsa no lugar do tipo 1
                struct.pack_into('<II', lvd, 248, 64, 1)
                lvd[424:488] = spar_map
            if self.vat:                                                          # mapa tipo 2, partição virtual (446)
                lvd[430:434] = bytes([2, 64, 0, 0])
                lvd[435:435 + 22] = b'*UDF Virtual Partition'
                struct.pack_into('<HH', lvd, 466, 1, 1)
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
