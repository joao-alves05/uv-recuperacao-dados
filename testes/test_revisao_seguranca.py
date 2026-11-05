# -*- coding: utf-8 -*-
"""Teto de página nas rotas de achados e desempenho/limite do mapa de setores ruins."""
import json
import os
import time
import unittest
from unittest import mock

from auxiliar import CasoComPasta, porta_livre

from recuperacao import setores_ruins, servico


class TestPaginaComTeto(CasoComPasta):
    def test_pagina_gigante_e_400_e_nao_500(self):
        porta = porta_livre()
        with mock.patch.dict(os.environ, {'RECDADOS_DADOS': self.dados, 'RECDADOS_PORTA': str(porta)}):
            ctx = servico.Contexto()
            self.addCleanup(ctx.store.fechar)
            c = servico.criar_app(ctx).test_client()
            jobs = {'achados': ctx.store.criar('varredura', os.path.join(self.trabalho, 'o.bin'), os.path.join(self.trabalho, 'd'), {}),
                    'recuperados': ctx.store.criar('recuperar', os.path.join(self.trabalho, 'o.bin'), os.path.join(self.trabalho, 'e'), {})}
            h = {'X-RecDados-Token': ctx.token}
            base = 'http://127.0.0.1:%d' % porta
            for rota, job in jobs.items():
                for pagina in ('99999999999999999999999', str(10 ** 18), '1000001'):
                    r = c.get('/api/jobs/%s/%s?pagina=%s' % (job['id'], rota, pagina), headers=h, base_url=base)
                    self.assertEqual(r.status_code, 400, (rota, pagina, r.status_code))
                r = c.get('/api/jobs/%s/%s?pagina=1' % (job['id'], rota), headers=h, base_url=base)
                self.assertEqual(r.status_code, 200, rota)


class TestSetoresRuins(CasoComPasta):
    def test_cortes_com_muitas_faixas_e_rapido_e_igual_ao_calculo_linear(self):
        faixas = [(i * 4096, i * 4096 + 2048) for i in range(200_000)]

        def linear(off, n):
            return [(max(a, off), min(b, off + n)) for a, b in faixas if b > off and a < off + n]
        for off, n in ((0, 10), (4096 * 150_000 + 100, 9000), (4096 * 199_999, 99999), (10 ** 12, 5), (2048, 2048)):
            self.assertEqual(setores_ruins.cortes(faixas, off, n), linear(off, n), (off, n))
        t = time.perf_counter()
        for k in range(2000):
            setores_ruins.cortes(faixas, 4096 * (190_000 + k % 5000), 1024)
        self.assertLess(time.perf_counter() - t, 1.0)       # busca linear levava vários segundos aqui

    def test_mapa_enorme_e_ignorado(self):
        img = os.path.join(self.trabalho, 'g.img')
        with open(img + '.mapa.json', 'w', encoding='utf-8') as f:
            json.dump({'faixas': [[0, 10, 'ruim']]}, f)
        self.assertEqual(setores_ruins.carregar(img), [(0, 10)])
        with mock.patch.object(setores_ruins, 'MAX_MAPA', 5):
            self.assertEqual(setores_ruins.carregar(img), [])


if __name__ == '__main__':
    unittest.main()
