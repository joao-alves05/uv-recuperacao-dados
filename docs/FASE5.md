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

---

# Contrato implementado (back-end)

## Tabela `achados` (para a fase 5 e para a fase 4)

Quem grava achado usa **`JobStore.adicionar_achados(job_id, lista, checkpoint=None)`** (`recuperacao/jobs.py`). Devolve os ids novos. Tudo numa transação: se um achado for inválido (`ValueError`), nada do lote é gravado. Se vier `checkpoint` (um dict), ele é gravado na mesma transação (tabela `checkpoints`, lida com `obter_checkpoint(job_id)`), então o que foi achado e o ponto de retomada nunca divergem. Bancos antigos da fase 1 ganham as colunas novas ao abrir (`ALTER TABLE`, sem perder linhas).

Cada achado é um dict. Campos (os de `*` são obrigatórios):

| Campo | Tipo | Significado |
|---|---|---|
| `tamanho`* | int ≥ 0 | bytes do arquivo |
| `offset` ou `extents`* | int / lista | onde estão os bytes **na origem do job** (offset absoluto na origem, não relativo à partição). Contíguo: `offset`. Fragmentado: `extents: [[offset, comprimento], ...]` (a soma tem de dar `tamanho`; `offset` passa a ser o do primeiro extent) |
| `nome` | texto | nome mostrado. Se faltar: nome do `caminho`, senão `f<setor de 512 bytes, 7 dígitos>.<ext>` (ex.: `f0001234.jpg`) |
| `caminho` | texto ou nulo | caminho original (fase 4). O carving deixa nulo |
| `apagado` | bool ou nulo | `true` apagado, `false` existente, **nulo = não se sabe** (carving) |
| `grupo` | `foto` `video` `audio` `documento` `compactado` `banco` `outro` | padrão `outro` |
| `tipo` | texto até 40 | `JPG`, `DOCX`... (padrão: a extensão em maiúsculas) |
| `extensao` | letras/números até 10, sem ponto | usada no nome e na prévia |
| `estado` | `bom` `parcial` `ruim` | padrão `bom` |
| `pontuacao` | 0 a 100 ou nulo | confiança |
| `data` | `AAAA-MM-DD` ou `AAAA-MM-DD HH:MM:SS` ou nulo | metadados/sistema de arquivos; ordena como texto |
| `largura`, `altura` | int ou nulo | só fotos |
| `recuperado_em` | segundos desde 1970 ou nulo | preenchido pelo job `recuperar`; **quem grava achado não preenche** |

`achados.obter(store, id)` devolve o dict com `extents` já como lista e `apagado` como bool/nulo; `achados.extents_de(achado)` dá `[(offset, comprimento)]`; `achados.ler_achado(leitor, achado, maximo=None)` gera os bytes lendo da origem (só leitura). Esses dois últimos são o jeito de ler um achado: o job `recuperar` e a prévia usam as mesmas funções, então achados da fase 4 (com `caminho`, `apagado` e `extents`) funcionam sem código novo.

## Tabela de assinaturas (`recuperacao/assinaturas.json`)

`{"assinaturas": [ {tipo, grupo, extensao, cabecalho (hex), cabecalho_offset?, rodape? (hex), tamanho_max (bytes), validar?, forma?} ]}`. Tipo novo = linha nova. Sem `validar`, vale o `rodape` (o arquivo vai do cabeçalho até o primeiro rodapé; pontuação 60). Tabela inválida é recusada com frase clara (`ErroAssinaturas`). Os validadores (`recuperacao/validar.py`) podem trocar `tipo`, `extensao` e `grupo` pelo que acharem dentro (TIFF vira CR2/NEF/ARW/DNG; ZIP vira DOCX/XLSX/PPTX/ODT/ODS/ODP/EPUB/JAR/APK; OLE2 vira DOC/XLS/PPT; MP4 vira MOV/M4A/HEIC). Tipos prontos: JPG, PNG, GIF, BMP, TIFF e RAW, MP4/MOV/M4A/HEIC, AVI, WAV, MP3 (começando em ID3), PDF, ZIP e derivados, RAR 4 e 5, 7z, SQLite, OLE2, RTF.

Estado e pontuação: `bom` = estrutura inteira e coerente (100, ou 90 nos tipos de conferência mais fraca, como MP3 e RTF); `parcial` = começo válido e fim ausente (o `tamanho` é o trecho válido, pontuação 30 a 60); `ruim` = inteiro mas com conferência falha (CRC errado, JPG sem imagem; pontuação 10 a 20). O que não passa nem na conferência mínima não vira achado.

## Job `varredura`

`POST /api/jobs` `{"tipo": "varredura", "origem": "<arquivo, imagem, disco ou volume>", "opcoes": {"modo": "rapida" | "profunda", "bloco": 4096..67108864}}` (o `destino` é ignorado; o job usa `varredura#<hash da origem>`, então duas varreduras da mesma origem ao mesmo tempo dão 409). `rapida` só considera cabeçalhos no começo de um setor; `profunda` considera todo byte. Blocos de 4 MiB (opção `bloco`, que existe para os testes).

- Cada bloco entrega seus achados, gravados na hora (o painel os vê enquanto a varredura anda) junto com o checkpoint `{pos, tamanho, modo, ruins}`. Pausar, cair o serviço e retomar continua do último checkpoint, sem perder nem repetir achado.
- Depois de um achado `bom` ou `parcial` a varredura pula para o fim dele (não acha o que está embutido, como um JPG dentro de um ZIP guardado). Um `parcial` termina onde começa outro arquivo que valida inteiro (ele não engole o vizinho).
- Setor ilegível vira zeros e é contado em `setores_ruins`. Nada é escrito na origem.
- Limitações: arquivo fragmentado sai `parcial` ou `ruim` (a heurística do PhotoRec para JPG não foi feita); ZIP cortado cujo resto foi sobrescrito por zeros não é detectado (os tamanhos do cabeçalho local parecem coerentes); ZIP64 só é achado se tiver diretório central comum ou descritor de dados; cabeçalhos de OLE2 com mais de 109 setores de FAT usam a cadeia DIFAT, mas não foi testado com arquivo grande; MP4 com `moov` perdido sai `parcial`.
- Fase 4: o modo "pelo sistema de arquivos" entra como novo valor de `modo` neste mesmo job, gravando em `adicionar_achados`.

## Prévia

`GET /api/achados/<id>/previa`: foto `JPG/PNG/GIF/BMP` de até 2 MB sai como está (o `Content-Type` vem dos primeiros bytes, não do nome); qualquer outra coisa (outro grupo, foto maior, bytes que não são imagem) sai um ícone SVG do grupo com a extensão (`image/svg+xml`). 404 para id inexistente.

## Testes desta parte

`testes/test_carving.py` (motor e validadores, imagens na memória), `testes/test_varredura.py` (job, FAT sintética de 8 MiB em `testes/fat_sintetica.py`, tabela de achados, prévia, queda do serviço). Os arquivos de exemplo vêm de `testes/amostras.py`. A "FAT32" da imagem sintética não é válida para o Windows (poucos clusters); o carving não a lê.

## API de achados (5.3)

`GET /api/jobs/<id>/achados?grupo=&tipo=&estado=&min=&max=&de=&ate=&busca=&ordem=&sentido=&pagina=&por_pagina=` (só jobs `varredura`; 404 se não existe, 409 se é de outro tipo). `grupo`, `tipo` e `estado` aceitam lista separada por vírgula. `min`/`max` em bytes; `de`/`ate` são datas `AAAA-MM-DD` e o dia final conta inteiro; `busca` procura no nome e no caminho original (sem diferenciar maiúsculas, `%` e `_` são texto). `ordem` = `nome` | `tamanho` | `data` | `tipo` (padrão `nome`), `sentido` = `asc` | `desc`; achado sem data vai sempre para o fim; desempate por id. `por_pagina` de 1 a 500 (padrão 50). Resposta: `{total, pagina, por_pagina, itens:[{...campos do contrato..., previa}], resumo:{por_grupo:{foto: n, ...}}}`. O `resumo` respeita todos os filtros **menos o de grupo**, para as abas mostrarem a contagem de cada grupo. Parâmetro inválido = 400 com frase em português.

## Job `recuperar`

`POST /api/jobs/<id_da_varredura>/recuperar` com `{"ids": [1,2,3] | "todos", "filtros": {...mesmos nomes acima...}, "destino": "<pasta>"}` → 201 com o job (`tipo: "recuperar"`, `origem` = a da varredura, `destino` = a pasta, `opcoes: {varredura, total}`). Com `"todos"` valem os `filtros` (a seleção é congelada na hora do pedido); com lista, só os ids (todos precisam ser dessa varredura). Recusas (400): destino vazio, arquivo, caminho de rede, a própria origem ou dentro dela; origem de disco/volume com destino no mesmo disco (`unidade.conferir`) ou sem como saber; nada escolhido; id de outra varredura. 409 se já há trabalho em andamento para o mesmo destino. Esse tipo só nasce por esta rota (`POST /api/jobs` com `tipo: "recuperar"` dá 400).

- Cada achado vira `<destino>/<Fotos|Videos|Audios|Documentos|Compactados|Bancos de dados|Outros>/<nome>`. O nome é limpo (sem caminho, sem `<>:"/\|?*`, sem nome reservado do Windows) e, se já existir no destino ou for repetido, ganha ` (2)`, ` (3)`. Nada existente é sobrescrito.
- Grava em `<arquivo>.parte` e renomeia só no fim. Leitura que falha vira zeros e o resultado fica `com trechos ilegíveis`; erro de escrita no destino falha o job (retomável). Os bytes lidos vêm de `achados.extents_de`, então achados da fase 4 funcionam.
- Retomável: a lista de ids fica no checkpoint e o que cada achado virou na tabela `recuperacoes (job_id, achado_id, ordem, arquivo, resultado, bytes, sha256, quando)`. Pausar, cancelar, cair e retomar pula o que está `ok` e refaz o que ficou `copiando` (mesmo nome; se o arquivo já estiver lá idêntico, aceita).
- Ao concluir cada arquivo, grava `achados.recuperado_em`.
- No fim: `relatorio.html` e `relatorio.csv` (UTF-8 com BOM) no destino, com arquivo recuperado, nome, caminho original, grupo, tipo, estado, pontuação, tamanho, resultado e SHA-256. Se `relatorio.*` já existir (de outro trabalho), usa `relatorio-<id do job>.*`; nunca sobrescreve. Célula de planilha que começa com `= + - @` ganha `'` na frente; o HTML escapa tudo.
- Testes: `testes/test_recuperar.py`.

### Detalhes para o painel (5.4)

- `pasta=<caminho>` em `/achados`: só os achados cujo `caminho` original está nessa pasta ou em subpastas (`\` e `/` valem igual, maiúsculas não importam; `C:/Fotos` não pega `C:/Fotos2`). Achados do carving não têm caminho e saem quando o filtro de pasta está ligado. É o filtro que a árvore da fase 4 usa.
- `resumo` = `{por_grupo, tamanho_total}`. `por_grupo` ignora **só** o filtro de grupo (as abas não somem); `tamanho_total` é a soma dos bytes de tudo o que passa por **todos** os filtros (inclusive o de grupo), para avisar "não cabe no destino" antes de recuperar com `"todos"`.
- `por_pagina` vai de 1 a 500 (100 vale). `tipo` vem em maiúsculas (`JPG`) e `data` como `AAAA-MM-DD` ou `AAAA-MM-DD hh:mm:ss`.
- `ids: "todos"` só vale com os `filtros`; para "todos menos estes", mande a lista de ids (limite de 200000 por pedido).
- `POST /api/jobs/<id_do_recuperar>/abrir`: abre a pasta de destino do job no Explorer (`os.startfile`; o caminho vem do job gravado, nunca do pedido). 409 fora do Windows, se a pasta ainda não existe ou se o job não é `recuperar`.
- `GET /api/jobs/<id_do_recuperar>/relatorio`: serve o `relatorio.html` desse job (o nome certo fica no checkpoint, mesmo quando saiu como `relatorio-<id>.html`). O CSS embutido do arquivo é trocado por `<link>` para `/api/jobs/<id>/relatorio.css`, porque a política do serviço (`style-src 'self'`) não aceita estilo embutido; o arquivo salvo no destino continua autônomo. 404 antes de o relatório existir.

### Erro de escrita e espaço livre (job `recuperar`)

- Arquivo que não grava no destino (disco cheio, nome inválido, permissão negada) **não derruba o trabalho**: a linha em `recuperacoes` vira `resultado='falhou'` com o `motivo` (texto do sistema, até 200 caracteres), o `.parte` é apagado, o evento do job conta e o trabalho segue. O job termina `concluido`; o evento final diz quantos falharam. O `relatorio.csv` ganhou a coluna `motivo` e o html mostra `falhou ao gravar: <motivo>`. Retomar ou rodar de novo tenta outra vez só os `falhou`.
- Antes de criar o job, `POST .../recuperar` compara o espaço livre do disco do destino (pasta existente mais próxima) com a soma dos tamanhos escolhidos: se não cabe nem a metade, **400** ("Não cabe: ..."); se cabe a metade mas não tudo, o job é criado e a resposta traz `aviso` (texto); se não dá para saber o espaço, deixa tentar. O job também grava um evento "Atenção: o destino tem ..." ao começar (e ao retomar) quando o que falta gravar não cabe.

## Varrer só uma partição (`opcoes.offset` e `opcoes.tamanho`)

`POST /api/jobs {"tipo": "varredura", "origem": "...", "opcoes": {"modo": "rapida"|"profunda", "offset": 1048576, "tamanho": 2097152}}` busca por assinatura **só** dentro de `[offset, offset+tamanho)` da origem (por exemplo, uma partição achada pela busca de partições). Sem `tamanho`, vai do `offset` até o fim da origem; sem `offset`, começa em 0.
- Valem inteiros de bytes (0 ou mais; `tamanho` maior que 0; bool, texto e decimal dão 400). O trecho tem de caber na origem e `offset` e `tamanho` têm de ser múltiplos do setor, exceto o `tamanho` que chega exatamente ao fim da origem. Em arquivo, tudo é conferido ao criar o job (400); em disco ou volume os limites são conferidos ao abrir (o job falha com a frase). Valem em todos os modos. Com `modo: "sistema"` ou `sistema_antes: true`, a lista pelo sistema de arquivos olha **só o que começa em `offset`** (`sa.detectar(leitor, offset, tamanho)`), sem ler a tabela de partições do disco (serve para tabela zerada ou danificada), com offsets absolutos; o carving depois roda só no trecho, e os achados de dentro que a lista já tem com nome não se repetem. Sem sistema de arquivos reconhecido no trecho, `sistema` falha com a frase de sempre e `sistema_antes` segue só para a busca por assinatura.
- Os achados saem com `offset` **absoluto na origem** (e o nome `f<setor>` também). Um arquivo que passaria do fim do trecho sai `parcial` com o que cabe dentro; nada fora do trecho é lido nem achado. O progresso é medido sobre o trecho. `recuperar` lê desses offsets absolutos, sem mudança.
- O checkpoint guarda `offset` e `janela`; retomar com outro trecho é recusado. O segundo plano (threads ou processos) respeita o trecho.

### O que cada arquivo virou (`recuperados`) e o relatório da imagem

- `GET /api/jobs/<id_do_recuperar>/recuperados?resultado=&pagina=&por_pagina=` → `{total, pagina, por_pagina, itens:[{arquivo, achado_id, resultado, motivo, bytes}], resumo:{por_resultado:{ok: n, ...}}}`, na ordem em que foram pedidos. `arquivo` é o caminho relativo no destino (`Fotos/f0001234.jpg`; nulo se o achado não foi achado). `resultado` = `ok` | `com_falhas` (trechos ilegíveis zerados) | `falhou` (não gravou; o `motivo` traz o texto do sistema) | `sumiu` | `copiando` (em andamento ou interrompido); `resultado` aceita vários separados por vírgula. `pagina` a partir de 1, `por_pagina` de 1 a 500 (padrão 50); valor inválido = 400. 404 se o job não existe, 409 se não é `recuperar`. O `resumo` ignora o filtro `resultado`.
- `GET /api/jobs/<id>/relatorio` (e `/relatorio.css`) serve também o relatório do job `imagem` (`<destino>.relatorio.html`, ver TECNICO.md): 404 enquanto não foi gerado (a imagem só gera ao concluir), 409 para outros tipos de job. O arquivo é achado pelo nome gravado no checkpoint, só como nome simples ao lado da imagem (ou na pasta de destino, no `recuperar`).
