# -*- coding: utf-8 -*-
"""HFS+ e HFSX (leitura): volume header (com a cópia reserva do fim), B-tree do catálogo, extents do fork de dados.

Lista pastas e arquivos VIVOS com o caminho original; serve para volume de Mac com a estrutura danificada em que o
catálogo ainda está inteiro. Entram: nome (Unicode, normalizado para NFC), tamanho do fork de dados, datas (1904-UTC),
os 8 extents do registro e, se faltar, os do arquivo de extents overflow.

O que NÃO dá para fazer, e por quê:
  * Arquivo apagado: o HFS+ tira o registro do catálogo e reaproveita o nó da B-tree; o que sobra nos nós é acaso.
    Não há como recuperar pela estrutura: deixe para a busca por assinatura (carving).
  * Resource fork e atributos estendidos em geral: não são listados.
  * Arquivo comprimido (decmpfs, flag UF_COMPRESSED + atributo "com.apple.decmpfs"): tipo 3 (zlib no atributo) e tipo 4
    (zlib em blocos de 64 KiB no resource fork) saem descomprimidos: a entrada leva `extents_decmpfs`, extents de 4
    valores [offset, n, u, 'zlib'] (ver `achados.py`), que `ler()` e o achado usam. LZVN (7/8) e LZFSE (11/12) saem `ruim` com a nota.
  * Links simbólicos não são listados; a pasta escondida "HFS+ Private Data" também não.
  * Hard link de ARQUIVO é listado com o nome e o caminho do link e os dados do inode real ("iNode<n>" na pasta
    escondida). Se o inode não está no catálogo, o link sai `ruim` com a explicação. Hard link de PASTA (Time Machine,
    ".HFS+ Private Directory Data") NÃO é listado: o conteúdo não aparece pelo caminho do link.
  * Volume com journal sujo: o catálogo no disco pode estar atrasado em relação ao journal, que não é reaplicado.
  * HFS+ embrulhado em HFS clássico (assinatura "BD") não é reconhecido.
"""
import struct
import unicodedata
import zlib

from .base import ErroSA, Entrada, Volume, MAX_PROFUNDIDADE, epoch, juntar, limpar_nome

MAC_PARA_UNIX = 2082844800                 # segundos entre 1904-01-01 e 1970-01-01
ID_RAIZ, ID_PAI_DA_RAIZ = 2, 1
ID_EXTENTS, ID_CATALOGO = 3, 4
PERDIDOS = '/[perdidos]'
_ADIAR = object()
UF_COMPRESSED = 0x20
ATTR_DECMPFS = 'com.apple.decmpfs'
BLOCO_DECMPFS = 1 << 16
MAX_BLOCOS_DECMPFS = 1 << 16                 # 4 GiB descomprimidos por arquivo
MAX_TIPO3_DECMPFS = 16 << 20                 # teto de bytes descomprimidos do tipo 3
MAX_EXTENTS_DECMPFS = 90000                  # abaixo do teto de extents de um achado (achados.normalizar)
NUL4 = chr(0) * 4


def descomprimir_zlib(bruto, u):
    """Os `u` bytes de uma unidade decmpfs: zlib, ou crua se o primeiro byte é 0xFF. ValueError se for inválida."""
    if bruto[:1] == bytes([255]):
        dados = bruto[1:1 + u]
    else:
        try:
            dados = zlib.decompressobj().decompress(bruto, u)
        except zlib.error as erro:
            raise ValueError('zlib inválido: %s' % erro) from None
    if len(dados) < u:
        raise ValueError('unidade mais curta que o declarado')
    return dados


def _data(v):
    return float(v - MAC_PARA_UNIX) if v else None


def _fork(b, o):
    """HFSPlusForkData -> (tamanho lógico, total de blocos, [(início, quantidade)] dos 8 extents)."""
    tam, _clump, total = struct.unpack_from('>QII', b, o)
    ext = [struct.unpack_from('>II', b, o + 16 + 8 * i) for i in range(8)]
    return tam, total, [(a, n) for a, n in ext if n]


def _cabecalho(b):
    if len(b) < 512 or b[:2] not in (b'H+', b'HX'):
        raise ErroSA('Cabeçalho HFS+ inválido.')
    versao = struct.unpack_from('>H', b, 2)[0]
    if versao != (4 if b[:2] == b'H+' else 5):
        raise ErroSA('Versão de HFS+ não reconhecida.')
    bs, total = struct.unpack_from('>II', b, 40)
    if bs < 512 or bs > (1 << 20) or bs & (bs - 1) or total == 0:
        raise ErroSA('Tamanho de bloco HFS+ inválido.')
    return dict(hfsx=b[:2] == b'HX', bs=bs, total=total, atrib=struct.unpack_from('>I', b, 4)[0],
                extents_fork=_fork(b, 192), catalogo_fork=_fork(b, 272), atributos_fork=_fork(b, 352))


def reconhecer(leitor, inicio=0, tamanho=None):
    try:
        return Hfs(leitor, inicio, tamanho)
    except (ErroSA, OSError, struct.error):
        return None


class Hfs(Volume):
    def __init__(self, leitor, inicio=0, tamanho=None):
        super().__init__(leitor, inicio, tamanho)
        self.usou_reserva = False
        cab = None
        try:
            cab = _cabecalho(self._ler(1024, 512))
        except (ErroSA, OSError, struct.error):
            pass
        if cab is None:
            try:
                cab = _cabecalho(self._ler(self.tamanho - 1024, 512))     # cópia no fim do volume
                self.usou_reserva = True
            except (ErroSA, OSError, struct.error):
                raise ErroSA('Nenhum cabeçalho HFS+ legível (nem a cópia do fim).') from None
        self.hfsx = cab['hfsx']
        self.tipo = 'HFSX' if self.hfsx else 'HFS+'
        self.bs, self.total, self.journal = cab['bs'], cab['total'], bool(cab['atrib'] & 0x2000)
        self.rotulo = ''
        self.ao_ler = None
        self.no_tam = 4096
        self._ov = None
        self._attr_fork = cab['atributos_fork']
        self._decmpfs_attrs = None
        self._ext_extents = self._completar(cab['extents_fork'], None)
        self._cat_extents = self._completar(cab['catalogo_fork'], ID_CATALOGO)
        tam_cat = cab['catalogo_fork'][0]
        if tam_cat < 512 or not self._cat_extents:
            raise ErroSA('O arquivo de catálogo está vazio ou sem extents.')
        self._cat_tam = tam_cat
        self._cabecalho_arvore()

    # ---- forks e extents -------------------------------------------------------------------------------------
    def _completar(self, fork, id_arquivo):
        """Extents do fork: os 8 do registro mais os do arquivo de extents overflow, se o fork precisa de mais."""
        _tam, total, ext = fork
        ext = list(ext)
        if id_arquivo is not None and sum(n for _, n in ext) < total:
            ext += self._overflow().get(id_arquivo, [])
        return ext

    def _faixas(self, extents, off, n):
        """[(offset no volume, bytes)] para ler `n` bytes a partir do offset lógico `off` de um fork."""
        out, pos = [], 0
        for ini, cont in extents:
            tam = cont * self.bs
            if off < pos + tam and n > 0:
                d = max(0, off - pos)
                q = min(tam - d, n)
                out.append((ini * self.bs + d, q))
                off += q
                n -= q
            pos += tam
            if n <= 0:
                break
        if n > 0:
            raise ErroSA('leitura além dos extents do fork')
        return out

    def _ler_fork(self, extents, off, n):
        return b''.join(self._ler(o, q) for o, q in self._faixas(extents, off, n))

    # ---- B-tree ----------------------------------------------------------------------------------------------
    def _cabecalho_arvore(self):
        b = self._ler_fork(self._cat_extents, 0, 512)
        kind = struct.unpack_from('>b', b, 8)[0]
        if kind != 1:
            raise ErroSA('O catálogo não começa com um nó de cabeçalho.')
        profundidade, raiz, folhas, primeira, ultima, no_tam = struct.unpack_from('>HIIIIH', b, 14)
        total_nos = struct.unpack_from('>I', b, 14 + 22)[0]
        if no_tam < 512 or no_tam > 32768 or no_tam & (no_tam - 1) or self._cat_tam < no_tam:
            raise ErroSA('Tamanho de nó do catálogo inválido.')
        self.no_tam, self.primeira_folha = no_tam, primeira
        self.total_nos = min(total_nos or self._cat_tam // no_tam, self._cat_tam // no_tam)

    def _registros(self, extents, no_n):
        """Gera (tipo do nó, [bytes de cada registro]) de um nó."""
        b = self._ler_fork(extents, no_n * self.no_tam, self.no_tam)
        flink, _blink, kind, _alt, nreg = struct.unpack_from('>IIbBH', b, 0)
        if 14 + 2 * (nreg + 1) > self.no_tam:                  # a tabela de offsets nem caberia no nó
            raise ErroSA('Nó %d do catálogo com %d registros: inválido' % (no_n, nreg))
        offs = [struct.unpack_from('>H', b, self.no_tam - 2 * (i + 1))[0] for i in range(nreg + 1)]
        recs, pos = [], []
        for i in range(nreg):
            a, z = offs[i], offs[i + 1]
            if a < 14 or z > self.no_tam - 2 * (nreg + 1) or z < a:
                continue
            recs.append(b[a:z])
            pos.append(a)
        self._ult_pos = pos                                    # onde cada registro começa no nó
        return flink, kind, recs

    def _folhas(self, extents, primeira, total_nos):
        """Segue a lista ligada de nós-folha. Gera (nó, registros)."""
        n, vistos = primeira, 0
        while n and vistos <= total_nos:
            flink, kind, recs = self._registros(extents, n)
            if kind != -1:
                raise ErroSA('Nó %d do catálogo não é folha' % n)
            yield n, recs
            n, vistos = flink, vistos + 1

    def _overflow(self):
        """{id do arquivo: extents extras do fork de dados}, lido uma vez do arquivo de extents overflow."""
        if self._ov is not None:
            return self._ov
        ov = {}
        self._ov = ov                                        # (já preenchido ao fim; evita laço em arquivo de extents)
        try:
            b = self._ler_fork(self._ext_extents, 0, 512)
            if struct.unpack_from('>b', b, 8)[0] != 1:
                return ov
            no_tam, primeira = struct.unpack_from('>H', b, 14 + 18)[0], struct.unpack_from('>I', b, 14 + 10)[0]
            total = struct.unpack_from('>I', b, 14 + 22)[0] or 1
            if no_tam < 512 or no_tam & (no_tam - 1):
                return ov
            guardado, self.no_tam = self.no_tam, no_tam
            try:
                for _n, recs in self._folhas(self._ext_extents, primeira, total):
                    for r in recs:
                        if len(r) < 2 + 10 + 64:
                            continue
                        klen, fork, _pad, fid, ini = struct.unpack_from('>HBBII', r, 0)
                        if klen != 10 or fork != 0:
                            continue
                        ov.setdefault(fid, []).extend((a, n) for a, n in
                                                      (struct.unpack_from('>II', r, 12 + 8 * i) for i in range(8)) if n)
            finally:
                self.no_tam = guardado
        except (ErroSA, OSError, struct.error):
            pass
        return ov

    def _decmpfs(self):
        """{id do arquivo: (bytes do atributo com.apple.decmpfs, offset deles no volume ou None)}, lido uma vez da
        B-tree de atributos."""
        if self._decmpfs_attrs is not None:
            return self._decmpfs_attrs
        out = {}
        self._decmpfs_attrs = out
        tam, total, ext = self._attr_fork
        try:
            if not ext:
                return out
            b = self._ler_fork(ext, 0, 512)
            if struct.unpack_from('>b', b, 8)[0] != 1:
                return out
            no_tam, primeira = struct.unpack_from('>H', b, 14 + 18)[0], struct.unpack_from('>I', b, 14 + 10)[0]
            total_nos = struct.unpack_from('>I', b, 14 + 22)[0] or 1
            if no_tam < 512 or no_tam > 32768 or no_tam & (no_tam - 1):
                return out
            total_nos = min(total_nos, max(1, tam // no_tam))
            guardado, self.no_tam = self.no_tam, no_tam
            try:
                for n_no, recs in self._folhas(ext, primeira, total_nos):
                    for r, a in zip(recs, self._ult_pos):
                        if len(r) < 14:
                            continue
                        klen, _pad, fid, _ini, nl = struct.unpack_from('>HHIIH', r, 0)
                        d = (2 + klen + 1) & ~1
                        if klen < 12 + 2 * nl or 2 + klen > len(r) or d + 16 > len(r):
                            continue
                        nome = r[14:14 + 2 * nl].decode('utf-16-be', 'replace')
                        if nome != ATTR_DECMPFS or struct.unpack_from('>I', r, d)[0] != 0x10:    # 0x10 = dados inline
                            continue
                        n = struct.unpack_from('>I', r, d + 12)[0]
                        dados = bytes(r[d + 16:d + 16 + n])
                        try:
                            faixas = self._faixas(ext, n_no * no_tam + a + d + 16, len(dados))
                        except ErroSA:
                            faixas = []
                        out[fid] = (dados, faixas[0][0] if len(faixas) == 1 and dados else None)
            finally:
                self.no_tam = guardado
        except (ErroSA, OSError, struct.error):
            pass
        return out

    # ---- catálogo --------------------------------------------------------------------------------------------
    def _resolver(self, pai, pastas, memo, final):
        """('ok', caminho) | ('pular', None) | ('adiar', None) para o caminho da pasta `pai`."""
        cadeia, atual, base = [], pai, None
        for _ in range(MAX_PROFUNDIDADE):
            if atual in memo:
                base = memo[atual]
                break
            if atual in (ID_RAIZ, ID_PAI_DA_RAIZ):
                base = '/'
                break
            p = pastas.get(atual)
            if p is None:
                if not final:
                    return 'adiar', None
                base = PERDIDOS
                break
            cadeia.append(atual)
            atual = p['pai']
        else:
            base = PERDIDOS
        if base is None:
            return 'pular', None
        for k in reversed(cadeia):
            nome = pastas[k]['nome']
            base = None if nome.startswith('\0\0\0\0') else juntar(base, limpar_nome(nome))
            memo[k] = base
            if base is None:
                return 'pular', None
        return 'ok', base

    def _entrada(self, r, pastas, memo, final):
        estado, pai = self._resolver(r['pai'], pastas, memo, final)
        if estado == 'adiar':
            return _ADIAR
        if estado == 'pular' or r['nome'].startswith('\0\0\0\0'):
            return None
        nome = limpar_nome(r['nome'])
        e = Entrada(nome=nome, caminho=juntar(pai, nome), eh_dir=r['dir'], apagado=False, criado=r['criado'],
                    modificado=r['modif'], acessado=r['acess'], ident=r['id'])
        if r['dir']:
            memo[r['id']] = e.caminho
            return e
        self._dados(e, r)
        self.conferir_tamanho(e)
        return e

    def _dados(self, e, r):
        if r['fork'] is None:
            e.estado, e.nota = 'ruim', 'hard link sem inode'
            return
        if r['comprimido']:
            self._dados_decmpfs(e, r)
            return
        tam, total, ext = r['fork']
        e.tamanho = tam
        if tam == 0:
            return
        if sum(n for _, n in ext) < total:
            ext = ext + self._overflow().get(r['id'], [])
        runs, falta = [], tam
        for ini, cont in ext:
            if falta <= 0:
                break
            q = min(cont * self.bs, falta)
            if (ini + cont) * self.bs > self.tamanho:
                e.estado, e.nota = 'parcial', 'extent fora do volume'
                break
            runs.append((self.inicio + ini * self.bs, q))
            falta -= q
        e.runs = runs
        if falta > 0 and e.estado == 'bom':
            e.estado = 'parcial'
            e.nota = 'os extents não cobrem o tamanho (extent overflow ausente ou volume cortado)'
        if not runs:
            e.estado = 'ruim'

    def _dados_decmpfs(self, e, r):
        """Arquivo comprimido: lê o cabeçalho 'fpmc' (little-endian) do atributo e monta `e.extents_decmpfs`:
        [offset, n, u, 'zlib'] com n bytes comprimidos; u = 0 se a unidade continua no próximo extent, senão os bytes
        dela depois de descomprimida (o tipo 3 é uma unidade só; o tipo 4 tem uma por bloco de 64 KiB)."""
        e.estado = 'ruim'
        achou = self._decmpfs().get(r.get('attr_id', r['id']))
        if achou is None:
            e.nota = 'comprimido (decmpfs): atributo com.apple.decmpfs não encontrado'
            return
        attr, pos = achou
        if len(attr) < 16 or attr[:4] != b'fpmc':
            e.nota = 'comprimido (decmpfs): cabeçalho do atributo inválido'
            return
        tipo, tam = struct.unpack_from('<IQ', attr, 4)
        if tipo in (7, 8, 11, 12):
            e.nota = 'compressão LZVN/LZFSE não suportada'
            return
        if tipo not in (3, 4):
            e.nota = 'compressão decmpfs tipo %d não suportada' % tipo
            return
        try:
            ext = []
            if tam and tipo == 3:
                if tam > MAX_TIPO3_DECMPFS:
                    raise ErroSA('arquivo descomprimido grande demais (%d bytes)' % tam)
                if pos is None:
                    raise ErroSA('atributo fragmentado no disco')
                descomprimir_zlib(attr[16:], tam)                # confere já: o atributo é pequeno
                ext = [[self.inicio + pos + 16, len(attr) - 16, tam, 'zlib']]      # depois do cabeçalho 'fpmc'
            elif tam:
                ext = self._extents_tipo4(r, tam)
        except (ErroSA, OSError, struct.error, ValueError) as erro:
            e.nota = 'comprimido (decmpfs tipo %d): %s' % (tipo, erro)
            return
        e.extents_decmpfs = ext
        e.tamanho = tam
        e.runs = [(None, tam)] if tam else []
        e.estado = 'bom'

    def _extents_tipo4(self, r, tam):
        """Tipo 4: confere a tabela de blocos do resource fork e devolve os extents [offset, n, u, 'zlib']."""
        _t, total, ext = r['rsrc']
        if not ext or total == 0:
            raise ErroSA('resource fork ausente')
        nblocos = -(-tam // BLOCO_DECMPFS)
        if nblocos > MAX_BLOCOS_DECMPFS:
            raise ErroSA('tamanho descomprimido grande demais')
        base = struct.unpack_from('>I', self._ler_fork(ext, 0, 4), 0)[0]
        if base < 16 or base > 0x1000:
            raise ErroSA('cabeçalho do resource fork inválido')
        n = struct.unpack_from('<I', self._ler_fork(ext, base + 4, 4))[0]
        if n != nblocos:
            raise ErroSA('tabela de blocos com %d entradas, esperava %d' % (n, nblocos))
        tab = self._ler_fork(ext, base + 8, 8 * n)
        out, falta = [], tam
        for off, t in struct.iter_unpack('<II', tab):
            if t == 0 or t > 2 * BLOCO_DECMPFS:
                raise ErroSA('bloco comprimido com tamanho inválido')
            faixas = self._faixas(ext, base + 4 + off, t)
            for k, (o, q) in enumerate(faixas):
                out.append([self.inicio + o, q, min(BLOCO_DECMPFS, falta) if k == len(faixas) - 1 else 0, 'zlib'])
            falta -= BLOCO_DECMPFS
            if len(out) > MAX_EXTENTS_DECMPFS:
                raise ErroSA('fragmentado demais')
        return out

    def ler(self, entrada, *a, **k):
        ext = getattr(entrada, 'extents_decmpfs', None)
        if ext is None:
            yield from super().ler(entrada, *a, **k)
            return
        falta, pend = entrada.tamanho, []
        for off, n, u, _cod in ext:
            pend.append((off, n))
            if u == 0 or falta <= 0:
                continue
            try:
                dados = descomprimir_zlib(b''.join(self.leitor.ler(o, q) for o, q in pend), u)
            except ValueError as erro:
                raise OSError('decmpfs inválido: %s' % erro) from None
            pend = []
            yield dados[:falta]
            falta -= u

    def _registro(self, rec):
        """Registro de folha -> dict de pasta/arquivo, ou None (threads, hard links, links simbólicos e outros)."""
        if len(rec) < 8:
            return None
        klen = struct.unpack_from('>H', rec, 0)[0]
        if klen < 6 or 2 + klen > len(rec):
            return None
        pai, nl = struct.unpack_from('>IH', rec, 2)
        if nl > 255 or 8 + 2 * nl > 2 + klen:
            return None
        nome = unicodedata.normalize('NFC', rec[8:8 + 2 * nl].decode('utf-16-be', 'replace'))
        d = (2 + klen + 1) & ~1
        if d + 2 > len(rec):
            return None
        tipo = struct.unpack_from('>h', rec, d)[0]
        if tipo == 3:                                         # thread da pasta: o da raiz guarda o nome do volume
            if pai == ID_RAIZ and nl == 0 and d + 10 <= len(rec):
                nl2 = struct.unpack_from('>H', rec, d + 8)[0]
                self.rotulo = unicodedata.normalize('NFC', rec[d + 10:d + 10 + 2 * nl2].decode('utf-16-be', 'replace'))
            return None
        if tipo == 1 and len(rec) >= d + 88:
            fid, criado, modif, _attr, acess = struct.unpack_from('>IIIII', rec, d + 8)
            return dict(id=fid, pai=pai, nome=nome, dir=True, criado=_data(criado), modif=_data(modif),
                        acess=_data(acess), fork=None, comprimido=False)
        if tipo == 2 and len(rec) >= d + 248:
            fid, criado, modif, _attr, acess = struct.unpack_from('>IIIII', rec, d + 8)
            modo = struct.unpack_from('>H', rec, d + 42)[0] & 0o170000
            flags_dono = rec[d + 41]
            tipo_arq = rec[d + 48:d + 52]
            if tipo_arq == b'hlnk' and rec[d + 52:d + 56] == b'hfs+':       # hard link de arquivo: aponta para iNode<n>
                return dict(id=fid, pai=pai, nome=nome, dir=False, criado=_data(criado), modif=_data(modif),
                            acess=_data(acess), fork=None, comprimido=False,
                            hlnk=struct.unpack_from('>I', rec, d + 44)[0])
            if modo in (0o120000, 0o060000, 0o020000, 0o010000, 0o140000) or tipo_arq in (b'hlnk', b'fdrp'):   # hlnk sem 'hfs+' e hard link de pasta
                return None
            return dict(id=fid, pai=pai, nome=nome, dir=False, criado=_data(criado), modif=_data(modif),
                        acess=_data(acess), fork=_fork(rec, d + 88), rsrc=_fork(rec, d + 168),
                        comprimido=bool(flags_dono & UF_COMPRESSED))
        return None

    def varrer(self):
        """Gera as entradas lendo as folhas do catálogo; a que depende de uma pasta ainda não lida espera.
        `ao_ler(fração)` (se existir) é chamado a cada 16 nós, mesmo sem entrada nova; pode levantar exceção."""
        pastas, memo, pendentes, lidos = {}, {}, [], 0
        links, inodes = [], {}                               # links duros e {(pai, n): registro} dos "iNode<n>"
        try:
            for n, recs in self._folhas(self._cat_extents, self.primeira_folha, self.total_nos):
                lidos += 1
                for rec in recs:
                    try:
                        r = self._registro(rec)
                    except (struct.error, IndexError):
                        continue
                    if r is None:
                        continue
                    if r['dir']:
                        pastas[r['id']] = {'pai': r['pai'], 'nome': r['nome']}
                        if r['id'] == ID_RAIZ:             # a raiz é '/', não uma pasta dentro dele
                            self.rotulo = self.rotulo or r['nome']
                            continue
                    if r.get('hlnk'):
                        links.append(r)                      # o inode pode vir depois no catálogo: resolve no fim
                        continue
                    if not r['dir'] and r['nome'].startswith('iNode') and r['nome'][5:].isdigit():
                        inodes[(r['pai'], int(r['nome'][5:]))] = r
                    e = self._entrada(r, pastas, memo, False)
                    if e is _ADIAR:
                        pendentes.append(r)
                    elif e is not None:
                        yield e
                if pendentes:
                    restantes = []
                    for r in pendentes:
                        e = self._entrada(r, pastas, memo, False)
                        if e is _ADIAR:
                            restantes.append(r)
                        elif e is not None:
                            yield e
                    pendentes = restantes
                if self.ao_ler and lidos % 16 == 0:
                    self.ao_ler(min(1.0, lidos / max(1, self.total_nos)))
        except (OSError, ErroSA) as erro:                    # o que já saiu fica; quem lista marca a lista como incompleta
            raise ErroSA('O catálogo HFS+ ficou ilegível depois de %d nós: %s' % (lidos, erro)) from None
        for r in pendentes:
            e = self._entrada(r, pastas, memo, True)
            if e is not None and e is not _ADIAR:
                yield e
        privadas = {k for k, p in pastas.items() if p['nome'].startswith(NUL4 + 'HFS+ Private Data')}
        reais = {n: r for (pai, n), r in inodes.items() if pai in privadas}
        for r in links:
            real = reais.get(r['hlnk'])
            if real is not None:
                r = dict(r, fork=real['fork'], comprimido=real['comprimido'], rsrc=real.get('rsrc'), attr_id=real['id'])
            e = self._entrada(r, pastas, memo, True)
            if e is not None and e is not _ADIAR:
                if real is None:
                    e.estado, e.nota = 'ruim', 'hard link: o inode iNode%d não está no catálogo' % r['hlnk']
                yield e
        if self.ao_ler:
            self.ao_ler(1.0)
