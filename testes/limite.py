# -*- coding: utf-8 -*-
"""Só para teste: roda uma função com prazo, para um laço sem fim virar falha em vez de travar a suíte."""
import threading


def com_limite(funcao, segundos=15):
    res = {}

    def alvo():
        try:
            res['valor'] = funcao()
        except BaseException as e:                    # devolve a exceção para o teste decidir
            res['erro'] = e
    t = threading.Thread(target=alvo, daemon=True)
    t.start()
    t.join(segundos)
    if t.is_alive():
        raise AssertionError('travou: a função não terminou em %s s' % segundos)
    if 'erro' in res:
        raise res['erro']
    return res.get('valor')
