/* Painel de recuperação de dados.
   O painel só mostra e comanda: o trabalho mora no serviço. Fechar, recarregar ou abrir por outro endereço
   não muda nada lá. Nada importante fica no navegador (só a largura da lista).
   Tudo que vem do serviço entra por textContent (nunca como HTML). */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const ESTADOS = {
    na_fila: 'Na fila', rodando: 'Em andamento', pausado: 'Pausado', interrompido: 'Interrompido',
    concluido: 'Concluído', falhou: 'Com falha', cancelado: 'Cancelado',
  };
  const ACOES = { na_fila: ['cancelar'], rodando: ['pausar', 'cancelar'], pausado: ['retomar', 'cancelar'], interrompido: ['retomar', 'cancelar'] };
  const MODOS = {
    conectando: 'Conectando…', ao_vivo: 'Ao vivo', reconectando: 'Reconectando…',
    consulta: 'Atualizando a cada 2 segundos', sem_conexao: 'Sem conexão com o serviço',
  };
  const MSG_AVISO_SAIR = 'A recuperação continua em segundo plano. Pode fechar.';
  const FALHAS_ATE_CONSULTA = 3;
  const CONSULTA_MS = 2000;
  const TENTAR_VOLTAR_MS = 10000;
  const HIST_MS = 3000;

  /* ---------- estado (só o que a tela precisa; a verdade está no serviço) ---------- */
  const jobs = new Map();
  let carregou = false;
  let erroLista = '';
  let abertoId = null;
  let nova = false;
  let detalhe = null;            // { id, eventos }
  let detEstado = 'nada';        // nada | carregando | ok | naoachado | erro
  let detErro = '';
  let seqDet = 0;
  let ocupado = false;
  let confirmando = false;
  let erroAcao = '';
  let modo = 'conectando';
  let es = null;
  let falhas = 0;
  let emConsulta = false;
  let timerConsulta = null;
  let timerVoltar = null;
  let timerReconectar = null;
  let timerHist = null;
  let ultimoEstadoAberto = null;
  let avisandoSaida = false;
  let renderAgendado = false;
  let semAcessoAtivo = false;
  let appNome = 'UV Recuperação de Dados';      // padrão se /api/marca falhar; o nome vem de lá

  /* ---------- formatação ---------- */
  function nomeDe(caminho) {
    const partes = String(caminho == null ? '' : caminho).split(/[\\/]+/).filter(Boolean);
    return partes.length ? partes[partes.length - 1] : String(caminho || 'Sem nome');
  }
  function bytes(n) {
    n = Number(n);
    if (!isFinite(n) || n < 0) return '—';
    if (n < 1024) return Math.round(n) + ' B';
    const u = ['KB', 'MB', 'GB', 'TB'];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    const d = n < 10 ? 1 : 0;
    return n.toLocaleString('pt-BR', { minimumFractionDigits: d, maximumFractionDigits: d }) + ' ' + u[i];
  }
  function pct(j) {
    const p = Number(j.progresso);
    if (!isFinite(p)) return 0;
    return Math.max(0, Math.min(100, Math.floor(p * 100)));
  }
  function velocidade(j) {
    const v = Number(j.velocidade);
    return j.estado === 'rodando' && v > 0 ? bytes(v) + '/s' : '—';
  }
  function restante(j) {
    const v = Number(j.velocidade), t = Number(j.bytes_total), f = Number(j.bytes_feitos) || 0;
    if (j.estado !== 'rodando' || !(v > 0) || !(t > 0)) return '—';
    const s = Math.max(0, (t - f) / v);
    if (s < 60) return 'menos de 1 min';
    if (s < 3600) return 'cerca de ' + Math.round(s / 60) + ' min';
    if (s < 172800) {
      const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
      return h + ' h ' + String(m).padStart(2, '0') + ' min';
    }
    return 'mais de 2 dias';
  }
  function ruins(j) { return Math.max(0, parseInt(j.setores_ruins, 10) || 0); }
  function textoRuins(n) { return n === 1 ? '1 trecho ilegível' : n + ' trechos ilegíveis'; }
  function quando(v) {
    if (v == null || v === '') return '';
    const d = new Date(typeof v === 'number' ? (v < 1e12 ? v * 1000 : v) : v);
    if (isNaN(d.getTime())) return String(v);
    return d.toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
  function comparar(a, b) {                 // mais novo primeiro
    const x = a.criado_em, y = b.criado_em;
    if (x !== y && x != null && y != null) return x < y ? 1 : -1;
    return a.id < b.id ? 1 : a.id > b.id ? -1 : 0;
  }

  /* ---------- conversa com o serviço ---------- */
  async function api(metodo, url, corpo) {
    let r;
    try {
      r = await fetch(url, {
        method: metodo, credentials: 'same-origin', cache: 'no-store',
        headers: corpo ? { 'Content-Type': 'application/json' } : {},
        body: corpo ? JSON.stringify(corpo) : undefined,
      });
    } catch (e) {
      const err = new Error('Sem conexão com o serviço.');
      err.status = 0;
      throw err;
    }
    let dados = null;
    try { dados = await r.json(); } catch (e) { /* sem corpo */ }
    if (r.status === 401) semAcesso();
    if (!r.ok) {
      const err = new Error((dados && typeof dados.erro === 'string' && dados.erro) || 'Não foi possível concluir. Tente de novo.');
      err.status = r.status;
      throw err;
    }
    return dados;
  }
  function semAcesso() {
    semAcessoAtivo = true;
    $('sem-acesso').hidden = false;
    pararTudo();
  }
  function pararTudo() {
    if (es) { es.close(); es = null; }
    clearTimeout(timerReconectar);
    clearTimeout(timerVoltar);
    clearInterval(timerConsulta);
    timerConsulta = null;
  }

  const listaDe = (d) => (Array.isArray(d) ? d : d && Array.isArray(d.jobs) ? d.jobs : []);
  const jobDe = (d) => (d && d.job && typeof d.job === 'object' ? d.job : d);

  function substituir(lista) {
    const ids = new Set();
    for (const j of lista) {
      if (j && j.id) { jobs.set(j.id, j); ids.add(j.id); }
    }
    for (const id of [...jobs.keys()]) if (!ids.has(id)) jobs.delete(id);
    carregou = true;
    erroLista = '';
    depoisDeMudar();
  }
  function aplicarJob(j) {
    if (!j || typeof j !== 'object' || !j.id) return;
    jobs.set(j.id, j);
    carregou = true;
    depoisDeMudar();
  }
  function depoisDeMudar() {
    const aberto = abertoId && jobs.get(abertoId);
    if (aberto) {
      const mudou = aberto.estado !== ultimoEstadoAberto;
      ultimoEstadoAberto = aberto.estado;
      if (mudou) confirmando = confirmando && (ACOES[aberto.estado] || []).includes('cancelar');
      if (mudou && detEstado === 'ok') carregarDetalhe(abertoId, true);
      else if (detEstado === 'ok' && $('j-hist').open) agendarHistorico();
      if (detEstado === 'naoachado') carregarDetalhe(abertoId, true);
    }
    render();
  }
  function agendarHistorico() {
    if (timerHist) return;
    timerHist = setTimeout(() => { timerHist = null; if (abertoId) carregarDetalhe(abertoId, true); }, HIST_MS);
  }

  async function carregarLista() {
    try {
      substituir(listaDe(await api('GET', '/api/jobs')));
    } catch (e) {
      if (!carregou) { erroLista = e.message; render(); }
      throw e;
    }
  }

  async function carregarDetalhe(id, silencioso) {
    const meu = ++seqDet;
    if (!silencioso || !detalhe || detalhe.id !== id) {
      detEstado = 'carregando';
      detalhe = { id, eventos: [] };
      render();
    }
    try {
      const d = await api('GET', '/api/jobs/' + encodeURIComponent(id));
      if (meu !== seqDet || abertoId !== id) return;
      const j = jobDe(d);
      let ev = Array.isArray(d.eventos) ? d.eventos : [];
      if (ev.length && ev[0] && ev[0].id != null) ev = ev.slice().sort((a, b) => Number(b.id) - Number(a.id));
      if (j && j.id) jobs.set(j.id, j);
      ultimoEstadoAberto = j && j.estado;
      detalhe = { id, eventos: ev };
      detEstado = 'ok';
    } catch (e) {
      if (meu !== seqDet || abertoId !== id) return;
      if (e.status === 404) detEstado = 'naoachado';
      else if (!silencioso || detEstado !== 'ok') { detEstado = 'erro'; detErro = e.message; }
    }
    render();
  }

  /* ---------- ao vivo: SSE, reconexão e consulta ---------- */
  function definirModo(m) {
    if (modo === m) return;
    modo = m;
    render();
  }
  function conectar() {
    if (semAcessoAtivo) return;
    if (es) es.close();
    let fonte;
    try { fonte = new EventSource('/api/eventos'); } catch (e) { falharConexao(); return; }
    es = fonte;
    fonte.addEventListener('open', () => {
      if (es !== fonte) return;
      falhas = 0;
      if (emConsulta) pararConsulta();
      definirModo('ao_vivo');
      if (abertoId && detEstado !== 'carregando') carregarDetalhe(abertoId, true);
    });
    fonte.addEventListener('jobs', (e) => {
      if (es !== fonte) return;
      try { substituir(listaDe(JSON.parse(e.data))); } catch (x) { /* mensagem quebrada: a próxima corrige */ }
    });
    fonte.addEventListener('job', (e) => {
      if (es !== fonte) return;
      try { aplicarJob(jobDe(JSON.parse(e.data))); } catch (x) { /* idem */ }
    });
    fonte.onerror = () => {
      if (es !== fonte) return;
      if (emConsulta) { fonte.close(); es = null; return; }      // tentativa de voltar que não deu
      falhas++;
      if (fonte.readyState === 2) es = null;                     // o navegador desistiu: tentamos nós
      if (falhas >= FALHAS_ATE_CONSULTA) { fonte.close(); es = null; iniciarConsulta(); return; }
      definirModo('reconectando');
      if (!es) timerReconectar = setTimeout(conectar, 1000 * falhas);
    };
  }
  function falharConexao() {
    falhas++;
    if (falhas >= FALHAS_ATE_CONSULTA) iniciarConsulta();
    else { definirModo('reconectando'); timerReconectar = setTimeout(conectar, 1000 * falhas); }
  }
  function iniciarConsulta() {
    if (emConsulta || semAcessoAtivo) return;
    emConsulta = true;
    definirModo('consulta');
    const rodada = async () => {
      try {
        await carregarLista();
        if (modo === 'sem_conexao') definirModo('consulta');
        if (abertoId && (detEstado === 'erro')) carregarDetalhe(abertoId, true);
        else if (abertoId && detEstado === 'ok' && $('j-hist').open) agendarHistorico();
      } catch (e) {
        if (!semAcessoAtivo) definirModo('sem_conexao');
      }
    };
    rodada();
    timerConsulta = setInterval(rodada, CONSULTA_MS);
    const tentarVoltar = () => {
      if (!emConsulta) return;
      conectar();
      timerVoltar = setTimeout(tentarVoltar, TENTAR_VOLTAR_MS);
    };
    timerVoltar = setTimeout(tentarVoltar, TENTAR_VOLTAR_MS);
  }
  function pararConsulta() {
    emConsulta = false;
    clearInterval(timerConsulta);
    clearTimeout(timerVoltar);
    timerConsulta = null;
  }

  /* ---------- rotas ---------- */
  function lerRota() {
    const m = location.pathname.match(/^\/jobs\/([^/]+)\/?$/);
    let id = null;
    if (m) { try { id = decodeURIComponent(m[1]); } catch (e) { id = m[1]; } }
    return { id, nova: !id && location.hash === '#nova' };
  }
  function ir(url) {
    history.pushState(null, '', url);
    aplicarRota();
  }
  function aplicarRota() {
    const r = lerRota();
    nova = r.nova;
    if (r.id !== abertoId) {
      abertoId = r.id;
      confirmando = false;
      erroAcao = '';
      detalhe = null;
      detEstado = 'nada';
      ultimoEstadoAberto = null;
      seqDet++;
      if (abertoId) {
        const j = jobs.get(abertoId);
        ultimoEstadoAberto = j && j.estado;
        carregarDetalhe(abertoId);
        return;
      }
    }
    render();
  }

  /* ---------- desenho ---------- */
  function agendarRender() {
    if (renderAgendado) return;
    renderAgendado = true;
    setTimeout(() => { renderAgendado = false; desenhar(); }, 0);
  }
  function render() { agendarRender(); }

  function desenhar() {
    desenharTopo();
    desenharLista();
    desenharPagina();
    atualizarAvisoDeSaida();
  }

  function desenharTopo() {
    document.body.dataset.tela = abertoId ? 'job' : nova ? 'nova' : 'lista';
    const c = $('conexao');
    c.dataset.modo = modo;
    $('conexao-texto').textContent = MODOS[modo];
    const j = abertoId && jobs.get(abertoId);
    document.title = (j ? nomeDe(j.origem) + ' — ' : '') + appNome;
  }

  const itens = new Map();
  function criarItem(id) {
    const a = document.createElement('a');
    a.className = 'item';
    a.href = '/jobs/' + encodeURIComponent(id);
    const topo = document.createElement('div'); topo.className = 'item-topo';
    const nome = document.createElement('span'); nome.className = 'item-nome';
    const tag = document.createElement('span'); tag.className = 'tag';
    topo.append(nome, tag);
    const barra = document.createElement('div'); barra.className = 'barra';
    const cheia = document.createElement('div'); cheia.className = 'barra-cheia';
    barra.append(cheia);
    const meta = document.createElement('div'); meta.className = 'item-meta';
    const esq = document.createElement('span'); esq.className = 'esq';
    const dir = document.createElement('span'); dir.className = 'dir';
    meta.append(esq, dir);
    a.append(topo, barra, meta);
    a.addEventListener('click', (e) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      ir(a.getAttribute('href'));
    });
    const li = document.createElement('li');
    li.append(a);
    return { li, a, nome, tag, barra, cheia, esq, dir };
  }
  function atualizarItem(it, j) {
    const n = nomeDe(j.origem);
    it.nome.textContent = n;
    it.a.title = String(j.origem == null ? '' : j.origem);
    it.tag.textContent = ESTADOS[j.estado] || String(j.estado || '');
    it.tag.dataset.estado = j.estado;
    it.a.dataset.estado = j.estado;
    it.barra.dataset.estado = j.estado;
    it.cheia.style.width = pct(j) + '%';
    const partes = [pct(j) + ' %'];
    if (j.estado === 'rodando') {
      const v = velocidade(j), r = restante(j);
      if (v !== '—') partes.push(v);
      if (r !== '—') partes.push(r);
    }
    it.esq.textContent = partes.join(' · ');
    it.esq.title = it.esq.textContent;
    const r = ruins(j);
    it.dir.textContent = r ? textoRuins(r) : '';
    it.dir.hidden = !r;
    if (j.id === abertoId) it.a.setAttribute('aria-current', 'page'); else it.a.removeAttribute('aria-current');
  }
  function desenharLista() {
    const lista = [...jobs.values()].sort(comparar);
    $('lista-carregando').hidden = carregou || !!erroLista;
    $('lista-erro').hidden = !(erroLista && !carregou);
    $('lista-erro-texto').textContent = erroLista ? erroLista + ' Tentando de novo…' : '';
    $('lista-vazia').hidden = !(carregou && lista.length === 0);
    const ul = $('lista-itens');
    ul.hidden = lista.length === 0;
    const vivos = new Set();
    lista.forEach((j, i) => {
      vivos.add(j.id);
      let it = itens.get(j.id);
      if (!it) { it = criarItem(j.id); itens.set(j.id, it); }
      atualizarItem(it, j);
      if (ul.children[i] !== it.li) ul.insertBefore(it.li, ul.children[i] || null);
    });
    for (const [id, it] of itens) {
      if (!vivos.has(id)) { it.li.remove(); itens.delete(id); }
    }
  }

  function desenharPagina() {
    $('v-nova').hidden = !!abertoId;
    $('v-job').hidden = !abertoId;
    if (!abertoId) return;
    const j = jobs.get(abertoId);
    const temJob = !!j;
    $('j-nome').textContent = j ? nomeDe(j.origem) : 'Recuperação';
    $('j-nome').title = j ? String(j.origem == null ? '' : j.origem) : '';
    const tag = $('j-tag');
    tag.hidden = !j;
    if (j) { tag.textContent = ESTADOS[j.estado] || String(j.estado || ''); tag.dataset.estado = j.estado; }
    $('j-carregando').hidden = !(!temJob && (detEstado === 'carregando' || detEstado === 'nada'));
    $('j-naoachado').hidden = !(!temJob && detEstado === 'naoachado');
    $('j-erro').hidden = !(!temJob && detEstado === 'erro');
    $('j-erro-texto').textContent = detErro;
    $('j-conteudo').hidden = !temJob;
    if (!j) return;

    const p = pct(j);
    $('j-pct').textContent = p + ' %';
    $('j-msg').textContent = j.mensagem ? String(j.mensagem) : '';
    $('j-msg').title = $('j-msg').textContent;
    const barra = $('j-barra');
    barra.dataset.estado = j.estado;
    barra.setAttribute('aria-valuenow', String(p));
    $('j-barra-cheia').style.width = p + '%';
    const total = Number(j.bytes_total) > 0 ? ' de ' + bytes(j.bytes_total) : '';
    $('j-copiado').textContent = bytes(j.bytes_feitos || 0) + total;
    $('j-vel').textContent = velocidade(j);
    $('j-resta').textContent = restante(j);
    $('j-ruins').textContent = String(ruins(j));
    $('j-origem').textContent = String(j.origem == null ? '' : j.origem);
    $('j-origem').title = $('j-origem').textContent;
    $('j-destino').textContent = String(j.destino == null ? '' : j.destino);
    $('j-destino').title = $('j-destino').textContent;

    const alerta = $('j-alerta');
    let txt = '', tipo = '';
    if (j.estado === 'interrompido') {
      txt = 'Esta recuperação foi interrompida porque o programa parou de repente. O que já foi copiado está guardado. Clique em Retomar para continuar de onde parou.';
      tipo = 'aviso';
    } else if (j.estado === 'falhou') {
      txt = 'Não foi possível terminar.' + (j.erro ? ' ' + String(j.erro) : '');
      tipo = 'perigo';
    } else if (j.estado === 'cancelado') {
      txt = 'Recuperação cancelada. O que já foi copiado continua guardado.';
    } else if (j.estado === 'concluido') {
      txt = ruins(j) ? 'Cópia pronta, mas alguns trechos do original não puderam ser lidos. Eles ficaram em branco na cópia.' : 'Cópia pronta.';
    }
    alerta.hidden = !txt;
    alerta.textContent = txt;
    if (tipo) alerta.dataset.tipo = tipo; else delete alerta.dataset.tipo;

    const permitidas = ACOES[j.estado] || [];
    $('j-acoes').hidden = permitidas.length === 0 || confirmando;
    for (const b of $('j-acoes').querySelectorAll('[data-acao]')) {
      b.hidden = !permitidas.includes(b.dataset.acao);
      b.disabled = ocupado;
    }
    $('j-confirma').hidden = !confirmando;
    $('j-confirma-sim').disabled = ocupado;
    $('j-confirma-nao').disabled = ocupado;
    $('j-acao-erro').hidden = !erroAcao;
    $('j-acao-erro').textContent = erroAcao;

    desenharHistorico();
  }

  let histChave = '';
  function desenharHistorico() {
    const ev = detalhe && detalhe.id === abertoId ? detalhe.eventos : [];
    const chave = abertoId + '|' + ev.length + '|' + (ev[0] ? ev[0].id + '/' + ev[0].texto : '');
    $('j-hist-vazio').hidden = ev.length > 0;
    if (chave === histChave) return;
    histChave = chave;
    const ul = $('j-eventos');
    ul.replaceChildren();
    for (const e of ev) {
      const li = document.createElement('li');
      const t = document.createElement('time');
      t.textContent = quando(e.quando);
      const s = document.createElement('span');
      s.textContent = String(e.texto == null ? '' : e.texto);
      li.append(t, s);
      ul.append(li);
    }
  }

  /* ---------- aviso ao sair: só com recuperação em andamento, e nunca impede de fechar ---------- */
  function aoSair(e) {
    e.preventDefault();
    e.returnValue = MSG_AVISO_SAIR;
    return MSG_AVISO_SAIR;
  }
  function atualizarAvisoDeSaida() {
    const rodando = [...jobs.values()].some((j) => j.estado === 'rodando');
    if (rodando && !avisandoSaida) { window.addEventListener('beforeunload', aoSair); avisandoSaida = true; }
    else if (!rodando && avisandoSaida) { window.removeEventListener('beforeunload', aoSair); avisandoSaida = false; }
  }

  /* ---------- ações ---------- */
  async function acao(nome) {
    if (ocupado || !abertoId) return;
    const id = abertoId;
    ocupado = true;
    erroAcao = '';
    render();
    try {
      const r = await api('POST', '/api/jobs/' + encodeURIComponent(id) + '/' + nome);
      const j = jobDe(r);
      if (j && j.id === id && j.estado) aplicarJob(j);
    } catch (e) {
      if (id === abertoId) erroAcao = e.message;
    } finally {
      ocupado = false;
      confirmando = false;
    }
    render();
    if (id === abertoId) carregarDetalhe(id, true);
  }
  document.addEventListener('click', (e) => {
    const b = e.target.closest && e.target.closest('[data-acao]');
    if (!b) return;
    if (b.dataset.acao === 'cancelar') { confirmando = true; erroAcao = ''; render(); }
    else acao(b.dataset.acao);
  });
  $('j-confirma-sim').addEventListener('click', () => acao('cancelar'));
  $('j-confirma-nao').addEventListener('click', () => { confirmando = false; render(); });
  $('j-tentar').addEventListener('click', () => abertoId && carregarDetalhe(abertoId));
  $('lista-tentar').addEventListener('click', () => { carregarLista().catch(() => {}); });
  for (const b of document.querySelectorAll('[data-voltar]')) b.addEventListener('click', () => ir('/'));
  for (const a of document.querySelectorAll('[data-inicio]')) a.addEventListener('click', (e) => { e.preventDefault(); ir('/'); });
  $('nova-link').addEventListener('click', (e) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    ir('/#nova');
    if (document.body.dataset.tela === 'nova' || innerWidth > 760) $('f-origem').focus();
  });
  window.addEventListener('popstate', aplicarRota);

  /* ---------- formulário ---------- */
  function erroForm(t) {
    $('f-erro').textContent = t || '';
    $('f-erro').hidden = !t;
  }
  $('f-nova').addEventListener('submit', async (e) => {
    e.preventDefault();
    const origem = $('f-origem').value.trim(), destino = $('f-destino').value.trim();
    if (!origem || !destino) { erroForm('Preencha os dois campos.'); return; }
    erroForm('');
    const botao = $('f-enviar');
    botao.disabled = true;
    botao.textContent = 'Iniciando…';
    try {
      const j = jobDe(await api('POST', '/api/jobs', { tipo: 'imagem', origem, destino, opcoes: {} }));
      aplicarJob(j);
      $('f-nova').reset();
      if (j && j.id) ir('/jobs/' + encodeURIComponent(j.id));
    } catch (err) {
      erroForm(err.message);
    } finally {
      botao.disabled = false;
      botao.textContent = 'Iniciar';
    }
  });
  $('f-nova').addEventListener('input', () => erroForm(''));

  /* ---------- largura da lista: arrasta, recolhe, guarda ---------- */
  const LISTA_MIN = 240, LISTA_MAX = 560, LISTA_PADRAO = 320, LISTA_RECOLHE = 160;
  let lista = { l: LISTA_PADRAO, c: false };
  function lerLista() {
    try {
      const o = JSON.parse(localStorage.getItem('painel.lista') || 'null');
      if (o && typeof o.l === 'number') lista = { l: Math.max(LISTA_MIN, Math.min(LISTA_MAX, Math.round(o.l))), c: !!o.c };
    } catch (e) { /* usa o padrão */ }
  }
  function aplicarLista() {
    document.documentElement.style.setProperty('--lista-l', lista.l + 'px');
    document.body.classList.toggle('recolhida', lista.c);
    $('alternar-lista').setAttribute('aria-expanded', String(!lista.c));
    $('alca').setAttribute('aria-valuenow', String(lista.c ? 0 : lista.l));
  }
  function guardarLista() {
    try { localStorage.setItem('painel.lista', JSON.stringify(lista)); } catch (e) { /* sem espaço: tudo bem */ }
  }
  const alca = $('alca');
  alca.addEventListener('pointerdown', (e) => {
    try { alca.setPointerCapture(e.pointerId); } catch (x) { /* nada */ }
    alca.classList.add('arrastando');
    document.body.classList.add('arrastando-lista');
    alca.dataset.ativo = '1';
  });
  alca.addEventListener('pointermove', (e) => {
    if (!alca.dataset.ativo) return;
    const x = e.clientX - $('corpo').getBoundingClientRect().left;
    if (x < LISTA_RECOLHE) lista.c = true;
    else { lista.c = false; lista.l = Math.max(LISTA_MIN, Math.min(LISTA_MAX, Math.round(x))); }
    aplicarLista();
  });
  const soltar = () => {
    if (!alca.dataset.ativo) return;
    delete alca.dataset.ativo;
    alca.classList.remove('arrastando');
    document.body.classList.remove('arrastando-lista');
    guardarLista();
  };
  alca.addEventListener('pointerup', soltar);
  alca.addEventListener('pointercancel', soltar);
  alca.addEventListener('dblclick', () => { lista = { l: LISTA_PADRAO, c: false }; aplicarLista(); guardarLista(); });
  alca.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    lista.c = false;
    lista.l = Math.max(LISTA_MIN, Math.min(LISTA_MAX, lista.l + (e.key === 'ArrowRight' ? 16 : -16)));
    aplicarLista();
    guardarLista();
  });
  $('alternar-lista').addEventListener('click', () => { lista.c = !lista.c; aplicarLista(); guardarLista(); });

  /* ---------- marca: vem do serviço, nada de nome de empresa no código ---------- */
  async function carregarMarca() {
    try {
      const m = await api('GET', '/api/marca');
      if (m && typeof m.app === 'string' && m.app.trim()) {
        appNome = m.app.trim();
        $('marca-nome').textContent = appNome;
        $('sem-acesso-titulo').textContent = 'Abra pelo atalho ' + appNome;
      }
      const emp = m && typeof m.empresa === 'string' ? m.empresa.trim() : '';
      $('marca-sub').textContent = emp;
      $('marca-sub').hidden = !emp;
      if (m && typeof m.cor === 'string' && /^#[0-9a-fA-F]{6}$/.test(m.cor)) {
        const r = document.documentElement.style;
        r.setProperty('--btn', m.cor);
        r.setProperty('--btn-hover', 'color-mix(in srgb, ' + m.cor + ' 80%, black)');
      }
      render();
    } catch (e) { /* sem marca: fica o padrão neutro */ }
  }

  /* ---------- início ---------- */
  lerLista();
  aplicarLista();
  aplicarRota();
  carregarLista().catch(() => {});
  carregarMarca();
  conectar();
  window.__painel = { // só para leitura nos testes de tela
    estado: () => ({ modo, falhas, emConsulta, abertoId, detEstado, avisandoSaida, jobs: jobs.size }),
  };
})();
