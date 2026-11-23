# -*- coding: utf-8 -*-
"""UDF (leitura), versões 1.02 a 2.01: o formato dos DVDs gravados e dos discos "ponte" ISO/UDF.

Caminho: descritores BEA01 + NSR02/NSR03 (setor 16 em diante) -> âncora (AVDP) no setor 256, no último setor menos
256 ou no último setor -> conjunto de volume (PVD, partição, LVD) -> conjunto de arquivos (FSD) -> ICB raiz -> File
Entry / Extended File Entry -> diretórios (File Identifier Descriptors, inclusive os marcados como apagados).

Multi-sessão: se o leitor oferecer `sessoes()` (lista dos setores iniciais, vinda do TOC), cada sessão tem a âncora
em início + 256 e vale a última sessão que tem conjunto de volume válido. Sem isso, só as âncoras padrão são olhadas;
com `todas_sessoes=True` (ou se nenhuma âncora padrão existe) o disco inteiro é varrido atrás de âncoras.

Partição virtual (CD-R/RW gravado de forma incremental): o mapa tipo 2 "*UDF Virtual Partition" manda traduzir todo
bloco lógico (FSD, ICBs, extensões de arquivo) pela VAT. A VAT é um arquivo cujo File Entry fica no último setor
gravado (fim da imagem, ou o setor antes do início da sessão seguinte; procura nos últimos MAX_BUSCA_VAT setores):
UDF 2.00+ = tipo de arquivo 248 com cabeçalho; UDF 1.50 = só a tabela, terminada pelo "*UDF Virtual Alloc Tbl".
VAT ilegível não derruba o volume: abre sem listar nada e o motivo vai em `limitacao`. Só a partição virtual é
traduzida; referências à física no mesmo volume são lidas como se fossem virtuais.

Partição esparsa (CD-RW em pacotes, UDF 1.50+): o mapa tipo 2 "*UDF Sparable Partition" traz o tamanho do pacote e
até 4 cópias da Sparing Table ("*UDF Sparing Table", checada por tag, identificador e CRC). Bloco cujo pacote está na
tabela é lido do pacote de reposição. Regra do kernel (fs/udf/partition.c, udf_get_pblock_spar15): o pacote é
comparado com `origLocation` em endereço RELATIVO à partição (`packet = (block + offset) & ~(packet_len - 1)`), mas
`mappedLocation` é ABSOLUTO no disco: o físico é `mappedLocation + (block & (packet_len - 1))`, sem somar o início
da partição (`return le32(mappedLocation) + ((block + offset) & (packet_len - 1))`). Vale a
primeira cópia válida; nenhuma válida = aviso em `limitacao` e leitura direta, sem remapear.

Partição de metadados (UDF 2.50+, Blu-ray e HD em UDF): o mapa tipo 2 "*UDF Metadata Partition" aponta o Metadata
File (File Entry tipo 250) e o Metadata Mirror File (251). Os ICBs e os diretórios ficam dentro dele: o bloco da
partição de metadados é um deslocamento no arquivo, traduzido pelos extents dele para a partição física (a
"partition reference" dos long_ad e dos FIDs diz quem é quem; short_ad herda a do ICB). Se o Metadata File ou o
conteúdo dele (conjunto de arquivos) estiver ilegível, lê pelo espelho e avisa; nenhum dos dois serve = aviso e nada
listado. O Metadata Bitmap não é lido. Os dados dos arquivos ficam na partição física.

Fora do que lemos (dito em `limitacao`): fluxos nomeados, ACL e atributos estendidos. Só lê, pelo `Leitor`.
"""
import binascii
import struct
import sys
from array import array

from .base import ErroSA, ErroTeto, Entrada, Volume, MAX_PROFUNDIDADE, epoch, juntar, limpar_nome

SETOR = 2048
MAX_DIR = 64 << 20
MAX_TOTAL_PASTAS = 256 << 20            # bytes de pastas lidos numa varredura (pastas que se sobrepõem não viram bomba)
MAX_ANCORAS = 64
MAX_SALTOS_ICB = 16                      # Indirect Entries seguidas até o File Entry; mais que isso é laço ou lixo
MAX_BUSCA_VAT = 64                       # setores procurados de trás para frente atrás do File Entry da VAT
MAX_VAT = 16 << 20                       # bytes de VAT aceitos (4 milhões de entradas): a VAT só tem um bloco por ICB (arquivo ou
                                         # pasta), não por bloco de dados; 4 milhões de arquivos já passa do que um CD/DVD/BD guarda.
                                         # Além do teto, e além de `part_tam` entradas, a tabela não é lida (array('I'): 4 bytes cada)
MAX_ESPARSA = 1 << 20                    # entradas de pacote remapeado aceitas na Sparing Table
LIMITACAO = ('UDF: partição de metadados lida pelo Metadata File, sem o bitmap; sem fluxos nomeados e ACL.')


def _tag(b, minimo=16):
    """(id, local) se `b` começa com um descritor UDF de checksum certo; senão None."""
    if len(b) < minimo:
        return None
    ident, _, chk, _, _, _, _, loc = struct.unpack_from('<HHBBHHHI', b, 0)
    if (sum(b[:4]) + sum(b[5:16])) & 0xFF != chk or ident == 0:
        return None
    return ident, loc


def _dstring(b):
    if not b:
        return ''
    n = min(b[-1], len(b) - 1)
    dados = b[1:n]
    if not b[0]:
        return ''
    return (dados.decode('utf-16-be', 'replace') if b[0] == 16 else dados.decode('latin-1')).strip('\0')


def _nome_udf(b):
    if not b:
        return ''
    return b[1:].decode('utf-16-be', 'replace') if b[0] == 16 else b[1:].decode('latin-1')


def _data(b):
    if len(b) < 12:
        return None
    tz, ano, mes, dia, h, mi, s = struct.unpack_from('<HHBBBBB', b, 0)
    if not (1 <= mes <= 12 and 1 <= dia <= 31 and h < 24 and mi < 60 and s < 61):
        return None
    t = epoch(ano, mes, dia, h, mi, min(s, 59))
    if t is None:
        return None
    if tz >> 12 == 1:
        off = tz & 0xFFF
        off = off - 4096 if off & 0x800 else off
        if -1440 <= off <= 1440:
            t -= off * 60
    return t


def _vrs_ok(leitor, inicio):
    """BEA01 e NSR02/NSR03 nos descritores de volume (setores 16 a 31)."""
    bea = nsr = False
    for k in range(16, 32):
        d = leitor.ler(inicio + k * SETOR, 8)
        if len(d) < 6:
            break
        ident = d[1:6]
        if ident == b'BEA01':
            bea = True
        elif ident in (b'NSR02', b'NSR03') and bea:
            nsr = True
        elif ident == b'TEA01':
            break
        elif ident not in (b'CD001', b'CDW02', b'BOOT2'):
            break
    return bea and nsr


def _eh_avdp(leitor, inicio, setor):
    if setor < 0 or (setor + 1) * SETOR > leitor.tamanho - inicio:
        return False
    t = _tag(leitor.ler(inicio + setor * SETOR, 16))
    return t is not None and t[0] == 2


def achar_ancoras(leitor, inicio, tamanho, varrer=False, sessoes=None):
    """Setores com âncora (AVDP): as padrão, as das sessões informadas e, se `varrer`, as que o disco inteiro tiver."""
    ultimo = tamanho // SETOR - 1
    cands = {256, ultimo - 256, ultimo}
    for s in sessoes or ():
        cands.add(s + 256)
    achadas = {c for c in cands if _eh_avdp(leitor, inicio, c)}
    if varrer or not achadas:
        pos, bloco = 0, 1 << 20
        while pos < tamanho and len(achadas) < MAX_ANCORAS:
            dados = leitor.ler(inicio + pos, min(bloco, tamanho - pos))
            for k in range(0, len(dados) - SETOR + 1, SETOR):
                if dados[k] == 2 and dados[k + 1] == 0:
                    t = _tag(dados[k: k + 16])
                    if t and t[0] == 2 and struct.unpack_from('<I', dados, k + 16)[0]:
                        achadas.add((pos + k) // SETOR)
            pos += bloco
    return sorted(achadas)


def reconhecer(leitor, inicio=0, tamanho=None, todas_sessoes=False):
    try:
        t = (leitor.tamanho - inicio) if tamanho is None else min(tamanho, leitor.tamanho - inicio)
        if t < 300 * SETOR or not _vrs_ok(leitor, inicio):
            return None
        return Udf(leitor, inicio, tamanho, todas_sessoes)
    except (ErroSA, OSError, struct.error):
        return None


class ErroICBIndireto(ErroSA):
    """Cadeia de Indirect Entries em laço ou longa demais: o arquivo sai `ruim` com nota, a varredura segue."""


class Udf(Volume):
    tipo = 'UDF'

    def __init__(self, leitor, inicio=0, tamanho=None, todas_sessoes=False):
        super().__init__(leitor, inicio, tamanho)
        self.limitacao = LIMITACAO
        self.vat = None                                     # array('I') bloco virtual -> bloco físico (0xFFFFFFFF = livre), só em partição virtual
        self.meta, self.meta_ref = None, None               # [(início no arquivo, offset absoluto, bytes)] do Metadata File; nº da partition reference
        self._raiz_ref = None
        self.spar = None                                    # {1º bloco do pacote original: 1º bloco do pacote de reposição}
        self.spar_pacote = 0
        self._raiz = None
        self._sessoes = None
        sessoes = None
        if callable(getattr(leitor, 'sessoes', None)):
            try:
                sessoes = list(leitor.sessoes())
            except OSError:
                sessoes = None
        self._sessoes = sessoes
        try:
            ancoras = achar_ancoras(leitor, inicio, self.tamanho, todas_sessoes, sessoes)
        except OSError as e:
            raise ErroSA('Não consegui procurar a âncora do UDF: %s' % e) from None
        erro = 'Sem âncora UDF.'
        for a in reversed(ancoras):                         # a última sessão primeiro
            try:
                self._abrir_conjunto(a)
                self.ancora = a
                return
            except (ErroSA, OSError, struct.error) as e:
                erro = str(e)
        raise ErroSA(erro)

    # --- leitura por setor -----------------------------------------------------
    def _setor(self, n, quantos=1):
        return self._ler(n * SETOR, quantos * SETOR)

    def _abrir_conjunto(self, ancora):
        av = self._setor(ancora)
        extents = [struct.unpack_from('<II', av, 16), struct.unpack_from('<II', av, 24)]     # principal, reserva
        ultimo = None
        for comp, loc in extents:
            if comp == 0:
                continue
            try:
                self._conjunto(loc, min(comp, 256 * SETOR) // SETOR, ancora)
                return
            except (ErroSA, OSError, struct.error) as e:
                ultimo = e
        raise ErroSA(str(ultimo) if ultimo else 'Âncora UDF sem conjunto de volume.')

    def _conjunto(self, loc, n, ancora=0):
        pvd = pd = lvd = None
        for k in range(n):
            b = self._setor(loc + k)
            t = _tag(b)
            if t is None:
                break
            seq = struct.unpack_from('<I', b, 16)[0]
            if t[0] == 1 and (pvd is None or seq >= pvd[0]):
                pvd = (seq, b)
            elif t[0] == 5 and (pd is None or seq >= pd[0]):
                pd = (seq, b)
            elif t[0] == 6 and (lvd is None or seq >= lvd[0]):
                lvd = (seq, b)
            elif t[0] == 8:
                break
        if pd is None or lvd is None:
            raise ErroSA('Conjunto de volume UDF incompleto.')
        pd, lvd = pd[1], lvd[1]
        self.rotulo = _dstring(pvd[1][24:56]) if pvd else _dstring(lvd[84:212])
        self.bloco = struct.unpack_from('<I', lvd, 212)[0]
        if self.bloco != SETOR:
            raise ErroSA('Bloco UDF de %d bytes (só 2048 é lido).' % self.bloco)
        num_part = struct.unpack_from('<H', pd, 22)[0]
        self.part_ini = struct.unpack_from('<I', pd, 188)[0]
        self.part_tam = struct.unpack_from('<I', pd, 192)[0]
        tam_mapas, n_mapas = struct.unpack_from('<II', lvd, 264)
        off, mapa, virtual, esparsa, meta_map = 440, None, False, None, None
        self.meta, self.meta_ref = None, None
        for ref in range(min(n_mapas, 8)):
            tipo, ln = lvd[off], lvd[off + 1]
            if ln < 2:
                break
            if tipo == 1:
                if struct.unpack_from('<H', lvd, off + 4)[0] == num_part and mapa is None:
                    mapa = off
            elif tipo == 2 and ln >= 40 and lvd[off + 5: off + 27] == b'*UDF Virtual Partition':
                virtual = True
            elif (tipo == 2 and ln >= 64 and lvd[off + 5: off + 28] == b'*UDF Sparable Partition'
                  and struct.unpack_from('<H', lvd, off + 38)[0] == num_part and mapa is None):
                mapa = off
                esparsa = lvd[off: off + ln]
            elif (tipo == 2 and ln >= 60 and lvd[off + 5: off + 28] == b'*UDF Metadata Partition'
                  and struct.unpack_from('<H', lvd, off + 38)[0] == num_part):
                meta_map = struct.unpack_from('<II', lvd, off + 40)         # Metadata File, Metadata Mirror File
                self.meta_ref = ref
            else:
                raise ErroSA('Tipo de partição UDF não suportado.')
            off += ln
        if mapa is None:
            raise ErroSA('O volume UDF não tem partição física legível.')
        self.spar, self.spar_pacote = None, 0
        if esparsa:
            self._ler_esparsa(esparsa)
        self.vat = None
        if virtual:
            try:
                self.vat = self._ler_vat(ancora)
            except (ErroSA, OSError, struct.error) as e:
                self.limitacao = '%s AVISO: VAT ilegível (%s); os arquivos da partição virtual não foram listados.' % (LIMITACAO, e)
                self._raiz = None
                return
        fsd = struct.unpack_from('<IIH', lvd, 248)         # tamanho, bloco e partition reference (long_ad do conjunto de arquivos)
        if meta_map is None:
            self._raiz, self._raiz_ref = self._achar_fsd(fsd[1], fsd[2])
            return
        avisos = []
        for k, (nome, loc, tipo) in enumerate((('Metadata File', meta_map[0], 250), ('Metadata Mirror File', meta_map[1], 251))):
            try:
                self.meta = None
                self.meta = self._ler_meta(loc, tipo)
                self._raiz, self._raiz_ref = self._achar_fsd(fsd[1], fsd[2])
                if k:
                    self.limitacao = '%s AVISO: %s; lido pelo Metadata Mirror File.' % (self.limitacao, avisos[0])
                return
            except (ErroSA, OSError, struct.error) as e:
                avisos.append('%s ilegível (%s)' % (nome, e))
        self.meta, self._raiz = None, None
        self.limitacao = '%s AVISO: %s; os arquivos da partição de metadados não foram listados.' % (self.limitacao, '; '.join(avisos))

    def _achar_fsd(self, fsd_lbn, ref):
        for k in range(16):
            b = self._particao(fsd_lbn + k, 1, ref)
            t = _tag(b)
            if t and t[0] == 256:
                if not struct.unpack_from('<I', b, 400)[0]:
                    raise ErroSA('Conjunto de arquivos UDF sem pasta raiz.')
                return struct.unpack_from('<I', b, 404)[0], struct.unpack_from('<H', b, 408)[0]
            if t and t[0] == 8:
                break
        raise ErroSA('Não achei o conjunto de arquivos (FSD) do UDF.')

    def _ler_meta(self, loc, tipo):
        """Extents do Metadata File (ou do espelho) no bloco físico `loc`: [(início no arquivo, offset absoluto, bytes)]."""
        r = self._ler_icb(loc)
        if r['tipo'] != tipo or r['runs'] is None:
            raise ErroSA('File Entry de metadados inválido no bloco %d' % loc)
        out, ini = [], 0
        for off, n in r['runs']:
            out.append((ini, off, n))
            ini += n
        if not out:
            raise ErroSA('arquivo de metadados vazio')
        return out

    def _meta_fisico(self, lbn):
        """Bloco físico (relativo à partição) do bloco `lbn` da partição de metadados."""
        b = lbn * SETOR
        for ini, off, n in self.meta:
            if ini <= b < ini + n:
                if off is None:
                    break
                return (off + b - ini - self.inicio) // SETOR - self.part_ini
        raise ErroSA('bloco de metadados %d fora do Metadata File' % lbn)

    def _ler_vat(self, ancora):
        """Tabela (array bloco virtual -> bloco físico; 0xFFFFFFFF = livre) do File Entry da VAT mais perto do fim gravado."""
        fim = self.tamanho // SETOR
        for ini in sorted(self._sessoes or []):
            if ancora < ini < fim:                          # a VAT da sessão da âncora acaba antes da próxima
                fim = ini
                break
        ultimo = None
        for sec in range(fim - 1, max(self.part_ini, fim - 1 - MAX_BUSCA_VAT) - 1, -1):
            try:
                t = _tag(self._setor(sec))
                if t is None or t[0] not in (261, 266):
                    continue
                r = self._ler_icb(sec - self.part_ini)
                if r['info'] > MAX_VAT:
                    raise ErroSA('VAT com %d bytes, acima do teto de %d' % (r['info'], MAX_VAT))
                dados = self._conteudo(r)
            except (ErroSA, OSError, struct.error) as e:
                ultimo = e
                continue
            if r['tipo'] == 248:                            # UDF 2.00+: cabeçalho de L_HD bytes, depois as entradas
                hl = struct.unpack_from('<H', dados, 0)[0]
                if hl < 4 or hl > len(dados):
                    raise ErroSA('cabeçalho da VAT inválido')
                corpo = dados[hl:]
            elif b'*UDF Virtual Alloc Tbl' in dados[-36:-4]:      # UDF 1.50: só a tabela e 36 bytes de rodapé
                corpo = dados[:-36]
            else:
                continue
            n = len(corpo) // 4
            if self.part_tam:
                n = min(n, self.part_tam)                   # a tradução não precisa de mais entradas que blocos na partição
            tab = array('I')
            tab.frombytes(corpo[:n * 4])
            if sys.byteorder == 'big':
                tab.byteswap()
            del corpo, dados
            if not n or any(v != 0xFFFFFFFF and (v >= self.part_tam > 0) for v in tab):
                raise ErroSA('tabela da VAT inconsistente')
            return tab
        raise ErroSA(str(ultimo) if ultimo else 'VAT não encontrada nos últimos %d setores gravados' % MAX_BUSCA_VAT)

    def _ler_esparsa(self, mapa):
        """Sparing Table da primeira cópia válida; sem nenhuma, avisa em `limitacao` e deixa a leitura direta."""
        pacote, n, tam = struct.unpack_from('<HBxI', mapa, 40)
        motivo = 'nenhuma cópia da Sparing Table'
        if pacote == 0:
            motivo = 'tamanho de pacote zero'
        else:
            for k in range(min(n, 4)):
                loc = struct.unpack_from('<I', mapa, 48 + 4 * k)[0]
                try:
                    b = self._setor(loc, max(1, min(-(-tam // SETOR), 64)))
                    if b[17:35] != b'*UDF Sparing Table':
                        raise ErroSA('sem o identificador')
                    crc, comp = struct.unpack_from('<HH', b, 8)         # a tag da Sparing Table tem id 0: só o checksum vale
                    if (sum(b[:4]) + sum(b[5:16])) & 0xFF != b[4] or 16 + comp > len(b) or binascii.crc_hqx(b[16:16 + comp], 0) != crc:
                        raise ErroSA('tag ou CRC inválido')
                    qtd = struct.unpack_from('<H', b, 48)[0]
                    tab = {}
                    for i in range(min(qtd, MAX_ESPARSA, (len(b) - 56) // 8)):
                        orig, novo = struct.unpack_from('<II', b, 56 + 8 * i)
                        if orig < 0xFFFFFFF0:
                            tab[orig] = novo
                    self.spar, self.spar_pacote = tab, pacote
                    return
                except (ErroSA, OSError, struct.error) as e:
                    motivo = 'cópia %d da Sparing Table ilegível (%s)' % (k + 1, e)
        self.limitacao = '%s AVISO: %s; blocos de pacotes remapeados serão lidos sem remapear.' % (self.limitacao, motivo)

    def _fisico(self, lbn, ref=None):
        """Bloco físico da partição para um bloco lógico: traduzido pela VAT (partição virtual) e depois pela Sparing Table."""
        if self.meta is not None and ref == self.meta_ref:
            lbn = self._meta_fisico(lbn)
        elif self.vat is not None:
            if not 0 <= lbn < len(self.vat) or self.vat[lbn] == 0xFFFFFFFF:
                raise ErroSA('bloco virtual UDF %d sem entrada na VAT' % lbn)
            lbn = self.vat[lbn]
        if self.spar:
            base = lbn - lbn % self.spar_pacote
            if base in self.spar:
                lbn = self.spar[base] - self.part_ini + lbn - base     # mappedLocation é absoluto; `lbn` volta relativo à partição
        return lbn

    def _particao(self, lbn, n=1, ref=None):
        """n blocos a partir do bloco `lbn` da partição (`ref`: partition reference, só importa com partição de metadados)."""
        if self.vat is not None or self.spar or self.meta is not None:
            return b''.join(self._particao_fisica(self._fisico(lbn + k, ref), 1) for k in range(n))
        return self._particao_fisica(lbn, n)

    def _particao_fisica(self, lbn, n=1):
        if lbn < 0 or (self.part_tam and lbn + n > self.part_tam):
            raise ErroSA('bloco UDF fora da partição')
        return self._setor(self.part_ini + lbn, n)

    # --- ICBs e extensões ------------------------------------------------------
    def _extents(self, ad_bytes, tipo_aloc, info, fim_volume, ref=None):
        """[(offset absoluto no leitor ou None, bytes)] de uma lista de descritores de alocação, cortada em `info`."""
        runs, falta = [], info
        tam_ad = 8 if tipo_aloc == 0 else 16
        off, vistos = 0, 0
        while off + tam_ad <= len(ad_bytes) and falta > 0 and vistos < 4096:
            comp, lbn = struct.unpack_from('<II', ad_bytes, off)
            ref_ad = struct.unpack_from('<H', ad_bytes, off + 8)[0] if tam_ad == 16 else ref      # short_ad herda a do ICB
            off += tam_ad
            vistos += 1
            kind, n = comp >> 30, comp & 0x3FFFFFFF
            if n == 0:
                break
            if kind == 3:                                  # continua noutro bloco (AED)
                b = self._particao(lbn, 1, ref_ad)
                if (_tag(b) or (0,))[0] != 258:
                    raise ErroSA('Continuação de alocação UDF inválida.')
                ln = struct.unpack_from('<I', b, 20)[0]
                ad_bytes, off = b[24:24 + ln], 0
                continue
            n = min(n, falta)
            if kind == 0 and (self.vat is not None or self.spar or self.meta is not None):   # cada bloco lógico vai para o seu físico; junta os vizinhos
                restante, k = n, 0
                while restante > 0:
                    f = self._fisico(lbn + k, ref_ad)
                    q = min(SETOR, restante)
                    if runs and len(runs[-1]) == 3 and runs[-1][2] == f - 1 and runs[-1][1] % SETOR == 0:
                        runs[-1] = (runs[-1][0], runs[-1][1] + q, f)
                    else:
                        runs.append((self.inicio + (self.part_ini + f) * SETOR, q, f))
                    restante -= q
                    k += 1
                falta -= n
                continue
            runs.append(((self.inicio + (self.part_ini + lbn) * SETOR) if kind == 0 else None, n))
            falta -= n
        return [(r[0], r[1]) for r in runs]

    def _ler_icb(self, lbn, ref=None):
        """File Entry / Extended File Entry do bloco `lbn`: {tipo, info, runs, inline, datas}."""
        b = self._particao(lbn, 1, ref)
        t = _tag(b)
        vistos = {lbn}
        while t is not None and t[0] == 259:               # Indirect Entry (ECMA-167 4/14.7): o ICB seguinte vem no long_ad em 36
            if len(vistos) > MAX_SALTOS_ICB:
                raise ErroICBIndireto('cadeia de ICB indireto com mais de %d saltos' % MAX_SALTOS_ICB)
            lbn = struct.unpack_from('<I', b, 40)[0]
            if lbn in vistos:
                raise ErroICBIndireto('cadeia de ICB indireto em laço (bloco %d)' % lbn)
            ref = struct.unpack_from('<H', b, 44)[0]
            vistos.add(lbn)
            b = self._particao(lbn, 1, ref)
            t = _tag(b)
        if t is None or t[0] not in (261, 266):
            raise ErroSA('ICB UDF inválido no bloco %d.' % lbn)
        tipo_arq, flags = b[27], struct.unpack_from('<H', b, 34)[0]
        aloc = flags & 7
        info = struct.unpack_from('<Q', b, 56)[0]
        if t[0] == 261:
            datas = (_data(b[84:96]), None, _data(b[72:84]))               # modificado, criado, acessado
            l_ea, l_ad = struct.unpack_from('<II', b, 168)
            base = 176
        else:
            datas = (_data(b[92:104]), _data(b[104:116]), _data(b[80:92]))
            l_ea, l_ad = struct.unpack_from('<II', b, 208)
            base = 216
        ads = b[base + l_ea: base + l_ea + l_ad]
        r = {'tipo': tipo_arq, 'info': info, 'runs': None, 'inline': None, 'datas': datas}
        if aloc == 3:
            r['inline'] = ads[:info]
        elif aloc in (0, 1):
            r['runs'] = self._extents(ads, aloc, info, self.inicio + self.tamanho, ref)
        else:
            raise ErroSA('Tipo de alocação UDF %d não suportado.' % aloc)
        return r

    # --- varredura -------------------------------------------------------------
    def _conteudo(self, r):
        """Bytes de uma pasta (ou arquivo pequeno) a partir do resultado de `_ler_icb`."""
        if r['inline'] is not None:
            return r['inline']
        out, falta = bytearray(), r['info']
        for off, n in r['runs']:
            q = min(n, falta)
            out += bytes(q) if off is None else self.leitor.ler(off, q)
            falta -= q
        return bytes(out)

    def varrer(self):
        self._lidos_pastas = 0
        if self._raiz is None:                              # VAT ilegível: nada a listar, o motivo está em `limitacao`
            return
        try:
            yield from self._pasta(self._raiz, '/', 0, {self._raiz}, self._raiz_ref)
        except OSError as e:
            raise ErroSA('Não consegui ler a pasta raiz do UDF: %s' % e) from None

    def _pasta(self, lbn, pai, nivel, vistos, ref=None):
        r = self._ler_icb(lbn, ref)
        if r['tipo'] != 4 or r['info'] > MAX_DIR:
            raise ErroSA('A pasta UDF no bloco %d é inválida.' % lbn)
        self._lidos_pastas += r['info']
        if self._lidos_pastas > MAX_TOTAL_PASTAS:
            raise ErroTeto('pastas com mais de %d MiB lidos no total (pastas sobrepostas?)' % (MAX_TOTAL_PASTAS >> 20))
        self._avisar(min(1.0, self._lidos_pastas / max(1, self.tamanho)))
        dados = self._conteudo(r)
        off = 0
        while off + 38 <= len(dados):
            t = _tag(dados[off: off + 16])
            if t is None or t[0] != 257:
                break
            carac, l_fi = dados[off + 18], dados[off + 19]
            icb_lbn = struct.unpack_from('<I', dados, off + 24)[0]
            icb_ref = struct.unpack_from('<H', dados, off + 28)[0]
            l_iu = struct.unpack_from('<H', dados, off + 36)[0]
            ini = off + 38 + l_iu
            nome_b = dados[ini: ini + l_fi]
            off += (38 + l_iu + l_fi + 3) & ~3
            if carac & 0x08 or not l_fi:                      # entrada da pasta-pai
                continue
            nome = limpar_nome(_nome_udf(nome_b))
            try:
                f = self._ler_icb(icb_lbn, icb_ref)
            except ErroICBIndireto as e:
                yield Entrada(nome=nome, caminho=juntar(pai, nome), eh_dir=False, tamanho=0, apagado=bool(carac & 0x04),
                              estado='ruim', ident=icb_lbn, runs=[], nota='%s; arquivo não lido' % e)
                continue
            except (ErroSA, OSError):
                continue
            if f['tipo'] not in (0, 4, 5):
                continue
            eh_dir = f['tipo'] == 4
            ent = Entrada(nome=nome, caminho=juntar(pai, nome), eh_dir=eh_dir, tamanho=0 if eh_dir else f['info'],
                          apagado=bool(carac & 0x04), modificado=f['datas'][0], criado=f['datas'][1],
                          acessado=f['datas'][2], ident=icb_lbn)
            if not eh_dir:
                if f['inline'] is not None:
                    ent.inline = f['inline']
                else:
                    ent.runs = f['runs']
                    if any(o is not None and o + n > self.inicio + self.tamanho for o, n in f['runs']):
                        ent.estado, ent.nota = 'parcial', 'a imagem termina antes do fim do arquivo'
            if ent.apagado:
                ent.estado, ent.nota = 'parcial', 'marcado como apagado no UDF; o conteúdo pode ter sido reutilizado'
            self.conferir_tamanho(ent)
            yield ent
            if eh_dir and not ent.apagado and icb_lbn not in vistos and nivel < MAX_PROFUNDIDADE:
                vistos.add(icb_lbn)
                try:
                    yield from self._pasta(icb_lbn, ent.caminho, nivel + 1, vistos, icb_ref)
                except ErroTeto:
                    raise
                except (ErroSA, OSError):
                    continue
