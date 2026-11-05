# -*- coding: utf-8 -*-
"""Fase 6: CD/DVD/Blu-ray. Drive falso com setores ilegíveis, Rock Ridge, UDF (inclusive várias sessões) e
recuperação parcial. Tudo com imagens montadas em Python: nunca drive real."""
import hashlib
import json
import os
import time
import unittest
from unittest import mock

from auxiliar import CasoComPasta, esperar, sha256_de
import imagens_udf as iu
from test_somente_leitura import Espiao

from recuperacao import arquivos_sa, leitor, sa, setores_ruins
from recuperacao.executor import Executor
from recuperacao.jobs import JobStore
from recuperacao.sa import iso, udf
from recuperacao.tipos import imagem, recuperar

S = 2048


class DriveFalso(leitor.Leitor):
    """Leitor de 2048 que se passa por unidade óptica: setores que nunca leem e setores que falham só nas primeiras vezes."""
    alinhar = True

    def __init__(self, dados, ruins=(), instaveis=None):
        super().__init__(len(dados), S)
        self.dados, self.ruins, self.instaveis = dados, set(ruins), dict(instaveis or {})
        self.tentativas = {}

    def _bruto(self, offset, n):
        for s in range(offset // S, -(-(offset + n) // S)):
            if s in self.ruins:
                raise OSError('erro de leitura do setor %d' % s)
            if s in self.instaveis:
                self.tentativas[s] = self.tentativas.get(s, 0) + 1
                if self.tentativas[s] <= self.instaveis[s]:
                    raise OSError('setor %d instável' % s)
        return bytes(self.dados[offset: offset + n])


class TestImagemOptica(CasoComPasta):
    def setUp(self):
        super().setUp()
        self.store = JobStore(os.path.join(self.dados, 'jobs.db'))
        self.addCleanup(self.store.fechar)
        self.executor = Executor(self.store, {'imagem': imagem})
        self.executor.iniciar()
        self.addCleanup(self.executor.encerrar, 5.0)

    def rodar(self, drive, opcoes=None, origem='/dev/sr0', nome='cd.img'):
        destino = os.path.join(self.trabalho, nome)
        with mock.patch('recuperacao.leitor.abrir', return_value=drive):
            j = self.store.criar('imagem', origem, destino, dict({'bloco': 65536}, **(opcoes or {})))
            self.executor.submeter(j['id'])
            job = esperar(lambda: (x := self.store.obter(j['id']))['estado'] in ('concluido', 'falhou') and x, 30, 0.02, 'job')
        return job, destino

    def test_validar_aceita_optico_em_linux_e_windows(self):
        d = os.path.join(self.trabalho, 'x.img')
        for o in ('/dev/sr0', '\\\\.\\CdRom0'):
            self.assertIsNone(imagem.validar(o, d, {}), o)

    def test_imagem_com_setores_ruins_zera_so_eles_e_o_mapa_marca_so_eles(self):
        dados = os.urandom(400 * S)
        drive = DriveFalso(dados, ruins={37, 38, 250})
        job, destino = self.rodar(drive)
        self.assertEqual(job['estado'], 'concluido', job['erro'])
        esperado = bytearray(dados)
        for s in (37, 38, 250):
            esperado[s * S:(s + 1) * S] = bytes(S)
        with open(destino, 'rb') as f:
            self.assertEqual(f.read(), bytes(esperado))
        with open(destino + '.mapa.json', encoding='utf-8') as f:
            faixas = json.load(f)['faixas']
        self.assertEqual([(a, b) for a, b, e in faixas if e == 'ruim'], [(37 * S, 39 * S), (250 * S, 251 * S)])
        self.assertEqual({e for _, _, e in faixas} - {'ruim', 'ok'}, set())
        self.assertEqual(job['setores_ruins'], 3)
        eventos = ' '.join(e['texto'] for e in self.store.eventos(job['id']))
        self.assertIn('pode demorar', eventos)

    def test_setor_instavel_e_recuperado_na_releitura_com_tentativas_padrao_da_optica(self):
        dados = os.urandom(200 * S)
        drive = DriveFalso(dados, ruins={10}, instaveis={90: 6})
        job, destino = self.rodar(drive)
        self.assertEqual(job['estado'], 'concluido', job['erro'])
        with open(destino, 'rb') as f:
            lido = f.read()
        self.assertEqual(lido[90 * S:91 * S], dados[90 * S:91 * S])
        self.assertEqual(lido[10 * S:11 * S], bytes(S))
        with open(destino + '.mapa.json', encoding='utf-8') as f:
            self.assertEqual([(a, b) for a, b, e in json.load(f)['faixas'] if e == 'ruim'], [(10 * S, 11 * S)])

    def test_sem_releitura_o_setor_instavel_fica_ruim(self):
        """Controle: com tentativas=0 o mesmo setor instável não é recuperado (prova que o padrão 3 faz diferença)."""
        dados = os.urandom(200 * S)
        job, destino = self.rodar(DriveFalso(dados, instaveis={90: 6}), {'tentativas': 0}, nome='sem.img')
        with open(destino + '.mapa.json', encoding='utf-8') as f:
            self.assertEqual([(a, b) for a, b, e in json.load(f)['faixas'] if e == 'ruim'], [(90 * S, 91 * S)])

    def test_pular_e_voltar_sai_igual_com_setores_ruins_espalhados(self):
        dados = os.urandom(600 * S)
        ruins = {5, 6, 7, 8, 100, 101, 300}
        job, destino = self.rodar(DriveFalso(dados, ruins=ruins))
        with open(destino, 'rb') as f:
            lido = f.read()
        for s in range(600):
            esperado = bytes(S) if s in ruins else dados[s * S:(s + 1) * S]
            self.assertEqual(lido[s * S:(s + 1) * S], esperado, s)


class ApiOptica:
    """API do Windows falsa de uma unidade óptica: o IOCTL de tamanho e o de setor falham."""
    def __init__(self, dados):
        self.dados = dados
        self.geometria = False

    def CreateFileW(self, caminho, acesso, compartilha, disposicao, flags):
        assert acesso == leitor.GENERIC_READ
        return 1

    def tamanho(self, h):
        raise OSError('sem IOCTL de tamanho')

    def tamanho_por_geometria(self, h):
        self.geometria = True
        return len(self.dados)

    def setor(self, h):
        raise OSError('sem geometria')

    def ler(self, h, offset, n):
        assert offset % S == 0 and n % S == 0, 'leitura de óptico precisa ser alinhada a 2048'
        return self.dados[offset: offset + n]

    def fechar(self, h):
        pass


class TestLeitorOptico(unittest.TestCase):
    def test_windows_cdrom_usa_geometria_e_setor_2048(self):
        dados = os.urandom(10 * S)
        api = ApiOptica(dados)
        with leitor.abrir('\\\\.\\CdRom0', api=api) as l:
            self.assertEqual((l.tamanho, l.setor), (10 * S, 2048))
            self.assertTrue(api.geometria)
            self.assertEqual(l.ler(S + 5, 100), dados[S + 5:S + 105])        # pedido desalinhado vira leitura alinhada

    def test_disco_comum_que_falha_o_ioctl_continua_falhando(self):
        with self.assertRaises(leitor.ErroLeitor):
            leitor.abrir('\\\\.\\PhysicalDrive0', api=ApiOptica(b''))


class TestRockRidge(unittest.TestCase):
    ARQ = {'Relatório Final 2024.txt': ('RELATO~1.TXT;1', b'conteudo do relatorio ' * 10),
           'foto.jpg': ('FOTO.JPG;1', os.urandom(5000))}

    def abrir(self, rr):
        return leitor.Leitor.__new__(leitor.Leitor), iu.iso_rock_ridge(self.ARQ, rr)

    def vol(self, rr):
        class Mem(leitor.Leitor):
            def __init__(s, d):
                super().__init__(len(d), S)
                s.d = d

            def _bruto(s, o, n):
                return bytes(s.d[o:o + n])
        return iso.reconhecer(Mem(iu.iso_rock_ridge(self.ARQ, rr)))

    def test_nomes_longos_vem_do_rock_ridge(self):
        v = self.vol(True)
        self.assertEqual(v.tipo, 'ISO9660/Rock Ridge')
        ents = {e.caminho: e for e in v.varrer()}
        self.assertEqual(set(ents), {'/Relatório Final 2024.txt', '/foto.jpg'})
        self.assertEqual(b''.join(v.ler(ents['/Relatório Final 2024.txt'])), self.ARQ['Relatório Final 2024.txt'][1])
        self.assertEqual(ents['/foto.jpg'].tamanho, 5000)

    def test_sem_rock_ridge_fica_o_nome_curto(self):
        v = self.vol(False)
        self.assertEqual(v.tipo, 'ISO9660')
        self.assertEqual({e.caminho for e in v.varrer()}, {'/RELATO~1.TXT', '/FOTO.JPG'})


class Mem(leitor.Leitor):
    def __init__(self, d, sessoes=None):
        super().__init__(len(d), S)
        self.d = d
        if sessoes is not None:
            self.sessoes = lambda: sessoes

    def _bruto(self, o, n):
        return bytes(self.d[o:o + n])


GRANDE = os.urandom(5000)
TRES = os.urandom(3 * S)


def arvore(extra=None):
    a = {'docs/pequeno.txt': b'ola mundo', 'docs/grande.bin': GRANDE, 'top.bin': TRES,
         'docs/velho.txt': ('apagado', b'texto antigo apagado, ainda com ponteiros')}
    a.update(extra or {})
    return a


class TestUdf(CasoComPasta):
    def disco(self, arq=None, setores=1200, **kw):
        img = iu.disco_udf([iu.Sessao(arq or arvore(), 0, alt_fim=setores - 1, **kw)], setores)
        return img

    def test_lista_arquivos_pastas_embutido_apagado_e_datas(self):
        v = sa.detectar(Mem(self.disco()))
        self.assertIsInstance(v, udf.Udf)
        self.assertEqual((v.tipo, v.rotulo), ('UDF', 'UDFTESTE'))
        ents = {e.caminho: e for e in v.varrer()}
        self.assertEqual(set(ents), {'/docs', '/docs/pequeno.txt', '/docs/grande.bin', '/top.bin', '/docs/velho.txt'})
        self.assertTrue(ents['/docs'].eh_dir)
        self.assertEqual(b''.join(v.ler(ents['/docs/pequeno.txt'])), b'ola mundo')            # embutido no File Entry
        self.assertEqual(b''.join(v.ler(ents['/docs/grande.bin'])), GRANDE)
        self.assertEqual(b''.join(v.ler(ents['/top.bin'])), TRES)
        self.assertEqual(ents['/top.bin'].tamanho, 3 * S)
        velho = ents['/docs/velho.txt']
        self.assertTrue(velho.apagado)
        self.assertEqual(velho.estado, 'parcial')
        self.assertFalse(ents['/top.bin'].apagado)
        self.assertEqual(ents['/top.bin'].modificado, 1710498030.0)                              # 2024-03-15 10:20:30 UTC
        self.assertIn('metadados', v.limitacao)

    def test_nao_e_udf_nem_iso_em_disco_comum(self):
        self.assertIsNone(udf.reconhecer(Mem(bytearray(os.urandom(400 * S)))))
        self.assertIsNone(udf.reconhecer(Mem(bytearray(400 * S))))

    def test_ancora_no_ultimo_setor_basta_se_a_do_256_sumiu(self):
        img = self.disco()
        img[256 * S:257 * S] = bytes(S)
        v = udf.reconhecer(Mem(img))
        self.assertIsNotNone(v)
        self.assertEqual(v.ancora, 1199)

    def test_conjunto_de_volume_reserva_salva_o_principal_danificado(self):
        img = self.disco()
        img[32 * S:36 * S] = bytes(4 * S)                    # apaga o conjunto principal; a reserva fica em 48
        v = udf.reconhecer(Mem(img))
        self.assertIsNotNone(v)
        self.assertEqual(b''.join(v.ler(next(e for e in v.varrer() if e.caminho == '/top.bin'))), TRES)

    def test_varias_sessoes_a_ultima_vale_com_toc_ou_com_varredura(self):
        antiga = iu.Sessao({'antigo.txt': b'a' * 300}, 0, rotulo='SESSAO1')
        nova = iu.Sessao({'novo.txt': b'n' * 300, 'antigo.txt': b'a' * 300}, 600, rotulo='SESSAO2')
        img = iu.disco_udf([antiga, nova], 1500)
        nomes = lambda v: sorted(e.caminho for e in v.varrer())
        self.assertEqual(nomes(udf.reconhecer(Mem(img))), ['/antigo.txt'])                        # só as âncoras padrão
        com_toc = udf.reconhecer(Mem(img, sessoes=[0, 600]))
        self.assertEqual((com_toc.rotulo, nomes(com_toc)), ('SESSAO2', ['/antigo.txt', '/novo.txt']))
        varrido = udf.reconhecer(Mem(img), todas_sessoes=True)
        self.assertEqual((varrido.rotulo, varrido.ancora), ('SESSAO2', 856))
        self.assertEqual(nomes(varrido), ['/antigo.txt', '/novo.txt'])

    def test_particao_virtual_nao_suportada_vira_nao_reconhecido(self):
        img = self.disco()
        for vds in (32, 48):
            lvd = (vds + 2) * S
            img[lvd + 440] = 2                                  # mapa tipo 2 (virtual/esparsa)
            img[lvd + 4] = (sum(img[lvd:lvd + 4]) + sum(img[lvd + 5:lvd + 16])) & 0xFF
        self.assertIsNone(udf.reconhecer(Mem(img)))

    def test_origem_nao_muda(self):
        caminho = os.path.join(self.trabalho, 'dvd.iso')
        with open(caminho, 'wb') as f:
            f.write(self.disco())
        antes = (sha256_de(caminho), os.stat(caminho).st_mtime_ns)
        with Espiao(caminho) as esp, leitor.abrir(caminho) as l:
            v = sa.detectar(l)
            for e in v.varrer():
                b''.join(v.ler(e)) if not e.eh_dir else None
        self.assertTrue(esp.aberturas)
        self.assertEqual(esp.escritas(), [])
        self.assertEqual((sha256_de(caminho), os.stat(caminho).st_mtime_ns), antes)


class CtrlFalso:
    def pausa_pedida(self):
        return False

    def cancelamento_pedido(self):
        return False


class TestRecuperacaoParcial(CasoComPasta):
    def test_cortes_e_mapa(self):
        faixas = [(100, 200), (500, 600)]
        self.assertEqual(setores_ruins.cortes(faixas, 0, 100), [])
        self.assertEqual(setores_ruins.cortes(faixas, 150, 400), [(150, 200), (500, 550)])
        self.assertEqual(setores_ruins.cortes(faixas, 550, 1000), [(550, 600)])
        self.assertEqual(setores_ruins.carregar(os.path.join(self.trabalho, 'nao.img')), [])
        img = os.path.join(self.trabalho, 'm.img')
        with open(img + '.mapa.json', 'w', encoding='utf-8') as f:
            json.dump({'faixas': [[0, 10, 'ok'], [10, 20, 'ruim'], [20, 30, 'pendente'], [30, 40, 'ok']]}, f)
        self.assertEqual(setores_ruins.carregar(img), [(10, 20), (20, 30)])

    def test_arquivo_udf_com_setor_ilegivel_sai_parcial_com_zeros_so_ali_e_as_lacunas_batem(self):
        dados = os.urandom(10 * S + 100)
        img = iu.disco_udf([iu.Sessao({'filmes/clipe.bin': dados}, 0, alt_fim=999)], 1000)
        caminho = os.path.join(self.trabalho, 'dvd.img')
        with open(caminho, 'rb' if False else 'wb') as f:
            f.write(img)
        with leitor.abrir(caminho) as l:
            v = sa.detectar(l)
            ent = next(e for e in v.varrer() if e.nome == 'clipe.bin')
        (off, n), = ent.runs
        ruim = (off // S + 4, off // S + 5)                       # o 5º setor do arquivo
        with open(caminho, 'r+b') as f:                         # simula a imagem que o job `imagem` deixaria
            f.seek(ruim[0] * S)
            f.write(bytes(S))
        with open(caminho + '.mapa.json', 'w', encoding='utf-8') as f:
            json.dump({'faixas': [[0, ruim[0] * S, 'ok'], [ruim[0] * S, ruim[1] * S, 'ruim'],
                                  [ruim[1] * S, 1000 * S, 'ok']]}, f)
        achado, motivo = arquivos_sa.achado_de(ent)
        self.assertIsNone(motivo)
        with leitor.abrir(caminho) as l:
            l.ruins = setores_ruins.carregar(caminho)
            tmp = os.path.join(self.trabalho, 'saida.parte')
            resultado, feitos, sha = recuperar._copiar(l, achado, tmp, CtrlFalso())
            lacunas = l.lacunas_do_arquivo
        self.assertEqual((resultado, feitos), ('com_falhas', len(dados)))
        esperado = bytearray(dados)
        esperado[4 * S:5 * S] = bytes(S)
        with open(tmp, 'rb') as f:
            self.assertEqual(f.read(), bytes(esperado))
        self.assertEqual(lacunas, [(4 * S, S)])
        self.assertEqual(sha, hashlib.sha256(bytes(esperado)).hexdigest())
        destino = os.path.join(self.trabalho, 'out')
        os.makedirs(destino)
        setores_ruins.anotar(destino, 'Videos/clipe.bin', lacunas)
        with open(os.path.join(destino, 'lacunas.csv'), encoding='utf-8') as f:
            self.assertEqual(f.read().splitlines(), ['arquivo_recuperado,deslocamento,bytes', 'Videos/clipe.bin,%d,%d' % (4 * S, S)])

    def test_arquivo_longe_do_setor_ruim_sai_ok(self):
        dados = os.urandom(3 * S)
        caminho = os.path.join(self.trabalho, 'ok.bin')
        with open(caminho, 'wb') as f:
            f.write(b'\0' * 10 * S + dados)
        with open(caminho + '.mapa.json', 'w', encoding='utf-8') as f:
            json.dump({'faixas': [[0, 5 * S, 'ruim'], [5 * S, 13 * S, 'ok']]}, f)
        with leitor.abrir(caminho) as l:
            l.ruins = setores_ruins.carregar(caminho)
            resultado, feitos, _ = recuperar._copiar(l, {'tamanho': len(dados), 'offset': 10 * S}, os.path.join(self.trabalho, 't.parte'), CtrlFalso())
            self.assertEqual((resultado, l.lacunas_do_arquivo), ('ok', []))


if __name__ == '__main__':
    unittest.main()
