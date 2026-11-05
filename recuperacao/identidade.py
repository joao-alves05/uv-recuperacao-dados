# -*- coding: utf-8 -*-
"""Identidade da origem: "este ainda é o mesmo disco/arquivo de quando a busca começou?".

Os trabalhos guardam a origem pelo caminho (PhysicalDrive2 do Windows, /dev/sdb, um arquivo). Se o USB cai e o sistema devolve
o mesmo número a outro disco, recuperar pelo caminho copiaria bytes de outra pessoa como se fossem os achados. Por isso, ao
CRIAR uma imagem ou uma varredura gravamos uma impressão da origem (tamanho + SHA-256 do primeiro e do último MiB) e
conferimos antes de recuperar, de mostrar prévia e de retomar. Só lê, pelo Leitor. O número de série do dispositivo
(IOCTL) não entra: o tamanho e as duas pontas bastam para distinguir discos e arquivos diferentes.
"""
import hashlib

from . import leitor

PONTA = 1024 * 1024
MSG_MUDOU = ('A origem não é mais a mesma de quando este trabalho começou (outro disco no mesmo caminho, ou o conteúdo '
             'mudou). Por segurança, nada foi lido nem recuperado. Refaça a busca com a origem atual.')


def impressao(caminho):
    """{"tamanho", "inicio", "fim"} da origem, ou None se não dá para abri-la (o trabalho vai falhar por si)."""
    try:
        with leitor.abrir(caminho) as l:
            tam = l.tamanho
            return {'tamanho': tam,
                    'inicio': hashlib.sha256(l.ler(0, PONTA)).hexdigest(),
                    'fim': hashlib.sha256(l.ler(max(0, tam - PONTA), PONTA)).hexdigest()}
    except (OSError, ValueError):
        return None


def conferir(caminho, guardada):
    """Frase de erro se a origem de hoje não é a de `guardada`; None se é a mesma (ou se nada foi guardado)."""
    if not isinstance(guardada, dict) or not guardada:
        return None
    agora = impressao(caminho)
    if agora is None:
        return 'Não consegui abrir a origem para conferir se ainda é a mesma. Ela está conectada?'
    if any(agora.get(k) != guardada.get(k) for k in ('tamanho', 'inicio', 'fim')):
        return MSG_MUDOU
    return None


def da_origem_do_job(store, job):
    """A impressão que vale para ler a origem de `job` (a do próprio job, ou a da varredura que o `recuperar` usa)."""
    opcoes = job.get('opcoes') or {}
    if job['tipo'] == 'recuperar':
        varredura = store.obter(opcoes.get('varredura')) if isinstance(opcoes.get('varredura'), str) else None
        opcoes = (varredura or {}).get('opcoes') or {}
    return opcoes.get('impressao_origem')
