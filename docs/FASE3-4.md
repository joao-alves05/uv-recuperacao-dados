# Fases 3 e 4: partições e sistemas de arquivos

Regras de sempre (só leitura, destino em outro disco, testes com imagens sintéticas montadas em Python, nada de dado de empresa). Os achados com nome entram na mesma tabela `achados` da fase 5 (com `caminho` original, `apagado: true|false`, datas do sistema de arquivos) e usam a mesma lista e o mesmo job `recuperar`.

## Fase 3: partições (`recuperacao/particoes.py`)

- Ler MBR (inclusive estendidas/lógicas), GPT (cabeçalho e cópia reserva no fim do disco, CRC), e reconhecer tabela BSD disklabel e Apple Partition Map (só listar).
- `GET /api/fontes/<id>/particoes`: partições da tabela + estado (ok, tabela danificada, GPT reserva usada).
- Busca de partição perdida (job `particoes`): varrer o disco atrás de assinaturas de início de sistema de arquivos — boot sector FAT/exFAT/NTFS (inclusive a cópia reserva do NTFS no fim e a do FAT no setor 6), superbloco ext (1024 bytes do início, magic 0xEF53, e cópias nos grupos), HFS+/APFS, ISO9660 — e propor partições (início, tamanho, tipo, confiança). Nunca grava a tabela no disco: oferece "listar os arquivos desta partição" (fase 4) e exportar a tabela reconstruída como arquivo (`.json` e um `.mbr`/`.gpt` para o técnico aplicar à mão, se quiser).

### Como ficou a fase 3 (pronta)

**Abrir uma partição como volume (para a fase 4).** `particoes.abrir_volume(base, inicio, tamanho=None)` devolve um `Leitor` (somente leitura) que enxerga só a faixa `[inicio, inicio+tamanho)` da base, com o offset 0 no começo da partição. `inicio` pode ser um dicionário de partição (`{"inicio", "tamanho", ...}`, como o de `analisar()['particoes']` e o dos candidatos da busca). Tamanho maior que o disco é cortado e `volume.truncado` fica `True`; fechar o volume não fecha a base. Exemplo:

    with leitor.abrir(caminho) as base:
        p = particoes.analisar(base)['particoes'][0]
        with particoes.abrir_volume(base, p) as vol:
            vol.ler(0, 512)          # setor de boot da partição
            particoes.identificar(vol, 0)['sistema']      # 'fat32', 'ntfs', 'ext4'...

`particoes.identificar(leitor, inicio)` diz qual sistema de arquivos começa ali (`fat12/16/32`, `exfat`, `ntfs`, `ext2/3/4`, `hfs+`, `hfsx`, `apfs`, `iso9660`) pelas assinaturas, ou `None`. Cada partição já traz o campo `sistema` com esse resultado.

**Tabela (`particoes.analisar(leitor)`).** Devolve `{esquema, estado, setor, tamanho, particoes, avisos}`. `esquema`: `mbr`, `gpt`, `apm`, `bsd`, `volume` (disco inteiro é um volume, sem tabela) ou `nenhum`. `estado`: `ok`, `tabela_danificada`, `gpt_reserva_usada` ou `sem_tabela`. Cada partição: `origem_tabela` (`mbr`, `mbr-logica`, `gpt`, `apm`), `inicio`/`tamanho`/`fim` em bytes, `tipo`, `tipo_nome`, `sistema`, `avisos` e, conforme a tabela, `ativa`, `logica`, `estendida`, `nome`, `guid`. APM e as sub-partições BSD (`subparticoes` dentro da fatia MBR 0xA5/0xA6/0xA9) trazem `somente_listar: true`. O GPT é procurado em setores de 512 e de 4096 (uma imagem de arquivo não diz qual); a cópia reserva é lida onde o cabeçalho principal manda e, se faltar, no último setor.

**Busca (job `particoes`).** `POST /api/jobs {"tipo":"particoes","origem":<disco|volume|imagem|arquivo>,"destino":"<pasta em outro disco>/nome.json","opcoes":{"bloco":4194304}}` (`bloco` opcional, múltiplo de 4096). Pausa, retoma e sobrevive a queda (`<destino>.busca.json` guarda o ponto e os sinais já achados). Em disco/volume, o destino em outro disco é conferido por `unidade.conferir`. Saída, nunca na origem:

- `nome.json`: `tabela_atual`, `candidatos` (todos, com `na_tabela_atual`), `proposta` (os candidatos de confiança >= 0.5 que não se sobrepõem), `aplicacao` (qual arquivo gravar em qual byte).
- `nome.mbr`: setor 0 de um MBR com a proposta (até 4 primárias, sem código de boot; só os sistemas com tipo de MBR conhecido).
- `nome.gpt` (setores 0 a 33: MBR protetor, cabeçalho e entradas, a gravar no byte 0) e `nome.gpt-fim` (entradas e cabeçalho reservas, a gravar em `aplicacao.gpt_fim.gravar_em_byte`).
- `GET /api/jobs/<id>/particoes` devolve o `nome.json` quando o job terminou (404 antes disso).

Cada candidato: `inicio`, `tamanho`, `fim`, `sistema`, `confianca` (0 a 1), `nivel` (`alta` >= 0.8, `media` >= 0.5, `baixa`), `evidencias` (frases) e `avisos`. A confiança soma: setor de boot ou superbloco principal (0,45), cópia reserva no lugar esperado (0,35: NTFS no fim, FAT32 no setor 6, exFAT no setor 12, ext no grupo 1, HFS+ no fim), posição gravada na própria estrutura conferindo (0,15: setores ocultos do FAT, deslocamento do exFAT), estrutura interna coerente (0,15: tabela FAT, MFT, descritores de grupo, Fletcher-64 do APFS, descritor final do ISO) e perde 0,25 se a partição passaria do fim do disco. Se só a cópia reserva sobrou, o início é calculado a partir dela e a confiança fica menor.

**Limites conhecidos.** FAT12/16 não têm cópia reserva; o volume ou partição FAT12/16 é reconhecido pela estrutura do BPB mesmo sem o rótulo "FAT12/16" no setor de boot, mas a BUSCA de partição perdida ainda procura pelo texto do rótulo, então um FAT12/16 sem rótulo e sem tabela não aparece nela; HFS+ sem o cabeçalho do fim, APFS e ISO ficam em confiança média/baixa (sem cópia fixa); partição antiga sobrescrita parcialmente pode aparecer como candidato de confiança baixa, sobreposto à atual; MBR exportado não leva código de boot, estendidas nem lógicas; o `.gpt` usa setor de 512 ou de 4096 conforme a origem; o Fletcher-64 do APFS e os offsets de BSD/APM foram escritos pela especificação pública e só testados com imagens montadas aqui, nunca com disco real.

## Fase 4: sistemas de arquivos (`recuperacao/sa/`)

Implementação própria em Python puro (sem dependência nova), leitura apenas:
- **FAT12/16/32**: percorrer diretórios, listar arquivos e apagados (0xE5) com nome (LFN), tamanho, datas e primeiro cluster; recuperar apagado assumindo clusters contíguos livres (como o padrão das ferramentas) e marcar "parcial" se a FAT mostra reuso.
- **exFAT**: entradas de diretório (0x85/0xC0/0xC1 e versões com bit de uso apagado), `NoFatChain`, nomes UTF-16.
- **NTFS**: MFT (registros em uso e livres), atributos `$FILE_NAME` e `$DATA` (residente e não residente com data runs), reconstruir caminho pelo pai; recuperar apagado pelos data runs se os clusters não foram reutilizados (conferir no `$Bitmap`).
- **ext2/3/4** (leitura): superbloco, grupos, inodes, extents e blocos indiretos; listar; apagados só onde o inode ainda tem os ponteiros (ext3/4 zera — dizer isso).
- **ISO9660/Joliet** (e base para a fase 6): volume descriptors, diretórios, nomes Joliet.
- HFS+/HFSX: leitura dos vivos (apagados só por carving); APFS: só reconhecer (ver abaixo).
- Job `varredura` ganha o modo "pelo sistema de arquivos" (rápido, com nomes) antes do carving (profundo); o painel mostra os dois juntos, com a árvore de pastas do original (`GET /api/jobs/<id>/arvore?caminho=`).

### Como ficou a fase 4 (pronta)

**Leitores (`recuperacao/sa/`).** `sa.detectar(leitor, inicio=0, tamanho=None)` devolve um `Volume` (`.tipo`, `.rotulo`, `.limitacao`) ou `None`; `vol.varrer()` gera `Entrada` (nome, caminho, eh_dir, tamanho, apagado, criado/modificado/acessado, estado `bom|parcial|ruim`, `runs` = `[(offset absoluto na origem, bytes)]`, `inline`, `nota`) e `vol.ler(entrada)` gera os bytes. Recebem o leitor da ORIGEM e o início da partição, então os offsets já saem absolutos (não usam `abrir_volume`). Recuperação de apagado: FAT e exFAT assumem clusters contíguos (e conferem a FAT/bitmap: reuso = `parcial`); NTFS usa os data runs e confere o `$Bitmap`; ext2 usa os ponteiros preservados; ext3 e ext4 zeram os ponteiros ao apagar, então o nome aparece mas o conteúdo não (só carving). APFS é só reconhecido (`limitacao`, `varrer()` vazio); HFS+/HFSX têm leitura, descrita abaixo. Limites de cada um estão no docstring do módulo.

**Job `varredura`, modo `sistema`.** `POST /api/jobs {"tipo":"varredura","origem":...,"opcoes":{"modo":"sistema"}}` lê a tabela de partições (`particoes.analisar`) e lista cada partição reconhecida; sem tabela, o disco inteiro. Rápido: só lê estruturas, não varre byte a byte. Cada arquivo vira um achado com `caminho`, `apagado`, `estado`, `data` (modificação, em UTC), `grupo` pela extensão e `offset` ou `extents` absolutos (a prévia e o job `recuperar` leem pelo contrato de sempre). Com mais de uma partição reconhecida, o caminho ganha o prefixo `/[Partição N]`. `opcoes.sistema_antes: true` com `rapida` ou `profunda` faz a lista pelo sistema de arquivos e depois o carving no MESMO job (o carving descarta o achado que começa no mesmo offset de um arquivo já listado com nome, e o job avisa quantos; o que só o carving acha continua aparecendo). Checkpoint da fase: `{"fase":"sistema","feitos":<entradas já vistas>,...}`; a ordem das entradas é fixa, então pausar/retomar não repete achado. Ficam fora da lista, com um evento que diz quantos: apagado só com o nome (ext3/ext4) e arquivo esparso (com buracos). Dado residente do NTFS (dentro da MFT) vira `extents` que pulam os 2 bytes trocados pelo fixup. Pasta não é achado.

**Árvore.** `GET /api/jobs/<id>/arvore?caminho=<pasta>` -> `{"caminho": "/pasta", "pastas": [{"nome", "caminho", "total"}]}`: subpastas diretas, `total` = achados dentro contando as subpastas; raiz = caminho vazio ou `/`; 404 se o job não existe ou não é `varredura`; 400 se o caminho tiver byte nulo. Os arquivos da pasta saem da lista de achados.

**HFS+ e HFSX (`sa/hfs.py`, leitura).** Lê o volume header (em `inicio+1024`, ou a cópia dos últimos 1024 bytes do volume se o principal estiver ruim: `vol.usou_reserva`), o arquivo de catálogo (B-tree: segue os nós-folha, registros de pasta e de arquivo, nomes Unicode normalizados para NFC) e os extents do fork de dados (os 8 do registro mais o arquivo de extents overflow). Datas Mac (1904, tratadas como UTC). Lista pastas e arquivos VIVOS com o caminho; serve para volume de Mac com a estrutura danificada em que o catálogo ainda está inteiro. Entra na varredura `sistema` como os outros (a pasta que ainda não foi lida faz o filho esperar, como no NTFS; `ao_ler` é chamado a cada 16 nós). Se o catálogo não abrir, `detectar` cai no "só reconhecido" de antes. **Apagados não são recuperáveis pela estrutura**: o HFS+ tira o registro do catálogo e reaproveita o nó, então o que sobra é acaso; deixe para o carving. Também ficam de fora, e o docstring do módulo diz por quê: resource fork e atributos estendidos, arquivo comprimido (decmpfs, sai `ruim`), hard links (o registro do link aponta para um inode na pasta escondida "HFS+ Private Data", que não é listada), links simbólicos, journal sujo (não é reaplicado) e HFS+ embrulhado em HFS clássico.

**APFS: só reconhecido, de propósito.** O contêiner tem superbloco com checkpoints (é preciso achar o mais novo válido), mapa de objetos virtuais, volumes com árvores B-tree de cópia na escrita (cada alteração grava nós novos e o estado antigo continua espalhado), criptografia por volume comum em Mac e snapshots. Ler isso direito (achar a versão consistente, resolver os OIDs, descomprimir) é um trabalho do tamanho de um leitor novo e está fora do alcance desta etapa; um leitor parcial listaria estado velho ou misturado sem avisar. `sa.detectar` devolve o volume com `limitacao` e a recomendação do carving.

## Testes

Imagens montadas em Python no teste: MBR com 2 partições (FAT32 e NTFS mínimos), GPT com cópia reserva e cabeçalho principal corrompido, disco com a tabela zerada (a busca reencontra as partições pelas assinaturas), FAT32 e exFAT com arquivos apagados de nome longo, NTFS mínimo com MFT de arquivo residente e não residente apagados, ext4 com extents, ISO9660 com Joliet. Conferir nomes, caminhos, tamanhos e SHA-256 dos recuperados, e que a origem não muda.
