"""MBR exportado com 5 ou mais partições: 3 primárias + estendida com a cadeia de EBRs (lógicas)."""
import struct
import unittest

import imagens_particoes as ip
from test_particoes import Base

from recuperacao import particoes
from recuperacao.tipos import particoes as tipo_particoes

MIB = ip.MIB
SEC = 512


def disco5():
    """36 MiB com 5 sistemas de arquivos. Devolve (img, proposta)."""
    img = bytearray(36 * MIB)
    ip.fat32(img, 1 * MIB, 4 * MIB)
    ip.ntfs(img, 6 * MIB, 4 * MIB)
    ip.ext(img, 11 * MIB, 10 * MIB)
    ip.fat32(img, 22 * MIB, 4 * MIB)
    ip.ntfs(img, 28 * MIB, 4 * MIB)
    dados = [(1, 4, 'fat32'), (6, 4, 'ntfs'), (11, 10, 'ext4'), (22, 4, 'fat32'), (28, 4, 'ntfs')]
    prop = [{'inicio': i * MIB, 'tamanho': t * MIB, 'fim': (i + t) * MIB, 'sistema': s} for i, t, s in dados]
    return img, prop


def gravar_tabela(img, prop):
    mbr, _ = particoes.montar_mbr(prop, len(img))
    img[0:512] = mbr
    for byte, ebr in particoes.montar_ebrs(prop, len(img)):
        img[byte:byte + 512] = ebr
    return mbr


class TestMbrEstendida(Base):
    def test_cinco_particoes_viram_tres_primarias_e_estendida_com_duas_logicas(self):
        img, prop = disco5()
        mbr = gravar_tabela(img, prop)
        tipos = [mbr[446 + 16 * k + 4] for k in range(4)]
        self.assertEqual(tipos[:3], [0x0C, 0x07, 0x83])
        self.assertIn(tipos[3], (0x05, 0x0F))
        ebrs = particoes.montar_ebrs(prop, len(img))
        self.assertEqual([b for b, _ in ebrs], [22 * MIB - SEC, 28 * MIB - SEC])

    def test_enderecos_das_ebrs_sao_relativos_a_estendida(self):
        img, prop = disco5()
        mbr = gravar_tabela(img, prop)
        e_lba, e_n = struct.unpack_from('<II', mbr, 446 + 48 + 8)
        ext = (22 * MIB - SEC) // SEC
        self.assertEqual(e_lba, ext)
        self.assertEqual(e_lba + e_n, 32 * MIB // SEC)
        (b1, ebr1), (b2, ebr2) = particoes.montar_ebrs(prop, len(img))
        # EBR 1: lógica em +1 setor do próprio EBR; o próximo EBR é relativo à ESTENDIDA, não ao EBR 1
        self.assertEqual(struct.unpack_from('<II', ebr1, 446 + 8), (1, 4 * MIB // SEC))
        self.assertIn(ebr1[446 + 16 + 4], (0x05, 0x0F))
        self.assertEqual(struct.unpack_from('<II', ebr1, 446 + 16 + 8)[0], (28 * MIB - SEC) // SEC - ext)
        # EBR 2 é o último: sem próximo
        self.assertEqual(struct.unpack_from('<II', ebr2, 446 + 8), (1, 4 * MIB // SEC))
        self.assertEqual(ebr2[446 + 16:446 + 32], bytes(16))
        self.assertEqual((ebr1[510:512], ebr2[510:512]), (b'\x55\xaa', b'\x55\xaa'))

    def test_analisar_le_de_volta_as_cinco(self):
        img, prop = disco5()
        gravar_tabela(img, prop)
        t = particoes.analisar(self.abrir(img))
        self.assertEqual((t['esquema'], t['estado']), ('mbr', 'ok'))
        reais = [p for p in t['particoes'] if not p.get('estendida')]
        self.assertEqual([(p['inicio'], p['tamanho'], p['sistema']) for p in reais],
                         [(p['inicio'], p['tamanho'], p['sistema']) for p in prop])
        self.assertEqual([p['origem_tabela'] for p in reais], ['mbr'] * 3 + ['mbr-logica'] * 2)
        self.assertEqual(sum(1 for p in t['particoes'] if p.get('estendida')), 1)

    def test_quatro_ou_menos_nada_muda(self):
        img, prop = disco5()
        prop = prop[:4]
        mbr, av = particoes.montar_mbr(prop, len(img))
        self.assertEqual(av, [])
        self.assertEqual(particoes.montar_ebrs(prop, len(img)), [])
        self.assertEqual([mbr[446 + 16 * k + 4] for k in range(4)], [0x0C, 0x07, 0x83, 0x0C])
        self.assertEqual([struct.unpack_from('<II', mbr, 446 + 16 * k + 8)[0] for k in range(4)],
                         [p['inicio'] // SEC for p in prop])

    def test_logica_sem_setor_livre_para_o_ebr_fica_de_fora_com_aviso(self):
        img, prop = disco5()
        prop[3] = dict(prop[3], inicio=21 * MIB, fim=25 * MIB)      # colada no fim da ext4 (11 + 10 MiB)
        mbr, av = particoes.montar_mbr(prop, len(img))
        self.assertTrue(any('EBR' in a and 'de fora' in a for a in av))
        self.assertEqual([b for b, _ in particoes.montar_ebrs(prop, len(img))], [28 * MIB - SEC])

    def test_tipo_estendida_0f_para_disco_grande(self):
        self.assertEqual(particoes._tipo_estendida(1000), 0x05)
        self.assertEqual(particoes._tipo_estendida(20_000_000), 0x0F)

    def test_resultado_do_job_traz_mbr_e_ebrs_e_aplicando_tudo_a_tabela_le_as_cinco(self):
        img, prop = disco5()
        l = self.abrir(img)
        nomes = {'mbr': 'x.mbr', 'mbr_ebrs': 'x.mbr-ebrs', 'gpt': 'x.gpt', 'gpt_fim': 'x.gpt-fim'}
        res, bins = tipo_particoes.montar_resultado('o.img', l, 512, {'particoes': []}, [dict(p) for p in prop], prop, 0, nomes)
        ap = res['aplicacao']
        self.assertEqual((ap['mbr']['arquivo'], ap['mbr']['junto_com']), ('x.mbr', 'mbr_ebrs'))
        self.assertEqual((ap['mbr_ebrs']['arquivo'], ap['mbr_ebrs']['junto_com']), ('x.mbr-ebrs', 'mbr'))
        self.assertEqual(len(bins['mbr_ebrs']), 2 * 512)
        self.assertEqual([g['gravar_em_byte'] for g in ap['mbr_ebrs']['gravar']], [22 * MIB - SEC, 28 * MIB - SEC])
        branco = bytearray(len(img))
        branco[ap['mbr']['gravar_em_byte']:512] = bins['mbr']
        for g in ap['mbr_ebrs']['gravar']:
            d = g['deslocamento_no_arquivo']
            branco[g['gravar_em_byte']:g['gravar_em_byte'] + g['bytes']] = bins['mbr_ebrs'][d:d + g['bytes']]
        for p in prop:                                       # os dados das partições vêm da imagem original
            branco[p['inicio']:p['inicio'] + 4096 * 4] = img[p['inicio']:p['inicio'] + 4096 * 4]
        t = particoes.analisar(self.abrir(branco, 'aplicado.img'))
        reais = [p for p in t['particoes'] if not p.get('estendida')]
        self.assertEqual([(p['inicio'], p['tamanho']) for p in reais], [(p['inicio'], p['tamanho']) for p in prop])

    def test_com_quatro_ou_menos_o_job_nao_gera_arquivo_de_ebrs(self):
        img, prop = disco5()
        nomes = {'mbr': 'x.mbr', 'mbr_ebrs': 'x.mbr-ebrs', 'gpt': 'x.gpt', 'gpt_fim': 'x.gpt-fim'}
        res, bins = tipo_particoes.montar_resultado('o.img', self.abrir(img), 512, {'particoes': []}, [dict(p) for p in prop[:4]], prop[:4], 0, nomes)
        self.assertNotIn('mbr_ebrs', bins)
        self.assertNotIn('mbr_ebrs', res['aplicacao'])
        self.assertNotIn('junto_com', res['aplicacao']['mbr'])


if __name__ == '__main__':
    unittest.main()
