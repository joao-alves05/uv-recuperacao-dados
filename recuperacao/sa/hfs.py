# -*- coding: utf-8 -*-
"""HFS+ e HFSX (leitura): volume header (com a cópia reserva do fim), B-tree do catálogo, extents do fork de dados.

Lista pastas e arquivos VIVOS com o caminho original; serve para volume de Mac com a estrutura danificada em que o
catálogo ainda está inteiro. Entram: nome (Unicode, normalizado para NFC), tamanho do fork de dados, datas (1904-UTC),
os 8 extents do registro e, se faltar, os do arquivo de extents overflow.

O que NÃO dá para fazer, e por quê:
  * Arquivo apagado: o HFS+ tira o registro do catálogo e reaproveita o nó da B-tree; o que sobra nos nós é acaso.
    Não há como recuperar pela estrutura: deixe para a busca por assinatura (carving).
  * Resource fork, atributos estendidos e arquivo comprimido (decmpfs): os bytes não estão no fork de dados. Arquivo
    comprimido sai `ruim` com a explicação.
  * Hard links (o registro do link aponta para um inode escondido na pasta "HFS+ Private Data") e links simbólicos
    não são listados; a pasta escondida também não.
  * Volume com journal sujo: o catálogo no disco pode estar atrasado em relação ao journal, que não é reaplicado.
  * HFS+ embrulhado em HFS clássico (assinatura "BD") não é reconhecido.
"""
import struct
import unicodedata

from .base import ErroSA, Entrada, Volume, MAX_PROFUNDIDADE, epoch, juntar, limpar_nome

MAC_PARA_UNIX = 2082844800                 # segundos entre 1904-01-01 e 1970-01-01
ID_RAIZ, ID_PAI_DA_RAIZ = 2, 1
ID_EXTENTS, ID_CATALOGO = 3, 4
PERDIDOS = '/[perdidos]'
_ADIAR = object()
UF_COMPRESSED = 0x20


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
                extents_fork=_fork(b, 192), catalogo_fork=_fork(b, 272))


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
        recs = []
        for i in range(nreg):
            a, z = offs[i], offs[i + 1]
            if a < 14 or z > self.no_tam - 2 * (nreg + 1) or z < a:
                continue
            recs.append(b[a:z])
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
        if r['comprimido']:
            e.estado, e.nota = 'ruim', 'comprimido (decmpfs): os bytes não estão no fork de dados'
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
            if modo in (0o120000, 0o060000, 0o020000, 0o010000, 0o140000) or tipo_arq == b'hlnk':
                return None
            return dict(id=fid, pai=pai, nome=nome, dir=False, criado=_data(criado), modif=_data(modif),
                        acess=_data(acess), fork=_fork(rec, d + 88), comprimido=bool(flags_dono & UF_COMPRESSED))
        return None

    def varrer(self):
        """Gera as entradas lendo as folhas do catálogo; a que depende de uma pasta ainda não lida espera.
        `ao_ler(fração)` (se existir) é chamado a cada 16 nós, mesmo sem entrada nova; pode levantar exceção."""
        pastas, memo, pendentes, lidos = {}, {}, [], 0
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
        if self.ao_ler:
            self.ao_ler(1.0)
