# -*- coding: utf-8 -*-
"""Item #6: `opcoes.todas_sessoes` do job `varredura` chega até o reconhecimento do UDF (docs/FASE6.md)."""
import os
import unittest

import imagens_udf as iu
from test_varredura import BaseVarredura

FRASE = 'A opção todas_sessoes aceita só verdadeiro ou falso.'


class TestTodasSessoes(BaseVarredura):
    def imagem(self):
        antiga = iu.Sessao({'antigo.txt': b'a' * 300}, 0, rotulo='SESSAO1')
        nova = iu.Sessao({'novo.txt': b'n' * 300, 'antigo.txt': b'a' * 300}, 600, rotulo='SESSAO2')
        caminho = os.path.join(self.trabalho, 'multi.udf')
        with open(caminho, 'wb') as f:
            f.write(iu.disco_udf([antiga, nova], 1500))
        return caminho

    def caminhos(self, job):
        self.esperar_estado(job['id'], 'concluido')
        return sorted(a['caminho'] for a in self.achados_do_banco(job['id']))

    def test_com_a_opcao_acha_a_ultima_sessao(self):
        job = self.varrer(self.imagem(), 'sistema', todas_sessoes=True)
        self.assertEqual(self.caminhos(job), ['/antigo.txt', '/novo.txt'])

    def test_sem_a_opcao_continua_como_antes(self):
        self.assertEqual(self.caminhos(self.varrer(self.imagem(), 'sistema')), ['/antigo.txt'])
        self.assertEqual(self.caminhos(self.varrer(self.imagem(), 'sistema', todas_sessoes=False)), ['/antigo.txt'])

    def test_valor_que_nao_e_booleano_da_400(self):
        origem = self.imagem()
        for valor in ('sim', 1, 0, None, 'true'):
            r = self.req('POST', '/api/jobs', {'tipo': 'varredura', 'origem': origem,
                                               'opcoes': {'modo': 'sistema', 'todas_sessoes': valor}})
            self.assertEqual(r.status_code, 400, valor)
            self.assertIn(FRASE, str(r.get_json()), valor)


if __name__ == '__main__':
    unittest.main()
