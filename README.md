# UV Recuperação de Dados

Ferramenta de recuperação de dados para pen drives, cartões, HDs, SSDs e mídias ópticas.

Feita por **João Vitor Medeiros Alves** ([@joao-alves05](https://github.com/joao-alves05)) em parceria com **Fabio**, da UV Informática, que criou a primeira versão do app e traz a experiência de bancada em recuperação.

Nome, empresa, contato e cores do app vêm de configuração: nenhum dado de empresa fica no código.

## O que o programa faz

Abre um painel no navegador (o programa roda só no seu computador) onde você escolhe o disco, pen drive, cartão ou CD/DVD, e o programa: lê a mídia **só para leitura**; faz uma imagem bit a bit dela, pulando e voltando depois nos setores ruins como o ddrescue; lê as partições (MBR, GPT) e procura as que se perderam; lista os arquivos pelo sistema de arquivos (FAT, exFAT, NTFS, ext2/3/4, ISO 9660, UDF), inclusive os apagados com nome e pasta; e procura arquivos por assinatura (fotos, vídeos, documentos...) quando o sistema de arquivos não existe mais. Você marca o que quer e recupera para **outro disco**, com um relatório do que saiu inteiro, parcial ou ruim.

## Como baixar e rodar

### Windows

1. **Instale o Python** 3.11 ou mais novo em [python.org/downloads](https://www.python.org/downloads/). Na primeira tela do instalador, marque **"Add python.exe to PATH"**.
2. **Baixe o programa:** nesta página, clique no botão verde **Code** e depois em **Download ZIP**. Extraia o ZIP numa pasta, por exemplo `Documentos\uv-recuperacao-dados`.
   - Quem usa git pode clonar: `git clone https://github.com/joao-alves05/uv-recuperacao-dados.git`
3. **Abra o terminal na pasta:** abra a pasta extraída, clique na barra de endereço do Explorador, digite `cmd` e aperte Enter.
4. **Instale o Flask** (só na primeira vez):
   ```
   pip install flask
   ```
5. **Inicie:**
   ```
   python iniciar.py
   ```
   O painel abre no navegador. Pode fechar a aba quando quiser: o trabalho continua, e `python iniciar.py` abre o painel de novo.

### Linux

```
sudo apt install python3 python3-pip      # ou o gerenciador da sua distribuição
pip3 install flask
python3 iniciar.py
```

Para ler um disco inteiro (`/dev/sdb`, `/dev/sr0`) é preciso ser root: `sudo python3 iniciar.py`.

### Por que abrir como administrador

O Windows e o Linux só deixam um programa ler o **disco inteiro** (e não só os arquivos de uma pasta) para quem é administrador. Sem isso o programa só enxerga imagens e arquivos comuns, e avisa: "Para ler o disco inteiro, abra o programa como administrador". No Windows, dê dois cliques em `iniciar-admin.cmd` (ele pede a confirmação do Windows), ou use o botão de administrador do painel. Pedir administrador **não** dá ao programa o direito de gravar na origem: ele continua abrindo tudo só para leitura.

## O princípio que não se negocia

**O programa nunca escreve no dispositivo de origem.**

1. A mídia de origem é aberta só para leitura (no Windows, `GENERIC_READ`; no Linux, `O_RDONLY`), e testes automáticos conferem que nenhuma abertura de escrita existe.
2. O trabalho começa por uma imagem bit a bit da mídia, e a varredura roda sobre essa imagem.
3. Os arquivos recuperados vão sempre para um destino diferente da origem. Se origem e destino forem o mesmo disco, o programa bloqueia e avisa.
4. Até a tabela de partições reconstruída só é **proposta**: sai como arquivo (`.json`, `.mbr`, `.gpt`) para o técnico aplicar à mão, se quiser. O programa nunca grava uma tabela no disco.

## Passo a passo por cenário

Regra geral: **tenha um segundo disco com espaço livre** para receber o resultado. Em todos os casos, depois de perceber a perda, **pare de usar a mídia** (não copie, não formate, não instale nada nela): cada gravação nova pode sobrescrever o que você quer salvar.

**Pen drive ou cartão com arquivo apagado.** Escolha a unidade e use **Recuperar arquivos**. A varredura *pelo sistema de arquivos* é rápida e traz nomes e pastas originais, com os apagados marcados. Se o resultado vier magro, rode a **Rápida** (acha os arquivos pelo cabeçalho, em posições de setor) e depois a **Profunda** (todo byte, demorada). Marque o que quer e use **Recuperar selecionados**, escolhendo um destino em outro disco. Espere: nomes originais só pelo sistema de arquivos; pela assinatura os arquivos saem com nomes gerados (`f0001234.jpg`).

**Formatação rápida.** A formatação rápida apaga a tabela do sistema de arquivos, mas não os dados. Comece pela varredura pelo sistema de arquivos (às vezes sobra a estrutura antiga) e, se não achar, use a **Rápida** ou a **Profunda**. Os arquivos saem sem nome e sem pasta, e os que estavam fragmentados podem sair parciais. Se a mídia foi formatada e *usada* depois, o que foi sobrescrito não volta.

**HD com setor ruim (faça a imagem primeiro).** Disco que estala, trava ou some não deve ser lido repetidamente: cada leitura pode piorá-lo. Crie primeiro uma **imagem** do disco para um arquivo em outro disco. O programa lê em blocos grandes, pula as áreas ruins, volta nelas depois (passada reversa, com várias tentativas) e guarda um mapa dos setores; pode pausar, fechar e retomar de onde parou. Depois trabalhe **sobre a imagem**, nunca no disco doente. O que continuar ilegível sai como zeros, e os arquivos que tocam esses setores saem `parcial`, com os trechos faltantes listados em `lacunas.csv`.

**Partição perdida.** O disco aparece como "não formatado" ou sem partição. Use **Procurar partições perdidas**: o programa varre atrás de setores de boot e superblocos (FAT, exFAT, NTFS, ext, HFS+, APFS, ISO), inclusive das cópias reservas, e mostra candidatos com um nível de confiança (alta, média, baixa) e as evidências. Escolha o candidato e liste os arquivos dele. A tabela reconstruída pode ser exportada (`.json`, `.mbr`, `.gpt`), mas aplicá-la ao disco é decisão sua, feita fora do programa.

**CD/DVD/Blu-ray riscado.** Coloque o disco e crie uma **imagem** dele para outro disco. Mídia riscada pode levar **horas**, porque a unidade tenta de novo sozinha nos trechos ruins; pause e retome à vontade. O programa lê o disco todo, pula o que não consegue e volta depois (por padrão 3 tentativas nos setores ruins). Em seguida, **Recuperar arquivos** sobre a imagem lista os arquivos pelo ISO 9660/Joliet/Rock Ridge ou pelo UDF. Arquivo com setor ilegível sai parcial.

**SSD (e pen drives modernos) com arquivo apagado.** O SSD apaga de verdade o que foi excluído (comando TRIM) e o próprio disco zera o espaço livre, às vezes em minutos. Se o arquivo foi apagado há tempo, a chance é pequena e nenhum programa muda isso. Desligue o computador, não use o SSD, e tente a varredura pelo sistema de arquivos e a assinatura sobre uma imagem. Espere resultados muito menores que num HD. Para dado importante, procure um laboratório antes de tentar qualquer coisa.

## Limitações honestas

- **TRIM:** SSD e pen drive com TRIM zeram o que foi apagado; não há como desfazer.
- **Sobrescrita:** dado sobrescrito se foi para sempre.
- **Criptografia:** BitLocker, VeraCrypt, FileVault e LUKS precisam da chave; sem ela só se enxerga ruído.
- **ext3/ext4:** ao apagar, o sistema zera os ponteiros do arquivo; aparece o nome, mas o conteúdo só volta pela busca por assinatura.
- **NTFS:** arquivos comprimidos pelo NTFS e criptografados com EFS não são lidos; arquivos apagados só se os clusters não foram reutilizados.
- **HFS+ e APFS:** só são reconhecidos (e achados na busca de partições); os arquivos não são listados.
- **UDF:** 1.02 a 2.01. Sem partição virtual (VAT, CD-R/RW de gravação incremental), esparsa ou de metadados (Blu-ray), sem ICB indireto. Multi-sessão: a última sessão só é escolhida com a varredura de âncoras (o TOC da unidade ainda não é lido).
- **Correção de erro:** não há decodificação de ECC do dvdisaster (Reed-Solomon); se você tem o arquivo `.ecc`, use o dvdisaster sobre a imagem que este programa gerou.
- **Fragmentação:** a busca por assinatura recupera bem arquivos contíguos. Arquivo fragmentado (comum em disco cheio e muito usado) sai parcial ou misturado.
- **Disco real:** os testes usam imagens sintéticas e drives falsos; nada foi validado contra hardware com defeito de verdade.

## Arquitetura

O trabalho pesado não depende da página. Um **serviço local** (só em `127.0.0.1`, com token de sessão) roda em segundo plano; cada operação é um **job** gravado em disco, com checkpoints, que sobrevive a queda de energia; o **painel** só mostra e comanda, e pode ser fechado e reaberto.

1. **Serviço e jobs** (Flask, SQLite): [docs/FASE1.md](docs/FASE1.md) e [docs/TECNICO.md](docs/TECNICO.md).
2. **Leitura e imagem** (`Leitor` só leitura, mapa de setores ruins): [docs/FASE2.md](docs/FASE2.md).
3. **Partições** (MBR, GPT, busca de partição perdida) e **sistemas de arquivos** (FAT, exFAT, NTFS, ext, ISO), em Python puro: [docs/FASE3-4.md](docs/FASE3-4.md).
4. **Carving** por assinatura, validação, lista de resultados e recuperação: [docs/FASE5.md](docs/FASE5.md).
5. **CD, DVD e Blu-ray** (UDF, Rock Ridge, leitura resiliente, recuperação parcial): [docs/FASE6.md](docs/FASE6.md).
6. **Painel** em `recuperacao/painel/`, sem scripts embutidos (política de segurança restrita a `'self'`).

Testes: `python -m unittest discover -s testes`. O teste de tela usa o Edge: `python testes/testar_painel.py`.

## Roteiro

| Fase | O quê | Estado |
|---|---|---|
| 1 | Serviço local, jobs persistentes e painel que se reconecta | pronta |
| 2 | Leitura só de leitura e imagem de disco com mapa de setores ruins | pronta |
| 3 | Partições: MBR, GPT e busca de partições apagadas | pronta |
| 4 | Sistemas de arquivos: FAT, exFAT, NTFS, ext2/3/4, ISO9660 | pronta |
| 5 | File carving por assinatura, com validação de cada arquivo | pronta |
| 6 | CD, DVD e Blu-ray: ISO9660, Rock Ridge, UDF e releitura resiliente | motor pronto |
| 7 | Interface em 3 passos, prévia, filtros e desempenho para discos acima de 1 TB | em andamento |

Cada fase entra com testes automatizados feitos sobre imagens pequenas de teste, nunca sobre um disco real.

## Referências

O motor se inspira em ferramentas abertas, respeitando as licenças delas: [TestDisk e PhotoRec](https://www.cgsecurity.org/), [The Sleuth Kit](https://www.sleuthkit.org/), [GNU ddrescue](https://www.gnu.org/software/ddrescue/), [dvdisaster](https://dvdisaster.jcea.es/), [Scalpel](https://github.com/sleuthkit/scalpel) e Foremost, [ntfs-3g](https://github.com/tuxera/ntfs-3g) e [extundelete](https://extundelete.sourceforge.net/). O fluxo de tela se inspira nos programas comerciais do mercado: escolher o local, varrer, ver a prévia e recuperar.

Essas ferramentas são **inspiração**: delas usamos ideias, métodos e formatos públicos. **Nenhum código GPL (ou de qualquer outra licença delas) foi copiado para este projeto**; tudo aqui é implementação própria em Python puro, e quando uma delas é útil (por exemplo o dvdisaster), ela é usada à parte, como programa separado.

## Licença

Código aberto, sob a [licença MIT](LICENSE). © 2026 João Vitor Medeiros Alves e Fabio (UV Informática).

As ferramentas citadas em Referências têm licenças próprias (várias são GPL). Delas este projeto usa só ideias e formatos públicos, ou as chama como programas separados: nenhum código delas é copiado para cá.
