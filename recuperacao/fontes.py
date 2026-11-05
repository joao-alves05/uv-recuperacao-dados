# -*- coding: utf-8 -*-
"""Fontes: lista os discos e volumes do computador para o painel escolher a origem.

Só lê metadados do sistema (PowerShell/WMI no Windows, /sys no Linux). Nunca abre o disco em si, e muito
menos para escrita. Falha de um disco, ou do provedor inteiro, não derruba a lista.

Cada fonte: {"id", "caminho", "nome", "tamanho", "tipo", "removivel", "setor", "sistema", "volumes", "avisos", "sistema_letra"}.
`avisos` são frases de mídia (TRIM em SSD/pen drive, disquete, CD/DVD) para o painel mostrar.
`volumes` (extra) mostra o que está dentro: [{"letra", "rotulo", "sistema_arquivos", "tamanho", "livre"}].
"""
import json
import logging
import os
import re
import subprocess

from . import leitor

TIPOS = ('hd', 'ssd', 'usb', 'cartao', 'optico', 'disquete', 'arquivo', 'desconhecido')
LIMITE_SCRIPT_S = 20
NO_WINDOW = 0x08000000
# Tamanhos exatos de disquete (bytes): 1,44 MB, 720 KB, 1,2 MB, 360 KB, 320 KB, 180 KB, 160 KB
TAMANHOS_DISQUETE = frozenset([1474560, 737280, 1228800, 368640, 327680, 184320, 163840])
_DISQUETE = re.compile(r'floppy|disquete', re.I)
AVISO_TRIM = ('Este tipo de dispositivo (SSD ou pen drive) pode ter apagado de vez o que você excluiu: o próprio '
              'dispositivo zera o espaço livre (TRIM). A chance de recuperar arquivos apagados é menor.')
AVISO_OPTICO = ('CD/DVD/Blu-ray: a leitura de mídia riscada pode demorar bastante, porque a unidade tenta de novo sozinha '
                'nos trechos ruins. O programa lê o disco todo para uma imagem, pula o que não lê e volta depois; '
                'o que continuar ilegível sai como zeros e é listado no relatório.')
AVISO_DISQUETE = ('Disquete: é um disco pequeno e muito antigo. A leitura é lenta e é comum haver setores ruins; '
                  'deixe o programa tentar de novo nos pontos que falharem.')
_CARTAO = re.compile(r'\b(sd|sdhc|sdxc|mmc|micro\s?sd|card|cart[aã]o)\b', re.I)

# Um script só: só consulta metadados (CIM/Storage); nada de abrir o disco.
SCRIPT_WINDOWS = r'''
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$discos = @(Get-CimInstance Win32_DiskDrive | Select-Object Index, DeviceID, Model, Size, MediaType, InterfaceType, BytesPerSector)
$fisicos = @(Get-PhysicalDisk | Select-Object DeviceId, MediaType, BusType)
$opticos = @(Get-CimInstance Win32_CDROMDrive | Select-Object Id, Drive, Caption, MediaLoaded, Size)
$sistema = $null
try { $sistema = (Get-Partition -DriveLetter $env:SystemDrive.Substring(0,1) | Get-Disk).Number } catch {}
$volumes = @(Get-Partition | Where-Object { $_.DriveLetter } | ForEach-Object { $v = Get-Volume -DriveLetter $_.DriveLetter; [pscustomobject]@{ Disco = $_.DiskNumber; Letra = ([string]$_.DriveLetter + ':'); Rotulo = $v.FileSystemLabel; Fs = $v.FileSystem; Tamanho = $v.Size; Livre = $v.SizeRemaining } })
[pscustomobject]@{ SistemaLetra = $env:SystemDrive; Discos = $discos; Fisicos = $fisicos; Opticos = $opticos; Sistema = $sistema; Volumes = $volumes } | ConvertTo-Json -Depth 5 -Compress
'''


def _lista(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _inteiro(v, padrao=0):
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return padrao


def _texto(v, padrao=''):
    return str(v).strip() if v not in (None, '') else padrao


# --- Windows ---------------------------------------------------------------
def _executar_powershell(script):
    r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
                       capture_output=True, timeout=LIMITE_SCRIPT_S, stdin=subprocess.DEVNULL,
                       creationflags=NO_WINDOW if os.name == 'nt' else 0)
    return r.stdout.decode('utf-8', 'replace')


def _tipo_windows(d, fisico):
    interface = _texto(d.get('InterfaceType')).upper()
    midia = _texto(d.get('MediaType')).lower()
    modelo = _texto(d.get('Model'))
    barramento = _texto((fisico or {}).get('BusType')).upper()
    removivel = 'removable' in midia or 'external' in midia or interface == 'USB' or barramento == 'USB'
    if removivel and _CARTAO.search(modelo):
        return 'cartao', True
    if removivel:
        return 'usb', True
    tipo_fisico = _texto((fisico or {}).get('MediaType')).upper()
    if tipo_fisico == 'SSD':
        return 'ssd', False
    if tipo_fisico == 'HDD':
        return 'hd', False
    if 'fixed' in midia:
        return 'hd', False
    return 'desconhecido', False


def montar_windows(bruto, consultar_seek=None):
    """Transforma o JSON do script em fontes. Disco com dado quebrado é pulado; o resto segue."""
    fontes = []
    discos = _lista(bruto.get('Discos'))
    fisicos = {str(f.get('DeviceId')): f for f in _lista(bruto.get('Fisicos')) if isinstance(f, dict)}
    sistema = bruto.get('Sistema')
    letra_sistema = _texto(bruto.get('SistemaLetra')).upper()
    if not re.fullmatch(r'[A-Z]:', letra_sistema):
        letra_sistema = ''
    volumes = [v for v in _lista(bruto.get('Volumes')) if isinstance(v, dict)]
    for d in sorted((d for d in discos if isinstance(d, dict)), key=lambda d: _inteiro(d.get('Index'), 9999)):
        try:
            indice = _inteiro(d.get('Index'), -1)
            if indice < 0:
                continue
            tipo, removivel = _tipo_windows(d, fisicos.get(str(indice)))
            if tipo == 'desconhecido' and consultar_seek:
                # o Windows não disse se é SSD: pergunta ao dispositivo (DeviceSeekPenalty, só consulta)
                try:
                    penalidade = consultar_seek(indice)
                except Exception:
                    penalidade = None
                if penalidade is not None:
                    tipo = 'hd' if penalidade else 'ssd'
            dentro = [{'letra': _texto(v.get('Letra')), 'rotulo': _texto(v.get('Rotulo')),
                       'sistema_arquivos': _texto(v.get('Fs')), 'tamanho': _inteiro(v.get('Tamanho')),
                       'livre': _inteiro(v.get('Livre'))} for v in volumes if _inteiro(v.get('Disco'), -2) == indice]
            fontes.append({'id': 'disco-%d' % indice, 'caminho': '\\\\.\\PhysicalDrive%d' % indice,
                           'nome': _texto(d.get('Model'), 'Disco %d' % indice), 'tamanho': _inteiro(d.get('Size')),
                           'tipo': tipo, 'removivel': removivel, 'setor': _inteiro(d.get('BytesPerSector'), 512) or 512,
                           'sistema': sistema is not None and _inteiro(sistema, -1) == indice, 'volumes': dentro,
                           'sistema_letra': letra_sistema if sistema is not None and _inteiro(sistema, -1) == indice else ''})
        except Exception:
            logging.getLogger('recdados').exception('disco ignorado na listagem')
    for n, o in enumerate(o for o in _lista(bruto.get('Opticos')) if isinstance(o, dict)):
        try:
            fontes.append({'id': 'optico-%d' % n, 'caminho': '\\\\.\\CdRom%d' % n,
                           'nome': _texto(o.get('Caption'), 'Unidade óptica %d' % n), 'tamanho': _inteiro(o.get('Size')),
                           'tipo': 'optico', 'removivel': True, 'setor': 2048, 'sistema': False,
                           'volumes': [{'letra': _texto(o.get('Drive')), 'rotulo': '', 'sistema_arquivos': '',
                                        'tamanho': _inteiro(o.get('Size')), 'livre': 0}] if o.get('Drive') else []})
        except Exception:
            logging.getLogger('recdados').exception('unidade óptica ignorada na listagem')
    return fontes


def provedor_windows(executar=_executar_powershell, consultar_seek=leitor.seek_penalty_do_disco):
    saida = executar(SCRIPT_WINDOWS).strip()
    return montar_windows(json.loads(saida), consultar_seek) if saida else []


# --- Linux -----------------------------------------------------------------
def _ler(caminho, padrao=''):
    try:
        with open(caminho, 'r', encoding='utf-8', errors='replace') as f:
            return f.read().strip()
    except OSError:
        return padrao


def _disco_da_raiz(proc_mounts):
    """Nome do dispositivo montado em / (ex.: sda1, nvme0n1p2), ou ''."""
    for linha in _ler(proc_mounts).splitlines():
        partes = linha.split()
        if len(partes) > 1 and partes[1] == '/' and partes[0].startswith('/dev/'):
            return os.path.basename(partes[0])
    return ''


def provedor_linux(raiz_sys='/sys/block', proc_mounts='/proc/mounts', dev='/dev'):
    fontes = []
    try:
        nomes = sorted(os.listdir(raiz_sys))
    except OSError:
        return []
    raiz = _disco_da_raiz(proc_mounts)
    for nome in nomes:
        if re.match(r'(loop|ram|zram|dm-|md|nbd)', nome):
            continue
        try:
            base = os.path.join(raiz_sys, nome)
            setores = _inteiro(_ler(os.path.join(base, 'size')))
            if setores <= 0 and not nome.startswith(('sr', 'fd')):
                continue
            removivel = _ler(os.path.join(base, 'removable')) == '1'
            rotacional = _ler(os.path.join(base, 'queue', 'rotational')) == '1'
            modelo = ' '.join(x for x in (_ler(os.path.join(base, 'device', 'vendor')),
                                          _ler(os.path.join(base, 'device', 'model'))) if x)
            real = os.path.realpath(base)
            if nome.startswith('sr'):
                tipo = 'optico'
            elif nome.startswith('fd'):
                tipo = 'disquete'
            elif nome.startswith('mmcblk'):
                tipo, removivel = 'cartao', True
            elif removivel or '/usb' in real.replace('\\', '/'):
                tipo, removivel = 'usb', True
            elif _ler(os.path.join(base, 'queue', 'rotational')) == '':
                tipo = 'desconhecido'
            else:
                tipo = 'hd' if rotacional else 'ssd'
            fontes.append({'id': nome, 'caminho': os.path.join(dev, nome).replace('\\', '/'),
                           'nome': modelo or nome, 'tamanho': setores * 512, 'tipo': tipo, 'removivel': removivel,
                           'setor': _inteiro(_ler(os.path.join(base, 'queue', 'logical_block_size')), 512) or 512,
                           'sistema': bool(raiz) and raiz.startswith(nome) and (len(raiz) == len(nome) or
                                                                                  not raiz[len(nome)].isalpha()),
                           'volumes': []})
        except Exception:
            logging.getLogger('recdados').exception('disco ignorado na listagem')
    return fontes


def provedor_sistema():
    if os.name == 'nt':
        return provedor_windows()
    return provedor_linux()


# --- saída padronizada ------------------------------------------------------
def avisos_de(tipo):
    """Avisos de mídia para o painel mostrar junto da fonte."""
    return {'ssd': [AVISO_TRIM], 'usb': [AVISO_TRIM], 'optico': [AVISO_OPTICO], 'disquete': [AVISO_DISQUETE]}.get(tipo, [])


def _limpar(f):
    """Garante o formato do contrato; devolve None se não dá para aproveitar."""
    if not isinstance(f, dict) or not f.get('caminho') or not f.get('id'):
        return None
    tipo = f.get('tipo') if f.get('tipo') in TIPOS else 'desconhecido'
    tamanho = _inteiro(f.get('tamanho'))
    if tipo in ('usb', 'cartao', 'desconhecido') and (tamanho in TAMANHOS_DISQUETE or _DISQUETE.search(_texto(f.get('nome')))):
        tipo = 'disquete'                                  # reconhecido pelo tamanho exato ou pelo nome; tratado como disco pequeno
    volumes = [{'letra': _texto(v.get('letra')), 'rotulo': _texto(v.get('rotulo')),
                'sistema_arquivos': _texto(v.get('sistema_arquivos')), 'tamanho': _inteiro(v.get('tamanho')),
                'livre': _inteiro(v.get('livre'))} for v in _lista(f.get('volumes')) if isinstance(v, dict)]
    return {'id': str(f['id']), 'caminho': str(f['caminho']), 'nome': _texto(f.get('nome'), str(f['id'])),
            'tamanho': tamanho, 'tipo': tipo, 'removivel': bool(f.get('removivel')),
            'setor': _inteiro(f.get('setor'), 512) or 512, 'sistema': bool(f.get('sistema')), 'volumes': volumes, 'avisos': avisos_de(tipo),
            'sistema_letra': _texto(f.get('sistema_letra')) if f.get('sistema') else ''}


def listar(provedor=None):
    """Lista as fontes. `provedor` (injetável nos testes) devolve a lista bruta; se falhar, a lista sai vazia."""
    try:
        bruto = (provedor or provedor_sistema)()
    except Exception:
        logging.getLogger('recdados').exception('não consegui listar os discos')
        return []
    limpas = []
    for f in _lista(bruto):
        try:
            item = _limpar(f)
        except Exception:
            item = None
        if item:
            limpas.append(item)
    return limpas
