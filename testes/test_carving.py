# -*- coding: utf-8 -*-
"""Fase 5.1 e 5.2: tabela de assinaturas, validadores e motor de carving, sobre imagens montadas na memória."""
import json
import os
import unittest
from unittest import mock

import amostras as A
import auxiliar
from auxiliar import CasoComPasta

from recuperacao import carving, validar


class Memoria:
    """Leitor falso sobre bytes. `ruins` = setores que dão erro de leitura."""

    def __init__(self, dados, setor=512, ruins=()):
        self.dados = dados
        self.tamanho = len(dados)
        self.setor = setor
        self.ruins = set(ruins)

    def ler(self, off, n):
        if off < 0 or n < 0:
            raise ValueError
        n = min(n, max(0, self.tamanho - off))
        for s in range(off // self.setor, (off + n - 1) // self.setor + 1 if n else 0):
            if s in self.ruins:
                raise OSError('setor ruim %d' % s)
        return self.dados[off:off + n]


def varrer(dados, modo='rapida', bloco=4096, catalogo=None, **kw):
    v = carving.Varredor(Memoria(dados, **kw), catalogo or carving.carregar(), modo, bloco)
    achados, ruins = [], 0
    for _, novos, r in v.blocos(0):
        achados += novos
        ruins += r
    return achados, ruins, v


def com_folga(arquivo, antes=1024, depois=3000):
    return bytes(antes) + arquivo + bytes(depois)


# tipo esperado, extensão, construtor
CASOS = [
    ('JPG', 'jpg', lambda: A.jpeg()),
    ('PNG', 'png', lambda: A.png()),
    ('GIF', 'gif', A.gif),
    ('BMP', 'bmp', A.bmp),
    ('TIFF', 'tif', lambda: A.tiff()),
    ('CR2', 'cr2', lambda: A.tiff(cr2=True)),
    ('NEF', 'nef', lambda: A.tiff(marca=b'NIKON CORPORATION')),
    ('ARW', 'arw', lambda: A.tiff(marca=b'SONY')),
    ('DNG', 'dng', lambda: A.tiff(dng=True)),
    ('MP4', 'mp4', A.mp4),
    ('MOV', 'mov', lambda: A.mp4(marca=b'qt  ')),
    ('M4A', 'm4a', lambda: A.mp4(marca=b'M4A ')),
    ('HEIC', 'heic', A.heic),
    ('AVI', 'avi', A.avi),
    ('WAV', 'wav', A.wav),
    ('MP3', 'mp3', A.mp3),
    ('PDF', 'pdf', A.pdf),
    ('ZIP', 'zip', A.zip_simples),
    ('ZIP', 'zip', lambda: A.zip_simples(sem_busca=True)),
    ('DOCX', 'docx', A.docx),
    ('XLSX', 'xlsx', A.xlsx),
    ('ODT', 'odt', A.odt),
    ('RAR', 'rar', A.rar4),
    ('RAR', 'rar', A.rar5),
    ('7Z', '7z', A.sete_z),
    ('SQLITE', 'sqlite', A.sqlite_bytes),
    ('DOC', 'doc', A.ole2),
    ('XLS', 'xls', lambda: A.ole2(u'Workbook')),
    ('RTF', 'rtf', A.rtf),
]


class TestTabela(unittest.TestCase):
    def test_tabela_padrao_tem_os_tipos_minimos(self):
        tipos = {a['tipo'] for a in carving.carregar().assinaturas}
        for t in ('JPG', 'PNG', 'GIF', 'BMP', 'TIFF', 'MP4', 'AVI', 'WAV', 'MP3', 'PDF', 'ZIP', 'RAR', '7Z', 'SQLITE',
                  'DOC', 'RTF'):
            self.assertIn(t, tipos)

    def test_toda_linha_da_tabela_vale(self):
        for a in carving.carregar().assinaturas:
            self.assertIn(a['grupo'], ('foto', 'video', 'audio', 'documento', 'compactado', 'banco', 'outro'))
            self.assertIn(a['validar'], validar.VALIDADORES)


class TestTabelaExtensivel(CasoComPasta):
    def tabela(self, *linhas):
        caminho = os.path.join(self.pasta, 'a.json')
        with open(caminho, 'w', encoding='utf-8') as f:
            json.dump({'assinaturas': list(linhas)}, f)
        return carving.carregar(caminho)

    def test_tipo_novo_so_na_tabela_sem_mexer_no_codigo(self):
        cat = self.tabela({'tipo': 'XYZ', 'grupo': 'outro', 'extensao': 'xyz', 'cabecalho': '58595A31',
                           'rodape': '454E4458', 'tamanho_max': 1000})
        arquivo = b'XYZ1' + b'conteudo qualquer' + b'ENDX'
        achados, _, _ = varrer(com_folga(arquivo), catalogo=cat)
        self.assertEqual([(a['tipo'], a['offset'], a['tamanho']) for a in achados], [('XYZ', 1024, len(arquivo))])
        self.assertEqual(achados[0]['nome'], 'f0000002.xyz')

    def test_tabela_invalida_e_recusada_com_frase_clara(self):
        base = {'tipo': 'X', 'grupo': 'outro', 'extensao': 'x', 'cabecalho': '5858', 'rodape': '59', 'tamanho_max': 10}
        for mudanca in ({'grupo': 'planeta'}, {'cabecalho': 'zz'}, {'cabecalho': ''}, {'tamanho_max': 0},
                        {'extensao': '../x'}, {'rodape': None, 'validar': None}, {'validar': 'nao_existe'},
                        {'cabecalho_offset': -1}, {'tipo': ''}):
            with self.assertRaises(carving.ErroAssinaturas, msg=str(mudanca)):
                self.tabela(dict(base, **mudanca))
        with self.assertRaises(carving.ErroAssinaturas):
            carving.carregar(os.path.join(self.pasta, 'nao-existe.json'))


class TestValidacaoPorTipo(unittest.TestCase):
    def test_cada_tipo_e_achado_com_inicio_tamanho_e_bytes_certos(self):
        for tipo, ext, construir in CASOS:
            with self.subTest(tipo=tipo, ext=ext):
                arquivo = construir()
                img = com_folga(arquivo)
                for modo in ('rapida', 'profunda'):
                    achados, _, v = varrer(img, modo)
                    self.assertEqual(len(achados), 1, (modo, achados))
                    a = achados[0]
                    self.assertEqual((a['tipo'], a['extensao'], a['offset'], a['tamanho'], a['estado']),
                                     (tipo, ext, 1024, len(arquivo), 'bom'), modo)
                    self.assertEqual(A.sha256(img[a['offset']:a['offset'] + a['tamanho']]), A.sha256(arquivo))
                    self.assertGreaterEqual(a['pontuacao'], 70)
                    self.assertEqual(v.erros_validador, 0)

    def test_grupos(self):
        esperado = {'JPG': 'foto', 'MP4': 'video', 'WAV': 'audio', 'PDF': 'documento', 'DOCX': 'documento',
                    'ZIP': 'compactado', 'SQLITE': 'banco', 'HEIC': 'foto', 'M4A': 'audio', 'ODT': 'documento'}
        for tipo, ext, construir in CASOS:
            if tipo in esperado:
                achados, _, _ = varrer(com_folga(construir()))
                self.assertEqual(achados[0]['grupo'], esperado[tipo], tipo)

    def test_data_e_dimensoes_vem_do_conteudo(self):
        a, _, _ = varrer(com_folga(A.jpeg(data='2021:05:17 10:20:30', largura=640, altura=480)))
        self.assertEqual((a[0]['data'], a[0]['largura'], a[0]['altura']), ('2021-05-17 10:20:30', 640, 480))
        a, _, _ = varrer(com_folga(A.png(10, 6)))
        self.assertEqual((a[0]['largura'], a[0]['altura']), (10, 6))
        a, _, _ = varrer(com_folga(A.tiff(data='2020:01:02 03:04:05', largura=100, altura=50)))
        self.assertEqual((a[0]['data'], a[0]['largura'], a[0]['altura']), ('2020-01-02 03:04:05', 100, 50))
        a, _, _ = varrer(com_folga(A.pdf(data='20190304050607')))
        self.assertEqual(a[0]['data'], '2019-03-04 05:06:07')
        a, _, _ = varrer(com_folga(A.docx()))
        self.assertEqual(a[0]['data'], '2021-05-17 10:20:30')
        a, _, _ = varrer(com_folga(A.mp4(criacao=3786825600)))
        self.assertRegex(a[0]['data'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')
        a, _, _ = varrer(com_folga(A.jpeg()))
        self.assertIsNone(a[0]['data'])

    def test_nome_gerado_usa_o_setor_do_achado(self):
        a, _, _ = varrer(bytes(512 * 1234) + A.jpeg() + bytes(2000))
        self.assertEqual(a[0]['nome'], 'f0001234.jpg')


class TestEstadoParcialERuim(unittest.TestCase):
    def test_jpeg_cortado_no_meio_da_imagem_e_parcial_com_o_trecho_valido(self):
        arq = A.jpeg(scan=6000)
        corte = 2600
        achados, _, _ = varrer(com_folga(arq[:corte], depois=20000))
        self.assertEqual(len(achados), 1)
        a = achados[0]
        self.assertEqual((a['estado'], a['tamanho']), ('parcial', corte))
        self.assertLess(a['pontuacao'], 70)
        self.assertGreater(a['pontuacao'], 20)

    def test_jpeg_cortado_e_seguido_por_outro_arquivo_nao_engole_o_vizinho(self):
        jpg, png = A.jpeg(scan=6000), A.png()
        img = com_folga(jpg[:2600] + png, depois=2000)
        achados, _, _ = varrer(img, 'profunda')
        self.assertEqual([(a['tipo'], a['estado']) for a in achados], [('JPG', 'parcial'), ('PNG', 'bom')])
        self.assertEqual(achados[0]['tamanho'], 2600)

    def test_jpeg_sem_dados_de_imagem_e_ruim(self):
        arq = A.jpeg()
        corte = arq.index(b'\xff\xda')
        achados, _, _ = varrer(com_folga(arq[:corte]))
        self.assertEqual([(a['estado']) for a in achados], ['ruim'])
        self.assertLessEqual(achados[0]['pontuacao'], 20)

    def test_png_com_crc_errado_e_ruim_e_cortado_e_parcial(self):
        arq = bytearray(A.png())
        arq[45] ^= 0xFF                                    # dentro do IDAT
        achados, _, _ = varrer(com_folga(bytes(arq)))
        self.assertEqual([(a['estado'], a['tamanho']) for a in achados], [('ruim', len(arq))])
        achados, _, _ = varrer(com_folga(A.png()[:-20]))
        self.assertEqual(achados[0]['estado'], 'parcial')

    def test_zip_cortado_vira_parcial_com_as_entradas_inteiras(self):
        arq = A.zip_simples()
        achados, _, _ = varrer(com_folga(arq[:300], depois=0))
        self.assertEqual(len(achados), 1)
        self.assertEqual(achados[0]['estado'], 'parcial')
        self.assertLessEqual(achados[0]['tamanho'], 300)
        # sem o diretório central o que sobra é o trecho das entradas
        sem_cd = arq[:arq.index(b'PK\x01\x02')]
        achados, _, _ = varrer(com_folga(sem_cd))
        self.assertEqual((achados[0]['estado'], achados[0]['tamanho']), ('parcial', len(sem_cd)))

    def test_pdf_sem_fim_e_parcial_e_sem_xref_valido_tambem(self):
        arq = A.pdf()
        achados, _, _ = varrer(com_folga(arq[:arq.index(b'xref')], depois=6000))
        self.assertEqual(achados[0]['estado'], 'parcial')
        self.assertLess(achados[0]['tamanho'], len(arq))
        quebrado = arq.replace(b'startxref\n', b'startxref\n9')
        achados, _, _ = varrer(com_folga(quebrado))
        self.assertEqual(achados[0]['estado'], 'parcial')

    def test_pdf_com_atualizacao_incremental_vai_ate_o_ultimo_eof(self):
        base = A.pdf()
        extra = b'5 0 obj\n<< /X 1 >>\nendobj\nxref\n0 1\n0000000000 65535 f \ntrailer\n<< >>\nstartxref\n%d\n%%%%EOF\n' % len(base)
        achados, _, _ = varrer(com_folga(base + extra))
        self.assertEqual(achados[0]['tamanho'], len(base) + len(extra))

    def test_mp4_sem_moov_e_parcial(self):
        achados, _, _ = varrer(com_folga(A.mp4(com_moov=False)))
        self.assertEqual(achados[0]['estado'], 'parcial')

    def test_mp4_cortado_no_mdat(self):
        arq = A.mp4(mdat=4000)
        achados, _, _ = varrer(com_folga(arq[:3000], depois=0))
        self.assertEqual((achados[0]['estado'], achados[0]['tamanho']), ('parcial', 3000))

    def test_sete_z_com_crc_do_cabecalho_errado_nao_e_achado(self):
        arq = bytearray(A.sete_z())
        arq[8] ^= 1
        self.assertEqual(varrer(com_folga(bytes(arq)))[0], [])
        arq = bytearray(A.sete_z())
        arq[-1] ^= 1                                       # cabeçalho seguinte adulterado
        achados, _, _ = varrer(com_folga(bytes(arq)))
        self.assertEqual(achados[0]['estado'], 'ruim')

    def test_sqlite_cortado_e_parcial(self):
        arq = A.sqlite_bytes()
        achados, _, _ = varrer(com_folga(arq[:12288], depois=0))
        self.assertEqual(achados[0]['estado'], 'parcial')

    def test_riff_declarando_mais_do_que_existe_e_parcial(self):
        achados, _, _ = varrer(com_folga(A.wav(2000)[:1200], depois=0))
        self.assertEqual(achados[0]['estado'], 'parcial')


class TestCorpoConferido(unittest.TestCase):
    """Revisão final, item 3: RIFF, BMP e SQLite não são "bom" só porque o cabeçalho tem um tamanho plausível."""

    def um(self, arquivo, depois=3000):
        achados, _, _ = varrer(com_folga(arquivo, depois=depois))
        return achados

    def test_riff_com_tamanho_declarado_maior_que_os_pedacos_nao_e_bom(self):
        import struct
        wav = bytearray(A.wav(1000))
        struct.pack_into('<I', wav, 4, len(wav) - 8 + 1500)               # diz ter 1500 bytes a mais (aí vêm zeros)
        achados = self.um(bytes(wav))
        self.assertEqual([(a['tipo'], a['estado']) for a in achados], [('WAV', 'parcial')])
        self.assertLessEqual(achados[0]['tamanho'], len(wav))
        # o chunk de dados que passa do fim declarado também
        wav = bytearray(A.wav(1000))
        struct.pack_into('<I', wav, 40, 5000)
        self.assertEqual(self.um(bytes(wav))[0]['estado'], 'parcial')

    def test_wav_sem_chunk_de_dados_e_avi_sem_lista_de_filmagem_nao_sao_bons(self):
        import struct
        fmt = struct.pack('<4sIHHIIHH', b'fmt ', 16, 1, 1, 8000, 8000, 1, 8)
        corpo = b'WAVE' + fmt + b'junk' + struct.pack('<I', 8) + bytes(8)
        sem_data = b'RIFF' + struct.pack('<I', len(corpo)) + corpo
        self.assertEqual(self.um(sem_data)[0]['estado'], 'parcial')
        lista = b'LIST' + struct.pack('<I', 12) + b'hdrl' + b'avih' + struct.pack('<I', 0)
        corpo = b'AVI ' + lista + b'JUNK' + struct.pack('<I', 8) + bytes(8)
        self.assertEqual(self.um(b'RIFF' + struct.pack('<I', len(corpo)) + corpo)[0]['estado'], 'parcial')
        self.assertEqual(self.um(A.avi())[0]['estado'], 'bom')
        self.assertEqual(self.um(A.wav())[0]['estado'], 'bom')

    def test_wav_com_chunk_de_nome_invalido_no_meio_e_parcial_ate_ali(self):
        wav = bytearray(A.wav(1000))
        wav[36:40] = b'\x00\x01\x02\x03'                                   # onde ficava o id do chunk "data"
        achados = self.um(bytes(wav))
        self.assertEqual(achados[0]['estado'], 'parcial')
        self.assertLess(achados[0]['tamanho'], len(wav))

    def test_bmp_com_dimensoes_que_nao_cabem_no_tamanho_nao_e_achado(self):
        import struct
        bmp = bytearray(A.bmp(2, 2))
        struct.pack_into('<ii', bmp, 18, 50000, 50000)                    # 2,5 bilhões de pixels em 70 bytes
        self.assertEqual(self.um(bytes(bmp)), [])
        bmp = bytearray(A.bmp(2, 2))
        struct.pack_into('<I', bmp, 2, 60)                                # tamanho menor que o cabeçalho + pixels
        self.assertEqual(self.um(bytes(bmp)), [])
        bmp = bytearray(A.bmp(2, 2))
        struct.pack_into('<I', bmp, 30, 99)                               # compressão que não existe
        self.assertEqual(self.um(bytes(bmp)), [])
        self.assertEqual(self.um(A.bmp(40, 30))[0]['estado'], 'bom')

    def test_sqlite_com_esquema_ou_paginas_invalidos(self):
        banco = bytearray(A.sqlite_bytes())
        banco[100] = 0x00                                                 # a página do esquema não é uma árvore
        self.assertEqual(self.um(bytes(banco)), [])
        banco = bytearray(A.sqlite_bytes())
        banco[56:60] = b'\x00\x00\x00	'                                 # codificação de texto inexistente
        self.assertEqual(self.um(bytes(banco)), [])
        banco = bytearray(A.sqlite_bytes())
        self.assertEqual(self.um(bytes(banco))[0]['estado'], 'bom')

    def test_sqlite_com_paginas_do_meio_zeradas_e_parcial(self):
        banco = bytearray(A.sqlite_bytes(2000))
        paginas = len(banco) // 4096
        self.assertGreater(paginas, 20)
        meio = (paginas // 2) * 4096
        banco[meio:] = bytes(len(banco) - meio)                           # a metade final foi sobrescrita
        achados = self.um(bytes(banco), depois=0)
        self.assertEqual(achados[0]['estado'], 'parcial')
        self.assertLess(achados[0]['tamanho'], len(banco))
        self.assertGreaterEqual(achados[0]['tamanho'], 4096)


class TestDadoMalucoNaoDerrubaAVarredura(unittest.TestCase):
    """Revisão final, item 4: o que o validador não esperava (data absurda, OSError...) descarta o candidato, não a varredura."""

    def test_mp4_com_data_de_criacao_de_64_bits_absurda(self):
        import struct
        for criacao in (2 ** 63, 2 ** 64 - 1, 2 ** 40):
            mvhd = A._caixa(b'mvhd', b'\x01\x00\x00\x00' + struct.pack('>QQ', criacao, criacao) + bytes(80))
            mp4 = A._caixa(b'ftyp', b'isom\x00\x00\x02\x00isomiso2mp41') + A._caixa(b'moov', mvhd)                 + A._caixa(b'mdat', A.aleatorio(2000, 4))
            achados, _, v = varrer(com_folga(mp4))
            self.assertEqual([(a['tipo'], a['estado'], a['data']) for a in achados], [('MP4', 'bom', None)], criacao)
            self.assertEqual(v.erros_validador, 0)

    def test_qualquer_excecao_do_validador_vira_candidato_descartado(self):
        for erro in (OSError(22, 'Invalid argument'), OverflowError('timestamp'), RuntimeError('bug'), ZeroDivisionError()):
            cat = carving.carregar()
            for a in cat.assinaturas:
                if a['tipo'] == 'GIF':
                    def quebrado(j, off, limite, assin, erro=erro):
                        raise erro
                    a['_funcao'] = quebrado
            achados, _, v = varrer(com_folga(A.gif()) + A.png(), 'profunda', catalogo=cat)
            self.assertEqual(v.erros_validador, 1, repr(erro))
            self.assertEqual([a['tipo'] for a in achados], ['PNG'], repr(erro))

    def test_a_retomada_passa_pelo_mesmo_ponto_sem_cair(self):
        cat = carving.carregar()
        for a in cat.assinaturas:
            if a['tipo'] == 'GIF':
                def quebrado(j, off, limite, assin):
                    raise OSError(22, 'Invalid argument')
                a['_funcao'] = quebrado
        img = bytes(8192) + A.gif() + bytes(8192) + A.png() + bytes(4096)
        v = carving.Varredor(Memoria(img), cat, 'profunda', 4096)
        primeiro = []
        for pos, novos, _ in v.blocos(0):
            primeiro += novos
            ponto = pos
            if pos >= 8192:
                break
        v2 = carving.Varredor(Memoria(img), cat, 'profunda', 4096)
        resto = [a for _, novos, _ in v2.blocos(ponto) for a in novos]
        self.assertEqual([a['tipo'] for a in primeiro + resto], ['PNG'])


def dormir(segundos):
    import time
    time.sleep(segundos)
    return 1


class TestEncerrarPool(unittest.TestCase):
    """Revisão final, item 7: leitura presa não pode travar o encerramento nem deixar processo órfão."""

    def test_mata_o_processo_preso_numa_leitura_dentro_do_prazo(self):
        import time
        from concurrent.futures import ProcessPoolExecutor
        pool = ProcessPoolExecutor(max_workers=2)
        futuros = [pool.submit(dormir, 600) for _ in range(2)]
        esperar_ate = time.monotonic() + 30
        while time.monotonic() < esperar_ate and sum(1 for p in pool._processes.values() if p.is_alive()) < 2:
            time.sleep(0.05)
        procs = list(pool._processes.values())
        self.assertEqual(len(procs), 2)
        time.sleep(1.0)                                              # os dois já estão dentro do sleep
        t0 = time.monotonic()
        presas = carving.encerrar_pool(pool, prazo=1.0)
        self.assertLess(time.monotonic() - t0, 8.0)
        self.assertFalse(presas)
        self.assertTrue(all(not p.is_alive() for p in procs), 'sobrou processo filho')
        del futuros

    def test_thread_presa_nao_trava_e_avisa_para_nao_fechar_o_descritor(self):
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor
        solta = threading.Event()
        pool = ThreadPoolExecutor(max_workers=1)
        pool.submit(solta.wait, 60)
        time.sleep(0.2)
        t0 = time.monotonic()
        self.assertTrue(carving.encerrar_pool(pool, prazo=0.5))
        self.assertLess(time.monotonic() - t0, 5.0)
        solta.set()

    def test_pool_ocioso_encerra_sem_esperar_o_prazo(self):
        import time
        from concurrent.futures import ProcessPoolExecutor
        pool = ProcessPoolExecutor(max_workers=1)
        self.assertEqual(pool.submit(dormir, 0).result(timeout=60), 1)
        procs = list(pool._processes.values())
        t0 = time.monotonic()
        self.assertFalse(carving.encerrar_pool(pool, prazo=30.0))
        self.assertLess(time.monotonic() - t0, 10.0)
        self.assertTrue(all(not p.is_alive() for p in procs))


class TestFalsosPositivos(unittest.TestCase):
    def test_bytes_aleatorios_nao_geram_achado_em_nenhum_modo(self):
        lixo = A.aleatorio(1 << 20, 123)
        for modo in ('rapida', 'profunda'):
            achados, _, v = varrer(lixo, modo, bloco=65536)
            self.assertEqual(achados, [], modo)
            self.assertEqual(v.erros_validador, 0)

    def test_cabecalhos_soltos_sem_estrutura_nao_valem(self):
        soltos = [b'BM' + b'texto qualquer sem estrutura', b'\xff\xd8\xff' + bytes(100), b'PK\x03\x04' + bytes(100),
                  b'%PDF-1.4 e nada mais', b'GIF89a' + bytes(5), b'ID3' + bytes(50), b'RIFF' + bytes(50),
                  b'SQLite format 3\x00' + bytes(200), b'\x89PNG\r\n\x1a\n' + bytes(30), b'{\\rtf1', b'II*\x00' + bytes(20),
                  b'Rar!\x1a\x07\x00' + bytes(40), b'7z\xbc\xaf\x27\x1c' + bytes(40), bytes(4) + b'ftyp' + bytes(20)]
        for s in soltos:
            achados, _, v = varrer(com_folga(s, depois=6000), 'profunda')
            self.assertEqual(achados, [], s[:12])
            self.assertEqual(v.erros_validador, 0, s[:12])

    def test_arquivo_embutido_em_outro_nao_e_achado_a_parte(self):
        zip_com_jpg = A._zip([('a.jpg', A.jpeg()), ('b.png', A.png())], comp=0)
        achados, _, _ = varrer(com_folga(zip_com_jpg), 'profunda')
        self.assertEqual([a['tipo'] for a in achados], ['ZIP'])


class TestModosEBlocos(unittest.TestCase):
    def test_rapida_so_olha_inicio_de_setor_e_profunda_olha_todo_byte(self):
        jpg = A.jpeg()
        img = bytes(1000) + jpg + bytes(4000)                 # começa no byte 1000 (não é múltiplo de 512)
        self.assertEqual(varrer(img, 'rapida')[0], [])
        achados, _, _ = varrer(img, 'profunda')
        self.assertEqual([(a['tipo'], a['offset']) for a in achados], [('JPG', 1000)])

    def test_arquivos_colados_um_no_outro(self):
        seq = [A.jpeg(semente=1), A.png(), A.pdf(), A.zip_simples()]
        img = bytearray()
        offs = []
        for s in seq:
            offs.append(len(img))
            img += s + bytes((-len(s)) % 512)
        achados, _, _ = varrer(bytes(img) + bytes(2000))
        self.assertEqual([a['offset'] for a in achados], offs)

    def test_cabecalho_na_fronteira_do_bloco_e_arquivo_maior_que_o_bloco(self):
        grande = A.jpeg(scan=20000)
        for deslocamento in (4096 - 2, 4096 - 6, 4096, 4096 + 1, 8192 - 3):
            img = bytes(deslocamento) + grande + bytes(5000)
            achados, _, _ = varrer(img, 'profunda', bloco=4096)
            self.assertEqual([(a['tipo'], a['offset'], a['tamanho']) for a in achados],
                             [('JPG', deslocamento, len(grande))], deslocamento)
        # ftyp fica 4 bytes depois do começo do MP4: o começo está no bloco anterior
        mp4 = A.mp4(mdat=5000)
        achados, _, _ = varrer(bytes(4096 - 2) + mp4 + bytes(5000), 'profunda', bloco=4096)
        self.assertEqual([(a['tipo'], a['offset']) for a in achados], [('MP4', 4094)])

    def test_mesmo_resultado_em_qualquer_tamanho_de_bloco(self):
        img = bytearray(60000)
        for off, s in ((512, A.jpeg(semente=3)), (8192, A.png()), (12000, A.mp4()), (30000, A.pdf()),
                       (40960, A.docx()), (50176, A.tiff(cr2=True))):
            img[off:off + len(s)] = s
        base = [(a['offset'], a['tipo'], a['tamanho']) for a in varrer(bytes(img), 'profunda', bloco=60000)[0]]
        self.assertEqual(len(base), 6)
        for bloco in (4096, 5000, 12345):
            achados, _, _ = varrer(bytes(img), 'profunda', bloco=bloco)
            self.assertEqual([(a['offset'], a['tipo'], a['tamanho']) for a in achados], base, bloco)

    def test_retomar_do_ponto_do_checkpoint_nao_perde_nem_repete(self):
        img = bytearray(70000)
        for off, s in ((512, A.jpeg(semente=3)), (8192, A.png()), (12288, A.jpeg(scan=15000)), (30720, A.pdf()),
                       (40960, A.docx())):
            img[off:off + len(s)] = s
        todos = [(a['offset'], a['tipo']) for a in varrer(bytes(img), 'rapida', bloco=4096)[0]]
        for parar_depois in (1, 2, 3, 5):
            v = carving.Varredor(Memoria(bytes(img)), carving.carregar(), 'rapida', 4096)
            achados, pos = [], 0
            for i, (pos, novos, _) in enumerate(v.blocos(0), 1):
                achados += novos
                if i == parar_depois:
                    break
            v2 = carving.Varredor(Memoria(bytes(img)), carving.carregar(), 'rapida', 4096)   # processo novo
            for pos, novos, _ in v2.blocos(pos):
                achados += novos
            self.assertEqual([(a['offset'], a['tipo']) for a in achados], todos, parar_depois)

    def test_posicao_entregue_nunca_passa_do_tamanho_e_cresce(self):
        img = A.jpeg() + bytes(20000)
        v = carving.Varredor(Memoria(img), carving.carregar(), 'rapida', 4096)
        posicoes = [p for p, _, _ in v.blocos(0)]
        self.assertEqual(posicoes, sorted(set(posicoes)))
        self.assertEqual(posicoes[-1], len(img))

    def test_setor_ilegivel_vira_zeros_e_e_contado(self):
        jpg = A.jpeg()
        jpg += bytes((-len(jpg)) % 512)
        img = jpg + bytes(512 * 4) + A.png() + bytes(2000)
        achados, ruins, _ = varrer(img, ruins=(len(jpg) // 512 + 2,), bloco=4096)
        self.assertEqual(ruins, 1)
        self.assertEqual([a['tipo'] for a in achados], ['JPG', 'PNG'])

    def test_validador_que_estoura_e_contado_e_a_varredura_segue(self):
        cat = carving.carregar()
        for a in cat.assinaturas:
            if a['tipo'] == 'GIF':
                def quebrado(j, off, limite, assin):
                    raise ValueError('dado maluco')
                a['_funcao'] = quebrado
        achados, _, v = varrer(com_folga(A.gif()) + A.png(), 'profunda', catalogo=cat)
        self.assertEqual(v.erros_validador, 1)
        self.assertEqual([a['tipo'] for a in achados], ['PNG'])

    def test_modo_invalido_e_recusado(self):
        with self.assertRaises(ValueError):
            carving.Varredor(Memoria(b''), carving.carregar(), 'qualquer')


class TestSomenteLeitura(unittest.TestCase):
    def test_modulos_do_motor_nao_abrem_nada_para_escrita(self):
        raiz = os.path.join(auxiliar.RAIZ, 'recuperacao')
        for nome in ('carving.py', 'validar.py', os.path.join('tipos', 'varredura.py')):
            with open(os.path.join(raiz, nome), encoding='utf-8') as f:
                texto = f.read()
            for proibido in ('O_WRONLY', 'O_RDWR', 'O_CREAT', "'wb'", '"wb"', "'r+b'", "'ab'", "'w'", 'GENERIC_WRITE',
                             '.write(', 'os.remove', 'os.rename', 'shutil'):
                self.assertNotIn(proibido, texto, '%s usa %s' % (nome, proibido))


def varrer_arquivo(caminho, modo, bloco=65536, trabalhadores=None, inicio=0):
    from recuperacao import leitor
    achados = []
    with leitor.abrir(caminho) as l:
        v = carving.Varredor(l, carving.carregar(), modo, bloco, trabalhadores)
        for pos, novos, _ in v.blocos(inicio):
            achados += novos
        return achados, v


class TestSegundoPlano(CasoComPasta):
    def imagem(self):
        img = bytearray(3 << 20)
        pecas = [(512, A.jpeg(semente=3)), (8192, A.png()), (20480, A.jpeg(scan=400000, semente=4)), (700000, A.pdf()),
                 (900000 // 512 * 512, A.docx()), (1500000 // 512 * 512, A.mp4(mdat=9000)), (2000000 // 512 * 512, A.mp3()),
                 (2700000 // 512 * 512, A.zip_simples())]
        for off, p in pecas:
            img[off:off + len(p)] = p
        img[1234567:1234567 + 70] = A.bmp()
        caminho = os.path.join(self.pasta, 'img.bin')
        with open(caminho, 'wb') as f:
            f.write(img)
        return caminho

    def chaves(self, achados):
        return [(a['offset'], a['tipo'], a['tamanho'], a['estado']) for a in achados]

    def test_busca_usa_no_maximo_metade_dos_nucleos(self):
        class Falso:
            caminho, tamanho = 'x', carving.MINIMO_SEGUNDO_PLANO
        v = carving.Varredor.__new__(carving.Varredor)
        v.leitor, v.cat, v.modo = Falso(), type('C', (), {'caminho': 'y'})(), 'profunda'
        for nucleos, esperado in ((1, 1), (2, 1), (4, 2), (8, 3), (16, 3)):
            with mock.patch.object(carving.os, 'cpu_count', return_value=nucleos):
                self.assertEqual(v._escolher(None), esperado, nucleos)

    def test_threads_e_processos_dao_o_mesmo_resultado_que_sozinho(self):
        caminho = self.imagem()
        for modo in ('rapida', 'profunda'):
            base, v0 = varrer_arquivo(caminho, modo, trabalhadores=0)
            self.assertEqual(v0.segundo_plano, 'nenhum')
            self.assertGreaterEqual(len(base), 7)
            for bloco in (65536, 100000, 1 << 20):
                for n in (1, 2):
                    achados, v = varrer_arquivo(caminho, modo, bloco=bloco, trabalhadores=n)
                    self.assertEqual(v.segundo_plano, 'processos' if modo == 'profunda' else 'threads', (modo, bloco, n))
                    self.assertEqual(self.chaves(achados), self.chaves(base), (modo, bloco, n))

    def test_retomar_no_meio_com_segundo_plano(self):
        caminho = self.imagem()
        todos, _ = varrer_arquivo(caminho, 'profunda', trabalhadores=0)
        depois = [a for a in todos if a['offset'] >= 1 << 20]
        achados, _ = varrer_arquivo(caminho, 'profunda', trabalhadores=2, inicio=1 << 20)
        self.assertEqual(self.chaves(achados), self.chaves(depois))

    def test_trabalhador_que_nao_abre_a_origem_cai_para_o_modo_sozinho(self):
        from recuperacao import leitor
        caminho = self.imagem()
        base, _ = varrer_arquivo(caminho, 'profunda', trabalhadores=0)
        with leitor.abrir(caminho) as l:
            l.caminho = os.path.join(self.pasta, 'nao-existe.bin')             # o trabalhador não consegue abrir
            v = carving.Varredor(l, carving.carregar(), 'profunda', 65536, 2)
            achados = [a for _, novos, _ in v.blocos(0) for a in novos]
            self.assertEqual(v.segundo_plano, 'nenhum')
        self.assertEqual(self.chaves(achados), self.chaves(base))

    def test_escolha_automatica(self):
        from recuperacao import leitor
        caminho = self.imagem()
        with leitor.abrir(caminho) as l:
            self.assertEqual(carving.Varredor(l, carving.carregar(), 'rapida').trabalhadores, 0)          # pequena demais
            l.tamanho = 10 * carving.MINIMO_SEGUNDO_PLANO
            self.assertEqual(carving.Varredor(l, carving.carregar(), 'rapida').trabalhadores, 2)
            self.assertTrue(1 <= carving.Varredor(l, carving.carregar(), 'profunda').trabalhadores <= carving.MAX_PROCESSOS)
            l.caminho = None
            self.assertEqual(carving.Varredor(l, carving.carregar(), 'profunda').trabalhadores, 0)
        self.assertEqual(carving.Varredor(Memoria(bytes(10)), carving.carregar(), 'profunda', trabalhadores=3).trabalhadores, 0)

    def test_gerador_fechado_no_meio_encerra_os_trabalhadores(self):
        from recuperacao import leitor
        caminho = self.imagem()
        with leitor.abrir(caminho) as l:
            v = carving.Varredor(l, carving.carregar(), 'rapida', 65536, 2)
            g = v.blocos(0)
            next(g)
            g.close()                                                              # pausar o job faz isto
        os.remove(caminho)                                                         # no Windows falharia se ficasse aberto

    def test_rapida_so_olha_inicio_de_setor_com_setor_de_4k(self):
        mp4 = A.mp4(mdat=5000)
        img = bytearray(64 * 1024)
        img[8192:8192 + len(mp4)] = mp4                                           # alinhado a 4096
        img[40960 + 512:40960 + 512 + len(mp4)] = mp4                             # alinhado só a 512
        achados, _, _ = varrer(bytes(img), 'rapida', setor=4096)
        self.assertEqual([a['offset'] for a in achados], [8192])
        achados, _, _ = varrer(bytes(img), 'rapida', setor=512)
        self.assertEqual([a['offset'] for a in achados], [8192, 40960 + 512])


class TestLeituraGrande(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'ReadFile do Windows')
    def test_pedido_acima_de_4_gib_nao_estoura_o_dword_do_readfile(self):
        from recuperacao import leitor

        class Falso:
            pedidos = []

            def SetFilePointerEx(self, *a):
                return 1

            def ReadFile(self, h, buf, n, lido, _):
                self.pedidos.append(n)
                return 1
        api = leitor.ApiWindows()
        api.k = Falso()
        api.ler(1, 0, 5 << 30)
        self.assertEqual(Falso.pedidos, [leitor.MAX_LEITURA])


class TestOrigemMaiorQue4GiB(CasoComPasta):
    def test_offsets_acima_de_4_gib_sem_estouro_de_32_bits(self):
        import subprocess
        caminho = os.path.join(self.pasta, 'grande.bin')
        open(caminho, 'wb').close()
        if os.name == 'nt':
            r = subprocess.run(['fsutil', 'sparse', 'setflag', caminho], capture_output=True)
            if r.returncode != 0:
                self.skipTest('sem arquivo esparso neste sistema')
        base = (1 << 32) + (1 << 20)                                              # 4 GiB + 1 MiB
        jpg, png = A.jpeg(semente=8), A.png()
        try:
            with open(caminho, 'r+b') as f:
                f.truncate(base + (16 << 20))
                f.seek(base)
                f.write(jpg)
                f.seek(base + (8 << 20) + 4096)
                f.write(png)
        except OSError:
            self.skipTest('sem espaço para o arquivo esparso')
        for modo in ('rapida', 'profunda'):
            for n in (0, 2):
                achados, _ = varrer_arquivo(caminho, modo, bloco=1 << 20, trabalhadores=n, inicio=(1 << 32) - 4096)
                self.assertEqual([(a['offset'], a['tipo'], a['tamanho']) for a in achados],
                                 [(base, 'JPG', len(jpg)), (base + (8 << 20) + 4096, 'PNG', len(png))], (modo, n))


if __name__ == '__main__':
    unittest.main()
