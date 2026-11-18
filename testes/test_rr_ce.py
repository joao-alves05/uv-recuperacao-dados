# -*- coding: utf-8 -*-
"""Rock Ridge: área de continuação (CE) com o nome longo; cadeia de CE, laço e bloco fora da imagem."""
import os
import struct
import unittest

from auxiliar import CasoComPasta

import imagens_udf as iu
from recuperacao import leitor as leitor_mod
from recuperacao import sa

SETOR = 2048
LONGO = ('relatorio_final_' + 'abcdefghij' * 20 + '_versao_definitiva.txt')[:230]     # > 200 caracteres


def nm(texto, continua):
    b = texto.encode('utf-8')
    return b'NM' + bytes([5 + len(b), 1, 1 if continua else 0]) + b


def ce(lba, off, tam):
    d = b''.join(struct.pack('<I', v) + struct.pack('>I', v) for v in (lba, off, tam))
    return b'CE' + bytes([28, 1]) + d


def imagem(su_do_arquivo, areas):
    """ISO com a raiz no setor 20, um arquivo 'ARQ.TXT' com `su_do_arquivo` e áreas de continuação {lba: bytes}."""
    img = bytearray(iu.iso_rock_ridge({'x': ('ARQ.TXT;1', b'conteudo')}))
    ini = 20 * SETOR
    regs, off = [], 0
    while off < SETOR and img[ini + off]:
        regs.append(bytes(img[ini + off: ini + off + img[ini + off]]))
        off += img[ini + off]
    r = regs[2]                                           # ".", ".." e o arquivo; troca a área de uso do arquivo
    base = r[:33 + r[32] + (1 if r[32] % 2 == 0 else 0)]
    novo = base + su_do_arquivo
    if len(novo) % 2:
        novo += b'\0'
    novo = bytes([len(novo)]) + novo[1:]
    regs[2] = novo
    img[ini:ini + SETOR] = b''.join(regs).ljust(SETOR, b'\0')
    for lba, dados in areas.items():
        if (lba + 1) * SETOR > len(img):
            img.extend(b'\0' * ((lba + 1) * SETOR - len(img)))
        img[lba * SETOR:lba * SETOR + len(dados)] = dados
    return img


class TestRockRidgeCE(CasoComPasta):
    def listar(self, img):
        c = os.path.join(self.pasta, 'rr.iso')
        with open(c, 'wb') as f:
            f.write(img)
        with leitor_mod.abrir(c) as l:
            vol = sa.detectar(l)
            self.assertEqual(vol.tipo, 'ISO9660/Rock Ridge')
            return {e.nome: e for e in vol.varrer()}

    def test_nome_longo_na_continuacao_sai_inteiro(self):
        a, b, c = LONGO[:100], LONGO[100:200], LONGO[200:]
        # 1ª parte no registro, 2ª na CE do setor 40, 3ª na CE (encadeada) do setor 41
        areas = {40: nm(b, True) + ce(41, 0, len(nm(c, False))), 41: nm(c, False)}
        r = self.listar(imagem(nm(a, True) + ce(40, 0, len(areas[40])), areas))
        self.assertEqual(list(r), [LONGO])
        self.assertEqual(r[LONGO].estado, 'bom')

    def test_ce_que_aponta_para_si_mesma_nao_trava_e_avisa(self):
        a = LONGO[:100]
        areas = {40: nm(LONGO[100:150], True) + ce(40, 0, len(nm(LONGO[100:150], True)) + 28)}
        r = self.listar(imagem(nm(a, True) + ce(40, 0, len(areas[40])), areas))
        nome = a + LONGO[100:150]
        self.assertEqual(list(r), [nome])
        self.assertIn('laço', r[nome].nota)

    def test_ce_fora_da_imagem_avisa_e_nao_derruba(self):
        r = self.listar(imagem(nm(LONGO[:100], True) + ce(999999, 0, 40), {}))
        self.assertEqual(list(r), [LONGO[:100]])
        self.assertIn('fora da imagem', r[LONGO[:100]].nota)


if __name__ == '__main__':
    unittest.main()
