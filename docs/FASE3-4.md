# Fases 3 e 4: partições e sistemas de arquivos

Regras de sempre (só leitura, destino em outro disco, testes com imagens sintéticas montadas em Python, nada de dado de empresa). Os achados com nome entram na mesma tabela `achados` da fase 5 (com `caminho` original, `apagado: true|false`, datas do sistema de arquivos) e usam a mesma lista e o mesmo job `recuperar`.

## Fase 3: partições (`recuperacao/particoes.py`)

- Ler MBR (inclusive estendidas/lógicas), GPT (cabeçalho e cópia reserva no fim do disco, CRC), e reconhecer tabela BSD disklabel e Apple Partition Map (só listar).
- `GET /api/fontes/<id>/particoes`: partições da tabela + estado (ok, tabela danificada, GPT reserva usada).
- Busca de partição perdida (job `particoes`): varrer o disco atrás de assinaturas de início de sistema de arquivos — boot sector FAT/exFAT/NTFS (inclusive a cópia reserva do NTFS no fim e a do FAT no setor 6), superbloco ext (1024 bytes do início, magic 0xEF53, e cópias nos grupos), HFS+/APFS, ISO9660 — e propor partições (início, tamanho, tipo, confiança). Nunca grava a tabela no disco: oferece "listar os arquivos desta partição" (fase 4) e exportar a tabela reconstruída como arquivo (`.json` e um `.mbr`/`.gpt` para o técnico aplicar à mão, se quiser).

## Fase 4: sistemas de arquivos (`recuperacao/sa/`)

Implementação própria em Python puro (sem dependência nova), leitura apenas:
- **FAT12/16/32**: percorrer diretórios, listar arquivos e apagados (0xE5) com nome (LFN), tamanho, datas e primeiro cluster; recuperar apagado assumindo clusters contíguos livres (como o padrão das ferramentas) e marcar "parcial" se a FAT mostra reuso.
- **exFAT**: entradas de diretório (0x85/0xC0/0xC1 e versões com bit de uso apagado), `NoFatChain`, nomes UTF-16.
- **NTFS**: MFT (registros em uso e livres), atributos `$FILE_NAME` e `$DATA` (residente e não residente com data runs), reconstruir caminho pelo pai; recuperar apagado pelos data runs se os clusters não foram reutilizados (conferir no `$Bitmap`).
- **ext2/3/4** (leitura): superbloco, grupos, inodes, extents e blocos indiretos; listar; apagados só onde o inode ainda tem os ponteiros (ext3/4 zera — dizer isso).
- **ISO9660/Joliet** (e base para a fase 6): volume descriptors, diretórios, nomes Joliet.
- HFS+/APFS/UDF: só reconhecer e listar a raiz se der; o resto fica registrado como limitação.
- Job `varredura` ganha o modo "pelo sistema de arquivos" (rápido, com nomes) antes do carving (profundo); o painel mostra os dois juntos, com a árvore de pastas do original (`GET /api/jobs/<id>/arvore?caminho=`).

## Testes

Imagens montadas em Python no teste: MBR com 2 partições (FAT32 e NTFS mínimos), GPT com cópia reserva e cabeçalho principal corrompido, disco com a tabela zerada (a busca reencontra as partições pelas assinaturas), FAT32 e exFAT com arquivos apagados de nome longo, NTFS mínimo com MFT de arquivo residente e não residente apagados, ext4 com extents, ISO9660 com Joliet. Conferir nomes, caminhos, tamanhos e SHA-256 dos recuperados, e que a origem não muda.
