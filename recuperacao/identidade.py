# -*- coding: utf-8 -*-
"""Identidade da origem: "este ainda é o mesmo disco/arquivo de quando a busca começou?".

Os trabalhos guardam a origem pelo caminho (PhysicalDrive2 do Windows, /dev/sdb, um arquivo). Se o USB cai e o sistema devolve
o mesmo número a outro disco, recuperar pelo caminho copiaria bytes de outra pessoa como se fossem os achados. Por isso, ao
CRIAR uma imagem ou uma varredura gravamos uma impressão da origem (arquivo: tamanho + SHA-256 do primeiro e do último MiB) e
conferimos antes de recuperar, de mostrar prévia e de retomar. Só lê, pelo Leitor. Em disco físico ou volume as pontas
mudam sozinhas (FAT, bit de sujo, dados no fim da partição), então ali a impressão é o tamanho mais identificadores que a
escrita não altera: fabricante/modelo/série (IOCTL), assinatura MBR ou GUID GPT, e o número de série do volume.
"""
import hashlib
import os
import re

from . import leitor

PONTA = 1024 * 1024
MSG_MUDOU = ('A origem não é mais a mesma de quando este trabalho começou (outro disco no mesmo caminho, ou o conteúdo '
             'mudou). Por segurança, nada foi lido nem recuperado. Refaça a busca com a origem atual.')

MSG_NAO_CONFERIU = ('Não deu para conferir por completo se a origem é a mesma (um trecho do disco não pôde ser lido); '
                    'a recuperação segue sem essa conferência.')
MSG_IMPRESSAO_ANTIGA = ('Este trabalho foi criado por uma versão que conferia a origem pelo conteúdo das pontas do disco, '
                        'que muda com o uso normal do Windows; agora só o tamanho foi conferido.')
MSG_SEM_IDENTIDADE = ('Não deu para gravar a identidade do disco; a troca de disco não será conferida.')


def _ponta(l, offset):
    """SHA-256 de uma ponta, ou None se o trecho não deu para ler (setor ruim: isso não prova que o disco é outro)."""
    try:
        return hashlib.sha256(l.ler(offset, PONTA)).hexdigest()
    except (OSError, ValueError):
        return None


def _estavel(caminho):
    """Disco físico ou volume (não óptico): as pontas mudam com o uso normal do sistema, então a impressão usa só o que não muda."""
    return leitor.eh_dispositivo(caminho) and not leitor.eh_optico(caminho)


def _descritor(caminho):
    """'fabricante|modelo|série' do disco físico no Windows (IOCTL), '' se não se aplica, None se a consulta falhou."""
    m = re.fullmatch(r'\\\\\.\\PhysicalDrive(\d+)', caminho, re.I)
    if os.name != 'nt' or not m:
        return ''
    try:
        return '|'.join(leitor.descritor_do_disco(int(m.group(1))))
    except Exception:
        return None


def _setor(l, n):
    try:
        return l.ler(n * l.setor, 512)
    except (OSError, ValueError):
        return None


def _ids_estaveis(l, caminho):
    """{'descritor', 'serie'}: o descritor do disco (fabricante+modelo+série) e o número de série do volume (boot sector)
    ou a assinatura MBR / GUID GPT do disco. None em quem não deu para ler."""
    ids = {'descritor': _descritor(caminho), 'serie': None}
    boot = _setor(l, 0)
    if boot is None or len(boot) < 512:
        return ids
    if boot[3:11] == b'NTFS    ':
        ids['serie'] = 'ntfs:' + boot[0x48:0x50].hex()
    elif boot[3:11] == b'EXFAT   ':
        ids['serie'] = 'exfat:' + boot[0x64:0x68].hex()
    elif boot[0x52:0x5A] == b'FAT32   ':
        ids['serie'] = 'fat32:' + boot[0x43:0x47].hex()
    elif boot[0x36:0x3E] in (b'FAT16   ', b'FAT12   ', b'FAT     '):
        ids['serie'] = 'fat:' + boot[0x27:0x2B].hex()
    elif boot[510:512] == b'\x55\xAA':
        if boot[0x1C2] == 0xEE:                              # MBR protetor: o disco é GPT
            gpt = _setor(l, 1)
            if gpt is not None and gpt[:8] == b'EFI PART' and len(gpt) >= 72:
                ids['serie'] = 'gpt:' + gpt[56:72].hex()
        else:
            ids['serie'] = 'mbr:' + boot[0x1B8:0x1BC].hex()
    else:
        ids['serie'] = ''                                    # leu e não há identificador (disco bruto): nada a conferir
    return ids


def impressao(caminho):
    """Arquivo: {"tamanho", "inicio", "fim"}. Disco/volume: {"tamanho", "tipo": "dispositivo", "ids": {...}}.
    None se não dá para abrir a origem (o trabalho vai falhar por si). Uma parte que não deu para ler vem como None
    (a conferência pula essa parte e avisa)."""
    try:
        with leitor.abrir(caminho) as l:
            tam = l.tamanho
            if _estavel(caminho):
                return {'tamanho': tam, 'tipo': 'dispositivo', 'ids': _ids_estaveis(l, caminho)}
            return {'tamanho': tam, 'inicio': _ponta(l, 0), 'fim': _ponta(l, max(0, tam - PONTA))}
    except (OSError, ValueError):
        return None


def completa(imp):
    if isinstance(imp, dict) and imp.get('tipo') == 'dispositivo':
        ids = imp.get('ids') or {}
        return ids.get('serie') is not None and ids.get('descritor') is not None
    return isinstance(imp, dict) and imp.get('inicio') is not None and imp.get('fim') is not None


def conferir(caminho, guardada, aviso=None):
    """Frase de erro se a origem de hoje não é a de `guardada`; None se é a mesma (ou se nada foi guardado).
    Só o tamanho diferente, ou uma parte lida nas duas vezes e diferente, reprova. Parte ilegível não bloqueia:
    chama `aviso(frase)` (se houver) e segue. Disco/volume guardado com as pontas (versão antiga) só confere o tamanho."""
    if not isinstance(guardada, dict) or not guardada:
        return None
    agora = impressao(caminho)
    if agora is None:
        return 'Não consegui abrir a origem para conferir se ainda é a mesma. Ela está conectada?'
    if agora.get('tamanho') != guardada.get('tamanho'):
        return MSG_MUDOU
    puladas = []
    if agora.get('tipo') == 'dispositivo':
        if guardada.get('tipo') != 'dispositivo':
            if aviso:
                aviso(MSG_IMPRESSAO_ANTIGA)
            return None
        for k, v in (agora.get('ids') or {}).items():
            antes = (guardada.get('ids') or {}).get(k)
            if v is None or antes is None:
                puladas.append(k)
            elif v != antes:
                return MSG_MUDOU
    else:
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
