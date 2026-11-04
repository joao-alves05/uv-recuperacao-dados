# Fase 2: disco de verdade, só leitura, imagem com setor ruim

Continua o contrato da fase 1 (`docs/FASE1.md`). Cada parte abaixo é um commit próprio, com teste que reprova a versão anterior. Regras da fase 1 seguem valendo: origem só leitura, destino ≠ origem, testes nunca abrem disco real (usam leitor falso ou arquivo), nenhum dado de empresa no código.

## 2.1 Fontes: listar discos e volumes

- `recuperacao/fontes.py`: `listar()` devolve `[{"id", "caminho", "nome", "tamanho", "tipo", "removivel", "setor", "sistema"}]`.
  - `tipo`: `hd`, `ssd`, `usb`, `cartao`, `optico`, `disquete`, `arquivo`, `desconhecido`.
  - Windows: discos físicos `\\.\PhysicalDriveN` e unidades ópticas `\\.\CdRomN` (via `ctypes`/WMI/PowerShell `Get-CimInstance Win32_DiskDrive`, sem abrir para escrita); volumes com letra para mostrar o que está dentro.
  - Linux: `/sys/block/*` (`size`, `queue/rotational`, `removable`, `device/model`).
  - Nunca abre nada com escrita para descobrir; falha de um disco não derruba a lista.
- `GET /api/fontes` (com sessão). O provedor é injetável para os testes.

## 2.2 Leitor de disco bruto só leitura

- `recuperacao/leitor.py`: `abrir(caminho) -> Leitor` com `tamanho`, `setor` (tamanho lógico) e `ler(offset, n)`.
  - Windows: `CreateFileW(GENERIC_READ, FILE_SHARE_READ|FILE_SHARE_WRITE, OPEN_EXISTING)`; tamanho por `IOCTL_DISK_GET_LENGTH_INFO`; setor por `IOCTL_DISK_GET_DRIVE_GEOMETRY_EX`; leitura alinhada ao setor (offset e tamanho múltiplos), sem `FILE_FLAG_NO_BUFFERING` obrigatório mas compatível.
  - Linux: `os.open(O_RDONLY)`, tamanho por `BLKGETSIZE64`/seek ao fim.
  - Arquivo comum continua funcionando pelo mesmo `Leitor`.
- `GENERIC_WRITE`, `O_WRONLY`, `O_RDWR` não podem aparecer no módulo: teste estático + teste que intercepta as chamadas.
- Sem permissão (Windows sem administrador): erro claro "Para ler o disco inteiro, abra o programa como administrador" e o `iniciar-admin.cmd` que pede elevação.
- O tipo `imagem` passa a aceitar disco/volume como origem.

## 2.3 Mesma unidade

- Destino na mesma unidade física da origem é recusado (400, "O destino está no mesmo disco da origem…").
  - Windows: disco do volume de destino por `IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS`; compara com o número do `PhysicalDrive` da origem (ou com o disco do volume, se a origem for volume).
  - Linux: `st_dev` do destino → disco pai em `/sys/dev/block`.
- Testes com mapeamento falso.

## 2.4 Setor ruim como no ddrescue

- Primeira passada: blocos grandes; bloco com erro vira `ruim` provisório, sem zerar o vizinho.
- Bisseção: dentro do bloco ruim, ler em metades até o tamanho do setor; só os setores que falham ficam `ruim`; o resto vira `ok`.
- Pulo crescente: depois de N erros seguidos, pular uma área que dobra (até um teto) e voltar depois.
- Segunda passada nas faixas ruins, em ordem reversa, com `tentativas` (opção).
- Opções do job: `tentativas` (padrão 1), `limite_velocidade` (B/s, 0 = sem), `pular` (bool).
- `setores_ruins` passa a contar setores, não blocos; eventos agrupados (uma linha por faixa, não por setor).

## 2.5 Queda antes do primeiro mapa

- O mapa nasce antes do destino (ou no mesmo passo, com marca de dono); retomada aceita destino criado por nós sem dados ainda. Destino que não é nosso nunca é tocado.

## 2.6 Avisos de mídia

- SSD e pen drive: aviso de TRIM ("o que foi apagado pode já ter sido zerado pelo próprio dispositivo"); tipo detectado pelo `DeviceSeekPenalty`/`rotational`.
- Disquete reconhecido (1,44 MB/720 KB, FAT12) e tratado como disco pequeno.
- Óptico listado (a leitura especial é a fase 6): aviso "leitura de CD/DVD completa chega na fase 6".

## 2.7 Painel

- Nova imagem: escolher a fonte numa lista (com tipo, tamanho, nome, aviso) em vez de digitar; destino por campo de texto com a pasta; aviso se faltar espaço no destino.
- Detalhe do job: mapa de setores (barra com ok/ruim/pendente), velocidade, tempo restante, setores ruins, opções (tentativas, limite de velocidade).
- Mensagem clara de "precisa de administrador".

## 2.8 Testes de integração

- "Disco" sintético (leitor falso) com setores ruins em posições conhecidas: a imagem final tem exatamente esses setores zerados e o mapa marca só eles; kill no meio da bisseção e retomada.
