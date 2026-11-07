# -*- coding: utf-8 -*-
"""ext2, ext3 e ext4 (leitura): superbloco, grupos, inodes, extents, blocos indiretos e entradas de pasta apagadas.

Como achamos apagados: ao remover um arquivo o ext junta a entrada dele à anterior (aumenta o `rec_len` da vizinha),
mas os bytes do nome e do número do inode continuam na "folga" da pasta. Varremos essa folga e só aceitamos inode
com `dtime` preenchido (de fato apagado). Depois o conteúdo depende do sistema:
  * ext2 mantém os ponteiros do inode: dá para recuperar (conferimos o bitmap de blocos; reuso vira `parcial`);
  * ext3 zera os ponteiros e o tamanho ao apagar, e o ext4 zera os extents: o nome aparece, mas o conteúdo não
    é recuperável por aqui (estado `ruim` com a explicação); o que sobra é o carving.
Limites: inode apagado cuja entrada foi a primeira do bloco da pasta (o ext zera o número do inode) e arquivo
dentro de pasta apagada não são achados; "inline data" só até 60 bytes; criptografia/compressão não são tratadas;
htree é lido como pasta linear (os blocos de folha têm o mesmo formato).
"""
import struct

from .base import ErroSA, ErroTeto, Entrada, Volume, MAX_PROFUNDIDADE, juntar, limpar_nome

MAGIC = 0xEF53
RAIZ = 2
F_EXTENTS = 0x80000
F_INLINE = 0x10000000
INC_FILETYPE, INC_EXTENTS, INC_64BIT, INC_FLEX = 0x2, 0x40, 0x80, 0x200
MAX_NOS_EXTENTS = 256                 # nós da árvore de extents visitados por arquivo
MAX_EXTENTS = 100000                 # extents (e entradas de índice) vistos por arquivo
MAX_BLOCOS_MAPA = 1 << 20         # arquivo com mapa de blocos (ext2/3) acima disto (1 GiB com blocos de 1 KiB) sai ruim


def _super(b):
    if len(b) < 264 or struct.unpack_from('<H', b, 56)[0] != MAGIC:
        raise ErroSA('Superbloco ext inválido.')
    s = dict(inodes=struct.unpack_from('<I', b, 0)[0], blocos=struct.unpack_from('<I', b, 4)[0],
             primeiro=struct.unpack_from('<I', b, 20)[0], logbs=struct.unpack_from('<I', b, 24)[0],
             bpg=struct.unpack_from('<I', b, 32)[0], ipg=struct.unpack_from('<I', b, 40)[0],
             rev=struct.unpack_from('<I', b, 76)[0], compat=struct.unpack_from('<I', b, 92)[0],
             incompat=struct.unpack_from('<I', b, 96)[0], rotulo=b[120:136])
    s['isize'] = struct.unpack_from('<H', b, 88)[0] if s['rev'] >= 1 else 128
    s['dsize'] = struct.unpack_from('<H', b, 254)[0]
    if s['incompat'] & INC_64BIT:
        s['blocos'] |= struct.unpack_from('<I', b, 336)[0] << 32
    if (s['logbs'] > 6 or s['bpg'] == 0 or s['ipg'] == 0 or s['inodes'] == 0 or s['blocos'] == 0
            or s['isize'] < 128 or s['isize'] & (s['isize'] - 1) or s['isize'] > (1024 << s['logbs'])):
        raise ErroSA('Superbloco ext com parâmetros inválidos.')
    return s


def reconhecer(leitor, inicio=0, tamanho=None):
    try:
        _super(leitor.ler(inicio + 1024, 1024))
        return Ext(leitor, inicio, tamanho)
    except (ErroSA, OSError, struct.error):
        return None


class Ext(Volume):
    def __init__(self, leitor, inicio=0, tamanho=None):
        super().__init__(leitor, inicio, tamanho)
        try:
            self.__dict__.update(_super(self._ler(1024, 1024)))
        except OSError as e:
            raise ErroSA('Não consegui ler o superbloco: %s' % e) from None
        self.bs = 1024 << self.logbs
        self.ngrupos = -(-(self.blocos - self.primeiro) // self.bpg)
        self.tipo = ('ext4' if self.incompat & (INC_EXTENTS | INC_64BIT | INC_FLEX)
                     else 'ext3' if self.compat & 0x4 else 'ext2')
        self.rotulo = self.rotulo.split(b'\0')[0].decode('utf-8', 'replace')
        self.gdesc = 64 if (self.incompat & INC_64BIT and self.dsize >= 64) else 32
        self._gd = {}
        self._bm = {}

    # ---- estruturas ------------------------------------------------------------------------------------------
    def _grupo(self, g):
        d = self._gd.get(g)
        if d is None:
            if not 0 <= g < self.ngrupos:
                raise ErroSA('grupo %d fora do volume' % g)
            b = self._ler((self.primeiro + 1) * self.bs + g * self.gdesc, self.gdesc)
            bitmap, _, tabela = struct.unpack_from('<III', b, 0)
            if self.gdesc >= 64:
                bitmap |= struct.unpack_from('<I', b, 32)[0] << 32
                tabela |= struct.unpack_from('<I', b, 40)[0] << 32
            d = self._gd[g] = (bitmap, tabela)
        return d

    def _inode(self, ino):
        if not 1 <= ino <= self.inodes:
            raise ErroSA('inode %d fora do volume' % ino)
        g, i = divmod(ino - 1, self.ipg)
        return self._ler(self._grupo(g)[1] * self.bs + i * self.isize, self.isize)

    def _bloco_ocupado(self, b):
        """True se o bitmap diz que o bloco está em uso; None se não deu para ler."""
        g, i = divmod(b - self.primeiro, self.bpg)
        try:
            mapa = self._bm.get(g)
            if mapa is None:
                mapa = self._bm[g] = self._ler(self._grupo(g)[0] * self.bs, self.bs)
            return bool(mapa[i // 8] >> (i % 8) & 1)
        except (OSError, ErroSA, IndexError):
            return None

    def _bloco(self, n):
        if n <= 0 or n >= self.blocos or (n + 1) * self.bs > self.tamanho:
            raise ErroSA('bloco %d fora do volume' % n)
        return self._ler(n * self.bs, self.bs)

    # ---- mapa de blocos --------------------------------------------------------------------------------------
    def _extents(self, dados, prof=0, cont=None):
        """Folhas da árvore de extents em `dados` (cabeçalho + entradas): [(bloco lógico, físico ou None, quantidade)].
        Físico None = extent não inicializado: o conteúdo é zero."""
        if len(dados) < 12 or struct.unpack_from('<H', dados, 0)[0] != 0xF30A or prof > 5:
            raise ErroSA('árvore de extents inválida')
        cont = cont if cont is not None else {'nos': 0, 'ext': 0}     # contadores compartilhados pela árvore toda
        cont['nos'] += 1
        if cont['nos'] > MAX_NOS_EXTENTS:
            raise ErroTeto('árvore de extents com nós demais')
        n, _, profundidade = struct.unpack_from('<HHH', dados, 2)
        out = []
        for k in range(min(n, (len(dados) - 12) // 12)):
            e = dados[12 + 12 * k: 24 + 12 * k]
            cont['ext'] += 1
            if cont['ext'] > MAX_EXTENTS:
                raise ErroTeto('árvore de extents com extents demais')
            if profundidade == 0:
                bl, ln, hi, lo = struct.unpack('<IHHI', e)
                if ln > 32768:                               # extent não inicializado (fallocate): lê como zeros, e o
                    out.append((bl, None, ln - 32768))       # bloco físico pode ter lixo ou sobra de outro arquivo
                else:
                    out.append((bl, (hi << 32) | lo, ln))
            else:
                bl, lo, hi = struct.unpack_from('<IIH', e, 0)
                out += self._extents(self._bloco((hi << 32) | lo), prof + 1, cont)
        return out

    def _ponteiros(self, bloco, nivel, saida, limite):
        """Percorre blocos indiretos; `saida` recebe o bloco físico de cada bloco lógico (0 = buraco)."""
        if len(saida) >= limite:
            return
        if bloco == 0:
            saida.extend([0] * min((self.bs // 4) ** nivel, limite - len(saida)))
            return
        for p in struct.unpack('<%dI' % (self.bs // 4), self._bloco(bloco)):
            if len(saida) >= limite:
                return
            if nivel == 1:
                saida.append(p)
            else:
                self._ponteiros(p, nivel - 1, saida, limite)

    def _mapa(self, ino_dados, tam):
        """Lista [(físico ou None, quantidade de blocos)] cobrindo `tam` bytes."""
        flags = struct.unpack_from('<I', ino_dados, 32)[0]
        iblock = ino_dados[40:100]
        nb = -(-tam // self.bs)
        if flags & F_EXTENTS:
            ext = sorted(self._extents(iblock), key=lambda x: x[0])
            fis, atual = [], 0
            for bl, ph, ln in ext:
                if bl >= nb:
                    break
                if bl > atual:
                    fis.append((None, bl - atual))
                fis.append((ph, min(ln, nb - bl)))
                atual = bl + min(ln, nb - bl)
            if atual < nb:
                fis.append((None, nb - atual))
            return fis
        ptrs = list(struct.unpack('<15I', iblock))
        lista = ptrs[:12]
        topo = min(nb, MAX_BLOCOS_MAPA)          # acima do teto devolve só o começo; _dados marca o resto como não lido
        for nivel, p in enumerate(ptrs[12:], 1):
            if len(lista) >= topo:
                break
            self._ponteiros(p, nivel, lista, topo)
        lista = lista[:topo] + [0] * max(0, topo - len(lista))
        fis = []
        for p in lista:
            if fis and ((p == 0 and fis[-1][0] is None) or (p and fis[-1][0] is not None
                                                           and fis[-1][0] + fis[-1][1] == p)):
                fis[-1] = (fis[-1][0], fis[-1][1] + 1)
            else:
                fis.append((p or None, 1))
        return fis

    # ---- entradas --------------------------------------------------------------------------------------------
    def _dados(self, ent, ino_dados, apagado):
        mode, = struct.unpack_from('<H', ino_dados, 0)
        tam = struct.unpack_from('<I', ino_dados, 4)[0]
        if mode & 0xF000 == 0x8000:
            tam |= struct.unpack_from('<I', ino_dados, 108)[0] << 32
        flags = struct.unpack_from('<I', ino_dados, 32)[0]
        ent.tamanho = tam
        if tam > 4 * self.tamanho:
            ent.estado, ent.nota = 'ruim', 'tamanho incoerente com o volume (inode danificado?)'
            return
        if flags & F_INLINE:
            if tam <= 60:
                ent.inline = ino_dados[40:40 + tam]
                g, i = divmod(ent.ident - 1, self.ipg)       # os bytes também estão na origem: no i_block do inode
                ent.runs = [(self.inicio + self._grupo(g)[1] * self.bs + i * self.isize + 40, tam)] if tam else []
            else:
                ent.estado, ent.nota = 'ruim', 'inline data maior que 60 bytes: não suportado'
            return
        if tam == 0 and not apagado:
            return
        try:
            fis = self._mapa(ino_dados, tam)
        except ErroTeto as e:
            ent.estado, ent.nota, ent.runs = 'ruim', 'estrutura grande demais para ler (%s); inode danificado?' % e, []
            return
        except (ErroSA, OSError, struct.error):
            fis = []
        if apagado and (tam == 0 or not any(p for p, _ in fis)):
            ent.estado = 'ruim'
            ent.nota = ('o ' + self.tipo + ' zerou os ponteiros ao apagar: o nome ficou, o conteúdo não dá para '
                        'recuperar pelo sistema de arquivos (só por carving)')
            ent.runs = []
            return
        runs, falta = [], tam
        for p, n in fis:
            if falta <= 0:
                break
            q = min(n * self.bs, falta)
            if p is not None and (p + n) * self.bs > self.tamanho:
                ent.estado, ent.nota = 'ruim', 'bloco fora do volume'
                return
            runs.append((None if p is None else self.inicio + p * self.bs, q))
            falta -= q
        ent.runs = runs
        if falta > 0:
            ent.estado, ent.nota = 'parcial', 'ponteiros não cobrem o tamanho'
            if not flags & F_EXTENTS and -(-tam // self.bs) > MAX_BLOCOS_MAPA:
                ent.nota = 'arquivo grande: lidos só os primeiros %d blocos; o resto não foi lido' % MAX_BLOCOS_MAPA
        elif apagado:
            ent.nota = 'ext2: ponteiros preservados'
            if any(self._bloco_ocupado(b) for p, n in fis if p for b in range(p, p + n)):
                ent.estado, ent.nota = 'parcial', 'blocos já reutilizados (conforme o bitmap)'

    def _entrada(self, ino, nome, pai, apagado_pai, fantasma):
        try:
            d = self._inode(ino)
        except (OSError, ErroSA):
            return None
        mode, links = struct.unpack_from('<H', d, 0)[0], struct.unpack_from('<H', d, 26)[0]
        dtime = struct.unpack_from('<I', d, 20)[0]
        tipo = mode & 0xF000
        if tipo not in (0x4000, 0x8000):
            return None
        if fantasma and not dtime:
            return None                                       # inode em uso por outro: a entrada velha é lixo
        nome = limpar_nome(nome)
        ent = Entrada(nome=nome, caminho=juntar(pai, nome), eh_dir=tipo == 0x4000, apagado=fantasma or apagado_pai,
                      acessado=float(struct.unpack_from('<I', d, 8)[0]) or None,
                      modificado=float(struct.unpack_from('<I', d, 16)[0]) or None, ident=ino)
        ent.criado = None
        if self.isize > 128 and len(d) >= 152 and struct.unpack_from('<H', d, 128)[0] >= 24:
            c = struct.unpack_from('<I', d, 144)[0]
            ent.criado = float(c) or None
        if not ent.eh_dir:
            self._dados(ent, d, fantasma)
        return ent, d

    def _ler_pasta(self, ino_dados):
        self._avisar()
        tam = struct.unpack_from('<I', ino_dados, 4)[0]
        if tam > (64 << 20):
            raise ErroSA('pasta grande demais')
        partes = []
        for p, n in self._mapa(ino_dados, tam):
            if p is None:
                partes.append(b'\0' * (n * self.bs))
            else:
                if (p + n) * self.bs > self.tamanho:
                    raise ErroSA('bloco fora do volume')
                partes.append(self._ler(p * self.bs, n * self.bs))
        return b''.join(partes)[:tam]

    def _nomes(self, dados):
        """Gera (inode, nome, fantasma) de cada bloco da pasta, inclusive as entradas escondidas na folga."""
        for ini in range(0, len(dados), self.bs):
            blk = dados[ini: ini + self.bs]
            off = 0
            while off + 8 <= len(blk):
                ino, rl, nl = struct.unpack_from('<IHB', blk, off)
                if rl < 8 or rl % 4 or off + rl > len(blk) or 8 + nl > rl:
                    break
                if ino:
                    yield ino, blk[off + 8: off + 8 + nl], False
                real = (8 + nl + 3) & ~3
                h = off + real
                while h + 8 <= off + rl:                      # folga: entradas apagadas coladas a esta
                    hi, hrl, hnl = struct.unpack_from('<IHB', blk, h)
                    if hrl < 8 or hrl % 4 or h + hrl > off + rl or 8 + hnl > hrl:
                        break
                    if hi and hnl:
                        yield hi, blk[h + 8: h + 8 + hnl], True
                    h += hrl
                off += rl

    def _pasta(self, ino, d, caminho, apagado, nivel, vistos):
        try:
            dados = self._ler_pasta(d)
        except (ErroSA, OSError, struct.error):
            return
        vistas = set()
        for i, nb, fantasma in self._nomes(dados):
            nome = nb.decode('utf-8', 'replace')
            if nome in ('.', '..') or (i, nome) in vistas:
                continue
            vistas.add((i, nome))
            r = self._entrada(i, nome, caminho, apagado, fantasma)
            if r is None:
                continue
            ent, di = r
            yield ent
            if ent.eh_dir and not fantasma and i not in vistos and nivel < MAX_PROFUNDIDADE:
                vistos.add(i)
                yield from self._pasta(i, di, ent.caminho, apagado, nivel + 1, vistos)

    def varrer(self):
        try:
            d = self._inode(RAIZ)
        except (OSError, ErroSA) as e:
            raise ErroSA('Não consegui ler o inode da raiz: %s' % e) from None
        yield from self._pasta(RAIZ, d, '/', False, 0, {RAIZ})
