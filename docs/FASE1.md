# Recuperação de Dados — Fase 1: serviço local, jobs persistentes e painel que se reconecta

Contrato entre o back-end e o front-end. Quem mudar o contrato atualiza este arquivo no mesmo commit.

## Por que esta fase existe

O app antigo (só o `.exe`, sem fonte; a tela dele serviu de base visual) encerra o servidor quando a página para de mandar `/api/ping` (`app.js`, linha 1012), e guarda a varredura só em memória. Fechar a aba, recarregar ou abrir por outro endereço perde o trabalho. Aqui a página vira só painel; o trabalho mora num serviço com jobs gravados em disco.

## Regras que valem em todas as fases

- **Nunca escrever na origem.** Toda origem abre só para leitura (`os.open(..., os.O_RDONLY | O_BINARY)`; nada de `r+`, `w`, `a`). Destino nunca é a origem, nunca fica dentro dela e nunca é o mesmo arquivo (comparar `os.path.samefile` e caminho resolvido). Recusar com mensagem clara.
- Testes só com arquivos temporários. Nunca abrir disco real deste PC (`\\.\PhysicalDrive*`, `C:\`, `/dev/sd*`) nos testes.
- Python 3 + Flask 3 (já instalado). Sem dependência nova nesta fase.
- Texto para o usuário em português, sem termo técnico desnecessário.

## Estrutura

```
recuperacao-dados/
  iniciar.py                 # um comando: sobe o serviço se não estiver no ar e abre o navegador com o token
  recuperacao/
    __init__.py
    config.py                # pasta de dados, porta, token (variáveis de ambiente para os testes)
    servico.py               # app Flask + rotas + SSE
    jobs.py                  # JobStore em SQLite
    executor.py              # threads que rodam os jobs; pausar/retomar/cancelar; checkpoint
    tipos/
      __init__.py
      imagem.py              # job de exemplo e útil: imagem bit a bit de uma origem (arquivo) com mapfile
    painel/                  # front (dev-front): index.html, painel.js, painel.css
  testes/
    test_jobs.py
    test_servico.py
    test_persistencia.py     # os 4 cenários obrigatórios
    test_somente_leitura.py
  README.md
```

## Configuração (`config.py`)

- Pasta de dados: `RECDADOS_DADOS` ou `%LOCALAPPDATA%\RecuperacaoDados` (Windows) / `~/.local/share/recuperacao-dados` (Linux). Lá ficam `jobs.db`, `token`, `servico.lock`, `logs/` e os mapfiles.
- Porta: `RECDADOS_PORTA` ou `8765`. Escuta SÓ em `127.0.0.1`.
- Bind exclusivo no Windows (`SO_EXCLUSIVEADDRUSE`, sem `SO_REUSEADDR`): uma segunda cópia do serviço não liga na mesma porta e sai com mensagem.

## Segurança do serviço

- Token aleatório (`secrets.token_urlsafe(32)`) criado na primeira subida e gravado em `token` (só o usuário lê). Continua o mesmo entre reinícios.
- `iniciar.py` primeiro confere que quem responde na porta é o nosso serviço (`GET /api/saude?desafio=<n>` devolve `prova` = HMAC-SHA256 do desafio com o token; se não conferir, avisa que outro programa usa a porta e não abre nada nem envia o token). Depois pede um **ticket** (`POST /api/ticket` com `X-RecDados-Token`) e abre `http://127.0.0.1:<porta>/?t=<ticket>`. O ticket vale 60 s e **uma vez só**; o serviço o troca por um cookie `recdados` (HttpOnly, SameSite=Strict, Path=/, 12 h) cujo valor é uma **sessão aleatória em memória** (nunca o token do arquivo) e redireciona para a mesma URL sem o `t`. O token do arquivo na URL não abre nada. Reiniciar o serviço apaga as sessões: reabrir pelo atalho.
- Toda rota `/api/*` exige o cookie de sessão (ou o cabeçalho `X-RecDados-Token`, para testes e scripts). Sem ele: 401 com `{"erro": "abra pelo atalho do programa"}`. `POST /api/ticket` aceita só o cabeçalho (cookie não gera ticket).
- Toda resposta leva `Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'` (sem `style=`/`<style>` inline; `elemento.style.x = ...` pelo JS pode).
- Recusa `Host` que não seja `127.0.0.1:<porta>` ou `localhost:<porta>` (proteção contra DNS rebinding) e `Origin` de outro site nos POST (403).
- Página sem cookie mostra só "Abra pelo atalho do programa" (sem dados).

## Job (SQLite, `jobs.db`)

Tabela `jobs`: `id` (texto curto, ex. `j-20261104-7f3a`), `tipo`, `origem`, `destino`, `opcoes` (JSON), `estado`, `progresso` (0–1), `bytes_total`, `bytes_feitos`, `velocidade` (B/s, média dos últimos 5 s), `setores_ruins` (int), `mensagem`, `criado_em`, `atualizado_em`, `iniciado_em`, `terminado_em`, `erro`.
Tabela `eventos`: `id`, `job_id`, `quando`, `texto` (log por job, o que aparece no painel).
Tabela `achados` (vazia nesta fase; as fases 4 e 5 gravam aqui os arquivos encontrados): `id`, `job_id`, `caminho`, `tipo`, `tamanho`, `data`, `estado`, `offset`.

Estados: `na_fila`, `rodando`, `pausado`, `interrompido`, `concluido`, `falhou`, `cancelado`.
- `interrompido` = estava `rodando` quando o serviço caiu; ao subir, o serviço marca assim e o painel oferece **Retomar**. Nunca retoma sozinho.
- Gravação no SQLite com WAL e uma conexão por thread; atualização de progresso no máximo 2 vezes por segundo.

## Executor

- Até 2 jobs rodando ao mesmo tempo (fila para o resto).
- Cada tipo implementa `executar(job, ctrl)`, onde `ctrl` dá `pausa_pedida()`, `cancelamento_pedido()`, `progresso(feitos, total, texto=None)` e `checkpoint()`.
- Checkpoint a cada 2 s ou 64 MiB (o que vier primeiro), e sempre ao pausar, cancelar ou encerrar.
- Encerramento limpo (Ctrl+C, fechar o console, `POST /api/desligar`): pede pausa a todos, espera o checkpoint (até 10 s) e sai. Os jobs ficam `pausado`.

## Tipo `imagem` (o job desta fase)

- `origem`: caminho de um arquivo (nesta fase só arquivo; disco bruto entra na fase 2). `destino`: caminho do arquivo de imagem a criar. `opcoes`: `{"bloco": 1048576}`.
- Lê a origem só para leitura em blocos e grava no destino na mesma posição (`os.pwrite`/`seek`+`write`).
- Mapfile `<destino>.mapa.json` no estilo do ddrescue: objeto `{"versao": 1, "origem": "<caminho resolvido>", "tamanho": n, "faixas": [[inicio, fim, "ok"|"ruim"|"pendente"], ...]}` (fim exclusivo). Bloco que der erro de leitura: tenta 2 vezes, marca `ruim`, preenche zero no destino e segue. Na retomada, origem e tamanho diferentes dos gravados = falha clara sem tocar na imagem. Imagem e mapa são criados com modo 0600.
- Retomar lê o mapfile e só lê as faixas `pendente`. Prova no teste: a origem não é relida no que já está `ok`.
- No fim, o SHA-256 do destino tem de ser igual ao da origem quando não houver setor ruim.

## API (JSON; erros sempre `{"erro": "<frase em português>"}`)

| Método | Rota | O quê |
|---|---|---|
| GET | `/api/saude` | `{"ok": true, "versao": "...", "pid": n}`; com `?desafio=<texto até 100>` acrescenta `"prova"`. Única rota sem token. Não mostra nada do usuário |
| POST | `/api/ticket` | `{"ticket": "...", "expira_em": 60}`. Só com `X-RecDados-Token`. Uso interno do `iniciar.py` |
| GET | `/api/marca` | `{"app","empresa","site","cor"}` vindos de `RECDADOS_MARCA_*` ou `marca.json` (padrão neutro). Com token |
| GET | `/api/jobs` | lista de jobs (mais novo primeiro) |
| POST | `/api/jobs` | `{"tipo": "imagem", "origem": "...", "destino": "...", "opcoes": {}}` → 201 com o job. 400 se origem não existir, destino = origem, destino dentro da origem, destino já existir, caminho de rede (`\\servidor\...`), `\\.\`/`\\?\` ou nome de dispositivo do Windows (NUL, CON, COM1...). **409** se já há job que não terminou com o mesmo destino (duplo clique) |
| GET | `/api/jobs/<id>` | o job + os últimos 200 eventos |
| POST | `/api/jobs/<id>/pausar` | 200; 409 se o estado não permitir |
| POST | `/api/jobs/<id>/retomar` | vale para `pausado` e `interrompido`; 409 nos outros |
| POST | `/api/jobs/<id>/cancelar` | 200; o mapfile e a imagem parcial ficam |
| GET | `/api/eventos` | SSE. Ao conectar manda `event: jobs` com a lista inteira; depois `event: job` a cada mudança (no máximo 2/s por job) e `: ping` a cada 15 s. Vários clientes ao mesmo tempo |
| POST | `/api/desligar` | encerramento limpo |
| GET | `/`, `/jobs/<id>` | o painel (o mesmo HTML; o JS lê a URL) |

Duas abas: as ações de um job passam por um lock por job. Se chegar uma ação enquanto outra está em curso no mesmo job: 409 `{"erro": "outra ação neste job está em andamento"}`. Ações repetidas (pausar o que já está pausado) devolvem 409 com mensagem clara, não erro 500.

## `iniciar.py`

- Se `/api/saude` responde: só abre o navegador com o token. Se não: sobe o serviço destacado (sem janela no Windows), espera a porta (até 10 s) e abre.
- Trava de subida (arquivo com `O_EXCL`) para dois cliques seguidos não subirem duas cópias.

## Painel (dev-front)

- Base visual: a identidade do app anterior.
- Lista de jobs com estado, barra, velocidade, tempo restante, setores ruins; clique abre `/jobs/<id>` (a URL muda com `history.pushState`; recarregar volta ao mesmo job).
- Formulário "Nova imagem": origem e destino (campo de texto nesta fase), botão Iniciar.
- Botões Pausar / Retomar / Cancelar conforme o estado; `interrompido` aparece em destaque com "Retomar".
- SSE com reconexão automática (o `EventSource` reconecta; mostrar "reconectando…" e, se o SSE falhar 3 vezes, cair para consulta a cada 2 s em `/api/jobs`).
- Nenhum estado importante só no JS ou no localStorage: tudo vem do serviço.
- `beforeunload` só avisa ("A recuperação continua em segundo plano. Pode fechar.") quando houver job rodando — e não bloqueia o fechamento.
- Tudo que vem do serviço (caminhos, mensagens) entra por `textContent`.

## Testes obrigatórios desta fase (`python -m unittest discover testes`)

1. Persistência (a): iniciar uma imagem, nenhum cliente conectado, o job termina sozinho.
2. (b): dois clientes SSE e uma segunda "aba" (outra sessão HTTP) veem o mesmo job e o mesmo progresso.
3. (c): matar o processo do serviço (`kill`/`taskkill /F`) no meio; subir de novo: o job aparece `interrompido`; retomar termina com SHA-256 igual ao da origem e sem reler as faixas já `ok`.
4. (d): duas ações simultâneas no mesmo job: uma passa, a outra 409; nada quebra.
5. Somente leitura: a origem sai byte a byte igual (hash e `mtime`), e o código nunca abre a origem com modo de escrita (teste que intercepta `open`/`os.open`).
6. Segurança: sem token 401; `Host` estranho recusado; `Origin` de outro site no POST 403; segunda cópia do serviço não sobe.
7. Encerramento limpo: `POST /api/desligar` com job rodando deixa o job `pausado` e o mapfile em dia.
