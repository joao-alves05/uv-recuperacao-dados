# -*- coding: utf-8 -*-
"""JobStore: jobs, eventos e achados em SQLite (WAL, uma conexão por thread).

Horários são segundos desde 1970 (float). Quem muda um job avisa os assinantes
(o SSE), que recebem só o id e leem o job de novo.
"""
import json
import os
import secrets
import sqlite3
import threading
import time

from . import achados as _achados

ESTADOS = ('na_fila', 'rodando', 'pausado', 'interrompido', 'concluido', 'falhou', 'cancelado')
TERMINAIS = ('concluido', 'falhou', 'cancelado')
INTERVALO_PROGRESSO = 0.5      # no máximo 2 gravações de progresso por segundo, por job

COLUNAS = ('id', 'tipo', 'origem', 'destino', 'opcoes', 'estado', 'progresso', 'bytes_total', 'bytes_feitos',
           'velocidade', 'setores_ruins', 'mensagem', 'criado_em', 'atualizado_em', 'iniciado_em',
           'terminado_em', 'erro')
EDITAVEIS = frozenset(COLUNAS) - {'id', 'tipo', 'origem', 'destino', 'opcoes', 'criado_em'}

ESQUEMA = '''
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, tipo TEXT NOT NULL, origem TEXT NOT NULL, destino TEXT NOT NULL,
    opcoes TEXT NOT NULL DEFAULT '{}', estado TEXT NOT NULL, progresso REAL NOT NULL DEFAULT 0,
    bytes_total INTEGER NOT NULL DEFAULT 0, bytes_feitos INTEGER NOT NULL DEFAULT 0,
    velocidade REAL NOT NULL DEFAULT 0, setores_ruins INTEGER NOT NULL DEFAULT 0,
    mensagem TEXT NOT NULL DEFAULT '', criado_em REAL NOT NULL, atualizado_em REAL NOT NULL,
    iniciado_em REAL, terminado_em REAL, erro TEXT);
CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, quando REAL NOT NULL, texto TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_eventos_job ON eventos (job_id, id);
CREATE TABLE IF NOT EXISTS achados (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, caminho TEXT, tipo TEXT, tamanho INTEGER,
    data TEXT, estado TEXT, offset INTEGER);
CREATE INDEX IF NOT EXISTS ix_achados_job ON achados (job_id, id);
CREATE TABLE IF NOT EXISTS checkpoints (job_id TEXT PRIMARY KEY, dados TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recuperacoes (
    job_id TEXT NOT NULL, achado_id INTEGER NOT NULL, ordem INTEGER NOT NULL, arquivo TEXT, resultado TEXT NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0, sha256 TEXT, quando REAL, motivo TEXT, PRIMARY KEY (job_id, achado_id));
'''


class DestinoEmUso(Exception):
    """Já existe um job que não terminou com este mesmo destino."""


def destino_normal(destino):
    return os.path.normcase(os.path.realpath(destino))


class Assinante:
    """Ids de job que mudaram desde a última leitura. Junta mudanças repetidas do mesmo job."""

    def __init__(self):
        self._cond = threading.Condition()
        self._ids = set()

    def avisar(self, job_id):
        with self._cond:
            self._ids.add(job_id)
            self._cond.notify_all()

    def esperar(self, timeout):
        with self._cond:
            if not self._ids:
                self._cond.wait(timeout)
            ids, self._ids = self._ids, set()
            return ids


class JobStore:
    def __init__(self, caminho):
        self.caminho = caminho
        self._local = threading.local()
        self._conexoes = []
        self._trava = threading.Lock()
        self._assinantes = []
        self._ultimo_progresso = {}
        self._trava_criar = threading.Lock()
        self._con().executescript(ESQUEMA)
        self._migrar_achados()

    # --- conexão ---------------------------------------------------------
    def _con(self):
        con = getattr(self._local, 'con', None)
        if con is None:
            con = sqlite3.connect(self.caminho, timeout=10, isolation_level=None, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA synchronous=NORMAL')
            con.execute('PRAGMA busy_timeout=10000')
            self._local.con = con
            with self._trava:
                self._conexoes.append(con)
        return con

    def fechar_conexao_da_thread(self):
        """Fecha a conexão desta thread (threads de requisição vêm e vão; as do executor ficam com a sua)."""
        con = getattr(self._local, 'con', None)
        if con is None:
            return
        self._local.con = None
        with self._trava:
            if con in self._conexoes:
                self._conexoes.remove(con)
        try:
            con.close()
        except sqlite3.Error:
            pass

    def fechar(self):
        with self._trava:
            for con in self._conexoes:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
            self._conexoes.clear()
        self._local = threading.local()

    # --- assinantes (SSE) ------------------------------------------------
    def assinar(self):
        a = Assinante()
        with self._trava:
            self._assinantes.append(a)
        return a

    def cancelar_assinatura(self, a):
        with self._trava:
            if a in self._assinantes:
                self._assinantes.remove(a)

    def _avisar(self, job_id):
        with self._trava:
            lista = list(self._assinantes)
        for a in lista:
            a.avisar(job_id)

    # --- jobs ------------------------------------------------------------
    @staticmethod
    def _job(linha):
        if linha is None:
            return None
        d = {c: linha[c] for c in COLUNAS}
        try:
            d['opcoes'] = json.loads(d['opcoes'])
        except ValueError:
            d['opcoes'] = {}
        return d

    def job_com_destino(self, destino):
        """O job que ainda não terminou e usa este destino (caminho resolvido), ou None."""
        alvo = destino_normal(destino)
        marcas = ','.join('?' * len(TERMINAIS))
        for linha in self._con().execute(f'SELECT id, destino FROM jobs WHERE estado NOT IN ({marcas})', TERMINAIS):
            if destino_normal(linha['destino']) == alvo:
                return linha['id']
        return None

    def criar(self, tipo, origem, destino, opcoes=None):
        with self._trava_criar:           # a checagem e a inserção andam juntas: duplo clique não passa
            if self.job_com_destino(destino):
                raise DestinoEmUso('Já existe um trabalho em andamento para esse destino.')
            return self._criar(tipo, origem, destino, opcoes)

    def _criar(self, tipo, origem, destino, opcoes):
        agora = time.time()
        for _ in range(20):
            job_id = 'j-%s-%s' % (time.strftime('%Y%m%d', time.localtime(agora)), secrets.token_hex(2))
            try:
                self._con().execute(
                    'INSERT INTO jobs (id, tipo, origem, destino, opcoes, estado, criado_em, atualizado_em)'
                    " VALUES (?,?,?,?,?, 'na_fila', ?, ?)",
                    (job_id, tipo, origem, destino, json.dumps(opcoes or {}, ensure_ascii=False), agora, agora))
            except sqlite3.IntegrityError:
                continue
            self._avisar(job_id)
            return self.obter(job_id)
        raise RuntimeError('Não consegui gerar um identificador para o job.')

    def obter(self, job_id):
        return self._job(self._con().execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def listar(self):
        linhas = self._con().execute('SELECT * FROM jobs ORDER BY criado_em DESC, rowid DESC').fetchall()
        return [self._job(l) for l in linhas]

    def atualizar(self, job_id, **campos):
        campos = {k: v for k, v in campos.items() if k in EDITAVEIS}
        campos['atualizado_em'] = time.time()
        sets = ', '.join(f'{k}=?' for k in campos)
        self._con().execute(f'UPDATE jobs SET {sets} WHERE id=?', (*campos.values(), job_id))
        self._avisar(job_id)

    def transicao(self, job_id, de, para, **extra):
        """Muda o estado só se o atual estiver em `de`. Devolve True se mudou (atômico)."""
        extra = {k: v for k, v in extra.items() if k in EDITAVEIS}
        extra.update(estado=para, atualizado_em=time.time())
        sets = ', '.join(f'{k}=?' for k in extra)
        marcas = ','.join('?' * len(de))
        cur = self._con().execute(f'UPDATE jobs SET {sets} WHERE id=? AND estado IN ({marcas})',
                                  (*extra.values(), job_id, *de))
        if cur.rowcount:
            self._avisar(job_id)
        return bool(cur.rowcount)

    def progresso(self, job_id, feitos, total, velocidade=0.0, ruins=0, mensagem=None, forcar=False):
        """Grava o progresso, no máximo 2 vezes por segundo por job (a menos que `forcar`). True se gravou."""
        agora = time.monotonic()
        with self._trava:
            if not forcar and agora - self._ultimo_progresso.get(job_id, -1e9) < INTERVALO_PROGRESSO:
                return False
            self._ultimo_progresso[job_id] = agora
        campos = dict(bytes_feitos=int(feitos), bytes_total=int(total), velocidade=float(velocidade),
                      setores_ruins=int(ruins), progresso=min(1.0, feitos / total) if total else 1.0)
        if mensagem is not None:
            campos['mensagem'] = mensagem
        self.atualizar(job_id, **campos)
        return True

    def marcar_interrompidos(self):
        """Ao subir: o que estava rodando (ou na fila) quando o serviço caiu vira `interrompido`. Devolve os ids."""
        ids = [l['id'] for l in self._con().execute("SELECT id FROM jobs WHERE estado IN ('rodando','na_fila')")]
        for job_id in ids:
            if self.transicao(job_id, ('rodando', 'na_fila'), 'interrompido', velocidade=0.0,
                              mensagem='O serviço foi interrompido. Toque em Retomar para continuar.'):
                self.evento(job_id, 'O serviço parou no meio do trabalho. Este job pode ser retomado.')
        return ids

    def pausar_fila(self):
        """No desligamento limpo: o que ainda espera na fila fica pausado."""
        for l in self._con().execute("SELECT id FROM jobs WHERE estado='na_fila'").fetchall():
            if self.transicao(l['id'], ('na_fila',), 'pausado'):
                self.evento(l['id'], 'Pausado: o serviço foi desligado antes de começar.')

    # --- achados e checkpoints (fase 5) ---------------------------------------
    def _migrar_achados(self):
        """Bancos da fase 1 têm a tabela `achados` antiga: acrescenta as colunas novas sem perder nada."""
        con = self._con()
        existentes = {l['name'] for l in con.execute('PRAGMA table_info(achados)')}
        for coluna, tipo in _achados.COLUNAS_NOVAS:
            if coluna not in existentes:
                try:
                    con.execute('ALTER TABLE achados ADD COLUMN %s %s' % (coluna, tipo))
                except sqlite3.OperationalError:
                    if coluna not in {l['name'] for l in con.execute('PRAGMA table_info(achados)')}:
                        raise                 # outra cópia pode ter criado a coluna no mesmo instante
        if 'motivo' not in {l['name'] for l in con.execute('PRAGMA table_info(recuperacoes)')}:
            try:
                con.execute('ALTER TABLE recuperacoes ADD COLUMN motivo TEXT')       # bancos da 5.3
            except sqlite3.OperationalError:
                if 'motivo' not in {l['name'] for l in con.execute('PRAGMA table_info(recuperacoes)')}:
                    raise

    def adicionar_achados(self, job_id, lista, checkpoint=None):
        """Grava achados (dicts, ver achados.normalizar) numa transação só e devolve os ids novos. Se vier `checkpoint`
        (dict), ele é gravado na MESMA transação: o que foi achado e o ponto de retomada nunca divergem.
        ValueError se algum achado for inválido (nada é gravado)."""
        linhas = [_achados.normalizar(job_id, a) for a in lista]
        con = self._con()
        con.execute('BEGIN IMMEDIATE')
        try:
            ids = [con.execute('INSERT INTO achados (%s) VALUES (%s)' % (
                ','.join('"%s"' % c for c in _achados.COLUNAS_GRAVAVEIS), ','.join('?' * len(l))), l).lastrowid
                for l in linhas]
            if checkpoint is not None:
                con.execute('INSERT OR REPLACE INTO checkpoints (job_id, dados) VALUES (?,?)',
                            (job_id, json.dumps(checkpoint)))
            con.execute('COMMIT')
        except BaseException:
            con.execute('ROLLBACK')
            raise
        return ids

    def salvar_checkpoint(self, job_id, dados):
        self._con().execute('INSERT OR REPLACE INTO checkpoints (job_id, dados) VALUES (?,?)', (job_id, json.dumps(dados)))

    def obter_checkpoint(self, job_id):
        l = self._con().execute('SELECT dados FROM checkpoints WHERE job_id=?', (job_id,)).fetchone()
        try:
            return json.loads(l['dados']) if l else None
        except ValueError:
            return None

    # --- eventos ---------------------------------------------------------
    def evento(self, job_id, texto):
        self._con().execute('INSERT INTO eventos (job_id, quando, texto) VALUES (?,?,?)',
                            (job_id, time.time(), str(texto)))

    def eventos(self, job_id, limite=200):
        linhas = self._con().execute('SELECT id, quando, texto FROM eventos WHERE job_id=? ORDER BY id DESC LIMIT ?',
                                     (job_id, limite)).fetchall()
        return [dict(l) for l in reversed(linhas)]      # do mais antigo para o mais novo
