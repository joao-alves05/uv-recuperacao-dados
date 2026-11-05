# -*- coding: utf-8 -*-
"""Log por job: um arquivo de texto por trabalho em `<pasta de dados>/logs/jobs/<id>.log`.

Guarda o que o trabalho leu, pulou e falhou (posições, tamanhos, nomes de arquivo, motivos), uma linha por fato, com a
hora. **Nunca grava conteúdo de arquivo.** Contém caminhos de origem e destino e nomes de arquivos (como o banco do
serviço): para apagar, veja "Dados pessoais e como apagar" no TECNICO.md. Escrever no log nunca derruba o trabalho.
"""
import os
import re
import time

from . import config

MAX_LINHAS = 100000          # por execução; passou disso o resto é descartado (com um aviso)
MAX_LINHA = 1000
_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')


def pasta():
    return os.path.join(config.pasta_logs(), 'jobs')


def caminho(job_id):
    return os.path.join(pasta(), (job_id if _ID.fullmatch(str(job_id)) else 'sem-id') + '.log')


class LogJob:
    def __init__(self, job_id):
        self.job_id = job_id
        self._f = None
        self._n = 0
        self._falhou = False

    def escrever(self, texto):
        if self._falhou or self._n > MAX_LINHAS:
            return
        try:
            if self._f is None:
                os.makedirs(pasta(), exist_ok=True)
                fd = os.open(caminho(self.job_id), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_BINARY', 0), 0o600)
                self._f = os.fdopen(fd, 'a', encoding='utf-8', buffering=1)
            self._n += 1
            if self._n > MAX_LINHAS:
                texto = 'Log cortado: passou de %d linhas nesta execução.' % MAX_LINHAS
            linha = ' '.join(str(texto).split())[:MAX_LINHA]
            self._f.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), linha))
        except OSError:
            self._falhou = True              # disco cheio, pasta sem permissão...: o trabalho segue sem log

    def fechar(self):
        f, self._f = self._f, None
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
