# UV Recuperação de Dados

Ferramenta de recuperação de dados para pen drives, cartões, HDs, SSDs e mídias ópticas.

Feita por **João Vitor Medeiros Alves** ([@joao-alves05](https://github.com/joao-alves05)) em parceria com **Fabio**, da UV Informática, que criou a primeira versão do app e traz a experiência de bancada em recuperação.

Nome, empresa, contato e cores do app vêm de configuração: nenhum dado de empresa fica no código.

> **Status:** em desenvolvimento. Esta é a reconstrução completa da primeira versão, com uma arquitetura nova.

## O princípio que não se negocia

**O programa nunca escreve no dispositivo de origem.**

1. A mídia de origem é aberta só para leitura.
2. O trabalho começa por uma imagem bit a bit da mídia, e a varredura roda sobre essa imagem.
3. Os arquivos recuperados vão sempre para um destino diferente da origem. Se origem e destino forem a mesma unidade, o programa bloqueia e avisa.

## Arquitetura

O trabalho pesado não depende da página. A primeira versão perdia tudo quando a aba fechava. Agora:

- um **serviço local** roda em segundo plano, só em `127.0.0.1` e com token de sessão;
- cada operação é um **job** gravado em disco, com checkpoints. Se a energia cair, o job continua de onde parou, no estilo do mapfile do GNU ddrescue;
- o **painel** no navegador só mostra e comanda. Pode fechar, recarregar ou abrir em outra aba: o progresso continua e aparece de novo, ao vivo.

## Roteiro

| Fase | O quê |
|---|---|
| 1 | Serviço local, jobs persistentes e painel que se reconecta |
| 2 | Leitura só de leitura e imagem de disco com mapa de setores ruins |
| 3 | Partições: MBR, GPT e busca de partições apagadas |
| 4 | Sistemas de arquivos: FAT, exFAT, NTFS, ext2/3/4, HFS+, ISO9660 |
| 5 | File carving por assinatura, com validação de cada arquivo |
| 6 | CD, DVD e Blu-ray: ISO9660, Joliet, UDF e releitura resiliente |
| 7 | Interface em 3 passos, prévia, filtros e desempenho para discos acima de 1 TB |

Cada fase entra com testes automatizados feitos sobre imagens pequenas de teste, nunca sobre um disco real.

## Referências

O motor se inspira em ferramentas abertas, respeitando as licenças delas: TestDisk e PhotoRec, The Sleuth Kit, GNU ddrescue, dvdisaster, Scalpel e Foremost. O fluxo de tela se inspira nos programas comerciais do mercado: escolher o local, varrer, ver a prévia e recuperar.

## Limitações conhecidas

Nenhuma ferramenta recupera dados sobrescritos. Em SSDs e pen drives com TRIM, o que foi apagado pode já ter sido zerado pelo próprio dispositivo. Mídia criptografada precisa da chave.

## Licença

© 2026 João Vitor Medeiros Alves e Fabio (UV Informática). Todos os direitos reservados.
