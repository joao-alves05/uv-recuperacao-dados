# -*- coding: utf-8 -*-
"""FAT12, FAT16 e FAT32: diretórios, nomes longos (LFN), entradas apagadas (0xE5) e recuperação.

Arquivo em uso: segue a cadeia da FAT. Arquivo apagado: a FAT zera a cadeia, então assumimos clusters contíguos a
partir do primeiro (o padrão das ferramentas de recuperação); se algum desses clusters já aparece ocupado na FAT, o
arquivo sai `parcial` (o espaço foi reaproveitado e os bytes podem ser de outro arquivo). Arquivo apagado que estava
fragmentado só volta inteiro se por sorte os pedaços forem vizinhos. Pasta apagada: lemos o primeiro cluster e os
seguintes que estejam livres, até 16; o que estava dentro dela também sai como apagado.
"""
import struct

from .base import ErroSA, Entrada, Volume, MAX_PROFUNDIDADE, data_dos, juntar, limpar_nome

ATR_DIR, ATR_ROTULO, ATR_LFN = 0x10, 0x08, 0x0F
PAGINA = 4096
MAX_PAGINAS = 256
MAX_CLUSTERS_PASTA_APAGADA = 16
MAX_PASTA = 64 << 20


def _potencia2(n):
    return n > 0 and n & (n - 1) == 0


def _checksum_curto(raw11):
    s = 0
    for b in raw11:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def _parametros(boot):
    """Valida o setor de boot e devolve os parâmetros; ErroSA se não for FAT."""
    if len(boot) < 512 or boot[510:512] != b'\x55\xaa':
        raise ErroSA('Setor de boot FAT sem a assinatura 55AA.')
    bps, spc, reservados, nfats, raiz_ent, total16, fatsz16 = struct.unpack_from('<HBHBHHxH', boot, 11)
    total32 = struct.unpack_from('<I', boot, 32)[0]
    fatsz32 = struct.unpack_from('<I', boot, 36)[0]
    if bps not in (512, 1024, 2048, 4096) or not _potencia2(spc) or spc > 128 or reservados < 1 or not 1 <= nfats <= 4:
        raise ErroSA('Parâmetros de FAT inválidos.')
    total = total16 or total32
    fatsz = fatsz16 or fatsz32
    if total == 0 or fatsz == 0:
        raise ErroSA('Parâmetros de FAT inválidos.')
    eh32 = fatsz16 == 0
    if eh32 != (raiz_ent == 0):
        raise ErroSA('Parâmetros de FAT inválidos.')
    raiz_setores = (raiz_ent * 32 + bps - 1) // bps
    dados_ini = reservados + nfats * fatsz + raiz_setores
    if dados_ini >= total:
        raise ErroSA('Parâmetros de FAT inválidos.')
    nclusters = (total - dados_ini) // spc
    if nclusters == 0:
        raise ErroSA('Parâmetros de FAT inválidos.')
    tipo = 'FAT12' if nclusters < 4085 else 'FAT16' if nclusters < 65525 else 'FAT32'
    if (tipo == 'FAT32') != eh32:
        raise ErroSA('Parâmetros de FAT inválidos.')
    raiz_cluster = struct.unpack_from('<I', boot, 44)[0] if eh32 else 0
    return dict(bps=bps, spc=spc, reservados=reservados, nfats=nfats, raiz_ent=raiz_ent, total=total, fatsz=fatsz,
                raiz_setores=raiz_setores, dados_ini=dados_ini, nclusters=nclusters, tipo=tipo, raiz_cluster=raiz_cluster)


def reconhecer(leitor, inicio=0, tamanho=None):
    try:
        boot = leitor.ler(inicio, 512)
        if boot[3:11] == b'EXFAT   ' or boot[3:7] == b'NTFS':
            return None
        _parametros(boot)
        return Fat(leitor, inicio, tamanho)
    except (ErroSA, OSError, struct.error):
        return None


class Fat(Volume):
    def __init__(self, leitor, inicio=0, tamanho=None):
        super().__init__(leitor, inicio, tamanho)
        try:
            p = _parametros(self._ler(0, 512))
        except OSError as e:
            raise ErroSA('Não consegui ler o setor de boot: %s' % e) from None
        self.__dict__.update(p)
        self.csize = self.bps * self.spc
        # imagem cortada: não prometer clusters que não cabem
        disponivel = self.tamanho // self.bps - self.dados_ini
        self.nclusters = max(0, min(self.nclusters, disponivel // self.spc))
        self.rotulo = ''
        self._paginas = {}
        self.eoc = {'FAT12': 0xFF8, 'FAT16': 0xFFF8, 'FAT32': 0x0FFFFFF8}[self.tipo]
        self.ruim = {'FAT12': 0xFF7, 'FAT16': 0xFFF7, 'FAT32': 0x0FFFFFF7}[self.tipo]

    # ---- FAT -------------------------------------------------------------------------------------------------
    def _fat_bytes(self, off, n):
        base = self.reservados * self.bps
        fim_fat = self.fatsz * self.bps
        out = bytearray()
        while n > 0:
            if off >= fim_fat:
                out += b'\0' * n
                break
            pg = off // PAGINA
            dados = self._paginas.get(pg)
            if dados is None:
                if len(self._paginas) >= MAX_PAGINAS:
                    self._paginas.clear()
                q = min(PAGINA, fim_fat - pg * PAGINA, max(0, self.tamanho - base - pg * PAGINA))
                dados = self._ler(base + pg * PAGINA, q) if q > 0 else b''
                self._paginas[pg] = dados
            ini = off - pg * PAGINA
            parte = dados[ini: ini + n]
            if not parte:
                out += b'\0' * n
                break
            out += parte
            off += len(parte)
            n -= len(parte)
        return bytes(out)

    def fat(self, c):
        """Valor da FAT para o cluster `c` (inteiro)."""
        if self.tipo == 'FAT32':
            return struct.unpack('<I', self._fat_bytes(c * 4, 4))[0] & 0x0FFFFFFF
        if self.tipo == 'FAT16':
            return struct.unpack('<H', self._fat_bytes(c * 2, 2))[0]
        v = struct.unpack('<H', self._fat_bytes(c + c // 2, 2))[0]
        return v >> 4 if c & 1 else v & 0xFFF

    def _cluster_off(self, c):
        return (self.dados_ini + (c - 2) * self.spc) * self.bps

    def _cadeia(self, primeiro):
        """Segue a FAT. Devolve ([[cluster inicial, quantidade]], terminou_bem)."""
        runs, c, n = [], primeiro, 0
        while True:
            if c < 2 or c >= self.nclusters + 2:
                return runs, False
            if runs and runs[-1][0] + runs[-1][1] == c:
                runs[-1][1] += 1
            else:
                runs.append([c, 1])
            n += 1
            if n > self.nclusters:
                return runs, False                          # laço na FAT
            prox = self.fat(c)
            if prox >= self.eoc:
                return runs, True
            if prox == 0 or prox == self.ruim:
                return runs, False
            c = prox

    def _faixas(self, runs_cluster, tamanho):
        faixas, falta = [], tamanho
        for c, q in runs_cluster:
            if falta <= 0:
                break
            n = min(q * self.csize, falta)
            faixas.append((self.inicio + self._cluster_off(c), n))
            falta -= n
        return faixas, falta

    # ---- diretórios ------------------------------------------------------------------------------------------
    def _dados_pasta(self, primeiro, apagada):
        self._avisar()
        if primeiro == 0 and self.tipo != 'FAT32':           # raiz fixa de FAT12/16
            return self._ler((self.reservados + self.nfats * self.fatsz) * self.bps, self.raiz_setores * self.bps)
        if not apagada:
            runs, _ = self._cadeia(primeiro)
            partes, total = [], 0
            for c, q in runs:
                n = min(q * self.csize, MAX_PASTA - total)
                if n <= 0:
                    break
                partes.append(self._ler(self._cluster_off(c), n))
                total += n
            return b''.join(partes)
        partes, c = [], primeiro
        while 2 <= c < self.nclusters + 2 and len(partes) < MAX_CLUSTERS_PASTA_APAGADA:
            if partes and (self.fat(c) != 0 or 0 in partes[-1][::32]):
                break
            partes.append(self._ler(self._cluster_off(c), self.csize))
            c += 1
        return b''.join(partes)

    def _nome_curto(self, raw, apagado):
        base = bytearray(raw[0:8].rstrip(b' '))
        if base and raw[0] == 0x05:
            base[0] = 0xE5
        if apagado and base:
            base[0] = ord('_')
        nome = bytes(base).decode('cp437', 'replace')
        ext = raw[8:11].rstrip(b' ').decode('cp437', 'replace')
        if raw[12] & 0x08:
            nome = nome.lower()
        if raw[12] & 0x10:
            ext = ext.lower()
        return nome + ('.' + ext if ext else '')

    def _nome_longo(self, lfn, raw11, apagado):
        """`lfn` = entradas LFN na ordem física. Devolve o nome ou None se não bater com o checksum."""
        if not lfn:
            return None
        chk = lfn[0][13]
        if apagado:                                         # o 0xE5 sobrescreveu a 1ª letra do nome curto
            if not any(_checksum_curto(bytes([b]) + raw11[1:]) == chk for b in range(256)):
                return None
        elif _checksum_curto(raw11) != chk:
            return None
        letras = b''
        for e in reversed(lfn):
            letras += e[1:11] + e[14:26] + e[28:32]
        fim = len(letras)
        for i in range(0, len(letras), 2):
            if letras[i:i + 2] in (b'\0\0', b'\xff\xff'):
                fim = i
                break
        return letras[:fim].decode('utf-16-le', 'replace') or None

    def _entradas(self, dados, pai, pai_apagado, nivel, vistos):
        lfn = []
        for i in range(0, len(dados) - 31, 32):
            e = dados[i:i + 32]
            if e[0] == 0:
                return
            apagado = e[0] == 0xE5
            attr = e[11]
            if attr & 0x3F == ATR_LFN:
                if lfn and lfn[-1][13] != e[13]:
                    lfn = []
                lfn.append(e)
                continue
            anteriores, lfn = lfn, []
            if attr & ATR_ROTULO:
                if not apagado and not pai_apagado and pai == '/':
                    self.rotulo = e[0:11].decode('cp437', 'replace').strip()
                continue
            if e[0:11] in (b'.' + b' ' * 10, b'..' + b' ' * 9):
                continue
            eh_dir = bool(attr & ATR_DIR)
            nome = limpar_nome(self._nome_longo(anteriores, e[0:11], apagado) or self._nome_curto(e, apagado))
            cl = struct.unpack_from('<H', e, 26)[0]
            if self.tipo == 'FAT32':
                cl |= struct.unpack_from('<H', e, 20)[0] << 16
            tam = 0 if eh_dir else struct.unpack_from('<I', e, 28)[0]
            apag = apagado or pai_apagado
            ent = Entrada(nome=nome, caminho=juntar(pai, nome), eh_dir=eh_dir, tamanho=tam, apagado=apag,
                          criado=data_dos(struct.unpack_from('<H', e, 16)[0], struct.unpack_from('<H', e, 14)[0], e[13]),
                          modificado=data_dos(struct.unpack_from('<H', e, 24)[0], struct.unpack_from('<H', e, 22)[0]),
                          acessado=data_dos(struct.unpack_from('<H', e, 18)[0]), ident=cl)
            self._preencher(ent, cl, tam, apag)
            yield ent
            if eh_dir and ent.estado != 'ruim' and cl not in vistos and nivel < MAX_PROFUNDIDADE:
                vistos.add(cl)
                try:
                    sub = self._dados_pasta(cl, apag)
                except (OSError, ErroSA):
                    continue
                yield from self._entradas(sub, ent.caminho, apag, nivel + 1, vistos)

    def _preencher(self, ent, cl, tam, apag):
        if ent.eh_dir:
            if cl < 2 or cl >= self.nclusters + 2:
                ent.estado = 'ruim'
            return
        if tam == 0:
            return
        if cl < 2 or cl >= self.nclusters + 2:
            ent.estado, ent.nota = 'ruim', 'primeiro cluster inválido'
            return
        try:
            if apag:
                n = -(-tam // self.csize)
                if cl + n - 1 >= self.nclusters + 2:
                    ent.estado, ent.nota = 'ruim', 'não cabe no volume'
                    return
                ent.runs, _ = self._faixas([[cl, n]], tam)
                if any(self.fat(c) != 0 for c in range(cl, cl + n)):
                    ent.estado, ent.nota = 'parcial', 'clusters já reutilizados por outro arquivo'
                else:
                    ent.nota = 'recuperado assumindo clusters contíguos'
            else:
                runs, ok = self._cadeia(cl)
                ent.runs, falta = self._faixas(runs, tam)
                if not ok or falta > 0:
                    ent.estado, ent.nota = 'parcial', 'cadeia de clusters interrompida'
        except OSError:
            ent.estado, ent.nota = 'ruim', 'FAT ilegível'

    def varrer(self):
        try:
            dados = self._dados_pasta(self.raiz_cluster, False)
        except (OSError, ErroSA) as e:
            raise ErroSA('Não consegui ler a pasta raiz: %s' % e) from None
        yield from self._entradas(dados, '/', False, 0, set())
