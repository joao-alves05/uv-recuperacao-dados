# -*- coding: utf-8 -*-
"""Monta um volume HFS+ mínimo só em Python (blocos de 4 KiB, nós de 1 KiB) para os testes da fase 4."""
import struct
import unicodedata

BS = 4096
NO = 1024
TOTAL_BLOCOS = 300
MAC = 2082844800
DATA_MAC = 1710498030 + MAC                       # 2024-03-15 10:20:30 UTC
INICIO_DADOS = 40                                 # blocos 3 a 39: catálogo


def _chave_cat(pai, nome):
    n = unicodedata.normalize('NFD', nome).encode('utf-16-be')
    return struct.pack('>HIH', 6 + len(n), pai, len(n) // 2) + n


def _par(chave, dados):
    r = chave + (b'\0' if len(chave) % 2 else b'') + dados
    return r + (b'\0' if len(r) % 2 else b'')


def _fork(tam, extents):
    ext = (list(extents) + [(0, 0)] * 8)[:8]
    return struct.pack('>QII', tam, 0, sum(n for _, n in extents)) + b''.join(struct.pack('>II', a, n) for a, n in ext)


def arvore(registros, maxkey, no=NO):
    """B-tree (header + folhas + 1 índice se preciso) de registros [(chave, dados)] já em ordem. Devolve os bytes."""
    pares = [_par(c, d) for c, d in registros]
    folhas, atual, uso = [], [], 14 + 2
    for i, p in enumerate(pares):
        if uso + len(p) + 2 > no:
            folhas.append(atual)
            atual, uso = [], 14 + 2
        atual.append(i)
        uso += len(p) + 2
    folhas.append(atual)

    def no_bytes(kind, altura, itens, flink, blink):
        b = bytearray(no)
        struct.pack_into('>IIbBH', b, 0, flink, blink, kind, altura, len(itens))
        pos, offs = 14, []
        for it in itens:
            offs.append(pos)
            b[pos:pos + len(it)] = it
            pos += len(it)
        offs.append(pos)
        for i, o in enumerate(offs):
            struct.pack_into('>H', b, no - 2 * (i + 1), o)
        return bytes(b)
    nos = []
    for k, ids in enumerate(folhas):
        nos.append(no_bytes(-1, 1, [pares[i] for i in ids], k + 2 if k + 1 < len(folhas) else 0, k if k else 0))
    raiz, profundidade = 1, 1
    if len(folhas) > 1:
        idx = []
        for k, ids in enumerate(folhas):
            chave = registros[ids[0]][0]
            idx.append(_par(chave, struct.pack('>I', k + 1)))
        nos.append(no_bytes(0, 2, idx, 0, 0))
        raiz, profundidade = len(folhas) + 1, 2
    total = len(nos) + 1
    cab = bytearray(no)
    struct.pack_into('>IIbBH', cab, 0, 0, 0, 1, 0, 3)
    struct.pack_into('>HIIIIHHII', cab, 14, profundidade, raiz, len(registros), 1, len(folhas), no, maxkey, total, 0)
    offs = [14, 14 + 106, 14 + 106 + 128, no - 8]
    for i, o in enumerate(offs):
        struct.pack_into('>H', cab, no - 2 * (i + 1), o)
    return bytes(cab) + b''.join(nos)


class HfsImg:
    def __init__(self, rotulo='Vol Teste'):
        self.rotulo = rotulo
        self.prox_id = 16
        self.livre = INICIO_DADOS
        self.escritas = []
        self.cat = []                              # (pai, nome, id, dir, dados_do_registro)
        self.overflow = []                         # (fileID, startBlock, [extents])
        self.n_arquivos = self.n_pastas = 0

    def _datas(self):
        return struct.pack('>IIIII', DATA_MAC, DATA_MAC, DATA_MAC, DATA_MAC, 0)

    def add_dir(self, pai, nome):
        i = self.prox_id
        self.prox_id += 1
        r = bytearray(88)
        struct.pack_into('>hHII', r, 0, 1, 0, 0, i)
        r[12:32] = struct.pack('>IIII', DATA_MAC, DATA_MAC, DATA_MAC, DATA_MAC)[:16] + b'\0\0\0\0'
        struct.pack_into('>H', r, 42, 0o040755)
        self.cat.append((pai, nome, i, True, bytes(r)))
        self.n_pastas += 1
        return i

    def add_file(self, pai, nome, dados, fragmentos=1, sem_overflow=False, comprimido=False, simbolico=False,
                 extents_forcados=None, tamanho_forcado=None):
        i = self.prox_id
        self.prox_id += 1
        n = max(1, -(-len(dados) // BS)) if dados else 0
        partes = [n] if fragmentos == 1 or n < fragmentos else [1] * (fragmentos - 1) + [n - fragmentos + 1]
        extents, pos = [], 0
        for q in (partes if n else []):
            ini = self.livre
            self.livre += q + (1 if fragmentos > 1 else 0)            # buraco: extents separados
            self.escritas.append((ini * BS, dados[pos * BS:(pos + q) * BS].ljust(q * BS, b'\0')))
            extents.append((ini, q))
            pos += q
        if extents_forcados is not None:                           # extents escolhidos a dedo (arquivo hostil), sem gravar dados
            extents = list(extents_forcados)
        r = bytearray(248)
        struct.pack_into('>hHII', r, 0, 2, 0, 0, i)
        r[12:28] = struct.pack('>IIII', DATA_MAC, DATA_MAC, DATA_MAC, DATA_MAC)
        r[41] = 0x20 if comprimido else 0
        struct.pack_into('>H', r, 42, 0o120755 if simbolico else 0o100644)
        r[88:168] = _fork(tamanho_forcado or len(dados), extents)
        self.cat.append((pai, nome, i, False, bytes(r)))
        self.n_arquivos += 1
        if len(extents) > 8 and not sem_overflow:
            resto, ini_blocos = extents[8:], sum(q for _, q in extents[:8])
            for k in range(0, len(resto), 8):
                lote = resto[k:k + 8]
                self.overflow.append((i, ini_blocos, lote))
                ini_blocos += sum(q for _, q in lote)
        return i

    def mover(self, ident, novo_pai):
        """Passa a pasta/arquivo `ident` para dentro de `novo_pai` (muda só o registro do catálogo)."""
        self.cat = [(novo_pai if i == ident else pai, nome, i, d, dados) for pai, nome, i, d, dados in self.cat]

    def gravar(self, caminho, sem_cabecalho_principal=False):
        raiz = bytearray(88)
        struct.pack_into('>hHII', raiz, 0, 1, 0, 0, 2)
        regs = [(_chave_cat(1, self.rotulo), bytes(raiz)),
                (_chave_cat(2, ''), struct.pack('>hhI', 3, 0, 1) + struct.pack('>H', len(self.rotulo)) +
                 unicodedata.normalize('NFD', self.rotulo).encode('utf-16-be'))]
        for pai, nome, i, eh_dir, dados in self.cat:
            regs.append((_chave_cat(pai, nome), dados))
            thread = struct.pack('>hhI', 3 if eh_dir else 4, 0, pai) + struct.pack('>H', len(nome)) + \
                unicodedata.normalize('NFD', nome).encode('utf-16-be')
            regs.append((_chave_cat(i, ''), thread))
        regs.sort(key=lambda r: (struct.unpack_from('>I', r[0], 2)[0], r[0][8:].decode('utf-16-be').lower()))
        catalogo = arvore(regs, 516)
        assert len(catalogo) <= 37 * BS, len(catalogo)
        ov = [(struct.pack('>HBBII', 10, 0, 0, fid, ini), b''.join(struct.pack('>II', a, n) for a, n in
                                                                  (lote + [(0, 0)] * 8)[:8]))
              for fid, ini, lote in sorted(self.overflow)]
        extents_file = arvore(ov, 10) if ov else arvore([], 10)
        bitmap = bytearray(BS)
        for b in range(self.livre):
            bitmap[b // 8] |= 0x80 >> (b % 8)
        cat_blocos = -(-len(catalogo) // BS)
        vh = bytearray(512)
        vh[0:2] = b'H+'
        struct.pack_into('>HI', vh, 2, 4, 0x100)
        struct.pack_into('>IIII', vh, 16, DATA_MAC, DATA_MAC, 0, 0)
        struct.pack_into('>II', vh, 32, self.n_arquivos, self.n_pastas)
        struct.pack_into('>IIII', vh, 40, BS, TOTAL_BLOCOS, 0, self.livre)
        struct.pack_into('>I', vh, 64, self.prox_id)
        vh[112:192] = _fork(len(bitmap), [(1, 1)])
        vh[192:272] = _fork(len(extents_file), [(2, 1)])
        vh[272:352] = _fork(len(catalogo), [(3, cat_blocos)])
        itens = self.escritas + [(BS, bytes(bitmap)), (2 * BS, extents_file.ljust(BS, b'\0')),
                                 (3 * BS, catalogo.ljust(cat_blocos * BS, b'\0'))]
        if not sem_cabecalho_principal:
            itens.append((1024, bytes(vh)))
        itens.append((TOTAL_BLOCOS * BS - 1024, bytes(vh)))
        with open(caminho, 'wb') as f:
            f.truncate(TOTAL_BLOCOS * BS)
            for off, d in itens:
                f.seek(off)
                f.write(d)
        return caminho
