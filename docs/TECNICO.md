# Recuperação de Dados — fase 1

Serviço local com jobs gravados em disco e um painel que se reconecta. O contrato com o painel está em `docs/FASE1.md`.

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
- A tabela `achados` existe, mas nada grava nela ainda.
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
