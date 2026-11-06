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

MSG_NAO_CONFERIU = ('Não deu para conferir por completo se a origem é a mesma (um trecho do disco não pôde ser lido); '
                    'a recuperação segue sem essa conferência.')
MSG_SEM_IDENTIDADE = ('Não deu para gravar a identidade do disco; a troca de disco não será conferida.')


def _ponta(l, offset):
    """SHA-256 de uma ponta, ou None se o trecho não deu para ler (setor ruim: isso não prova que o disco é outro)."""
    try:
        return hashlib.sha256(l.ler(offset, PONTA)).hexdigest()
    except (OSError, ValueError):
        return None


def impressao(caminho):
    """{"tamanho", "inicio", "fim"} da origem, ou None se não dá para abri-la (o trabalho vai falhar por si).
    Uma ponta que não deu para ler vem como None (a conferência pula essa ponta e avisa)."""
    try:
        with leitor.abrir(caminho) as l:
            tam = l.tamanho
            return {'tamanho': tam, 'inicio': _ponta(l, 0), 'fim': _ponta(l, max(0, tam - PONTA))}
    except (OSError, ValueError):
        return None


def completa(imp):
    return isinstance(imp, dict) and imp.get('inicio') is not None and imp.get('fim') is not None


def conferir(caminho, guardada, aviso=None):
    """Frase de erro se a origem de hoje não é a de `guardada`; None se é a mesma (ou se nada foi guardado).
    Só o tamanho diferente, ou uma ponta lida nas duas vezes e diferente, reprova. Ponta ilegível não bloqueia:
    chama `aviso(frase)` (se houver) e segue."""
    if not isinstance(guardada, dict) or not guardada:
        return None
    agora = impressao(caminho)
    if agora is None:
        return 'Não consegui abrir a origem para conferir se ainda é a mesma. Ela está conectada?'
    if agora.get('tamanho') != guardada.get('tamanho'):
        return MSG_MUDOU
    puladas = []
    for k in ('inicio', 'fim'):
        if agora.get(k) is None or guardada.get(k) is None:
            puladas.append(k)
        elif agora[k] != guardada[k]:
            return MSG_MUDOU
    if puladas and aviso:
        aviso(MSG_NAO_CONFERIU)
    return None


def da_origem_do_job(store, job):
    """A impressão que vale para ler a origem de `job` (a do próprio job, ou a da varredura que o `recuperar` usa)."""
    opcoes = job.get('opcoes') or {}
    if job['tipo'] == 'recuperar':
        varredura = store.obter(opcoes.get('varredura')) if isinstance(opcoes.get('varredura'), str) else None
        opcoes = (varredura or {}).get('opcoes') or {}
    return opcoes.get('impressao_origem')
