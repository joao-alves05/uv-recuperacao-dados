# -*- coding: utf-8 -*-
"""exFAT: conjuntos de entradas 0x85 (arquivo) + 0xC0 (fluxo) + 0xC1 (nome UTF-16), em uso ou apagados.

Apagar no exFAT só limpa o bit "em uso" do tipo de cada entrada (0x85 vira 0x05, 0xC0 vira 0x40, 0xC1 vira 0x41);
o nome, o tamanho e o primeiro cluster continuam lá. Arquivo com `NoFatChain` (ou apagado) é lido como clusters
contíguos. No apagado conferimos o bitmap de alocação: se algum cluster do trecho está ocupado de novo, o arquivo
sai `parcial`. Apagado que estava fragmentado (cadeia na FAT) só volta inteiro se os pedaços forem vizinhos.
"""
import struct

from .base import ErroSA, Entrada, Volume, MAX_PROFUNDIDADE, epoch, juntar, limpar_nome

MAX_PASTA = 64 << 20


def _parametros(b):
    if len(b) < 512 or b[3:11] != b'EXFAT   ' or b[510:512] != b'\x55\xaa' or any(b[11:64]):
        raise ErroSA('Setor de boot exFAT inválido.')
    vol_len, fat_off, fat_len, heap_off, nclust, raiz = struct.unpack_from('<QIIIII', b, 72)
    bshift, sshift, nfats = b[108], b[109], b[110]
    if not 9 <= bshift <= 12 or sshift > 25 - bshift or nfats not in (1, 2) or nclust == 0 or fat_len == 0:
        raise ErroSA('Parâmetros de exFAT inválidos.')
    if raiz < 2 or raiz >= nclust + 2:
        raise ErroSA('Parâmetros de exFAT inválidos.')
    return dict(bps=1 << bshift, spc=1 << sshift, fat_off=fat_off, fat_len=fat_len, heap_off=heap_off,
                nclusters=nclust, raiz=raiz, vol_len=vol_len)


def reconhecer(leitor, inicio=0, tamanho=None):
    try:
        _parametros(leitor.ler(inicio, 512))
        return ExFat(leitor, inicio, tamanho)
    except (ErroSA, OSError, struct.error):
        return None


def _ts(v, dec=0):
    if not v:
        return None
    return epoch(1980 + (v >> 25), (v >> 21) & 15, (v >> 16) & 31, (v >> 11) & 31, (v >> 5) & 63, (v & 31) * 2,
                 min(dec, 199) / 100.0)


class ExFat(Volume):
    tipo = 'exFAT'

    def __init__(self, leitor, inicio=0, tamanho=None):
        super().__init__(leitor, inicio, tamanho)
        try:
            self.__dict__.update(_parametros(self._ler(0, 512)))
        except OSError as e:
            raise ErroSA('Não consegui ler o setor de boot: %s' % e) from None
        self.csize = self.bps * self.spc
        disponivel = (self.tamanho - self.heap_off * self.bps) // self.csize
        self.nclusters = max(0, min(self.nclusters, disponivel))
        self.rotulo = ''
        self.bitmap = None                                  # (offset, bytes) do bitmap de alocação, se achado
        self._pg = {}

    def _off(self, c):
        return self.heap_off * self.bps + (c - 2) * self.csize

    def _valido(self, c):
        return 2 <= c < self.nclusters + 2

    def _fat(self, c):
        pos = self.fat_off * self.bps + c * 4
        pg = pos // 4096
        d = self._pg.get(pg)
        if d is None:
            if len(self._pg) > 256:
                self._pg.clear()
            d = self._pg[pg] = self._ler(pg * 4096, min(4096, self.tamanho - pg * 4096))
        return struct.unpack_from('<I', d, pos - pg * 4096)[0]

    def _cadeia(self, primeiro, contiguo, nbytes):
        """[(offset no volume, bytes)] e se está completo. `nbytes` None = até o fim da cadeia (raiz)."""
        runs, falta, ok = [], nbytes, True
        if contiguo:
            n = -(-nbytes // self.csize)
            if not self._valido(primeiro) or not self._valido(primeiro + max(n, 1) - 1):
                return [], False
            return [(self._off(primeiro), nbytes)] if nbytes else [], True
        c, vistos = primeiro, 0
        while True:
            if not self._valido(c) or vistos > self.nclusters:
                ok = False
                break
            vistos += 1
            o = self._off(c)
            if runs and runs[-1][0] + runs[-1][1] == o:
                runs[-1][1] += self.csize
            else:
                runs.append([o, self.csize])
            prox = self._fat(c)
            if prox >= 0xFFFFFFF8:
                break
            if prox == 0xFFFFFFF7 or prox == 0:
                ok = False
                break
            c = prox
        if nbytes is not None:
            cortado, falta = [], nbytes
            for o, n in runs:
                if falta <= 0:
                    break
                cortado.append((o, min(n, falta)))
                falta -= min(n, falta)
            return cortado, ok and falta <= 0
        return [(o, n) for o, n in runs], ok

    def _ocupado(self, c):
        """True se o bitmap diz que o cluster está em uso; None se não temos bitmap."""
        if self.bitmap is None:
            return None
        off, tam = self.bitmap
        i = c - 2
        if i // 8 >= tam:
            return None
        pg = (off + i // 8) // 4096
        d = self._pg.get(('b', pg))
        if d is None:
            if len(self._pg) > 256:
                self._pg.clear()
            d = self._pg[('b', pg)] = self._ler(pg * 4096, min(4096, self.tamanho - pg * 4096))
        return bool(d[off + i // 8 - pg * 4096] >> (i % 8) & 1)

    def _ler_pasta(self, primeiro, contiguo, nbytes):
        runs, _ = self._cadeia(primeiro, contiguo, nbytes if contiguo else None)
        partes, total = [], 0
        for o, n in runs:
            n = min(n, MAX_PASTA - total)
            if n <= 0:
                break
            partes.append(self._ler(o, n))
            total += n
        d = b''.join(partes)
        return d[:nbytes] if (nbytes and not contiguo and nbytes < len(d)) else d

    def _varrer_pasta(self, dados, pai, pai_apagado, nivel, vistos):
        i, n = 0, len(dados)
        while i + 32 <= n:
            t = dados[i]
            if t == 0:
                return
            if t not in (0x85, 0x05):
                i += 32
                continue
            sec = dados[i + 1]
            conj = dados[i: i + 32 * (1 + sec)]
            prox = i + 32 * (1 + sec)
            if sec < 2 or len(conj) < 32 * (1 + sec) or (conj[32] & 0x7F) != 0x40:
                i += 32                                      # conjunto quebrado: segue de entrada em entrada
                continue
            apagado = t == 0x05
            fluxo = conj[32:64]
            nl = fluxo[3]
            nomes = [conj[k: k + 32] for k in range(64, len(conj), 32) if (conj[k] & 0x7F) == 0x41]
            if nl == 0 or len(nomes) < -(-nl // 15):
                i += 32
                continue
            i = prox
            nome = b''.join(x[2:32] for x in nomes)[:nl * 2].decode('utf-16-le', 'replace')
            nome = limpar_nome(nome)
            attr = struct.unpack_from('<H', conj, 4)[0]
            eh_dir = bool(attr & 0x10)
            flags = fluxo[1]
            tam = struct.unpack_from('<Q', fluxo, 24)[0]
            cl = struct.unpack_from('<I', fluxo, 20)[0]
            apag = apagado or pai_apagado
            ent = Entrada(nome=nome, caminho=juntar(pai, nome), eh_dir=eh_dir, tamanho=0 if eh_dir else tam,
                          apagado=apag,
                          criado=_ts(struct.unpack_from('<I', conj, 8)[0], conj[20]),
                          modificado=_ts(struct.unpack_from('<I', conj, 12)[0], conj[21]),
                          acessado=_ts(struct.unpack_from('<I', conj, 16)[0]), ident=cl)
            contiguo = bool(flags & 2) or apag
            self._preencher(ent, cl, tam, contiguo, apag, eh_dir)
            yield ent
            if eh_dir and ent.estado != 'ruim' and cl not in vistos and nivel < MAX_PROFUNDIDADE and tam:
                vistos.add(cl)
                try:
                    sub = self._ler_pasta(cl, contiguo, tam)
                except (OSError, ErroSA):
                    continue
                yield from self._varrer_pasta(sub, ent.caminho, apag, nivel + 1, vistos)

    def _preencher(self, ent, cl, tam, contiguo, apag, eh_dir):
        if tam == 0:
            return
        if not self._valido(cl):
            ent.estado, ent.nota = 'ruim', 'primeiro cluster inválido'
            return
        try:
            if eh_dir:
                return                                       # pasta: o conteúdo é lido por _ler_pasta
            runs, ok = self._cadeia(cl, contiguo, tam)
            ent.runs = [(self.inicio + o, n) for o, n in runs]
            if not runs:
                ent.estado, ent.nota = 'ruim', 'não cabe no volume'
            elif not ok:
                ent.estado, ent.nota = 'parcial', 'cadeia de clusters interrompida'
            elif apag:
                ent.nota = 'recuperado assumindo clusters contíguos'
                n = -(-tam // self.csize)
                usados = [self._ocupado(c) for c in range(cl, cl + n)]
                if any(usados):
                    ent.estado, ent.nota = 'parcial', 'clusters já reutilizados por outro arquivo'
                elif self.bitmap is None:
                    ent.nota += ' (sem bitmap de alocação para conferir)'
        except OSError:
            ent.estado, ent.nota = 'ruim', 'FAT ilegível'

    def varrer(self):
        try:
            dados = self._ler_pasta(self.raiz, False, None)
        except (OSError, ErroSA) as e:
            raise ErroSA('Não consegui ler a pasta raiz: %s' % e) from None
        for i in range(0, len(dados) - 31, 32):             # rótulo e bitmap moram na raiz; achar antes dos arquivos
            if dados[i] == 0:
                break
            if dados[i] == 0x83:
                self.rotulo = dados[i + 2: i + 2 + min(dados[i + 1], 11) * 2].decode('utf-16-le', 'replace')
            elif dados[i] == 0x81:
                c, tam = struct.unpack_from('<IQ', dados, i + 20)
                if self._valido(c):
                    self.bitmap = (self._off(c), tam)
        yield from self._varrer_pasta(dados, '/', False, 0, set())
