# -*- coding: utf-8 -*-
"""Ponte entre os leitores de sistema de arquivos (`recuperacao/sa/`) e a tabela `achados` (fase 4, integração).

    for ent, volume in entradas(leitor):        # tudo que os sistemas de arquivos da origem listam, em ordem fixa
        achado, motivo = achado_de(ent, volume.prefixo)

Os offsets dos achados são absolutos na origem (os volumes de `sa/` recebem o leitor da origem e o início da
partição, então já devolvem `runs` absolutos). Só lê, pelo `Leitor`.
"""
import os
import re
import time

from . import particoes, sa

EXT_GRUPO = {}
for _grupo, _exts in (
        ('foto', 'jpg jpeg jpe png gif bmp tif tiff heic heif webp cr2 cr3 nef arw dng orf rw2 raf psd svg ico'),
        ('video', 'mp4 mov avi mkv wmv m4v 3gp mpg mpeg flv webm mts m2ts vob'),
        ('audio', 'mp3 wav m4a flac ogg oga aac wma aiff opus mid midi'),
        ('documento', 'pdf doc docx xls xlsx ppt pptx odt ods odp rtf txt csv md html htm xml json epub'),
        ('compactado', 'zip rar 7z gz tgz tar bz2 xz iso cab'),
        ('banco', 'sqlite sqlite3 db mdb accdb dbf sql bak')):
    for _e in _exts.split():
        EXT_GRUPO[_e] = _grupo

PONTUACAO = {('bom', False): 100, ('bom', True): 90, ('parcial', False): 60, ('parcial', True): 50,
             ('ruim', False): 10, ('ruim', True): 10}
_EXT_OK = re.compile(r'[A-Za-z0-9]{1,10}')


class VolumeAchado:
    """Um volume reconhecido na origem e o prefixo de caminho dele ('' se for o único)."""

    def __init__(self, volume, prefixo, numero):
        self.volume, self.prefixo, self.numero = volume, prefixo, numero

    @property
    def descricao(self):
        v = self.volume
        return '%s%s' % (v.tipo, (' "%s"' % v.rotulo) if v.rotulo else '')


def volumes(leitor, inicio=None, tamanho=None):
    """Sistemas de arquivos que a origem tem: um por partição reconhecida, ou o disco inteiro se não há tabela.
    Com `inicio` (e `tamanho`), olha só o sistema de arquivos que começa ali (uma partição já escolhida), sem ler a
    tabela de partições; os offsets continuam absolutos na origem."""
    if inicio is not None:
        try:
            vol = sa.detectar(leitor, inicio, tamanho)
        except (sa.ErroSA, OSError, ValueError):
            vol = None
        return [VolumeAchado(vol, '', 1)] if vol is not None else []
    try:
        tabela = particoes.analisar(leitor)['particoes']
    except (OSError, ValueError, KeyError):
        tabela = []
    cands = [p for p in tabela if not p.get('estendida') and not p.get('somente_listar')]
    if not cands:
        cands = [{'inicio': 0, 'tamanho': leitor.tamanho}]
    achados = []
    for p in cands:
        try:
            vol = sa.detectar(leitor, p['inicio'], p.get('tamanho'))
        except (sa.ErroSA, OSError, ValueError):
            vol = None
        if vol is not None:
            achados.append(vol)
    if len(achados) == 1:
        return [VolumeAchado(achados[0], '', 1)]
    return [VolumeAchado(v, '/[Partição %d]' % (i + 1), i + 1) for i, v in enumerate(achados)]


class Parar(Exception):
    """Levantada por quem chama (dentro de `ao_ler`) para interromper a leitura do sistema de arquivos no meio."""


def entradas(leitor, lista_volumes=None, ao_ler=None):
    """Gera (entrada, VolumeAchado) de todos os volumes, em ordem determinística (serve para retomar contando).
    `ao_ler(VolumeAchado, fração 0..1)`: chamado enquanto o volume lê as estruturas dele (hoje o NTFS, a cada pedaço
    da MFT), mesmo quando ainda não há entrada nova; pode levantar `Parar`."""
    for va in (lista_volumes if lista_volumes is not None else volumes(leitor)):
        if ao_ler is not None and hasattr(va.volume, 'ao_ler'):
            va.volume.ao_ler = lambda fracao, va=va: ao_ler(va, fracao)
        for e in va.volume.varrer():
            yield e, va


def _data(*candidatas):
    for t in candidatas:
        if t is None:
            continue
        try:
            return time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(t))
        except (OverflowError, OSError, ValueError):
            continue
    return None


def achado_de(e, prefixo=''):
    """Entrada de `sa/` -> (dict do contrato de achados, None) ou (None, motivo) quando não dá para recuperar:
    `pasta`, `sem_conteudo` (nome sem bytes recuperáveis, p.ex. ext3/ext4 apagado), `esparso` (tem buraco) ou
    `invalido` (posição negativa)."""
    if e.eh_dir:
        return None, 'pasta'
    runs = [(o, n) for o, n in e.runs if n > 0]
    if any(o is None for o, _ in runs):
        return None, 'esparso'
    if any(o < 0 for o, _ in runs):
        return None, 'invalido'                              # offset negativo: estrutura danificada, nada a ler
    cobre = sum(n for _, n in runs)
    if cobre == 0 and (e.tamanho > 0 or e.estado == 'ruim'):
        return None, 'sem_conteudo'
    estado = e.estado
    if cobre < e.tamanho and estado == 'bom':
        estado = 'parcial'
    ext = os.path.splitext(e.nome)[1].lstrip('.')
    ext = ext if _EXT_OK.fullmatch(ext) else None
    a = {'nome': e.nome[:255], 'caminho': prefixo + e.caminho, 'apagado': bool(e.apagado), 'tamanho': cobre,
         'grupo': EXT_GRUPO.get((ext or '').lower(), 'outro'), 'estado': estado,
         'pontuacao': PONTUACAO.get((estado, bool(e.apagado)), 10), 'data': _data(e.modificado, e.criado, e.acessado)}
    if ext:
        a['extensao'] = ext
        a['tipo'] = ext.upper()
    if len(runs) <= 1:
        a['offset'] = runs[0][0] if runs else 0
    else:
        a['extents'] = [[o, n] for o, n in runs]
    return a, None
