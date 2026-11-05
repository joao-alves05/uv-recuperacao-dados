# Recuperação de Dados — notas técnicas

Serviço local com jobs gravados em disco e um painel que se reconecta. O contrato com o painel está em `docs/FASE1.md`; as fases seguintes têm contrato próprio (`FASE2.md`, `FASE3-4.md`, `FASE5.md`, `FASE6.md`). O guia de uso está no `README.md`.

## Mapa dos módulos (fases 2 a 6)

| Módulo | Papel |
|---|---|
| `recuperacao/leitor.py` | Único ponto de leitura da origem (arquivo, disco, volume, CD/DVD), só leitura, alinhado ao setor |
| `recuperacao/fontes.py`, `unidade.py` | Lista de discos e recusa de destino no mesmo disco da origem |
| `recuperacao/tipos/imagem.py` | Cópia bit a bit com mapa de setores ruins (estilo ddrescue), inclusive CD/DVD |
| `recuperacao/particoes.py`, `tipos/particoes.py` | Tabelas MBR/GPT/BSD/APM, busca de partição perdida e tabela proposta (`.json`, `.mbr`, `.gpt`) |
| `recuperacao/sa/` | Sistemas de arquivos em Python puro, só leitura: FAT, exFAT, NTFS, ext, ISO 9660/Joliet/Rock Ridge, UDF |
| `recuperacao/carving.py`, `validar.py`, `assinaturas.json` | Busca por assinatura e validação de cada arquivo |
| `recuperacao/tipos/varredura.py`, `recuperar.py` | Jobs de varredura (sistema de arquivos, rápida, profunda) e de recuperação para o destino, com relatório |
| `recuperacao/setores_ruins.py` | Cruza o mapa da imagem com os arquivos: trechos não lidos viram `lacunas.csv` |
| `recuperacao/achados.py`, `rotas_*.py` | Tabela `achados` e as rotas de resultados, partições e árvore de pastas |

## Arquitetura

- `recuperacao/servico.py`: app Flask (só `127.0.0.1`), rotas `/api/*`, SSE em `/api/eventos`, cookie `recdados` e o ciclo de vida (bind exclusivo na porta, encerramento limpo).
- `recuperacao/jobs.py`: `JobStore` em SQLite (WAL, uma conexão por thread): tabelas `jobs`, `eventos`, `achados`. Horários em segundos desde 1970.
- `recuperacao/executor.py`: até 2 jobs ao mesmo tempo; pausar, retomar, cancelar (uma ação por vez em cada job, a outra recebe 409); checkpoint a cada 2 s ou 64 MiB.
- `recuperacao/tipos/imagem.py`: cópia bit a bit de um arquivo para uma imagem, com mapfile `<destino>.mapa.json` (faixas `[inicio, fim, "ok"|"ruim"|"pendente"]`, fim exclusivo). A origem só é aberta com `O_RDONLY`.
- `recuperacao/config.py`: pasta de dados, porta, token e marca.
- `iniciar.py`: sobe o serviço destacado se preciso e abre o navegador com o token.

O trabalho mora no serviço: fechar a aba não interrompe nada. Se o serviço cair, o job que rodava vira `interrompido` e só continua quando alguém pedir **Retomar**.

## Como iniciar

    python iniciar.py

Para subir só o serviço (em primeiro plano): `python -m recuperacao.servico`. Ctrl+C ou `POST /api/desligar` pausam os jobs e saem limpo.

## Configuração (variáveis de ambiente)

| Variável | Padrão |
|---|---|
| `RECDADOS_DADOS` | `%LOCALAPPDATA%\RecuperacaoDados` (Windows) ou `~/.local/share/recuperacao-dados` |
| `RECDADOS_PORTA` | `8765` |
| `RECDADOS_MARCA_APP`, `_EMPRESA`, `_SITE`, `_COR` | nome "UV Recuperação de Dados", empresa, site e cor vazios |

A marca também pode vir de `marca.json` na pasta de dados (`{"app","empresa","site","cor"}`); a variável vence o arquivo. Regras: textos até 80 caracteres, site só `https://`, cor só `#rrggbb`; valor inválido cai no padrão. O painel lê tudo em `GET /api/marca`. Nenhum dado de empresa (telefone, endereço, cidade, e-mail, site, CNPJ, clientes) fica no código; um teste varre o projeto e reprova se aparecer.

Na pasta de dados: `jobs.db`, `token` (criado na primeira subida), `servico.lock` (trava de subida do `iniciar.py`) e `logs/`.

## Como rodar os testes

    cd recuperacao-dados
    python -m unittest discover -s testes -v

Só usam arquivos temporários, porta livre e pasta de dados temporária; nunca abrem disco real. Os testes de persistência sobem o serviço em outro processo e o matam no fim. `RECDADOS_ATRASO_BLOCO` (segundos por bloco) existe só para os testes deixarem um job lento.

## Limitações desta fase

- A origem só pode ser um arquivo (disco bruto entra na fase 2).
- Tentativa de leitura: 2 por bloco; bloco ruim vira zero na imagem e fica `ruim` no mapa (não é relido na retomada).
- Se o serviço for morto, perde-se até 2 s de cópia (o que passou do último checkpoint é relido na retomada).
- A conferência final por SHA-256 relê origem e imagem inteiras e só vale quando não há setor ruim.
- A tabela `achados` é gravada pelo job `varredura` (carving) e, na fase 4, pelos achados do sistema de arquivos; contrato em `docs/FASE5.md`.
- O mapfile fica ao lado da imagem (`<destino>.mapa.json`), não na pasta de dados.
- Não há autenticação além do token local: quem lê a pasta de dados do usuário lê o token.
- Só arquivos locais: caminhos de rede, `\.\`, `\?\` e nomes de dispositivo do Windows (NUL, CON, COM1...) são recusados; disco bruto entra na fase 2.
- Setor ruim (como no ddrescue): primeira passada em blocos grandes (bloco com erro vira `suspeito`, sem zerar o vizinho; depois de 3 erros seguidos pula áreas cada vez maiores, `pulado`, e volta depois); bisseção só dentro dos blocos suspeitos até o tamanho do setor; segunda passada nos setores ruins, do fim para o começo, com `tentativas` leituras (padrão 1, 0 = não relê). Opções: `tentativas`, `limite_velocidade` (B/s, 0 = sem limite) e `pular` (padrão verdadeiro). A fase em andamento fica no mapa, então pausar, cair e retomar continua de onde parou sem reler o que está `ok`. `GET /api/jobs/<id>/mapa` devolve as faixas resumidas (no máximo 2000) para a barra de setores.
- Queda antes do primeiro mapa: o mapa (com marca de dono e `etapa: criando`) é gravado antes da imagem; se o serviço cair, a retomada recria a imagem ou usa a vazia que ele mesmo tinha criado. Destino que já existe sem mapa nosso nunca é tocado; se a queda ocorrer depois de a imagem receber o tamanho e antes do mapa final, o job recusa seguir (não dá para provar que o arquivo é nosso) e o usuário escolhe outro nome.

## Acesso e segurança

- O token fica no arquivo `token` da pasta de dados e nunca vai para a URL nem para o cookie. O `iniciar.py` confere a identidade do serviço (desafio HMAC), pede um ticket de uso único (60 s) e abre `/?t=<ticket>`; o serviço troca o ticket por um cookie de sessão aleatório (12 h, só em memória). Link antigo no histórico do navegador não abre mais nada.
- Toda resposta leva `Content-Security-Policy` restrito a `'self'` (o painel não pode ter estilo ou script inline).
- A imagem e o mapa são criados com modo 0600. No Windows o modo não se aplica: o arquivo herda as permissões da pasta de destino, então escolha uma pasta que só o seu usuário (e administradores) acessem.
- Um job por destino: pedir de novo o mesmo destino enquanto o primeiro não terminou devolve 409.

## Dados pessoais e como apagar

Caminhos de origem e destino (que podem ter nomes de pessoas ou clientes) ficam no banco `jobs.db` (tabelas `jobs` e `eventos`), em `logs/` e no mapa ao lado de cada imagem. Para apagar: cancele ou deixe terminar os jobs, feche o serviço (`POST /api/desligar` ou Ctrl+C) e apague `jobs.db`, `jobs.db-wal`, `jobs.db-shm` e a pasta `logs/` da pasta de dados; apague também cada imagem e seu `.mapa.json`. Apagar a pasta de dados inteira remove também o token e a marca (um novo token é criado na próxima subida).

## Desempenho da varredura (fase 7)

Medido num arquivo sintético de 1 GiB (dados aleatórios com 205 arquivos de 8 tipos espalhados), no PC de desenvolvimento (Windows, i5 de 4 núcleos, arquivo no cache do sistema, então os números mostram o limite do programa, não o do disco). Leitura pura em blocos de 4 MiB: 1070 a 1430 MB/s.

| | antes | depois |
|---|---|---|
| Rápida, só o motor | 85 MB/s | 1585 MB/s |
| Profunda, só o motor | 83 MB/s | 220 MB/s |
| Rápida, job completo (serviço, banco, 205 achados) | não medido | 413 MiB/s (2,5 s) |
| Profunda, job completo | não medido | 161 MiB/s (6,4 s) |

O que mudou (`recuperacao/carving.py`):
- **Rápida:** em vez de 17 buscas por bloco, pega o primeiro byte de cada setor de uma vez com fatia de passo `setor` (em C) e só confere o cabeçalho completo onde esse byte bate. Mais leitura antecipada: 2 threads (cada uma com a sua própria abertura só leitura da origem) mantêm 4 blocos prontos à frente.
- **Profunda:** continua a olhar todo byte (um `bytes.find` por cabeçalho, que no CPython gira em torno de 1,8 GB/s por cabeçalho), mas a busca de candidatos roda em até 3 processos (`min(3, núcleos - 1)`), cada um lendo o seu bloco; a validação continua em ordem na thread do job. Tentei a busca de todos os cabeçalhos por uma regex só e por filtro de bytes com `translate` + inteiros grandes: ficaram em ~90 MB/s, sem ganho.
- Só entra em segundo plano em origem com caminho real e 64 MiB ou mais; abaixo disso, ou com leitor falso, tudo roda na thread do job. Se um trabalhador não consegue abrir a origem ou morre, a varredura segue sozinha no mesmo ponto. Pausar fecha o gerador, que encerra os trabalhadores.
- **Memória:** no máximo `2 x trabalhadores` blocos em andamento (cada um 4 MiB + a sobra do cabeçalho). Pico medido no processo principal: 35 MiB na rápida e 27 MiB na profunda, com a origem de 1 GiB; cada processo da profunda usa mais uns 30 MiB.

### Imagens grandes (acima de 4 GiB e de 1 TB)

- Todo offset, tamanho e posição é `int` do Python; no banco são `INTEGER` de 64 bits; no JSON do mapa e do painel valem até 2^53 (9 PB). Os campos de 32 bits que existem são os do formato dos arquivos (tamanhos dentro de JPG, ZIP, etc.), lidos com o cuidado de sempre.
- Achado: o único ponto de 32 bits na leitura era o `ReadFile` do Windows (`DWORD`); agora cada chamada pede no máximo 1 GiB e o `Leitor` repete até completar. Teste com arquivo esparso de 4 GiB + 16 MiB (acha um JPG em 4 GiB + 1 MiB e um PNG mais adiante, nos dois modos, com e sem segundo plano) e teste do teto do `ReadFile`.
- A varredura não guarda mapa nenhum: a memória não depende do tamanho da origem. O job de imagem guarda o mapa como lista de faixas `[inicio, fim, estado]`: uma faixa só no começo, e cresce com o número de trechos ruins, não com o tamanho do disco. Um disco de 1 TB sem erros é uma faixa; com milhões de setores ruins espalhados a lista (que é regravada a cada checkpoint) fica grande e lenta. Não foi medido com um disco desses.
