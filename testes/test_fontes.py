# -*- coding: utf-8 -*-
"""Parte 2.1: listar discos e volumes. Provedores falsos e árvore /sys falsa; nada abre disco de verdade."""
import inspect
import json
import os
import unittest
from unittest import mock

import auxiliar
from auxiliar import CasoComPasta

from recuperacao import fontes, servico

CHAVES = {'id', 'caminho', 'nome', 'tamanho', 'tipo', 'removivel', 'setor', 'sistema', 'volumes', 'avisos', 'sistema_letra'}

BRUTO_WINDOWS = {
    'Discos': [
        {'Index': 0, 'DeviceID': 'x', 'Model': 'Disco Interno A', 'Size': 500107862016, 'MediaType': 'Fixed hard disk media',
         'InterfaceType': 'SCSI', 'BytesPerSector': 512},
        {'Index': 1, 'DeviceID': 'y', 'Model': 'Disco Interno B', 'Size': 1000204886016, 'MediaType': 'Fixed hard disk media',
         'InterfaceType': 'SCSI', 'BytesPerSector': 4096},
        {'Index': 2, 'DeviceID': 'z', 'Model': 'Pen Drive 8GB USB Device', 'Size': 8000000000,
         'MediaType': 'Removable Media', 'InterfaceType': 'USB', 'BytesPerSector': 512},
        {'Index': 3, 'DeviceID': 'w', 'Model': 'Generic SD Card Reader', 'Size': 32000000000,
         'MediaType': 'Removable Media', 'InterfaceType': 'USB', 'BytesPerSector': 512},
        {'Index': 'quebrado', 'Model': 'sem indice'},
    ],
    'Fisicos': [{'DeviceId': '0', 'MediaType': 'HDD', 'BusType': 'SATA'}, {'DeviceId': '1', 'MediaType': 'SSD', 'BusType': 'NVMe'},
                {'DeviceId': '2', 'MediaType': 'Unspecified', 'BusType': 'USB'}],
    'Opticos': {'Id': 'x', 'Drive': 'E:', 'Caption': 'Gravador DVD', 'MediaLoaded': False, 'Size': None},
    'Sistema': 1,
    'SistemaLetra': 'c:',
    'Volumes': [{'Disco': 0, 'Letra': 'D:', 'Rotulo': 'Dados', 'Fs': 'NTFS', 'Tamanho': 400000000000, 'Livre': 1000},
                {'Disco': 1, 'Letra': 'C:', 'Rotulo': '', 'Fs': 'NTFS', 'Tamanho': 900000000000, 'Livre': 5}],
}


class TestWindows(unittest.TestCase):
    def setUp(self):
        self.f = {x['id']: x for x in fontes.listar(lambda: fontes.montar_windows(BRUTO_WINDOWS))}

    def test_tipos_e_campos(self):
        self.assertEqual(set(self.f), {'disco-0', 'disco-1', 'disco-2', 'disco-3', 'optico-0'})     # o disco sem índice foi pulado
        for x in self.f.values():
            self.assertEqual(set(x), CHAVES)
            self.assertIn(x['tipo'], fontes.TIPOS)
        self.assertEqual([self.f[i]['tipo'] for i in ('disco-0', 'disco-1', 'disco-2', 'disco-3', 'optico-0')],
                         ['hd', 'ssd', 'usb', 'cartao', 'optico'])
        self.assertEqual(self.f['disco-0']['caminho'], '\\\\.\\PhysicalDrive0')
        self.assertEqual(self.f['optico-0']['caminho'], '\\\\.\\CdRom0')
        self.assertEqual(self.f['disco-1']['setor'], 4096)
        self.assertEqual(self.f['optico-0']['setor'], 2048)
        self.assertEqual(self.f['optico-0']['tamanho'], 0)          # sem mídia: tamanho 0, não quebra

    def test_sistema_removivel_e_volumes(self):
        self.assertEqual({k for k, v in self.f.items() if v['sistema']}, {'disco-1'})
        self.assertEqual({k: v['sistema_letra'] for k, v in self.f.items() if v['sistema_letra']}, {'disco-1': 'C:'})
        self.assertEqual({k for k, v in self.f.items() if v['removivel']}, {'disco-2', 'disco-3', 'optico-0'})
        self.assertEqual([v['letra'] for v in self.f['disco-1']['volumes']], ['C:'])
        self.assertEqual(self.f['disco-0']['volumes'][0]['rotulo'], 'Dados')
        self.assertEqual(self.f['optico-0']['volumes'][0]['letra'], 'E:')

    def test_um_unico_disco_vindo_como_objeto(self):
        bruto = {'Discos': BRUTO_WINDOWS['Discos'][0], 'Fisicos': BRUTO_WINDOWS['Fisicos'][0], 'Sistema': 0}
        self.assertEqual([x['id'] for x in fontes.montar_windows(bruto)], ['disco-0'])

    def test_provedor_executa_so_a_consulta_e_le_o_json(self):
        recebido = []
        lista = fontes.provedor_windows(lambda script: recebido.append(script) or json.dumps(BRUTO_WINDOWS))
        self.assertEqual(len(lista), 5)
        script = recebido[0]
        self.assertIn('Get-CimInstance Win32_DiskDrive', script)
        for proibido in ('CreateFile', 'OpenRead', 'OpenWrite', 'FileStream', 'Set-', 'Clear-', 'Remove-', 'Format-Volume',
                         'Initialize-Disk', 'Out-File', 'Add-Content'):
            self.assertNotIn(proibido, script)

    def test_saida_vazia_ou_quebrada_nao_derruba(self):
        self.assertEqual(fontes.provedor_windows(lambda s: ''), [])
        self.assertEqual(fontes.listar(lambda: fontes.provedor_windows(lambda s: 'isto não é json')), [])


class TestLinux(CasoComPasta):
    def disco(self, nome, setores, rotacional='1', removivel='0', modelo='Modelo X', vendor='ATA', setor='512'):
        base = os.path.join(self.sys, nome)
        os.makedirs(os.path.join(base, 'queue'))
        os.makedirs(os.path.join(base, 'device'))
        for rel, v in (('size', str(setores)), ('removable', removivel), ('queue/rotational', rotacional),
                       ('queue/logical_block_size', setor), ('device/model', modelo), ('device/vendor', vendor)):
            with open(os.path.join(base, rel), 'w') as f:
                f.write(v + '\n')

    def test_arvore_falsa(self):
        self.sys = os.path.join(self.pasta, 'sys-block')
        os.makedirs(self.sys)
        self.disco('sda', 1000215216, rotacional='0', modelo='SSD Interno')
        self.disco('sdb', 976773168, rotacional='1', modelo='HD Dados')
        self.disco('sdc', 15564800, removivel='1', modelo='Pen')
        self.disco('mmcblk0', 62333952, rotacional='0', removivel='0', modelo='SD')
        self.disco('sr0', 0, modelo='DVD')
        self.disco('loop0', 8192)
        self.disco('ram0', 8192)
        self.disco('sde', 0)                                         # disco sem tamanho: ignorado
        mounts = os.path.join(self.pasta, 'mounts')
        with open(mounts, 'w') as f:
            f.write('/dev/sda2 / ext4 rw 0 0\nproc /proc proc rw 0 0\n')
        r = {x['id']: x for x in fontes.listar(lambda: fontes.provedor_linux(self.sys, mounts, '/dev'))}
        self.assertEqual(set(r), {'sda', 'sdb', 'sdc', 'mmcblk0', 'sr0'})
        self.assertEqual([r[i]['tipo'] for i in ('sda', 'sdb', 'sdc', 'mmcblk0', 'sr0')],
                         ['ssd', 'hd', 'usb', 'cartao', 'optico'])
        self.assertEqual(r['sda']['tamanho'], 1000215216 * 512)
        self.assertEqual(r['sda']['caminho'], '/dev/sda')
        self.assertEqual(r['sda']['nome'], 'ATA SSD Interno')
        self.assertEqual({k for k, v in r.items() if v['sistema']}, {'sda'})
        self.assertTrue(r['sdc']['removivel'] and r['mmcblk0']['removivel'])
        self.assertFalse(r['sdb']['removivel'])

    def test_sem_sys(self):
        self.assertEqual(fontes.provedor_linux(os.path.join(self.pasta, 'nao-existe')), [])


class TestListar(unittest.TestCase):
    def test_provedor_que_explode_da_lista_vazia(self):
        def ruim():
            raise RuntimeError('powershell sumiu')
        self.assertEqual(fontes.listar(ruim), [])

    def test_item_ruim_nao_derruba_os_outros(self):
        bruto = [None, 'texto', {'id': 'a'}, {'id': 'b', 'caminho': '/dev/b', 'tamanho': 'muito', 'tipo': 'inventado',
                                              'setor': 0, 'volumes': 'x'},
                 {'id': 'c', 'caminho': '/dev/c', 'tamanho': 10, 'tipo': 'usb', 'removivel': 1}]
        r = fontes.listar(lambda: bruto)
        self.assertEqual([x['id'] for x in r], ['b', 'c'])
        self.assertEqual((r[0]['tipo'], r[0]['tamanho'], r[0]['setor'], r[0]['volumes']), ('desconhecido', 0, 512, []))
        self.assertIs(r[1]['removivel'], True)

    def test_modulo_nao_abre_nada_com_escrita(self):
        fonte = inspect.getsource(fontes)
        for proibido in ('CreateFile', 'GENERIC_WRITE', 'O_WRONLY', 'O_RDWR', "'wb'", "'w'", "'r+b'", "'a'"):
            self.assertNotIn(proibido, fonte.replace("'w', encoding", ''), proibido)
        self.assertNotIn('os.open(', fonte)

    def test_listagem_real_so_le_metadados_e_nunca_levanta(self):
        """Roda o provedor do sistema deste computador (PowerShell/WMI ou /sys). Só confere o formato."""
        sem_seek = (lambda: fontes.provedor_windows(consultar_seek=None)) if os.name == 'nt' else None
        r = fontes.listar(sem_seek)
        self.assertIsInstance(r, list)
        for x in r:
            self.assertEqual(set(x), CHAVES)


class TestRota(auxiliar.CasoComPasta):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {'RECDADOS_DADOS': self.dados, 'RECDADOS_PORTA': str(auxiliar.porta_livre())})
        p.start()
        self.addCleanup(p.stop)

    def cliente(self, provedor):
        ctx = servico.Contexto(provedor_fontes=provedor)
        self.addCleanup(ctx.store.fechar)
        return ctx, servico.criar_app(ctx).test_client(), 'http://127.0.0.1:%d' % ctx.porta

    def test_exige_sessao_e_usa_o_provedor_injetado(self):
        ctx, c, base = self.cliente(lambda: fontes.montar_windows(BRUTO_WINDOWS))
        self.assertEqual(c.get('/api/fontes', base_url=base).status_code, 401)
        r = c.get('/api/fontes', base_url=base, headers={'X-RecDados-Token': ctx.token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x['id'] for x in r.get_json()], ['disco-0', 'disco-1', 'disco-2', 'disco-3', 'optico-0'])
        self.assertEqual(set(r.get_json()[0]), CHAVES)

    def test_provedor_quebrado_responde_lista_vazia(self):
        def ruim():
            raise OSError('sem acesso')
        ctx, c, base = self.cliente(ruim)
        r = c.get('/api/fontes', base_url=base, headers={'X-RecDados-Token': ctx.token})
        self.assertEqual((r.status_code, r.get_json()), (200, []))


if __name__ == '__main__':
    unittest.main()
