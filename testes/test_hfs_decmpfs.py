# -*- coding: utf-8 -*-
"""HFS+: arquivo comprimido (decmpfs) tipo 3 (zlib no atributo) e tipo 4 (zlib no resource fork) sai descomprimido,
na varredura e na recuperação ponta a ponta; LZVN/LZFSE (tipo 8) sai `ruim` com a nota."""
import os
import struct
import unicodedata
import unittest
import zlib

from auxiliar import CasoComPasta

import imagens_hfs as ih
from recuperacao import achados as mod_achados
from recuperacao import arquivos_sa, leitor as leitor_mod
from recuperacao import sa
from test_sa_fat import conteudo, sha
from test_varredura import BaseVarredura

BLOCO_ATTR, BLOCO_RSRC = 250, 260                # blocos livres da imagem (dados vão até o bloco ~100)
ZERO = bytes(1)


def cab_decmpfs(tipo, tam):
    return b'fpmc' + struct.pack('<IQ', tipo, tam)


def chave_attr(fid, nome='com.apple.decmpfs'):
    n = unicodedata.normalize('NFD', nome).encode('utf-16-be')
    return struct.pack('>HHIIH', 12 + len(n), 0, fid, 0, len(n) // 2) + n


def attr_inline(dados):
    return struct.pack('>III', 0x10, 0, 0) + struct.pack('>I', len(dados)) + dados


def rsrc_tipo4(dados):
    """Resource fork decmpfs: cabeçalho de 0x100, tamanho, nº de blocos (LE) e tabela (offset, tamanho) relativa a 0x104."""
    blocos = [zlib.compress(dados[i:i + 65536]) for i in range(0, len(dados), 65536)]
    tab, pos = b'', 4 + 4 + 8 * len(blocos)
    for b in blocos:
        tab += struct.pack('<II', pos - 4, len(b))
        pos += len(b)
    corpo = struct.pack('<I', len(blocos)) + tab + b''.join(blocos)
    return struct.pack('>IIII', 0x100, 0x100 + 4 + len(corpo), len(corpo), 0).ljust(0x100, ZERO) + \
        struct.pack('>I', len(corpo)) + corpo


class ImagemDecmpfs:
    def montar(self):
        f = ih.HfsImg()
        pasta = f.add_dir(2, 'Docs')
        self.t3, self.t4, self.t8 = conteudo(3000, 11), conteudo(150000, 12), conteudo(500, 13)
        attrs, rsrc_dados = [], {}
        i3 = f.add_file(pasta, 'pequeno.txt', b'', comprimido=True)
        attrs.append((i3, cab_decmpfs(3, len(self.t3)) + zlib.compress(self.t3)))
        i4 = f.add_file(pasta, 'grande.bin', b'', comprimido=True)
        attrs.append((i4, cab_decmpfs(4, len(self.t4))))
        rsrc_dados[i4] = rsrc_tipo4(self.t4)
        i8 = f.add_file(pasta, 'lzvn.bin', b'', comprimido=True)
        attrs.append((i8, cab_decmpfs(8, len(self.t8)) + b'\x06lixo'))
        livre = BLOCO_RSRC
        for fid, rd in rsrc_dados.items():
            n = -(-len(rd) // ih.BS)
            rd = rd.ljust(n * ih.BS, ZERO)
            n1 = 20                                  # fork em 2 extents: um bloco comprimido cruza a emenda
            f.escritas.append((livre * ih.BS, rd[:n1 * ih.BS]))
            f.escritas.append(((livre + 40) * ih.BS, rd[n1 * ih.BS:]))
            novo = []
            for pai, nome, i, d, dados in f.cat:
                if i == fid:
                    dados = dados[:168] + struct.pack('>QII', len(rd), 0, n) + struct.pack('>II', livre, n1) + \
                        struct.pack('>II', livre + 40, n - n1) + ZERO * (8 * 6)
                novo.append((pai, nome, i, d, dados))
            f.cat = novo
            livre += n + 40
        cam = f.gravar(os.path.join(self.trabalho, 'h.img'))
        arv = ih.arvore([(chave_attr(i), attr_inline(a)) for i, a in sorted(attrs)], 266, no=4096)
        n_blocos = -(-len(arv) // ih.BS)
        with open(cam, 'r+b') as fh:
            fh.seek(BLOCO_ATTR * ih.BS)
            fh.write(arv)
            for off in (1024, ih.TOTAL_BLOCOS * ih.BS - 1024):          # nas duas cópias do cabeçalho de volume
                fh.seek(off + 352)
                fh.write(ih._fork(len(arv), [(BLOCO_ATTR, n_blocos)]))
        return cam


class TestHfsDecmpfs(ImagemDecmpfs, CasoComPasta):
    def varrer(self, caminho):
        with leitor_mod.abrir(caminho) as l:
            vol = sa.detectar(l)
            self.assertIsNotNone(vol)
            return {e.caminho: (e, b'' if e.eh_dir else b''.join(vol.ler(e))) for e in vol.varrer()}

    def test_tipo3_e_tipo4_descomprimidos(self):
        r = self.varrer(self.montar())
        for c, dados in (('/Docs/pequeno.txt', self.t3), ('/Docs/grande.bin', self.t4)):
            e, b = r[c]
            self.assertEqual((e.estado, e.tamanho, len(b), sha(b)), ('bom', len(dados), len(dados), sha(dados)), c)

    def test_tipo8_lzvn_nota_clara(self):
        e, _b = self.varrer(self.montar())['/Docs/lzvn.bin']
        self.assertEqual(e.estado, 'ruim')
        self.assertIn('LZVN/LZFSE não suportada', e.nota)

    def test_achado_com_extents_de_4_valores_normaliza_e_le(self):
        with leitor_mod.abrir(self.montar()) as l:
            vol = sa.detectar(l)
            entradas = {x.caminho: x for x in vol.varrer()}
            for caminho, dados in (('/Docs/pequeno.txt', self.t3), ('/Docs/grande.bin', self.t4)):
                a, motivo = arquivos_sa.achado_de(entradas[caminho])
                self.assertIsNone(motivo, caminho)
                self.assertEqual(a['tamanho'], len(dados))
                self.assertTrue(all(len(x) == 4 and x[3] == 'zlib' for x in a['extents']), caminho)
                mod_achados.normalizar('j', a)
                self.assertEqual(sha(b''.join(mod_achados.ler_achado(l, a))), sha(dados), caminho)
            self.assertTrue(any(x[2] == 0 for x in a['extents']))      # o grande tem bloco partido na emenda dos extents

    def test_tipo8_continua_na_lista_como_ruim(self):
        with leitor_mod.abrir(self.montar()) as l:
            e = next(x for x in sa.detectar(l).varrer() if x.nome == 'lzvn.bin')
            self.assertEqual(arquivos_sa.achado_de(e), (None, 'sem_conteudo'))     # como já era: ruim, sem bytes


class TestRecuperacaoDecmpfs(ImagemDecmpfs, BaseVarredura):
    def test_recuperado_tem_o_sha256_do_original(self):
        self.trabalho = self.pasta
        job = self.varrer(self.montar(), 'sistema')
        self.esperar_estado(job['id'], 'concluido')
        destino = os.path.join(self.pasta, 'saida')
        r = self.req('POST', '/api/jobs/%s/recuperar' % job['id'], {'ids': 'todos', 'destino': destino})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.esperar_estado(r.get_json()['id'], 'concluido')
        con = self.ctx.store._con()
        res = {l['nome']: l['resultado'] for l in con.execute(
            'SELECT a.nome, r.resultado FROM recuperacoes r JOIN achados a ON a.id = r.achado_id WHERE r.job_id=?',
            (r.get_json()['id'],))}
        arqs = {n: os.path.join(b, n) for b, _, ns in os.walk(destino) for n in ns}
        for nome, dados in (('pequeno.txt', self.t3), ('grande.bin', self.t4)):
            with open(arqs[nome], 'rb') as g:
                self.assertEqual(sha(g.read()), sha(dados), nome)
            self.assertEqual(res[nome], 'ok', nome)
        self.assertNotIn('lzvn.bin', arqs)


if __name__ == '__main__':
    unittest.main()
