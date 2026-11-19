# -*- coding: utf-8 -*-
"""NTFS: lê a MFT inteira (registros em uso e livres), monta o caminho pelo registro pai e recupera pelos data runs.

Cada registro da MFT é lido com os fixups aplicados. Vale o $FILE_NAME de melhor namespace (o nome 8.3 só se for o
único) e o $DATA sem nome (os fluxos alternativos são ignorados). $DATA residente vira `inline`; o não residente vira
`runs` pelos data runs. Registro apagado (flag "em uso" desligada) cujos clusters aparecem ocupados no $Bitmap sai
`parcial`: o espaço foi reaproveitado. Arquivo comprimido (LZNT1, unidades de 16 clusters) é descomprimido na leitura:
o achado leva extents de 3 valores (ver `achados.py`). Limites: arquivo criptografado sai `ruim`, e o comprimido cuja
estrutura não fecha (unidade incompleta, buraco antes dos dados) também; registros
de extensão ($ATTRIBUTE_LIST) não são seguidos, então arquivo muito fragmentado pode sair `parcial`; sem leitura do
espelho da MFT: se o registro 0 estiver ilegível o volume não abre. Os registros do sistema ($MFT, $Bitmap...) e o
que mora em $Extend não são listados.
"""
import struct

from .base import ErroSA, Entrada, Volume, MAX_PROFUNDIDADE, limpar_nome, BLOCO

FILETIME_UNIX = 116444736000000000
RAIZ = 5
PRIMEIRO_USUARIO = 24
PERDIDOS = '/[perdidos]'
MAX_RUNS = 100000
MAX_CLUSTERS_CONFERIDOS = 1 << 20      # no máximo isto de clusters conferidos no $Bitmap por arquivo
PEDACO_MFT = 4 << 20                    # bytes da MFT lidos de cada vez
UNIDADE_LZNT1 = 16                      # clusters por unidade de compressão
MAX_EXTENTS_LZNT1 = 90000               # abaixo do teto de extents de um achado (achados.normalizar)
_ADIAR = object()


def _tempo(ft):
    if not ft:
        return None
    t = (ft - FILETIME_UNIX) / 1e7
    return t if -62135596800 < t < 253402300799 else None


def _parametros(b):
    if len(b) < 512 or b[3:11] != b'NTFS    ' or b[510:512] != b'\x55\xaa':
        raise ErroSA('Setor de boot NTFS inválido.')
    bps, spc = struct.unpack_from('<HB', b, 11)
    total, mft, espelho = struct.unpack_from('<QQQ', b, 40)
    cpr = struct.unpack_from('<b', b, 64)[0]
    if bps not in (512, 1024, 2048, 4096) or spc not in (1, 2, 4, 8, 16, 32, 64, 128):
        raise ErroSA('Parâmetros de NTFS inválidos.')
    reg = (1 << -cpr) if cpr < 0 else cpr * bps * spc
    if reg < 256 or reg > 65536 or reg & (reg - 1) or total == 0 or mft == 0:
        raise ErroSA('Parâmetros de NTFS inválidos.')
    return dict(bps=bps, spc=spc, total=total, mft=mft, tam_reg=reg)


def reconhecer(leitor, inicio=0, tamanho=None):
    try:
        _parametros(leitor.ler(inicio, 512))
        return Ntfs(leitor, inicio, tamanho)
    except (ErroSA, OSError, struct.error):
        return None


def decodificar_runs(dados, ini=0):
    """[(lcn ou None se esparso, clusters)]. Para na primeira inconsistência."""
    runs, lcn, i = [], 0, ini
    while i < len(dados) and dados[i] and len(runs) < MAX_RUNS:
        nl, no = dados[i] & 15, dados[i] >> 4
        i += 1
        if nl == 0 or nl > 8 or no > 8 or i + nl + no > len(dados):
            break
        n = int.from_bytes(dados[i:i + nl], 'little')
        i += nl
        if no:
            lcn += int.from_bytes(dados[i:i + no], 'little', signed=True)
            runs.append((lcn, n))
        else:
            runs.append((None, n))
        i += no
    return runs


def lznt1_descomprimir(dados, limite=None):
    """Descomprime uma sequência de chunks LZNT1 (cabeçalho de 2 bytes: tamanho-3 nos 12 bits baixos, 0x3000 de
    assinatura, bit 15 = comprimido). Cabeçalho 0 encerra. Para em `limite` bytes, se dado. ValueError se for inválido."""
    out, i, n = bytearray(), 0, len(dados)
    while i + 2 <= n and (limite is None or len(out) < limite):
        cab = dados[i] | dados[i + 1] << 8
        if cab == 0:
            break
        if (cab >> 12) & 7 != 3:
            raise ValueError('cabeçalho de chunk LZNT1 inválido')
        fim = i + 2 + (cab & 0xFFF) + 1
        i += 2
        if fim > n:
            raise ValueError('chunk LZNT1 passa do fim dos dados')
        if not cab & 0x8000:                                 # não comprimido: os bytes vão como estão
            out += dados[i:fim]
            i = fim
            continue
        base = len(out)
        while i < fim:
            flags, i = dados[i], i + 1
            for b in range(8):
                if i >= fim:
                    break
                if not flags >> b & 1:
                    out.append(dados[i])
                    i += 1
                    continue
                if i + 2 > fim:
                    raise ValueError('token LZNT1 truncado')
                t = dados[i] | dados[i + 1] << 8
                i += 2
                pos = len(out) - base                        # a largura de deslocamento/tamanho depende da posição no chunk
                lg, k = 0, pos - 1
                while k >= 0x10:
                    k >>= 1
                    lg += 1
                tam = (t & (0xFFF >> lg)) + 3
                desl = (t >> (12 - lg)) + 1
                if desl > pos:
                    raise ValueError('referência LZNT1 antes do início do chunk')
                ini = len(out) - desl
                if desl >= tam:
                    out += out[ini:ini + tam]
                else:                                        # sobreposição: repete byte a byte
                    for _ in range(tam):
                        out.append(out[ini])
                        ini += 1
        if len(out) - base > 4096:
            raise ValueError('chunk LZNT1 maior que 4 KiB')
        i = fim
    return bytes(out) if limite is None else bytes(out[:limite])


def _fatiar_unidades(runs):
    """Corta os data runs em unidades de 16 clusters: [[(lcn ou None, clusters)]]. Run esparso inteiro, que começa em
    unidade nova, sai de uma vez (vários de 16): não percorre um por um."""
    atual, n = [], 0
    for lcn, cl in runs:
        while cl > 0:
            if lcn is None and n == 0 and cl >= UNIDADE_LZNT1:
                q = cl - cl % UNIDADE_LZNT1
                yield [(None, q)]
                cl -= q
                continue
            t = min(cl, UNIDADE_LZNT1 - n)
            atual.append((lcn, t))
            n += t
            cl -= t
            if lcn is not None:
                lcn += t
            if n == UNIDADE_LZNT1:
                yield atual
                atual, n = [], 0
    if atual:
        yield atual


def aplicar_fixups(rec):
    """Devolve o registro com os fixups desfeitos, ou None se for inválido (rasgado ou fora do formato)."""
    if rec[:4] != b'FILE' or len(rec) < 48:
        return None
    uo, uc = struct.unpack_from('<HH', rec, 4)
    if uc < 2 or uo < 42 or uo + uc * 2 > len(rec) or (uc - 1) * 512 > len(rec):
        return None
    b = bytearray(rec)
    chk = bytes(b[uo:uo + 2])
    for i in range(1, uc):
        fim = i * 512
        if bytes(b[fim - 2:fim]) != chk:
            return None
        b[fim - 2:fim] = b[uo + 2 * i: uo + 2 * i + 2]
    return bytes(b)


def atributos(rec):
    """Gera (tipo, nome, nao_residente, flags, conteudo, data runs, tamanho real, posição do conteúdo residente no registro)."""
    i = struct.unpack_from('<H', rec, 20)[0]
    while i + 16 <= len(rec):
        tipo, tam = struct.unpack_from('<II', rec, i)
        if tipo == 0xFFFFFFFF or tam < 16 or tam % 8 or i + tam > len(rec):
            return
        nr, nlen = rec[i + 8], rec[i + 9]
        noff, flags = struct.unpack_from('<HH', rec, i + 10)
        nome = rec[i + noff: i + noff + nlen * 2].decode('utf-16-le', 'replace') if nlen else ''
        if not nr:
            clen, coff = struct.unpack_from('<IH', rec, i + 16)
            if coff + clen > tam:
                return
            yield tipo, nome, False, flags, rec[i + coff: i + coff + clen], None, clen, i + coff
        else:
            if tam < 64:
                return
            roff = struct.unpack_from('<H', rec, i + 32)[0]
            real = struct.unpack_from('<Q', rec, i + 48)[0]
            yield tipo, nome, True, flags, None, decodificar_runs(rec[i: i + tam], roff), real, None
        i += tam


class Ntfs(Volume):
    tipo = 'NTFS'

    def __init__(self, leitor, inicio=0, tamanho=None):
        super().__init__(leitor, inicio, tamanho)
        try:
            self.__dict__.update(_parametros(self._ler(0, 512)))
        except OSError as e:
            raise ErroSA('Não consegui ler o setor de boot: %s' % e) from None
        self.csize = self.bps * self.spc
        self.rotulo = ''
        self.nclusters = min(self.total // self.spc, self.tamanho // self.csize)
        self._mft_runs = None
        self.ao_ler = None
        self._bitmap = None
        self._pg = {}

    # ---- MFT -------------------------------------------------------------------------------------------------
    def _registro_bruto(self, off):
        return self._ler(off, self.tam_reg)

    def _carregar_mft(self):
        rec = aplicar_fixups(self._registro_bruto(self.mft * self.csize))
        if rec is None:
            raise ErroSA('O registro 0 da MFT está ilegível.')
        for tipo, nome, nr, _f, _c, runs, real, _p in atributos(rec):
            if tipo == 0x80 and not nome and nr:
                self._mft_runs, self._mft_tam = runs, real
                return
        raise ErroSA('A MFT não tem $DATA não residente.')

    def _blocos_mft(self):
        """Gera (número do primeiro registro, bytes, offset do pedaço no volume) pelos data runs da MFT, em pedaços de até 4 MiB."""
        falta, n = self._mft_tam, 0
        por_pedaco = max(1, PEDACO_MFT // self.tam_reg) * self.tam_reg
        for lcn, cl in self._mft_runs:
            tam = cl * self.csize
            off = 0
            while off < tam and falta > 0:
                q = min(por_pedaco, tam - off, falta)
                q -= q % self.tam_reg
                if q <= 0 or lcn is None or (lcn * self.csize + off + q) > self.tamanho:
                    return
                try:
                    dados = self._ler(lcn * self.csize + off, q)
                except OSError:
                    dados = self._ler_registro_a_registro(lcn * self.csize + off, q)
                yield n, dados, lcn * self.csize + off
                n += q // self.tam_reg
                off += q
                falta -= q

    def _ler_registro_a_registro(self, off, q):
        """Setor ruim no meio do pedaço: relê registro por registro. O que não lê vira zeros (registro sem 'FILE' é
        ignorado) e entra na conta de `ilegiveis`; a lista segue sem esses registros."""
        partes = []
        for i in range(q // self.tam_reg):
            try:
                partes.append(self._ler(off + i * self.tam_reg, self.tam_reg))
            except OSError:
                partes.append(b'\0' * self.tam_reg)
                self.ilegiveis += max(1, self.tam_reg // max(1, getattr(self.leitor, 'setor', 512)))
        return b''.join(partes)

    def _bit_ocupado(self, c):
        """True se o $Bitmap marca o cluster como ocupado; None se não deu para ler o $Bitmap."""
        if self._bitmap is None:
            return None
        pos = c // 8
        for ini, fim, runs_off in self._bitmap:
            if ini <= pos < fim:
                pg = (runs_off + pos - ini) // 4096
                try:
                    d = self._pg.get(pg)
                    if d is None:
                        if len(self._pg) > 256:
                            self._pg.clear()
                        d = self._pg[pg] = self._ler(pg * 4096, min(4096, self.tamanho - pg * 4096))
                    return bool(d[runs_off + pos - ini - pg * 4096] >> (c % 8) & 1)
                except (IndexError, ErroSA):                 # $Bitmap aponta para fora do volume: não sabemos
                    return None
        return None

    # ---- varredura ------------------------------------------------------------------------------------------
    def _ler_registro(self, n, dados):
        return aplicar_fixups(dados[n * self.tam_reg:(n + 1) * self.tam_reg]) if dados else None

    def _extrair(self, n, rec):
        flags = struct.unpack_from('<H', rec, 22)[0]
        base = struct.unpack_from('<Q', rec, 32)[0] & 0xFFFFFFFFFFFF
        info = dict(n=n, em_uso=bool(flags & 1), dir=bool(flags & 2), base=base, nome=None, ns=9, pai=None,
                    criado=None, modif=None, acess=None, tam=0, runs=None, inline=None, achou_dados=False,
                    comprimido=False, cifrado=False, lista=False, rotulo=None)
        for tipo, nome, nr, af, cont, runs, real, pos in atributos(rec):
            if tipo == 0x10 and not nr and len(cont) >= 32:
                info['criado'], info['modif'], _, info['acess'] = (_tempo(x) for x in struct.unpack_from('<QQQQ', cont, 0))
            elif tipo == 0x30 and not nr and len(cont) >= 66:
                nl, ns = cont[64], cont[65]
                if 66 + nl * 2 > len(cont):
                    continue
                if info['nome'] is None or (info['ns'] == 2 and ns != 2):
                    info['nome'] = cont[66:66 + nl * 2].decode('utf-16-le', 'replace')
                    info['ns'] = ns
                    info['pai'] = struct.unpack_from('<Q', cont, 0)[0] & 0xFFFFFFFFFFFF
                    if struct.unpack_from('<I', cont, 56)[0] & 0x10000000:
                        info['dir'] = True
            elif tipo == 0x20:
                info['lista'] = True
            elif tipo == 0x60 and not nr:
                info['rotulo'] = cont.decode('utf-16-le', 'replace')
            elif tipo == 0x80 and not nome and not info['achou_dados']:
                info['achou_dados'] = True
                info['tam'] = real
                info['comprimido'] = bool(af & 0x0001)
                info['cifrado'] = bool(af & 0x4000)
                if nr:
                    info['runs'] = runs
                else:
                    info['inline'] = cont
                    info['inline_pos'] = pos
        return info

    def _resolver(self, pai, n_lidos, nomes, memo, final):
        """Caminho da pasta `pai`: ('ok', caminho) ou ('pular', None) se passa por registro de sistema ($Extend...) ou
        ('adiar', None) se algum ancestral ainda não foi lido (só antes do fim da MFT). Órfão cai em PERDIDOS."""
        cadeia, atual, base = [], pai, None
        for _ in range(MAX_PROFUNDIDADE):
            if atual in memo:
                base = memo[atual]
                break
            if atual == RAIZ:
                base = '/'
                break
            i = nomes.get(atual)
            if i is None:
                if atual >= n_lidos and not final:
                    return 'adiar', None
                base = PERDIDOS
                break
            if atual < PRIMEIRO_USUARIO:
                return 'pular', None
            cadeia.append(atual)
            atual = i['pai']
        else:
            base = PERDIDOS                                  # laço ou profundidade absurda
        for k in reversed(cadeia):
            base = base.rstrip('/') + '/' + limpar_nome(nomes[k]['nome'])
            memo[k] = base
        return 'ok', base

    def _montar(self, i, n_lidos, nomes, memo, final):
        """Entrada do registro `i`, ou ADIAR (falta ler um ancestral), ou None (não se lista)."""
        estado, pai = self._resolver(i['pai'], n_lidos, nomes, memo, final)
        if estado == 'adiar':
            return _ADIAR
        if estado == 'pular':
            return None
        nome = limpar_nome(i['nome'])
        ancestral_apagado = False
        if pai != '/' and pai != PERDIDOS:
            p = nomes.get(i['pai'])
            ancestral_apagado = bool(p) and not p['em_uso']
        e = Entrada(nome=nome, caminho=pai.rstrip('/') + '/' + nome, eh_dir=i['dir'],
                    tamanho=0 if i['dir'] else i['tam'], apagado=not i['em_uso'] or ancestral_apagado,
                    criado=i['criado'], modificado=i['modif'], acessado=i['acess'], ident=i['n'])
        if not i['dir']:
            self._dados(e, i)
            self.conferir_tamanho(e)
        return e

    def varrer(self):
        """Gera as entradas à medida que lê a MFT: a que já tem o caminho inteiro sai logo; a que depende de uma pasta
        de número maior espera essa pasta ser lida (ou o fim da MFT, quando vira órfã). `ao_ler(fração)`, se existir,
        é chamado depois de cada pedaço lido (serve para pausar, cancelar e mostrar progresso; pode levantar exceção)."""
        try:
            self._carregar_mft()
        except OSError as e:
            raise ErroSA('Não consegui ler a MFT: %s' % e) from None
        nomes, pendentes, memo, lidos = {}, [], {}, 0
        total = max(1, self._mft_tam // self.tam_reg)
        for ini, bloco, off_bloco in self._blocos_mft():
            for k in range(len(bloco) // self.tam_reg):
                lidos = ini + k + 1
                rec = aplicar_fixups(bloco[k * self.tam_reg:(k + 1) * self.tam_reg])
                if rec is None:
                    continue
                n = ini + k
                try:
                    info = self._extrair(n, rec)
                except (struct.error, IndexError):
                    continue
                info['rec_off'] = off_bloco + k * self.tam_reg
                info['usa'] = struct.unpack_from('<HH', rec, 4)
                if n == 3 and info['rotulo'] is not None:
                    self.rotulo = info['rotulo']
                if n == 6:
                    self._guardar_bitmap(info)
                if info['base'] != 0 or (info['nome'] is None and n != RAIZ):
                    continue
                nomes[n] = {'nome': info['nome'], 'pai': info['pai'], 'em_uso': info['em_uso']}
                if n < PRIMEIRO_USUARIO or info['nome'] is None:
                    continue
                e = self._montar(info, lidos, nomes, memo, False)
                if e is _ADIAR:
                    pendentes.append(info)
                elif e is not None:
                    yield e
            lidos = ini + len(bloco) // self.tam_reg
            if pendentes:
                restantes = []
                for info in pendentes:
                    e = self._montar(info, lidos, nomes, memo, False)
                    if e is _ADIAR:
                        restantes.append(info)
                    elif e is not None:
                        yield e
                pendentes = restantes
            if self.ao_ler:
                self.ao_ler(min(1.0, lidos / total))
        for info in pendentes:
            e = self._montar(info, lidos, nomes, memo, True)
            if e is not None and e is not _ADIAR:
                yield e

    def _guardar_bitmap(self, info):
        if info['runs']:
            faixas, pos = [], 0
            for lcn, cl in info['runs']:
                if lcn is not None:
                    faixas.append((pos, pos + cl * self.csize, lcn * self.csize))
                pos += cl * self.csize
            self._bitmap = faixas

    def _dados(self, e, i):
        if i['cifrado']:
            e.estado, e.nota = 'ruim', 'criptografado (EFS): não suportado'
            return
        if i['inline'] is not None:
            e.inline = i['inline']
            e.tamanho = len(i['inline'])
            e.runs = self._runs_residente(i)
            return
        if not i['achou_dados']:
            if i['lista']:
                e.estado, e.nota = 'parcial', 'dados em registro de extensão ($ATTRIBUTE_LIST), não seguido'
            return
        if i['comprimido']:
            achou = self._unidades_lznt1(e, i)
            if achou is None:
                return
            runs, validas, falta = achou
        else:
            runs, falta, validas = [], i['tam'], []
            for lcn, cl in i['runs'] or []:
                if falta <= 0:
                    break
                n = min(cl * self.csize, falta)
                if lcn is not None and (lcn < 0 or (lcn + cl) > self.nclusters):
                    e.estado, e.nota = 'ruim', 'data run fora do volume'
                    e.runs = []
                    return
                runs.append((None if lcn is None else self.inicio + lcn * self.csize, n))
                validas.append((lcn, -(-n // self.csize)))       # só os clusters que de fato pertencem ao arquivo
                falta -= n
        e.runs = runs
        if falta > 0:
            e.estado, e.nota = 'parcial', 'data runs não cobrem o tamanho'
            return
        if e.apagado and e.tamanho:
            usados, restante = False, MAX_CLUSTERS_CONFERIDOS
            try:
                for lcn, cl in validas:
                    if lcn is None:
                        continue
                    cl = min(cl, restante)
                    if any(self._bit_ocupado(c) for c in range(lcn, lcn + cl)):
                        usados = True
                        break
                    restante -= cl
                    if restante <= 0:
                        break
            except OSError:
                usados = False
            if usados:
                e.estado, e.nota = 'parcial', 'clusters já reutilizados (conforme o $Bitmap)'
            elif self._bitmap is None:
                e.nota = 'sem $Bitmap para conferir reuso'

    def _unidades_lznt1(self, e, i):
        """Arquivo comprimido: monta `e.extents_lznt1` e devolve (runs lógicos, clusters válidos, bytes que faltam), ou None
        depois de marcar a entrada `ruim`. Cada unidade de 16 clusters é: toda esparsa (zeros), toda alocada (gravada sem
        compressão) ou com clusters alocados no começo e esparsos no fim (comprimida). Extents: [off, n] comum ou
        esparso ([None, n]); [off, n, 0] é um pedaço de unidade comprimida e [off, n, u] o último dela, com u bytes
        depois de descomprimida."""
        def ruim(nota):
            e.estado, e.nota, e.runs = 'ruim', nota, []
            return None
        if i['tam'] > 4 * self.tamanho:
            return ruim('tamanho incoerente com o volume (estrutura danificada?)')
        csize, falta = self.csize, i['tam']
        ext, validas = [], []
        for pedacos in _fatiar_unidades(i['runs'] or []):
            if falta <= 0:
                break
            total = sum(cl for _, cl in pedacos)
            reais = [(l, cl) for l, cl in pedacos if l is not None]
            if any(l < 0 or l + cl > self.nclusters for l, cl in reais):
                return ruim('data run fora do volume')
            u = min(total * csize, falta)
            if not reais:                                    # unidade esparsa
                if ext and ext[-1][0] is None and len(ext[-1]) == 2:
                    ext[-1][1] += u
                else:
                    ext.append([None, u])
            elif total != UNIDADE_LZNT1:
                return ruim('comprimido (LZNT1): unidade de compressão incompleta')
            elif len(reais) == len(pedacos):                 # tudo alocado: gravada sem compressão
                resta = u
                for l, cl in reais:
                    q = min(cl * csize, resta)
                    off = self.inicio + l * csize
                    if ext and len(ext[-1]) == 2 and ext[-1][0] is not None and ext[-1][0] + ext[-1][1] == off:
                        ext[-1][1] += q
                    else:
                        ext.append([off, q])
                    validas.append((l, -(-q // csize)))
                    resta -= q
            else:                                            # comprimida: dados primeiro, esparso depois
                if pedacos[:len(reais)] != reais:
                    return ruim('comprimido (LZNT1): buraco antes dos dados da unidade')
                for k, (l, cl) in enumerate(reais):
                    ext.append([self.inicio + l * csize, cl * csize, u if k == len(reais) - 1 else 0])
                    validas.append((l, cl))
            if len(ext) > MAX_EXTENTS_LZNT1:
                return ruim('comprimido (LZNT1): fragmentado demais')
            falta -= u
        e.extents_lznt1 = ext
        cobertos = i['tam'] - max(falta, 0)
        return ([(None, cobertos)] if cobertos else []), validas, falta

    def ler(self, entrada, bloco=BLOCO):
        """Como `Volume.ler`, mas descomprime o arquivo LZNT1. Unidade ilegível ou inválida: OSError."""
        ext = getattr(entrada, 'extents_lznt1', None)
        if ext is None:
            yield from super().ler(entrada, bloco)
            return
        falta, pend = entrada.tamanho, []
        for e in ext:
            if len(e) == 3:
                pend.append((e[0], e[1]))
                if e[2] == 0:
                    continue
                try:
                    bruto = b''.join(self.leitor.ler(o, n) for o, n in pend)
                    dados = lznt1_descomprimir(bruto, e[2]).ljust(e[2], b'\0')
                except ValueError as erro:
                    raise OSError('LZNT1 inválido: %s' % erro) from None
                pend = []
                yield dados[:falta]
                falta -= e[2]
                continue
            off, n = e
            while n > 0 and falta > 0:
                q = min(n, falta, bloco)
                yield (b'\0' * q) if off is None else self.leitor.ler(off, q)
                if off is not None:
                    off += q
                n -= q
                falta -= q

    def _runs_residente(self, i):
        """Onde estão, na origem, os bytes de um $DATA residente. O NTFS troca os 2 últimos bytes de cada setor de 512
        do registro pelo número de sequência (fixup); os bytes verdadeiros ficam no vetor de fixups. Então o
        conteúdo que cruza esse ponto vira 3 faixas: antes, os 2 bytes do vetor, depois."""
        ini, n = i['inline_pos'], len(i['inline'])
        base = self.inicio + i['rec_off']
        uo, _uc = i['usa']
        faixas, pos, falta = [], ini, n
        while falta > 0:
            fim_setor = (pos // 512 + 1) * 512
            if pos >= fim_setor - 2:                         # dentro dos 2 bytes trocados deste setor
                s = fim_setor // 512
                q = min(fim_setor - pos, falta)
                faixas.append((base + uo + 2 * s + (pos - (fim_setor - 2)), q))
            else:
                q = min(fim_setor - 2 - pos, falta)
                faixas.append((base + pos, q))
            pos += q
            falta -= q
        fund = []
        for o, q in faixas:                                  # junta faixas vizinhas
            if fund and fund[-1][0] + fund[-1][1] == o:
                fund[-1] = (fund[-1][0], fund[-1][1] + q)
            else:
                fund.append((o, q))
        return fund
