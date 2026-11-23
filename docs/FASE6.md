# Fase 6: CD, DVD e Blu-ray

Regras de sempre: origem só leitura (pelo `Leitor`), destino em outro disco, testes só com imagens e "drive" falso (nunca drive real), nenhum dado de empresa, sem dependência nova.

## 6.1 Leitura da unidade

- Origem: `\\.\CdRomN` (Windows) ou `/dev/sr*` (Linux), pelo mesmo `Leitor`. Setor lógico 2048. No Windows, se `IOCTL_DISK_GET_LENGTH_INFO` falhar (comum em unidade óptica), o tamanho vem de `IOCTL_DISK_GET_DRIVE_GEOMETRY_EX`; se o setor não vier, vale 2048. Continua só `GENERIC_READ`.
- O job `imagem` passa a **aceitar** unidade óptica (antes recusava). Como a leitura de mídia riscada pode demorar muito (a unidade tenta de novo sozinha antes de devolver erro), o job avisa isso num evento ao começar.
- A conferência "destino no mesmo disco" não se aplica à óptica (não se grava na mídia); o destino continua sendo recusado se for a própria origem.
- Leitura resiliente é a do `imagem.py` (estilo ddrescue): primeira passada em blocos grandes, bloco com erro vira `suspeito`, bisseção até o setor de 2048 bytes, pulo crescente depois de erros seguidos e volta depois, passada reversa nos setores ruins, mapa `<destino>.mapa.json` com retomada. Para óptica o padrão de `tentativas` sobe de 1 para 3 (a menos que o pedido traga o valor).
- Limitação: o código de erro da unidade (SCSI sense) e leitura em modo bruto (RAW, C2) não são usados; só a leitura lógica normal.

## 6.2 Sistemas de arquivos de disco (`recuperacao/sa/`)

- **ISO 9660/Joliet** (`iso.py`, já existia) ganha **Rock Ridge básico**: nome longo (entrada `NM`, inclusive em partes) quando não há Joliet. Não lê `CE` (área de continuação), links simbólicos nem permissões.
- **UDF** (`udf.py`, novo, leitura): UDF 1.02 a 2.01 (DVD gravado, bloco 2048).
  - Reconhece pelos descritores `BEA01` + `NSR02/NSR03` a partir do setor 16.
  - Âncora (AVDP) no setor 256, no último setor menos 256 e no último setor; descritores de volume principal e reserva; partição física (mapa tipo 1); conjunto de arquivos (FSD); `File Entry` (261) e `Extended File Entry` (266); alocação curta, longa, embutida e continuação (AED); diretórios por `File Identifier Descriptor`, incluindo entradas marcadas como apagadas (`apagado: true`, estado `parcial`); nomes de 8 ou 16 bits; datas com fuso.
  - **Multi-sessão**: a unidade pode oferecer as sessões por `leitor.sessoes()` (lista de setores iniciais, vinda do TOC); a última sessão é a que tem a âncora em `início + 256`. O TOC real é lido em `leitor.py` no Linux (`toc_linux`, ioctl CDROMREADTOCHDR/ENTRY, `/dev/sr*`/`/dev/scd*`) e no Windows (`ApiWindows.toc`, IOCTL_CDROM_READ_TOC_EX, só em unidade com GetDriveType = DRIVE_CDROM), com a limitação de que o formato TOC lista faixas, não sessões. Sem essa API (arquivo/imagem, ou unidade que não responde ao TOC), o volume varre todos os setores atrás de âncoras (só quando a opção `todas_sessoes` é pedida ou quando nenhuma âncora padrão existe; a varredura lê o disco inteiro) e usa a de maior posição que tenha um conjunto de volume válido. Sem a opção, só as âncoras padrão são olhadas (rápido). **Como pedir:** `POST /api/jobs {"tipo":"varredura","origem":...,"opcoes":{"modo":"sistema","todas_sessoes":true}}`. O campo é booleano e vale na lista pelo sistema de arquivos (`modo: "sistema"` ou `sistema_antes: true`); o padrão é `false`. Qualquer outro valor (`"sim"`, `1`, `null`) dá 400 com "A opção todas_sessoes aceita só verdadeiro ou falso."
  - **Partição virtual (VAT, CD-R/RW gravado de forma incremental)**: o mapa tipo 2 `*UDF Virtual Partition` faz todo bloco lógico (conjunto de arquivos, ICBs, extensões de arquivo) ser traduzido pela VAT. O File Entry da VAT é procurado de trás para frente nos últimos 64 setores (`MAX_BUSCA_VAT`) do fim da imagem, ou antes do início da sessão seguinte quando o TOC informa as sessões; vale UDF 2.00+ (tipo de arquivo 248, cabeçalho de `L_HD` bytes) e UDF 1.50 (tabela terminada por `*UDF Virtual Alloc Tbl`). VAT não achada, corrompida ou com entradas fora da partição não derruba o volume: ele abre sem listar arquivos e o `limitacao` ganha "AVISO: VAT ilegível (motivo)". Referências à partição física no mesmo volume são lidas como virtuais.
  - **Partição esparsa (CD-RW em pacotes, UDF 1.50+)**: o mapa tipo 2 `*UDF Sparable Partition` traz o tamanho do pacote e até 4 cópias da Sparing Table (`*UDF Sparing Table`, conferida por checksum da tag, identificador e CRC). Vale a primeira cópia válida; bloco cujo pacote está na tabela é lido do pacote de reposição (como no kernel do Linux, `udf_get_pblock_spar15`: a posição original é relativa ao início da partição e a de reposição é absoluta no disco). Nenhuma cópia válida não derruba o volume: a leitura segue direta, sem remapear, e o `limitacao` ganha "AVISO: ..." (arquivos em pacotes remapeados podem sair com lixo).
  - **Partição de metadados (UDF 2.50+, Blu-ray e HD em UDF)**: o mapa tipo 2 `*UDF Metadata Partition` aponta o Metadata File (File Entry tipo 250) e o Metadata Mirror File (251). O conjunto de arquivos, os ICBs e os diretórios ficam dentro dele: o bloco da partição de metadados é um deslocamento no arquivo, traduzido pelos extents para a partição física (a partition reference dos long_ad e dos FIDs escolhe a partição; short_ad herda a do ICB). Se o Metadata File, ou o conjunto de arquivos dentro dele, estiver ilegível, a leitura cai no espelho com "AVISO" no `limitacao`; se nenhum dos dois servir, o volume abre sem listar nada, também com aviso. Os dados dos arquivos ficam na partição física. O Metadata Bitmap e os tamanhos de alocação/alinhamento não são lidos.
  - Fica de fora (e é dito no `limitacao` do volume): fluxos nomeados, ACL e atributos estendidos. O ICB indireto (Indirect Entry, estratégia 4096) é seguido até o File Entry, com no máximo 16 saltos; laço ou cadeia maior deixa o arquivo `ruim` com nota.
- `sa.detectar` tenta UDF antes de ISO 9660 (disco "ponte" lista os nomes completos do UDF); se o UDF não ler, cai para a ISO.

## 6.3 Recuperação parcial

- Imagem de óptica com setores ruins tem zeros nesses setores e faixas `ruim` no mapa. Ao recuperar arquivos de uma imagem que tem `<imagem>.mapa.json` ao lado, o job `recuperar` cruza as faixas de cada arquivo com as faixas não lidas do mapa (`ruim`, `suspeito`, `pulado`, `pendente`).
- Arquivo que toca setor não lido sai com os trechos lidos e zeros no resto, resultado `com trechos ilegíveis (zerados)` e o achado fica `parcial`.
- O relatório diz quais trechos: `lacunas.csv` na pasta de destino (`arquivo`, `deslocamento`, `bytes`; deslocamento dentro do arquivo recuperado). O módulo é `recuperacao/setores_ruins.py`.

## 6.4 Correção de erro (dvdisaster / RS-ECC)

Não implementado. Só valeria com um arquivo `.ecc` ao lado da imagem (formato do dvdisaster, Reed-Solomon sobre a imagem); decodificar RS é trabalho grande e fica registrado como limitação. Quem tiver o `.ecc` pode usar o dvdisaster à parte sobre a imagem que este programa gerou, e depois pedir a varredura de novo.

## 6.5 Testes

- "Drive" falso (`Leitor` de 2048 com setores que levantam `OSError`, e um setor que falha só nas primeiras tentativas): a imagem sai com zeros exatamente nos setores ruins, o mapa marca só eles, o setor instável é recuperado na releitura, retomada continua.
- ISO com Rock Ridge e UDF montados em Python (arquivos, pastas, acentos, arquivo embutido, arquivo apagado, multi-sessão), conferindo nomes, caminhos, tamanhos e conteúdo.
- Recuperação parcial: arquivo com um setor ilegível sai com zeros só ali e a lista de lacunas bate.
- A origem não muda (hash e interceptação de aberturas).

## 6.6 Painel

Não é desta fase de motor: o painel ainda mostra a unidade óptica como "chega numa próxima versão" e desabilita os botões; quem cuida do painel precisa liberar a imagem de CD/DVD.
