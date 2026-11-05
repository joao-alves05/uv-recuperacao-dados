# Fase 5: achar e recuperar arquivos (carving) + a lista de resultados

Vem antes das fases 3 e 4 (decisão de 04/11): é o que faz o botão "Recuperar arquivos" funcionar para pen drive e cartão. Regras de sempre: origem só leitura (pelo `Leitor`), destino em outro disco (`unidade.conferir`), testes só com imagens sintéticas, nenhum dado de empresa.

## 5.1 Tabela de assinaturas (`recuperacao/assinaturas.json`)

Extensível sem mexer no código. Cada tipo: `{"tipo", "grupo" (foto, video, audio, documento, compactado, banco, outro), "extensao", "cabecalho" (hex, com offset opcional), "rodape" (hex, opcional), "tamanho_max", "validar" (nome do validador)}`. Tipos mínimos: JPG, PNG, GIF, BMP, TIFF/RAW de câmera (CR2, NEF, ARW, DNG via TIFF), HEIC/MP4/MOV (caixas ISO BMFF: `ftyp` + percorrer as caixas para achar o tamanho), AVI/WAV (RIFF com tamanho no cabeçalho), MP3 (ID3 + quadros), PDF (`%PDF` ... `%%EOF`), ZIP e derivados (DOCX/XLSX/PPTX/ODT pelo conteúdo do ZIP, percorrendo o diretório central), RAR, 7z, SQLite (cabeçalho + tamanho da página × páginas), DOC/XLS antigos (OLE2), RTF, TXT não (muito falso positivo).

## 5.2 Motor (`recuperacao/carving.py`)

- Varre a origem (disco, volume, imagem ou arquivo) em blocos grandes, alinhado ao setor, procurando cabeçalhos; para cada achado, descobre o fim pelo melhor método do tipo (tamanho no cabeçalho > estrutura interna > rodapé > tamanho máximo).
- Arquivos contíguos (o caso comum em pen drive e cartão). Fragmentação: só a heurística simples do PhotoRec para JPG (se a validação falha, tentar pular blocos do sistema de arquivos) se der tempo; senão, marcar "parcial".
- Cada achado vai para a tabela `achados` assim que é descoberto: `job_id, nome (gerado: f0001234.jpg), tipo, grupo, tamanho, offset, estado (bom | parcial | ruim), data (EXIF/metadados quando houver), largura/altura para foto, pontuacao`.
- Validação pós-achado (`recuperacao/validar.py`): JPG decodifica marcadores até EOI; PNG confere CRC dos chunks; ZIP/Office abre o diretório central; PDF tem `%%EOF` e xref; MP4 caixas coerentes. Estado e pontuação saem daí.
- Job novo `varredura` (rápida = só cabeçalhos alinhados ao setor; profunda = todo byte) com checkpoint (offset já varrido) e retomada, como a imagem.
- Prévia: `GET /api/achados/<id>/previa` devolve miniatura de foto (sem dependência nova: só JPG/PNG/GIF/BMP servidos como estão, até 2 MB, e o painel reduz; o resto devolve ícone pelo grupo). Lê da origem só leitura.

## 5.3 API de resultados

- `GET /api/jobs/<id>/achados?grupo=&tipo=&estado=&min=&max=&de=&ate=&busca=&ordem=nome|tamanho|data|tipo&sentido=asc|desc&pagina=&por_pagina=` → `{total, pagina, itens:[...], resumo:{por_grupo:{foto: n, ...}}}`. Paginado (milhares de achados).
- `POST /api/jobs/<id>/recuperar` `{ids:[...]|"todos", filtros:{...}, destino:"G:\\Recuperados"}` → cria job `recuperar` (retomável) que copia cada achado da origem para o destino, organizado por grupo (`Fotos/`, `Videos/`...), com nomes únicos, sem nunca gravar na origem (`unidade.conferir`), e marca no achado `recuperado_em`. Relatório final `relatorio.html` + `relatorio.csv` no destino (bom/parcial/ruim, tamanho, tipo).

## 5.4 Painel

- "Recuperar arquivos" num volume: escolher "Rápida" ou "Profunda" (explicado em 1 frase cada) → job de varredura com progresso ao vivo; os achados aparecem enquanto varre.
- Lista de resultados (pedido do João): caixas de seleção, **selecionar tudo** (e por grupo/tipo), **ordenar por nome, tamanho, data e tipo, crescente e decrescente** clicando na coluna, filtros por grupo, tipo, tamanho, data e estado, busca por nome, abas por grupo com contagem, miniatura nas fotos (grade ou lista), estado de cada arquivo (bom/parcial/ruim, como o Recuva), botão **"Recuperar selecionados"** com o destino sugerido em outro disco, e depois "Abrir pasta" e o relatório.
- Tudo de terceiros por `textContent`; a miniatura por `<img src>` da rota própria (mesma origem, CSP já permite `img-src 'self' data:`).

## 5.5 Testes

Imagem sintética montada no teste: FAT32 de 8 MB criada em Python (sem ferramenta externa), com JPG, PNG, PDF, DOCX, ZIP, MP4 pequenos gravados e depois "apagados" (entrada de diretório com 0xE5), mais um JPG cortado (parcial) e lixo. A varredura acha todos com o tamanho certo, os bytes recuperados batem com o SHA-256 dos originais, o parcial sai "parcial", a origem não muda, a lista filtra/ordena/pagina, e a recuperação recusa destino no mesmo disco.
