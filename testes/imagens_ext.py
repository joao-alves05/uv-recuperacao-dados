# -*- coding: utf-8 -*-
"""Monta ext2, ext3 e ext4 mínimos (um grupo, blocos de 1024 B) só em Python para os testes da fase 4."""
import struct

T = 1710498030                      # 2024-03-15 10:20:30 UTC
BS = 1024


class ExtImg:
    def __init__(self, versao='ext2', rotulo='extteste'):
        self.versao = versao
        self.rotulo = rotulo
        self.isize = 256 if versao == 'ext4' else 128
        self.nblocos = 4096
        self.ipg = 64
        self.tabela = 5
        self.livre = 5 + self.ipg * self.isize // BS + 4
        self.usados = set(range(0, self.livre))
        self.prox_ino = 11
        self.inodes = {}                    # ino -> bytearray
        self.dados = []                     # (offset, bytes)
        self.eh_ext4 = versao == 'ext4'
        self.blocos_de = {}
        self.pastas = {2: {'pai': 2, 'ents': []}}
        self.inodes[2] = self._inode(0x4000 | 0o755, 0, 3, 0x80000 if self.eh_ext4 else 0, b'')

    # ---- inodes e blocos
    def _inode(self, mode, size, links, flags, iblock, dtime=0):
        b = bytearray(self.isize)
        struct.pack_into('<HHI', b, 0, mode, 0, size & 0xFFFFFFFF)
        struct.pack_into('<IIII', b, 8, T, T, T, dtime)
        struct.pack_into('<H', b, 26, links)
        struct.pack_into('<II', b, 28, -(-size // 512), flags)
        b[40:100] = iblock.ljust(60, b'\0')
        if mode & 0xF000 == 0x8000:
            struct.pack_into('<I', b, 108, size >> 32)
        if self.isize > 128:
            struct.pack_into('<H', b, 128, 32)
            struct.pack_into('<I', b, 144, T)
        return b

    def _blocos(self, n, inicio=None):
        if inicio is None:
            inicio = self.livre
            self.livre += n
        bl = list(range(inicio, inicio + n))
        self.usados.update(bl)
        return bl

    def _escreve_bloco(self, b, dados):
        self.dados.append((b * BS, dados.ljust(BS, b'\0')))

    def _iblock_mapa(self, bl):
        """Ponteiros diretos + indireto simples + duplo para a lista de blocos `bl`."""
        ptr = list(bl[:12]) + [0] * (12 - len(bl[:12]))
        resto = list(bl[12:])
        ind = dup = 0
        if resto:
            ind = self._blocos(1)[0]
            self._escreve_bloco(ind, struct.pack('<256I', *(resto[:256] + [0] * (256 - len(resto[:256])))))
            resto = resto[256:]
        if resto:
            dup = self._blocos(1)[0]
            filhos = []
            while resto:
                f = self._blocos(1)[0]
                parte = resto[:256]
                self._escreve_bloco(f, struct.pack('<256I', *(parte + [0] * (256 - len(parte)))))
                filhos.append(f)
                resto = resto[256:]
            self._escreve_bloco(dup, struct.pack('<256I', *(filhos + [0] * (256 - len(filhos)))))
        return struct.pack('<15I', *(ptr + [ind, dup, 0]))

    def _iblock_extents(self, pedacos):
        """`pedacos` = [(bloco lógico, físico, quantidade)]; até 4 ficam no inode, mais que isso vira árvore de 2 níveis."""
        def ent(bl, ph, n):
            return struct.pack('<IHHI', bl, n, ph >> 32, ph & 0xFFFFFFFF)
        if len(pedacos) <= 4:
            return struct.pack('<HHHHI', 0xF30A, len(pedacos), 4, 0, 0) + b''.join(ent(*p) for p in pedacos)
        folha = self._blocos(1)[0]
        self._escreve_bloco(folha, struct.pack('<HHHHI', 0xF30A, len(pedacos), 84, 0, 0) + b''.join(ent(*p) for p in pedacos))
        return (struct.pack('<HHHHI', 0xF30A, 1, 4, 1, 0)
                + struct.pack('<IIHH', pedacos[0][0], folha, 0, 0))

    def add_file(self, pai, nome, dados, fragmentos=1, bloco=None):
        ino = self.prox_ino
        self.prox_ino += 1
        n = -(-len(dados) // BS)
        if self.eh_ext4:
            pedacos, pos = [], 0
            partes = [n] if fragmentos == 1 else [max(1, n // fragmentos)] * (fragmentos - 1) + [n - n // fragmentos * (fragmentos - 1)]
            for q in partes:
                bl = self._blocos(q, bloco if (bloco is not None and pos == 0) else None)
                for k, b in enumerate(bl):
                    self._escreve_bloco(b, dados[(pos + k) * BS:(pos + k + 1) * BS])
                pedacos.append((pos, bl[0], q))
                pos += q
                if fragmentos > 1:
                    self.livre += 2
            iblock, flags = self._iblock_extents(pedacos), 0x80000
            self.blocos_de[ino] = [x for (_l, ph, q) in pedacos for x in range(ph, ph + q)]
        else:
            bl = self._blocos(n, bloco)
            for k, b in enumerate(bl):
                self._escreve_bloco(b, dados[k * BS:(k + 1) * BS])
            iblock, flags = self._iblock_mapa(bl), 0
            self.blocos_de[ino] = list(bl)
        self.inodes[ino] = self._inode(0x8000 | 0o644, len(dados), 1, flags, iblock)
        self.pastas[pai]['ents'].append([ino, nome, 1, False])
        return ino

    def add_file_fallocate(self, pai, nome, dados, extra_blocos, lixo=bytes([0xAA])):
        """ext4: `dados` num extent normal e mais `extra_blocos` num extent NÃO inicializado (fallocate) cujos blocos
        físicos guardam `lixo` (resto de outro arquivo). O tamanho do arquivo cobre os dois."""
        assert self.eh_ext4
        ino = self.prox_ino
        self.prox_ino += 1
        n = -(-len(dados) // BS)
        bl = self._blocos(n)
        for k, b in enumerate(bl):
            self._escreve_bloco(b, dados[k * BS:(k + 1) * BS])
        extra = self._blocos(extra_blocos)
        for b in extra:
            self._escreve_bloco(b, lixo * BS)
        pedacos = [(0, bl[0], n), (n, extra[0], extra_blocos + 32768)]
        self.blocos_de[ino] = list(bl) + list(extra)
        self.inodes[ino] = self._inode(0x8000 | 0o644, (n + extra_blocos) * BS, 1, 0x80000, self._iblock_extents(pedacos))
        self.pastas[pai]['ents'].append([ino, nome, 1, False])
        return ino

    def add_file_arvore_hostil(self, pai, nome, tam=10 * BS):
        """ext4 hostil: árvore de extents de 5 níveis em que cada nó aponta 84 vezes para o mesmo filho (um DAG que, lido
        como árvore, tem ~84**4 visitas). Só para provar que o leitor tem teto."""
        assert self.eh_ext4
        ino = self.prox_ino
        self.prox_ino += 1
        dados = self._blocos(1)[0]
        folha = self._blocos(1)[0]
        self._escreve_bloco(folha, struct.pack('<HHHHI', 0xF30A, 84, 84, 0, 0) +
                            b''.join(struct.pack('<IHHI', k, 1, 0, dados) for k in range(84)))
        filho = folha
        for nivel in range(1, 5):
            no = self._blocos(1)[0]
            self._escreve_bloco(no, struct.pack('<HHHHI', 0xF30A, 84, 84, nivel, 0) +
                                b''.join(struct.pack('<IIHH', k, filho, 0, 0) for k in range(84)))
            filho = no
        raiz = struct.pack('<HHHHI', 0xF30A, 4, 4, 5, 0) + b''.join(struct.pack('<IIHH', k, filho, 0, 0) for k in range(4))
        self.blocos_de[ino] = [dados]
        self.inodes[ino] = self._inode(0x8000 | 0o644, tam, 1, 0x80000, raiz)
        self.pastas[pai]['ents'].append([ino, nome, 1, False])
        return ino

    def mkdir(self, pai, nome):
        ino = self.prox_ino
        self.prox_ino += 1
        self.pastas[ino] = {'pai': pai, 'ents': []}
        self.inodes[ino] = self._inode(0x4000 | 0o755, BS, 2, 0x80000 if self.eh_ext4 else 0, b'')
        self.pastas[pai]['ents'].append([ino, nome, 2, False])
        return ino

    def apagar(self, pai, ino, preservar=False):
        """Apaga como o sistema: dtime, links 0, bitmap limpo e a entrada da pasta colada à anterior.
        ext3 zera ponteiros e tamanho; ext4 zera a contagem de extents (o tamanho fica)."""
        b = self.inodes[ino]
        struct.pack_into('<I', b, 20, T + 100)
        struct.pack_into('<H', b, 26, 0)
        self.usados.difference_update(self.blocos_de[ino])
        if preservar:
            pass
        elif self.versao == 'ext3':
            b[40:100] = b'\0' * 60
            struct.pack_into('<I', b, 4, 0)
        elif self.versao == 'ext4':
            struct.pack_into('<H', b, 42, 0)
        for e in self.pastas[pai]['ents']:
            if e[0] == ino:
                e[3] = True

    # ---- gravação
    @staticmethod
    def _nat(nome):
        return (8 + len(nome.encode()) + 3) & ~3

    def _serializa_pasta(self, ino):
        p = self.pastas[ino]
        ents = [[ino, '.', 2, False], [p['pai'], '..', 2, False]] + p['ents']
        blocos, grupos, uso = [], [], 0
        def fecha():
            nonlocal grupos, uso
            if not grupos:
                return
            b = bytearray(BS)
            off = 0
            for gi, g in enumerate(grupos):
                live = g[0]
                nl = live[1].encode()
                rl = sum(self._nat(e[1]) for e in g)
                if gi == len(grupos) - 1:
                    rl = BS - off
                struct.pack_into('<IHBB', b, off, live[0], rl, len(nl), live[2])
                b[off + 8:off + 8 + len(nl)] = nl
                h = off + self._nat(live[1])
                for e in g[1:]:
                    nn = e[1].encode()
                    struct.pack_into('<IHBB', b, h, e[0], self._nat(e[1]), len(nn), e[2])
                    b[h + 8:h + 8 + len(nn)] = nn
                    h += self._nat(e[1])
                off += rl
            blocos.append(bytes(b))
            grupos, uso = [], 0
        for e in ents:
            if uso + self._nat(e[1]) > BS:
                fecha()
            if e[3]:
                grupos[-1].append(e)
            else:
                grupos.append([e])
            uso += self._nat(e[1])
        fecha()
        return blocos

    def gravar(self, caminho):
        for ino in list(self.pastas):
            blocos = self._serializa_pasta(ino)
            bl = self._blocos(len(blocos))
            for b, d in zip(bl, blocos):
                self._escreve_bloco(b, d)
            ib = self.inodes[ino]
            struct.pack_into('<I', ib, 4, len(blocos) * BS)
            if self.eh_ext4:
                ib[40:100] = self._iblock_extents([(0, bl[0], len(bl))]).ljust(60, b'\0')
            else:
                ib[40:100] = self._iblock_mapa(bl)
        bitmap = bytearray(BS)
        for b in self.usados:
            bitmap[(b - 1) // 8] |= 1 << ((b - 1) % 8)
        inobm = bytearray(BS)
        for i in range(1, self.prox_ino):
            inobm[(i - 1) // 8] |= 1 << ((i - 1) % 8)
        itens = list(self.dados)
        sb = bytearray(1024)
        compat, incompat = (0x4, 0x2 | 0x40) if self.eh_ext4 else ((0x4, 0x2) if self.versao == 'ext3' else (0, 0x2))
        struct.pack_into('<IIIIIIIII', sb, 0, self.ipg, self.nblocos, 0, 0, 0, 1, 0, 0, 8192)
        struct.pack_into('<II', sb, 40, self.ipg, 0)
        struct.pack_into('<H', sb, 56, 0xEF53)
        struct.pack_into('<H', sb, 58, 1)
        struct.pack_into('<I', sb, 76, 1)
        struct.pack_into('<IH', sb, 84, 11, self.isize)
        struct.pack_into('<III', sb, 92, compat, incompat, 0)
        sb[120:120 + len(self.rotulo)] = self.rotulo.encode()
        itens.append((1024, bytes(sb)))
        gd = bytearray(32)
        struct.pack_into('<III', gd, 0, 3, 4, self.tabela)
        itens.append((2 * BS, bytes(gd)))
        itens.append((3 * BS, bytes(bitmap)))
        itens.append((4 * BS, bytes(inobm)))
        for ino, b in self.inodes.items():
            itens.append((self.tabela * BS + (ino - 1) * self.isize, bytes(b)))
        with open(caminho, 'wb') as f:
            f.truncate(self.nblocos * BS)
            for off, d in itens:
                f.seek(off)
                f.write(d)
        return caminho
