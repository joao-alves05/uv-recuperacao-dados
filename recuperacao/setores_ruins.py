# -*- coding: utf-8 -*-
"""Setores não lidos de uma imagem (fase 6): cruza o mapa `<imagem>.mapa.json` com os trechos de um arquivo.

A imagem de uma mídia com defeito tem zeros nos setores ruins e faixas `ruim` no mapa. Quem recupera arquivos dessa
imagem usa `carregar(imagem)` para saber quais faixas não foram lidas e `cortes(faixas, offset, n)` para dizer, por
pedaço copiado, quais bytes são zeros de verdade (não lidos). `anotar` grava essas lacunas em `lacunas.csv` no
destino, para o relatório dizer quais trechos de cada arquivo faltam. Só lê o mapa; escreve só no destino.
"""
import bisect
import csv
import json
import os

NAO_LIDO = ('ruim', 'suspeito', 'pulado', 'pendente')
NOME_CSV = 'lacunas.csv'
MAX_MAPA = 64 * 1024 * 1024          # teto do .mapa.json lido: arquivo maior que isso não é um mapa nosso


def carregar(imagem):
    """[(inicio, fim)] das faixas não lidas do mapa ao lado de `imagem`, em ordem; [] se não há mapa legível."""
    try:
        with open(imagem + '.mapa.json', encoding='utf-8') as f:
            texto = f.read(MAX_MAPA + 1)
        if len(texto) > MAX_MAPA:
            return []
        faixas = json.loads(texto)['faixas']
        return [(int(a), int(b)) for a, b, e in faixas if e in NAO_LIDO]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def cortes(faixas, offset, n):
    """Partes de [offset, offset+n) que caem em faixas não lidas: [(inicio, fim)] absolutos, em ordem."""
    fim, out = offset + n, []
    # faixas em ordem e sem sobreposição: o primeiro candidato é achado por bisseção, não varrendo a lista toda
    i = bisect.bisect_right(faixas, (offset, float('inf'))) - 1
    for a, b in faixas[max(i, 0):]:
        if b <= offset:
            continue
        if a >= fim:
            break
        out.append((max(a, offset), min(b, fim)))
    return out


def anotar(destino, arquivo, lacunas):
    """Acrescenta as lacunas [(deslocamento no arquivo, bytes)] de `arquivo` a `lacunas.csv` do destino."""
    caminho = os.path.join(destino, NOME_CSV)
    novo = not os.path.exists(caminho)
    with open(caminho, 'a', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        if novo:
            w.writerow(['arquivo_recuperado', 'deslocamento', 'bytes'])
        for desloc, n in lacunas:
            w.writerow([("'" + arquivo) if arquivo[:1] in ('=', '+', '-', '@') else arquivo, desloc, n])
