# -*- coding: utf-8 -*-
"""Executor: threads que rodam os jobs, com pausa, retomada, cancelamento e checkpoint.

Cada tipo implementa `executar(job, ctrl)` e devolve 'concluido', 'pausado' ou
'cancelado' (qualquer exceção vira `falhou`). `ctrl` é um Controle.
"""
import collections
import logging
import os
import queue
import threading
import time
import traceback

from . import config, identidade, logjob

MAX_SIMULTANEOS = 2
CHECKPOINT_S = 2.0
CHECKPOINT_BYTES = 64 * 1024 * 1024
JANELA_VELOCIDADE_S = 5.0
ESPERA_ACAO_S = 15.0

NOME_ESTADO = {'na_fila': 'na fila', 'rodando': 'rodando', 'pausado': 'pausado', 'interrompido': 'interrompido',
               'concluido': 'concluído', 'falhou': 'com falha', 'cancelado': 'cancelado'}


class ErroJob(Exception):
    """Falha esperada de um job; a mensagem (em português) aparece no painel."""


class ErroAcao(Exception):
    def __init__(self, codigo, mensagem):
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


class Controle:
    def __init__(self, store, job_id):
        self._store = store
        self.store = store            # os tipos gravam achados e checkpoint por aqui
        self._log = logjob.LogJob(job_id)
        self.job_id = job_id
        self._pausa = threading.Event()
        self._cancelamento = threading.Event()
        self.terminou = threading.Event()
        self._amostras = collections.deque()
        self._ultimo_cp = time.monotonic()
        self._bytes_cp = 0
        self._ult = (0, 0, 0, None)      # feitos, total, ruins, texto
        self.aviso_final = None          # o tipo preenche: vira parte da mensagem de "Concluído" (resultado parcial)

    # chamados pelo executor / pelas rotas
    def pedir_pausa(self):
        self._pausa.set()

    def pedir_cancelamento(self):
        self._cancelamento.set()

    # chamados pelo tipo
    def pausa_pedida(self):
        return self._pausa.is_set()

    def cancelamento_pedido(self):
        return self._cancelamento.is_set()

    def evento(self, texto):
        self._store.evento(self.job_id, texto)
        self._log.escrever(texto)

    def log(self, texto):
        """Só no arquivo de log do job (detalhe demais para a tela): o que foi lido, pulado, falhou. Sem conteúdo de arquivo."""
        self._log.escrever(texto)

    def velocidade(self):
        a = self._amostras
        if len(a) < 2 or a[-1][0] <= a[0][0]:
            return 0.0
        return max(0.0, (a[-1][1] - a[0][1]) / (a[-1][0] - a[0][0]))

    def progresso(self, feitos, total, texto=None, ruins=0):
        """Registra o avanço. Vai para o banco no máximo 2 vezes por segundo."""
        agora = time.monotonic()
        self._amostras.append((agora, feitos))
        while len(self._amostras) > 1 and agora - self._amostras[0][0] > JANELA_VELOCIDADE_S:
            self._amostras.popleft()
        self._ult = (feitos, total, ruins, texto)
        self._store.progresso(self.job_id, feitos, total, self.velocidade(), ruins, texto)

    def checkpoint(self, salvar=None, bytes_novos=0, forcar=False):
        """Chame depois de cada bloco com os bytes novos. A cada 2 s ou 64 MiB (o que vier primeiro), ou se
        `forcar`, chama `salvar()` (o tipo grava o que precisa para retomar) e grava o progresso. True se salvou."""
        self._bytes_cp += bytes_novos
        agora = time.monotonic()
        if not forcar and agora - self._ultimo_cp < CHECKPOINT_S and self._bytes_cp < CHECKPOINT_BYTES:
            return False
        if salvar:
            salvar()
        self._ultimo_cp = agora
        self._bytes_cp = 0
        feitos, total, ruins, texto = self._ult
        if total or feitos:
            self._store.progresso(self.job_id, feitos, total, self.velocidade(), ruins, texto, forcar=True)
        return True


class Executor:
    def __init__(self, store, tipos, maximo=MAX_SIMULTANEOS):
        self.store = store
        self.tipos = tipos
        self.maximo = maximo
        self._fila = queue.Queue()
        self._enfileirados = set()
        self._trava = threading.Lock()
        self._ativos = {}
        self._locks = {}
        self._fios = []
        self._parando = False

    def iniciar(self):
        for i in range(self.maximo):
            t = threading.Thread(target=self._trabalhador, name=f'recdados-exec-{i}', daemon=True)
            t.start()
            self._fios.append(t)

    def submeter(self, job_id):
        """Põe o job na fila. Id que já está na fila (ou rodando) não entra de novo."""
        with self._trava:
            if job_id in self._enfileirados or job_id in self._ativos:
                return
            self._enfileirados.add(job_id)
        self._fila.put(job_id)

    # --- trabalhador -----------------------------------------------------
    def _trabalhador(self):
        while True:
            job_id = self._fila.get()
            if job_id is None or self._parando:
                return
            with self._trava:
                self._enfileirados.discard(job_id)
            try:
                self._rodar(job_id)
            except Exception:
                self._registrar_erro(job_id, traceback.format_exc())

    def _rodar(self, job_id):
        ctrl = Controle(self.store, job_id)
        with self._trava:
            if job_id in self._ativos:
                return                          # já está rodando (entrada repetida na fila): não mexe no controle dele
            self._ativos[job_id] = ctrl         # registra antes de virar `rodando`: quem vê `rodando` acha o controle
        try:
            agora = time.time()
            if not self.store.transicao(job_id, ('na_fila',), 'rodando', iniciado_em=agora, erro=None,
                                        mensagem='Começando'):
                return                          # foi pausado ou cancelado na fila
            job = self.store.obter(job_id)
            self.store.evento(job_id, 'Começou a rodar.')
            ctrl.log('Começou a rodar: tipo %s, origem %s, destino %s.' % (job['tipo'], job['origem'], job['destino']))
            tipo = self.tipos.get(job['tipo'])
            try:
                if tipo is None:
                    raise ErroJob('Tipo de trabalho desconhecido: %s.' % job['tipo'])
                resultado = tipo.executar(job, ctrl)
                ctrl.log('Resultado: %s.' % resultado)
                self._finalizar(job_id, resultado, ctrl.aviso_final)
            except ErroJob as e:
                ctrl.log('Falhou: %s' % e)
                self._falhar(job_id, str(e))
            except Exception as e:
                self._registrar_erro(job_id, traceback.format_exc())
                ctrl.log('Erro inesperado: %s: %s' % (type(e).__name__, e))
                self._falhar(job_id, 'Erro inesperado: %s: %s' % (type(e).__name__, e))
        finally:
            with self._trava:
                if self._ativos.get(job_id) is ctrl:
                    del self._ativos[job_id]
            ctrl._log.fechar()
            ctrl.terminou.set()

    def _finalizar(self, job_id, resultado, aviso=None):
        agora = time.time()
        de = ('rodando',)
        if resultado == 'concluido':
            atual = self.store.obter(job_id)
            ruins = atual['setores_ruins']
            msg = 'Concluído.' if not ruins else 'Concluído, com %d setores que não foi possível ler.' % ruins
            if aviso:
                msg = (msg + ' ' + aviso[:1].upper() + aviso[1:]) if ruins else 'Concluído, mas ' + aviso
            self.store.transicao(job_id, de, 'concluido', progresso=1.0, terminado_em=agora, velocidade=0.0,
                                 mensagem=msg, bytes_feitos=atual['bytes_total'])
            self.store.evento(job_id, msg)
        elif resultado == 'pausado':
            self.store.transicao(job_id, de, 'pausado', velocidade=0.0, mensagem='Pausado.')
            self.store.evento(job_id, 'Pausado.')
        elif resultado == 'cancelado':
            self.store.transicao(job_id, de, 'cancelado', terminado_em=agora, velocidade=0.0,
                                 mensagem='Cancelado. O mapa e a imagem parcial foram mantidos.')
            self.store.evento(job_id, 'Cancelado.')
        else:
            raise ErroJob('O tipo de trabalho devolveu um resultado inválido: %r.' % (resultado,))

    def _falhar(self, job_id, erro):
        self.store.transicao(job_id, ('rodando',), 'falhou', terminado_em=time.time(), velocidade=0.0, erro=erro,
                             mensagem='Falhou: %s' % erro)
        self.store.evento(job_id, 'Falhou: %s' % erro)

    @staticmethod
    def _registrar_erro(job_id, texto):
        try:
            config.garantir_pastas()
            with open(os.path.join(config.pasta_logs(), 'executor.log'), 'a', encoding='utf-8') as f:
                f.write('[%s] job %s\n%s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), job_id, texto))
        except OSError:
            logging.getLogger('recdados').exception('erro no job %s', job_id)

    # --- ações (rotas) ---------------------------------------------------
    def _lock(self, job_id):
        with self._trava:
            return self._locks.setdefault(job_id, threading.Lock())

    def _acao(self, job_id, fazer):
        """Uma ação por vez em cada job; a segunda recebe 409 na hora, sem esperar."""
        if self.store.obter(job_id) is None:
            raise ErroAcao(404, 'Job não encontrado.')
        lock = self._lock(job_id)
        if not lock.acquire(blocking=False):
            raise ErroAcao(409, 'outra ação neste job está em andamento')
        try:
            return fazer()
        finally:
            lock.release()

    def _esperar_parar(self, job_id):
        with self._trava:
            ctrl = self._ativos.get(job_id)
        if ctrl is not None:
            ctrl.terminou.wait(ESPERA_ACAO_S)

    def pausar(self, job_id):
        def fazer():
            job = self.store.obter(job_id)
            estado = job['estado']
            if estado == 'pausado':
                raise ErroAcao(409, 'Este job já está pausado.')
            if estado == 'na_fila':
                if self.store.transicao(job_id, ('na_fila',), 'pausado', mensagem='Pausado.'):
                    self.store.evento(job_id, 'Pausado antes de começar.')
                    return self.store.obter(job_id)
                estado = self.store.obter(job_id)['estado']
            if estado == 'rodando':
                with self._trava:
                    ctrl = self._ativos.get(job_id)
                if ctrl is not None:
                    ctrl.pedir_pausa()
                    if not ctrl.terminou.wait(ESPERA_ACAO_S):
                        raise ErroAcao(409, 'O job ainda está parando. Tente de novo em instantes.')
                job = self.store.obter(job_id)
                if job['estado'] == 'pausado':
                    return job
                estado = job['estado']
            raise ErroAcao(409, 'Não dá para pausar: o job está %s.' % NOME_ESTADO.get(estado, estado))
        return self._acao(job_id, fazer)

    def retomar(self, job_id):
        def fazer():
            job = self.store.obter(job_id)
            if job['estado'] not in ('pausado', 'interrompido'):
                raise ErroAcao(409, 'Não dá para retomar: o job está %s.' % NOME_ESTADO.get(job['estado'], job['estado']))
            if self._parando:
                raise ErroAcao(409, 'O serviço está sendo desligado.')
            mudou = identidade.conferir(job['origem'], identidade.da_origem_do_job(self.store, job),
                                        lambda m: self.store.evento(job_id, m))
            if mudou:
                raise ErroAcao(409, mudou)           # outro disco no mesmo caminho: não retoma por cima
            if not self.store.transicao(job_id, ('pausado', 'interrompido'), 'na_fila', mensagem='Na fila para retomar.',
                                        erro=None):
                raise ErroAcao(409, 'O estado do job mudou. Atualize a tela.')
            self.store.evento(job_id, 'Retomado.')
            self.submeter(job_id)
            return self.store.obter(job_id)
        return self._acao(job_id, fazer)

    def cancelar(self, job_id):
        def fazer():
            job = self.store.obter(job_id)
            estado = job['estado']
            if estado in ('na_fila', 'pausado', 'interrompido'):
                if self.store.transicao(job_id, ('na_fila', 'pausado', 'interrompido'), 'cancelado',
                                        terminado_em=time.time(), velocidade=0.0,
                                        mensagem='Cancelado. O mapa e a imagem parcial foram mantidos.'):
                    self.store.evento(job_id, 'Cancelado.')
                    return self.store.obter(job_id)
                estado = self.store.obter(job_id)['estado']
            if estado == 'rodando':
                with self._trava:
                    ctrl = self._ativos.get(job_id)
                if ctrl is not None:
                    ctrl.pedir_cancelamento()
                    if not ctrl.terminou.wait(ESPERA_ACAO_S):
                        raise ErroAcao(409, 'O job ainda está parando. Tente de novo em instantes.')
                job = self.store.obter(job_id)
                if job['estado'] == 'cancelado':
                    return job
                estado = job['estado']
            raise ErroAcao(409, 'Não dá para cancelar: o job está %s.' % NOME_ESTADO.get(estado, estado))
        return self._acao(job_id, fazer)

    # --- encerramento ----------------------------------------------------
    def encerrar(self, espera=10.0):
        """Pede pausa a todos, espera os checkpoints (até `espera` s). Os jobs ficam `pausado`."""
        self._parando = True
        with self._trava:
            ativos = list(self._ativos.values())
        for ctrl in ativos:
            ctrl.pedir_pausa()
        fim = time.monotonic() + espera
        for ctrl in ativos:
            ctrl.terminou.wait(max(0.0, fim - time.monotonic()))
        self.store.pausar_fila()
        for _ in self._fios:
            self._fila.put(None)
