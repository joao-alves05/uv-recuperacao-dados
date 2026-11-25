# -*- coding: utf-8 -*-
"""NTFS: arquivo comprimido (LZNT1) é descomprimido na leitura e na recuperação (antes saía `ruim`).

O descompressor é testado sozinho com vetores montados à mão; depois, numa imagem NTFS sintética com unidades
comprimidas, sem compressão, esparsas e fragmentadas, o conteúdo recuperado tem de ter o mesmo SHA-256 do original.
"""
import hashlib
import os
import random
import struct
import unittest

import imagens_ntfs as nt
from auxiliar import CasoComPasta
from test_sa_fat import conteudo
from test_varredura import BaseVarredura

from recuperacao import achados as mod_achados
from recuperacao import arquivos_sa, leitor as leitor_mod, sa
from recuperacao.sa.ntfs import lznt1_descomprimir


def sha(b):
    return hashlib.sha256(b).hexdigest()


# ---- compressor mínimo, só para montar as imagens de teste ------------------------------------------------
def _chunk(c):
    """Um chunk (até 4096 bytes) em LZNT1: comprimido se ganhar, senão como está."""
    n, corpo, i, idx = len(c), bytearray(), 0, {}
    while i < n:
        pos_flag = len(corpo)
        corpo.append(0)
        for b in range(8):
            if i >= n:
                break
            lg, k = 0, i - 1
            while k >= 0x10:
                k >>= 1
                lg += 1
            max_tam, max_desl = min((0xFFF >> lg) + 3, n - i), 1 << (12 - lg)
            melhor, desl = 0, 0
            for p in idx.get(c[i:i + 3], [])[-32:]:
                d = i - p
                if d > max_desl:
                    continue
                q = 0
                while q < max_tam and c[p + q] == c[i + q]:          # a origem pode passar de i (sobreposição)
                    q += 1
                if q > melhor:
                    melhor, desl = q, d
            if melhor >= 3:
                corpo[pos_flag] |= 1 << b
                corpo += struct.pack('<H', ((desl - 1) << (12 - lg)) | (melhor - 3))
                passo = melhor
            else:
                corpo.append(c[i])
                passo = 1
            for j in range(i, i + passo):
                if j + 3 <= n:
                    idx.setdefault(c[j:j + 3], []).append(j)
            i += passo
    if len(corpo) < n:
        return struct.pack('<H', 0xB000 | (len(corpo) - 1)) + bytes(corpo)
    return struct.pack('<H', 0x3000 | (n - 1)) + c


def lznt1_comprimir(dados):
    return b''.join(_chunk(dados[i:i + 4096]) for i in range(0, len(dados), 4096))


class TestDescompressor(unittest.TestCase):
    def test_chunk_nao_comprimido(self):
        dados = conteudo(4096, 1)
        self.assertEqual(lznt1_descomprimir(b'\xff\x3f' + dados), dados)

    def test_chunk_comprimido_com_copia_sobreposta(self):
        # tokens: 'abc' literais e cópia de 9 bytes a 3 para trás (header 0xB005; flag 0x08; token 0x2006)
        self.assertEqual(lznt1_descomprimir(bytes.fromhex('05b0 08 616263 0620')), b'abcabcabcabc')
        self.assertEqual(lznt1_descomprimir(bytes.fromhex('03b0 02 61 6000')), b'a' * 100)

    def test_largura_do_deslocamento_muda_com_a_posicao(self):
        # 17 literais e depois cópia de 4 bytes a 17 para trás: na posição 17 o deslocamento tem 5 bits, o tamanho 11
        vetor = bytes.fromhex('15b0 00') + b'abcdefgh' + bytes.fromhex('00') + b'ijklmnop' + bytes.fromhex('02') + b'q' \
            + bytes.fromhex('0180')
        self.assertEqual(lznt1_descomprimir(vetor), b'abcdefghijklmnopq' + b'abcd')

    def test_misto_e_terminador_e_limite(self):
        cru = conteudo(4096, 2)
        vetor = b'\xff\x3f' + cru + bytes.fromhex('05b0 08 616263 0620') + b'\0\0' + b'lixo depois do terminador'
        esperado = cru + b'abcabcabcabc'
        self.assertEqual(lznt1_descomprimir(vetor), esperado)
        self.assertEqual(lznt1_descomprimir(vetor, 100), esperado[:100])

    def test_dado_invalido_levanta_valueerror(self):
        for ruim in (bytes.fromhex('ffff') + b'x' * 10,                     # assinatura errada
                     bytes.fromhex('ff3f') + b'x' * 10,                     # chunk passa do fim
                     bytes.fromhex('05b0 08 616263 0680'),                  # cópia antes do começo do chunk
                     bytes.fromhex('04b0 08 616263 06')):                   # token truncado
            with self.assertRaises(ValueError):
                lznt1_descomprimir(ruim)

    def test_ida_e_volta_com_o_compressor_do_teste(self):
        rnd = random.Random(5)
        for dados in (b'', b'a', b'x' * 10000, conteudo(5000, 3), (b'linha de texto %d\n' * 400) % tuple(range(400)),
                      bytes(rnd.choice(b'ab') for _ in range(9000))):
            self.assertEqual(lznt1_descomprimir(lznt1_comprimir(dados)), dados)


# ---- imagem NTFS com arquivo comprimido ---------------------------------------------------------------------
class ImgComprimida(nt.NtfsImg):
    def add_comprimido(self, pai, nome, original, fragmentar=(), apagado=False):
        """Grava `original` como o NTFS: unidades de 16 clusters, cada uma esparsa, sem compressão ou comprimida
        (dados no começo, esparso no fim). `fragmentar`: unidades comprimidas cujos clusters ficam em 2 runs."""
        cs, un = self.CS, 16 * self.CS
        runs = []

        def run(lcn, n):
            if lcn is None and runs and runs[-1][0] is None:
                runs[-1] = (None, runs[-1][1] + n)
            else:
                runs.append((lcn, n))

        def gravar(dados, nclusters, frag=False):
            partes = [nclusters] if not frag else [nclusters // 2, nclusters - nclusters // 2]
            pos = 0
            for q in partes:
                c = self.livre_cluster
                self.livre_cluster += q + (3 if frag else 0)
                self.escritas.append((c * cs, dados[pos * cs:(pos + q) * cs].ljust(q * cs, b'\0')))
                self.bits.update(range(c, c + q))
                run(c, q)
                pos += q

        for u, ini in enumerate(range(0, len(original), un)):
            parte = original[ini:ini + un]
            if not any(parte):
                run(None, 16)
                continue
            comp = lznt1_comprimir(parte) + b'\0\0'
            k = -(-len(comp) // cs)
            if k >= 16:
                gravar(parte, 16)                                   # não compensa comprimir: grava como está
            else:
                gravar(comp, k, frag=u in fragmentar and k > 1)
                run(None, 16 - k)
        n = self._novo_reg()
        attr = bytearray(self._attr_nres(0x80, runs, len(original), flags=1))
        attr[34] = 4                                                # unidade de compressão: 2**4 clusters
        self.registros[n] = self._registro(n, 1, [self._si(), self._fn(pai, nome, 0, 1, len(original)), bytes(attr)])
        self.runs_de[n] = [(l, q) for l, q in runs if l is not None]
        if apagado:
            self.apagar(n)
        return n


def texto(n, semente):
    rnd = random.Random(semente)
    palavras = [b'recuperacao', b'dados', b'NTFS', b'cluster', b'arquivo', b'unidade']
    out = bytearray()
    while len(out) < n:
        out += rnd.choice(palavras) + b' '
    return bytes(out[:n])


def montar_original():
    """unidade 0: 3 chunks de texto e 1 aleatório (chunk não comprimido dentro de unidade comprimida); 1: zeros (esparsa);
    2: aleatória (sem compressão); 3: texto (comprimida, fragmentada); 4: parcial, termina no meio de um chunk."""
    un = 16 * nt.NtfsImg.CS
    return (texto(4096, 1) + conteudo(4096, 2) + texto(4096, 3) + texto(4096, 4) + bytes(un) + conteudo(un, 5)
            + texto(un, 6) + texto(5000, 7))


class BaseImagem(CasoComPasta):
    def imagem(self, **kw):
        f = ImgComprimida()
        self.original = montar_original()
        self.n = f.add_comprimido(5, 'comprimido.dat', self.original, fragmentar=(3,), **kw)
        f.add_file(5, 'comum.bin', conteudo(3000, 9))
        self.f = f
        return f.gravar(os.path.join(self.pasta, 'ntfs_lznt1.img'))


class TestNtfsComprimido(BaseImagem):
    def varrer(self, caminho):
        with leitor_mod.abrir(caminho) as l:
            vol = sa.detectar(l)
            r = {}
            for e in vol.varrer():
                r[e.caminho] = (e, b'' if e.eh_dir else b''.join(vol.ler(e)))
        return r

    def test_conteudo_lido_pelo_volume_bate_com_o_original(self):
        r = self.varrer(self.imagem())
        e, b = r['/comprimido.dat']
        self.assertEqual((e.estado, e.tamanho, sha(b)), ('bom', len(self.original), sha(self.original)))
        self.assertEqual(r['/comum.bin'][0].estado, 'bom')

    def test_apagado_com_clusters_livres_tambem_sai_inteiro(self):
        r = self.varrer(self.imagem(apagado=True))
        e, b = r['/comprimido.dat']
        self.assertEqual((e.apagado, e.estado, sha(b)), (True, 'bom', sha(self.original)))

    def test_unidade_incompleta_continua_ruim(self):
        f = ImgComprimida()
        f.add_file(5, 'comprimido.bin', b'x' * 2000, flags_dados=1)     # 2 clusters alocados, nenhum esparso
        r = self.varrer(f.gravar(os.path.join(self.pasta, 'inc.img')))
        self.assertEqual(r['/comprimido.bin'][0].estado, 'ruim')

    def test_achado_traz_extents_de_3_valores_e_normaliza(self):
        r = self.varrer(self.imagem())
        a, motivo = arquivos_sa.achado_de(r['/comprimido.dat'][0])
        self.assertIsNone(motivo)
        self.assertEqual(a['tamanho'], len(self.original))
        self.assertTrue(any(len(e) == 3 for e in a['extents']))
        self.assertTrue(any(e[0] is None for e in a['extents']))       # a unidade esparsa
        mod_achados.normalizar('j', a)


class LeitorCurto:
    """Leitor falso: repassa tudo, mas na leitura que começa em `em` devolve menos: 1 byte a menos ou, com
    `no_chunk`, só até o fim do primeiro chunk LZNT1 (corte limpo: a descompressão não percebe sozinha)."""
    def __init__(self, real, em=None, no_chunk=False):
        self.real, self.em, self.no_chunk = real, em, no_chunk
        self.tamanho = real.tamanho

    def ler(self, off, n):
        dados = self.real.ler(off, n)
        if off != self.em:
            return dados
        return dados[:2 + (dados[0] | dados[1] << 8 & 0xFFF) + 1] if self.no_chunk else dados[:-1]


class TestLeituraCurtaNoVolume(BaseImagem):
    def entrada(self, vol):
        return next(e for e in vol.varrer() if e.nome == 'comprimido.dat')

    def test_leitura_curta_no_meio_de_unidade_comprimida_falha_explicitamente(self):
        with leitor_mod.abrir(self.imagem()) as l:
            vol = sa.detectar(l)
            e = self.entrada(vol)
            comp = [x for x in e.extents_lznt1 if len(x) == 3]
            vol.leitor = LeitorCurto(l, comp[0][0], no_chunk=True)  # unidade 0 chega só até o fim do 1o chunk
            with self.assertRaises(OSError):
                b''.join(vol.ler(e))
            vol.leitor = LeitorCurto(l, comp[1][0])                 # 1 byte a menos no meio de outra unidade
            with self.assertRaises(OSError):
                b''.join(vol.ler(e))

    def test_leitura_curta_em_extent_comum_tambem_falha(self):
        with leitor_mod.abrir(self.imagem()) as l:
            vol = sa.detectar(l)
            e = self.entrada(vol)
            cru = next(x for x in e.extents_lznt1 if len(x) == 2 and x[0] is not None)
            vol.leitor = LeitorCurto(l, cru[0])
            with self.assertRaises(OSError):
                b''.join(vol.ler(e))

    def test_leitor_normal_continua_igual(self):
        with leitor_mod.abrir(self.imagem()) as l:
            vol = sa.detectar(l)
            e = self.entrada(vol)
            vol.leitor = LeitorCurto(l, None)
            self.assertEqual(sha(b''.join(vol.ler(e))), sha(self.original))


class TestRecuperacaoDeComprimido(BaseImagem, BaseVarredura):
    def recuperar(self, caminho):
        job = self.varrer(caminho, 'sistema')
        self.esperar_estado(job['id'], 'concluido')
        self.destino = os.path.join(self.pasta, 'saida')
        r = self.req('POST', '/api/jobs/%s/recuperar' % job['id'], {'ids': 'todos', 'destino': self.destino})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.esperar_estado(r.get_json()['id'], 'concluido')
        con = self.ctx.store._con()
        res = {l['nome']: l['resultado'] for l in con.execute(
            'SELECT a.nome, r.resultado FROM recuperacoes r JOIN achados a ON a.id = r.achado_id WHERE r.job_id=?',
            (r.get_json()['id'],))}
        arqs = {n: os.path.join(b, n) for b, _, ns in os.walk(self.destino) for n in ns}
        return res, arqs

    def test_recuperado_tem_o_mesmo_sha256_do_original(self):
        res, arqs = self.recuperar(self.imagem())
        with open(arqs['comprimido.dat'], 'rb') as g:
            self.assertEqual(sha(g.read()), sha(self.original))
        self.assertEqual(res['comprimido.dat'], 'ok')

    def test_unidade_corrompida_zera_so_ela_e_avisa(self):
        caminho = self.imagem()
        lcn = self.f.runs_de[self.n][0][0]                       # primeiros clusters: dados da unidade 0
        with open(caminho, 'r+b') as g:
            g.seek(lcn * nt.NtfsImg.CS)
            g.write(b'\xff\xff')                                 # cabeçalho de chunk inválido
        res, arqs = self.recuperar(caminho)
        with open(arqs['comprimido.dat'], 'rb') as g:
            rec = g.read()
        un = 16 * nt.NtfsImg.CS
        self.assertEqual(res['comprimido.dat'], 'com_falhas')
        self.assertEqual(len(rec), len(self.original))
        self.assertEqual(rec[:un], bytes(un))
        self.assertEqual(rec[un:], self.original[un:])


if __name__ == '__main__':
    unittest.main()
