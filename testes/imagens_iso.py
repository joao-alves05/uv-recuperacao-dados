# -*- coding: utf-8 -*-
"""Monta uma ISO 9660 (com Joliet opcional) só em Python para os testes da fase 4."""
import struct

SETOR = 2048


def _tam_reg(nl):
    return 33 + nl + (1 if nl % 2 == 0 else 0)


def _reg(lba, tam, flags, nome):
    n = _tam_reg(len(nome))
    b = bytearray(n)
    b[0] = n
    struct.pack_into('<I', b, 2, lba)
    struct.pack_into('>I', b, 6, lba)
    struct.pack_into('<I', b, 10, tam)
    struct.pack_into('>I', b, 14, tam)
    b[18:25] = bytes([124, 3, 15, 10, 20, 30, 0])            # 2024-03-15 10:20:30
    b[25] = flags
    struct.pack_into('<H', b, 28, 1)
    struct.pack_into('>H', b, 30, 1)
    b[32] = len(nome)
    b[33:33 + len(nome)] = nome
    return bytes(b)


class No:
    def __init__(self, nome, eh_dir, dados=b'', curto=None, multi=False):
        self.nome, self.eh_dir, self.dados, self.multi = nome, eh_dir, dados, multi
        self.filhos = []
        self.curto = curto or (nome.upper() if eh_dir else (nome.upper() + ';1'))
        self.lba = {}


class IsoImg:
    def __init__(self, joliet=True, rotulo='ISOTESTE'):
        self.joliet = joliet
        self.rotulo = rotulo
        self.raiz = No('', True)
        self.lba_prim = 0

    def _achar(self, caminho):
        n = self.raiz
        for p in [x for x in caminho.split('/') if x]:
            n = next(f for f in n.filhos if f.nome == p)
        return n

    def add_dir(self, pai, nome):
        self._achar(pai).filhos.append(No(nome, True))
        return pai.rstrip('/') + '/' + nome

    def add_file(self, pai, nome, dados, curto=None, multi=False):
        self._achar(pai).filhos.append(No(nome, False, dados, curto, multi))

    def _nome(self, no, arvore):
        return no.nome.encode('utf-16-be') if arvore == 'j' else no.curto.encode('ascii')

    def _serializa(self, no, arvore, pai, resolver):
        regs = [_reg(resolver(no, arvore), 0, 2, b'\0'), _reg(resolver(pai, arvore), 0, 2, b'\1')]
        for f in no.filhos:
            if f.eh_dir:
                regs.append(_reg(resolver(f, arvore), self._tam_dir(f, arvore), 2, self._nome(f, arvore)))
            elif f.multi and len(f.dados) > 1:
                m = len(f.dados) // 2
                regs.append(_reg(f.lba.get('d', 0), m, 0x80, self._nome(f, arvore)))
                regs.append(_reg(f.lba.get('d2', 0), len(f.dados) - m, 0, self._nome(f, arvore)))
            else:
                regs.append(_reg(f.lba.get('d', 0), len(f.dados), 0, self._nome(f, arvore)))
        saida, atual = bytearray(), 0
        for r in regs:
            if atual + len(r) > SETOR:
                saida += b'\0' * (SETOR - atual)
                atual = 0
            saida += r
            atual = (atual + len(r)) % SETOR
        return bytes(saida).ljust(-(-max(len(saida), 1) // SETOR) * SETOR, b'\0')

    def _tam_dir(self, no, arvore):
        return len(self._serializa(no, arvore, no, lambda n, a: 0))

    def _percorre(self, no=None):
        no = no or self.raiz
        yield no
        for f in no.filhos:
            yield from self._percorre(f) if f.eh_dir else iter([f])

    def gravar(self, caminho):
        arvores = ['p'] + (['j'] if self.joliet else [])
        lba = 20
        for n in self._percorre():
            if n.eh_dir:
                for a in arvores:
                    n.lba[a] = lba
                    lba += self._tam_dir(n, a) // SETOR
            elif n.dados:
                n.lba['d'] = lba
                if n.multi and len(n.dados) > 1:
                    m = len(n.dados) // 2
                    lba += -(-m // SETOR)
                    n.lba['d2'] = lba
                    lba += -(-(len(n.dados) - m) // SETOR)
                else:
                    lba += -(-len(n.dados) // SETOR)
        total = lba
        pais = {}
        for n in self._percorre():
            for f in n.filhos:
                pais[id(f)] = n
        itens = []
        for n in self._percorre():
            if n.eh_dir:
                for a in arvores:
                    pai = pais.get(id(n), n)
                    itens.append((n.lba[a] * SETOR, self._serializa(n, a, pai, lambda x, ar: x.lba[ar])))
            elif n.dados:
                if n.multi and len(n.dados) > 1:
                    m = len(n.dados) // 2
                    itens.append((n.lba['d'] * SETOR, n.dados[:m]))
                    itens.append((n.lba['d2'] * SETOR, n.dados[m:]))
                else:
                    itens.append((n.lba['d'] * SETOR, n.dados))

        def descritor(tipo, id_vol, raiz_lba, raiz_tam, escape=b''):
            d = bytearray(SETOR)
            d[0], d[1:6], d[6] = tipo, b'CD001', 1
            d[40:72] = id_vol
            struct.pack_into('<I', d, 80, total)
            struct.pack_into('>I', d, 84, total)
            d[88:88 + len(escape)] = escape
            struct.pack_into('<H', d, 128, SETOR)
            struct.pack_into('>H', d, 130, SETOR)
            d[156:190] = _reg(raiz_lba, raiz_tam, 2, b'\0')
            return bytes(d)
        itens.append((16 * SETOR, descritor(1, self.rotulo.upper().ljust(32).encode(), self.raiz.lba['p'],
                                            self._tam_dir(self.raiz, 'p'))))
        n = 17
        if self.joliet:
            nome = self.rotulo.encode('utf-16-be').ljust(32, b'\0')
            itens.append((17 * SETOR, descritor(2, nome, self.raiz.lba['j'], self._tam_dir(self.raiz, 'j'), b'%/E')))
            n = 18
        fim = bytearray(SETOR)
        fim[0], fim[1:6], fim[6] = 255, b'CD001', 1
        itens.append((n * SETOR, bytes(fim)))
        with open(caminho, 'wb') as f:
            f.truncate(total * SETOR)
            for off, d in itens:
                f.seek(off)
                f.write(d)
        return caminho
