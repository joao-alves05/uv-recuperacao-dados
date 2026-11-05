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
  let rota = { tela: 'inicio', id: null };
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
  function tituloJob(j) {
    if (j && j.tipo === 'varredura') return 'Busca em ' + nomeDe(j.origem);
    if (j && j.tipo === 'recuperar') return 'Arquivos recuperados';
    return nomeDe(j && j.origem);
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
  function decodificar(t) {
    try { return decodeURIComponent(t); } catch (e) { return t; }
  }
  function lerRota() {
    const m = location.pathname.match(/^\/jobs\/([^/]+)\/?$/);
    if (m) return { tela: 'job', id: decodificar(m[1]) };
    const h = location.hash.replace(/^#/, '');
    if (h === 'lista') return { tela: 'lista', id: null };
    if (h === 'avancado' || h === 'nova') return { tela: 'avancado', id: null };
    const seg = h.split('/').map(decodificar);
    if ((seg[0] === 'volume' || seg[0] === 'disco') && seg[1]) {
      const tela = seg[2] === 'recuperar' ? 'buscar' : seg[2] === 'imagem' ? 'imagem' : 'alvo';
      return { tela, id: null, alvoTipo: seg[0], alvoId: seg[1], passo: seg[3] === 'confirmar' ? 3 : 2 };
    }
    return { tela: 'inicio', id: null };
  }
  function ir(url) {
    history.pushState(null, '', url);
    aplicarRota();
  }
  function aplicarRota() {
    const r = lerRota();
    rota = r;
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
    if (r.tela === 'inicio' && fontesEstado === 'ok' && Date.now() - fontesQuando > 5000) carregarFontes();
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
    desenharInicio();
    desenharFluxo();
    atualizarAvisoDeSaida();
  }

  function desenharTopo() {
    document.body.dataset.tela = rota.tela === 'lista' ? 'lista' : rota.tela === 'job' ? 'job' : 'inicio';
    const c = $('conexao');
    c.dataset.modo = modo;
    $('conexao-texto').textContent = MODOS[modo];
    const j = abertoId && jobs.get(abertoId);
    document.title = (j ? tituloJob(j) + ' — ' : '') + appNome;
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
    const n = tituloJob(j);
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
    const vt = rota.tela === 'lista' ? 'inicio' : rota.tela;
    $('v-inicio').hidden = vt !== 'inicio';
    $('v-volume').hidden = vt !== 'alvo';
    $('v-buscar').hidden = vt !== 'buscar';
    $('v-imagem').hidden = vt !== 'imagem';
    $('v-nova').hidden = vt !== 'avancado';
    $('v-job').hidden = !abertoId;
    if (!abertoId) { destruirResultados(); return; }
    const j = jobs.get(abertoId);
    const temJob = !!j;
    $('j-nome').textContent = j ? tituloJob(j) : 'Recuperação';
    $('j-nome').title = j ? String(j.origem == null ? '' : j.origem) : '';
    const tag = $('j-tag');
    tag.hidden = !j;
    if (j) { tag.textContent = ESTADOS[j.estado] || String(j.estado || ''); tag.dataset.estado = j.estado; }
    $('j-carregando').hidden = !(!temJob && (detEstado === 'carregando' || detEstado === 'nada'));
    $('j-naoachado').hidden = !(!temJob && detEstado === 'naoachado');
    $('j-erro').hidden = !(!temJob && detEstado === 'erro');
    $('j-erro-texto').textContent = detErro;
    $('j-conteudo').hidden = !temJob;
    if (!j) { destruirResultados(); return; }
    const tipoJob = j.tipo || 'imagem';

    const p = pct(j);
    $('j-pct').textContent = p + ' %';
    $('j-msg').textContent = j.mensagem ? String(j.mensagem) : '';
    $('j-msg').title = $('j-msg').textContent;
    const barra = $('j-barra');
    barra.dataset.estado = j.estado;
    barra.setAttribute('aria-valuenow', String(p));
    $('j-barra-cheia').style.width = p + '%';
    const total = Number(j.bytes_total) > 0 ? ' de ' + bytes(j.bytes_total) : '';
    $('j-l-copiado').textContent = tipoJob === 'varredura' ? 'Varrido' : tipoJob === 'recuperar' ? 'Recuperado' : 'Copiado';
    $('j-n-ruins').hidden = tipoJob !== 'imagem';
    $('j-copiado').textContent = bytes(j.bytes_feitos || 0) + total;
    $('j-vel').textContent = velocidade(j);
    $('j-resta').textContent = restante(j);
    $('j-ruins').textContent = String(ruins(j));
    $('j-origem').textContent = String(j.origem == null ? '' : j.origem);
    $('j-origem').title = $('j-origem').textContent;
    $('j-destino').textContent = String(j.destino == null ? '' : j.destino);
    $('j-destino').title = $('j-destino').textContent;
    $('j-dt-destino').textContent = tipoJob === 'recuperar' ? 'Salvando em' : 'Cópia';
    $('j-dt-destino').hidden = $('j-destino').hidden = tipoJob === 'varredura' || !j.destino;
    $('j-confirma-txt').textContent = tipoJob === 'varredura' ? 'Cancelar a busca? O que já foi achado continua na lista.' : tipoJob === 'recuperar' ? 'Cancelar a recuperação? O que já foi salvo continua na pasta.' : 'Cancelar esta recuperação? O que já foi copiado continua guardado.';

    const alerta = $('j-alerta');
    let txt = '', tipo = '';
    if (j.estado === 'interrompido') {
      txt = 'Esta recuperação foi interrompida porque o programa parou de repente. ' + (tipoJob === 'varredura' ? 'O que já foi achado está na lista.' : 'O que já foi copiado está guardado.') + ' Clique em Retomar para continuar de onde parou.';
      tipo = 'aviso';
    } else if (j.estado === 'falhou') {
      txt = 'Não foi possível terminar.' + (j.erro ? ' ' + String(j.erro) : '');
      tipo = 'perigo';
    } else if (j.estado === 'cancelado') {
      txt = tipoJob === 'varredura' ? 'Busca cancelada. O que já foi achado continua na lista.' : 'Recuperação cancelada. O que já foi copiado continua guardado.';
    } else if (j.estado === 'concluido') {
      txt = tipoJob === 'varredura' ? 'Busca terminada. Marque os arquivos que você quer recuperar.' : tipoJob === 'recuperar' ? 'Arquivos recuperados. Abra a pasta para ver.'
        : ruins(j) ? 'Cópia pronta, mas alguns trechos do original não puderam ser lidos. Eles ficaram em branco na cópia.' : 'Cópia pronta.';
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

    const pos = tipoJob === 'recuperar' && j.estado === 'concluido';
    $('j-pos').hidden = !pos;
    if (pos) $('j-relatorio').href = '/api/jobs/' + encodeURIComponent(j.id) + '/relatorio';
    if (!pos) { $('j-pos-erro').hidden = true; }
    $('v-job').classList.toggle('larga', tipoJob === 'varredura');
    document.querySelector('#j-conteudo .progresso').hidden = tipoJob === 'varredura' && j.estado === 'concluido';
    if (tipoJob === 'varredura') mostrarResultados(j); else destruirResultados();

    desenharHistorico();
  }

  /* ---------- resultados da busca (lista de achados) ---------- */
  let resComp = null;
  function destruirResultados() {
    if (!resComp) return;
    resComp.obj.destruir();
    resComp = null;
    $('j-resultados').hidden = true;
  }
  function mostrarResultados(j) {
    if (!window.Resultados) return;
    if (resComp && resComp.id !== j.id) destruirResultados();
    if (!resComp) {
      $('j-resultados').hidden = false;
      resComp = { id: j.id, obj: window.Resultados.montar($('j-resultados'), {
        jobId: j.id, api, bytes, destinos: () => destinosPara(jobs.get(j.id)),
        aoRecuperar: (novo) => { if (novo && novo.id) { aplicarJob(novo); ir('/jobs/' + encodeURIComponent(novo.id)); } },
      }) };
    }
    resComp.obj.atualizar(j);
  }
  function destinosPara(j) {
    const origem = j ? textoDe(j.origem) : '';
    const a = [...alvos.volumes, ...alvos.discos].find((x) => x.caminho === origem && !x.vazio);
    let lista;
    if (a) lista = candidatos(a);
    else {
      const l = /^\\\\\.\\([A-Za-z]:)$/.exec(origem);
      lista = alvos.volumes.filter((v) => v.tipo === 'volume' && !v.vazio && v.tipoFonte !== 'optico' && /^[A-Za-z]:$/.test(v.letra) && !(l && v.letra.toUpperCase() === l[1].toUpperCase()));
    }
    const sug = a ? sugerido(a, lista) : lista[0];
    return lista.map((v) => ({ rotulo: v.titulo, base: v.letra, livre: v.livre, sugerido: v === sug }));
  }
  $('j-abrir-pasta').addEventListener('click', async () => {
    if (!abertoId) return;
    $('j-pos-erro').hidden = true;
    try { await api('POST', '/api/jobs/' + encodeURIComponent(abertoId) + '/abrir'); } catch (e) {
      const j = jobs.get(abertoId);
      $('j-pos-erro').textContent = e.message + (j && j.destino ? ' A pasta é ' + j.destino : '');
      $('j-pos-erro').hidden = false;
    }
  });

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

  /* ---------- discos: o que aparece em "Meu computador" ---------- */
  const NS = 'http://www.w3.org/2000/svg';
  const ICONES = {
    hd: ['M4 6h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1z', 'M3 14h18', 'M7 16h.01'],
    ssd: ['M5 5h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z', 'M8 9h8v6H8z', 'M6 12h2M16 12h2'],
    usb: ['M9 2.5h6V8H9z', 'M7 8h10v13.5H7z', 'M12 12v5'],
    cartao: ['M7 2h8l4 4v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z', 'M9 6v3M12 6v3M15 6v3'],
    optico: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z', 'M12 10a2 2 0 1 0 0 4 2 2 0 0 0 0-4z'],
    disquete: ['M5 3h12l3 3v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z', 'M8 3v5h7V3', 'M8 21v-7h8v7'],
  };
  const TIPO_NOME = { hd: 'Disco rígido (HD)', ssd: 'SSD', usb: 'Pen drive', cartao: 'Cartão de memória', optico: 'CD/DVD', disquete: 'Disquete' };
  const TIPO_TITULO = { hd: 'Disco local', ssd: 'Disco local', usb: 'Pen drive', cartao: 'Cartão de memória', optico: 'CD/DVD', disquete: 'Disquete' };
  const SEM_MIDIA = { cartao: 'Sem cartão', usb: 'Sem cartão', optico: 'Sem disco', disquete: 'Sem disquete' };
  const ADMIN_OBJETO = { cartao: 'o cartão', usb: 'o pen drive' };

  function el(tag, cls, texto) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (texto != null) e.textContent = texto;
    return e;
  }
  function icone(tipo) {
    const s = document.createElementNS(NS, 'svg');
    s.setAttribute('viewBox', '0 0 24 24');
    s.setAttribute('aria-hidden', 'true');
    for (const d of ICONES[tipo] || ICONES.hd) {
      const p = document.createElementNS(NS, 'path');
      p.setAttribute('d', d);
      s.append(p);
    }
    return s;
  }
  const tipoDe = (f) => (ICONES[f.tipo] ? f.tipo : 'hd');
  const textoDe = (v) => String(v == null ? '' : v);

  function montarAlvos(lista) {
    const volumes = [], discos = [];
    for (const f of lista) {
      if (!f || typeof f !== 'object') continue;
      const tipo = tipoDe(f);
      const vols = Array.isArray(f.volumes) ? f.volumes.filter((v) => v && typeof v === 'object') : [];
      const doSistema = !!f.sistema;
      const marcado = doSistema ? (vols.find((v) => /^c:?$/i.test(textoDe(v.letra))) || vols[0]) : null;
      const base = { fonte: f, tipoFonte: tipo, removivel: !!f.removivel };
      for (const v of vols) {
        const letra = textoDe(v.letra);
        const tam = Number(v.tamanho) || 0;
        const rotulo = textoDe(v.rotulo).trim();
        const titulo = (rotulo || TIPO_TITULO[tipo]) + (letra ? ' (' + letra + ')' : '');
        const l = letra.replace(':', '');
        volumes.push(Object.assign({}, base, {
          tipo: 'volume', hash: 'volume/' + encodeURIComponent(l), letra, l, titulo, tamanho: tam, livre: Number(v.livre) || 0,
          fs: textoDe(v.sistema_arquivos), sistema: v === marcado, vazio: tam === 0,
          caminho: /^[A-Za-z]:$/.test(letra) && tipo !== 'optico' ? '\\\\.\\' + letra : textoDe(f.caminho),
        }));
      }
      if (!vols.length && !(Number(f.tamanho) > 0)) {
        volumes.push(Object.assign({}, base, { tipo: 'vazio', titulo: textoDe(f.nome) || TIPO_TITULO[tipo], tamanho: 0, livre: 0, fs: '', sistema: false, vazio: true, caminho: textoDe(f.caminho) }));
      }
      if (Number(f.tamanho) > 0 && tipo !== 'optico') {
        const letras = vols.map((v) => textoDe(v.letra)).filter(Boolean);
        discos.push(Object.assign({}, base, {
          tipo: 'disco', hash: 'disco/' + encodeURIComponent(textoDe(f.id)), titulo: textoDe(f.nome) || TIPO_TITULO[tipo], tamanho: Number(f.tamanho) || 0, livre: 0,
          fs: '', sistema: doSistema, vazio: false, caminho: textoDe(f.caminho), letras,
        }));
      }
    }
    // removíveis com mídia, depois discos fixos, depois leitores vazios; o disco do Windows por último
    const peso = (a) => (a.sistema ? 3 : a.vazio ? 2 : a.removivel ? 0 : 1);
    const ordenar = (arr) => arr.map((a, i) => [a, i]).sort((x, y) => peso(x[0]) - peso(y[0]) || x[1] - y[1]).map((x) => x[0]);
    return { volumes: ordenar(volumes), discos: ordenar(discos) };
  }
  function subtitulo(a) {
    if (a.vazio) return SEM_MIDIA[a.tipoFonte] || 'Sem mídia';
    const t = TIPO_NOME[a.tipoFonte] || 'Disco';
    return a.tipo === 'disco' ? t : [t, a.fs].filter(Boolean).join(' · ');
  }
  function meta(a) {
    if (a.vazio) return '';
    if (a.tipo === 'disco') return bytes(a.tamanho) + ' · ' + (a.letras.length ? a.letras.join(', ') : 'sem letra');
    if (a.tipoFonte === 'optico') return bytes(a.tamanho);
    return bytes(a.livre) + ' livres de ' + bytes(a.tamanho);
  }
  function cartaoFonte(a, botao) {
    const c = el(botao ? 'button' : 'div', 'fonte');
    if (botao) c.type = 'button';
    if (a.vazio) { c.classList.add('sem-midia'); if (botao) c.disabled = true; }
    c.dataset.tipo = a.tipoFonte;
    const ic = el('span', 'fonte-icone');
    ic.append(icone(a.tipoFonte));
    const topo = el('span', 'fonte-topo');
    const nome = el('span', 'fonte-nome', a.titulo);
    nome.title = a.titulo;
    topo.append(nome);
    const sub = el('span', 'fonte-sub', subtitulo(a));
    c.append(ic, topo, sub);
    if (!a.vazio && a.tipo === 'volume' && a.tipoFonte !== 'optico' && a.tamanho > 0) {
      const barra = el('span', 'barra');
      const cheia = el('span', 'barra-cheia');
      cheia.style.width = Math.max(0, Math.min(100, Math.round((1 - a.livre / a.tamanho) * 100))) + '%';
      barra.append(cheia);
      c.append(barra);
    }
    const m = meta(a);
    if (m) c.append(el('span', 'fonte-meta', m));
    if (a.sistema) c.append(el('span', 'tag', 'Disco do Windows'));
    return c;
  }

  /* ---------- estado dos discos ---------- */
  let fontes = null;
  let alvos = { volumes: [], discos: [] };
  let fontesEstado = 'carregando';       // carregando | ok | erro
  let fontesErro = '';
  let fontesVer = 0;
  let fontesAtualizando = false;
  let fontesQuando = 0;
  let seqFontes = 0;
  let admin = null;                      // true | false | null (o serviço não disse)

  async function carregarFontes() {
    const meu = ++seqFontes;
    fontesAtualizando = true;
    if (!fontes) fontesEstado = 'carregando';
    render();
    try {
      const s = await api('GET', '/api/saude').catch(() => null);
      admin = s && typeof s.admin === 'boolean' ? s.admin : null;
      const d = await api('GET', '/api/fontes');
      if (meu !== seqFontes) return;
      fontes = Array.isArray(d) ? d : d && Array.isArray(d.fontes) ? d.fontes : [];
      alvos = montarAlvos(fontes);
      fontesEstado = 'ok';
      fontesQuando = Date.now();
      fontesVer++;
    } catch (e) {
      if (meu !== seqFontes) return;
      if (!fontes) { fontesEstado = 'erro'; fontesErro = e.message; }
    } finally {
      if (meu === seqFontes) fontesAtualizando = false;
    }
    render();
  }

  /* ---------- tela inicial ---------- */
  let inicioVer = -1;
  function desenharInicio() {
    const ok = fontesEstado === 'ok';
    $('i-carregando').hidden = fontesEstado !== 'carregando';
    $('i-erro').hidden = fontesEstado !== 'erro';
    $('i-erro-texto').textContent = fontesErro;
    $('i-vazio').hidden = !(ok && alvos.volumes.length === 0 && alvos.discos.length === 0);
    $('i-conteudo').hidden = !(ok && (alvos.volumes.length || alvos.discos.length));
    const bt = $('i-atualizar');
    bt.disabled = fontesAtualizando;
    bt.textContent = fontesAtualizando ? 'Atualizando…' : 'Atualizar';
    $('ver-lista').textContent = jobs.size ? 'Ver recuperações (' + jobs.size + ')' : 'Ver recuperações';
    if (!ok || inicioVer === fontesVer) return;
    inicioVer = fontesVer;
    const g = $('i-volumes'), d = $('i-discos-grade');
    g.replaceChildren();
    d.replaceChildren();
    for (const a of alvos.volumes) g.append(ligar(cartaoFonte(a, true), a));
    for (const a of alvos.discos) d.append(ligar(cartaoFonte(a, true), a));
    $('i-sem-volumes').hidden = alvos.volumes.length > 0;
    if (!alvos.volumes.length && alvos.discos.length) $('i-discos').open = true;
  }
  function ligar(c, a) {
    if (a.hash) c.addEventListener('click', () => ir('/#' + a.hash));
    return c;
  }

  /* ---------- avisos por tipo ---------- */
  function avisosDe(a) {
    const l = [];
    const t = a.tipoFonte;
    if (admin === false && t !== 'optico') {
      l.push({ tipo: 'aviso', texto: 'Para ler ' + (ADMIN_OBJETO[t] || 'o disco') + ', o programa precisa de permissão de administrador: feche e abra pelo atalho “iniciar-admin.cmd”.' });
    }
    if (t === 'optico') l.push({ tipo: 'aviso', texto: 'A leitura de CD/DVD chega numa próxima versão.' });
    if (t === 'usb' || t === 'ssd') l.push({ tipo: '', texto: 'Neste tipo de disco o sistema pode apagar de vez o que foi excluído (TRIM): quanto antes, melhor.' });
    if (t === 'cartao') l.push({ tipo: '', texto: 'Pare de usar o cartão até terminar: não tire fotos nem salve nada nele.' });
    if (t === 'usb') l.push({ tipo: '', texto: 'Pare de usar o pen drive até terminar: não salve nada nele.' });
    if (a.sistema) l.push({ tipo: '', texto: 'Este é o disco do Windows. Use o computador o mínimo possível até terminar.' });
    return l;
  }
  function blocoAvisos(a) {
    const bloco = el('div', 'avisos');
    for (const v of avisosDe(a)) {
      const p = el('p', 'alerta', v.texto);
      if (v.tipo) p.dataset.tipo = v.tipo;
      bloco.append(p);
    }
    return bloco.children.length ? bloco : null;
  }

  /* ---------- painel do volume, busca e cópia ---------- */
  let alvoAtual = null;
  let fluxoChave = '';
  const img = { chave: '', destino: null, nome: '', erro: '', enviando: false };

  function acharAlvo(r) {
    if (!r.alvoTipo || !fontes) return null;
    const lista = r.alvoTipo === 'disco' ? alvos.discos : alvos.volumes;
    const h = (r.alvoTipo + '/' + encodeURIComponent(r.alvoId)).toLowerCase();
    return lista.find((a) => a.hash && a.hash.toLowerCase() === h && !a.vazio) || null;
  }
  const botao = (texto, cls, fn) => {
    const b = el('button', 'btn ' + cls, texto);
    b.type = 'button';
    b.addEventListener('click', fn);
    return b;
  };
  const urlAlvo = (a, resto) => '/#' + a.hash + (resto ? '/' + resto : '');

  function desenharFluxo() {
    const t = rota.tela;
    if (t !== 'alvo' && t !== 'buscar' && t !== 'imagem') { alvoAtual = null; fluxoChave = ''; return; }
    alvoAtual = acharAlvo(rota);
    const corpo = t === 'alvo' ? $('vol-corpo') : t === 'buscar' ? $('buscar-corpo') : $('imagem-corpo');
    const chave = [t, rota.passo, rota.alvoTipo, rota.alvoId, fontesVer, fontesEstado, admin, img.destino, modoBusca, alvoAtual ? 1 : 0].join('|');
    if (chave === fluxoChave) return;
    fluxoChave = chave;
    corpo.replaceChildren();
    if (!alvoAtual) {
      const v = el('div', 'vazio' + (fontesEstado === 'erro' ? ' vazio-erro' : ''));
      v.append(el('p', null, fontesEstado === 'carregando' ? 'Procurando os discos…' : fontesEstado === 'erro' ? fontesErro : 'Não encontramos mais este disco. Ele pode ter sido removido.'));
      if (fontesEstado !== 'carregando') v.append(botao('Voltar ao início', 'btn-suave', () => ir('/')));
      corpo.append(v);
      return;
    }
    if (t === 'alvo') fluxoVolume(corpo, alvoAtual);
    else if (t === 'buscar') fluxoBuscar(corpo, alvoAtual);
    else fluxoImagem(corpo, alvoAtual);
  }
  function fluxoVolume(corpo, a) {
    corpo.append(cartaoFonte(a, false));
    const av = blocoAvisos(a);
    if (av) corpo.append(av);
    const ac = el('div', 'acoes');
    const bloqueado = a.tipoFonte === 'optico';
    const b1 = botao('Recuperar arquivos', 'btn-primario', () => ir(urlAlvo(a, 'recuperar')));
    const b2 = botao('Fazer cópia de segurança (imagem)', 'btn-suave', () => ir(urlAlvo(a, 'imagem')));
    b1.id = 'b-recuperar';
    b2.id = 'b-imagem';
    b1.disabled = b2.disabled = bloqueado;
    ac.append(b1, b2);
    corpo.append(ac);
  }
  const MODOS_BUSCA = [
    ['rapida', 'Rápida', 'Procura as fotos, vídeos e documentos que ainda estão inteiros; leva alguns minutos.'],
    ['profunda', 'Profunda', 'Lê o disco inteiro, byte a byte, e acha mais arquivos, até os danificados; leva bem mais tempo.'],
  ];
  let modoBusca = 'rapida';
  let buscaErro = '';
  let buscaEnviando = false;
  function fluxoBuscar(corpo, a) {
    $('buscar-volta').onclick = () => ir(urlAlvo(a));
    corpo.append(cartaoFonte(a, false));
    const av = blocoAvisos(a);
    if (av) corpo.append(av);
    const c = el('div', 'cartao formulario');
    c.append(el('h2', null, 'Como procurar?'));
    const grupo = el('div', 'opcoes');
    grupo.setAttribute('role', 'radiogroup');
    grupo.setAttribute('aria-label', 'Como procurar');
    for (const [chave, nome, texto] of MODOS_BUSCA) {
      const lab = el('label', 'opcao');
      const r = el('input');
      r.type = 'radio'; r.name = 'modo-busca'; r.value = chave; r.checked = modoBusca === chave;
      r.addEventListener('change', () => { modoBusca = chave; render(); });
      const t = el('span', 'opcao-txt');
      t.append(el('span', 'opcao-nome', nome), el('span', 'fonte-meta opcao-explica', texto));
      lab.append(r, t);
      grupo.append(lab);
    }
    c.append(grupo);
    const erro = el('p', 'alerta alerta-perigo', buscaErro);
    erro.id = 'busca-erro';
    erro.setAttribute('role', 'alert');
    erro.hidden = !buscaErro;
    c.append(erro);
    const ac = el('div', 'acoes');
    const ini = botao('Começar a busca', 'btn-primario', async () => {
      if (buscaEnviando) return;
      buscaEnviando = true; buscaErro = '';
      ini.disabled = true; ini.textContent = 'Iniciando…'; erro.hidden = true;
      try {
        const j = jobDe(await api('POST', '/api/jobs', { tipo: 'varredura', origem: a.caminho, opcoes: { modo: modoBusca } }));
        aplicarJob(j);
        if (j && j.id) ir('/jobs/' + encodeURIComponent(j.id));
      } catch (e) {
        buscaErro = e.message;
        erro.textContent = e.message; erro.hidden = false;
      } finally {
        buscaEnviando = false;
        ini.disabled = a.tipoFonte === 'optico'; ini.textContent = 'Começar a busca';
      }
    });
    ini.id = 'b-iniciar-busca';
    ini.disabled = a.tipoFonte === 'optico';
    const b2 = botao('Fazer cópia de segurança antes', 'btn-suave', () => ir(urlAlvo(a, 'imagem')));
    b2.id = 'b-buscar-imagem';
    b2.disabled = a.tipoFonte === 'optico';
    ac.append(ini, b2);
    c.append(ac);
    c.append(el('p', 'dica', 'A busca só lê o disco: nada é gravado nele.'));
    corpo.append(c);
  }

  /* cópia de segurança: 1 origem, 2 onde salvar, 3 confirmar */
  const hoje = () => { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); };
  function nomeSugerido(a) {
    const parte = a.tipo === 'volume' ? a.l : textoDe(a.fonte.id).replace(/[^A-Za-z0-9]/g, '');
    return 'Recuperacao-' + parte + '-' + hoje() + '.img';
  }
  function candidatos(a) {
    const mesmo = (v) => v.fonte.id === a.fonte.id;
    const lista = alvos.volumes.filter((v) => v.tipo === 'volume' && !v.vazio && v.tipoFonte !== 'optico' && /^[A-Za-z]:$/.test(v.letra) &&
      !(a.tipo === 'volume' ? v.hash === a.hash : mesmo(v)));
    return lista.map((v, i) => [v, i]).sort((x, y) => (mesmo(x[0]) - mesmo(y[0])) || (y[0].livre - x[0].livre) || (x[1] - y[1])).map((x) => x[0]);
  }
  function sugerido(a, lista) {
    return lista.find((v) => v.livre >= a.tamanho && v.fonte.id !== a.fonte.id) || lista.find((v) => v.livre >= a.tamanho) || lista[0] || null;
  }
  const NOME_RUIM = /[\\/:*?"<>|]/;
  function problemaNome(n) {
    if (!n.trim()) return 'Escreva um nome para o arquivo.';
    if (NOME_RUIM.test(n)) return 'O nome não pode ter \\ / : * ? " < > |';
    return '';
  }
  function destinoCompleto(d, nome) { return d.letra + '\\' + nome.trim(); }

  function fluxoImagem(corpo, a) {
    $('imagem-volta').onclick = () => (rota.passo === 3 ? ir(urlAlvo(a, 'imagem')) : ir(urlAlvo(a)));
    const cand = candidatos(a);
    const sug = sugerido(a, cand);
    if (img.chave !== a.hash) {                    // origem nova: recomeça com a sugestão
      img.chave = a.hash;
      img.destino = sug ? sug.hash : null;
      img.nome = nomeSugerido(a);
      img.erro = '';
    }
    let dest = cand.find((v) => v.hash === img.destino) || null;
    if (!dest) { dest = sug; img.destino = dest ? dest.hash : null; }
    const passo = rota.passo === 3 && dest && !problemaNome(img.nome) ? 3 : 2;

    const ol = el('ol', 'passos');
    [['Origem', 1], ['Onde salvar', 2], ['Confirmar', 3]].forEach(([n, i]) => {
      const li = el('li', i < passo ? 'feito' : i === passo ? 'atual' : '');
      if (i === passo) li.setAttribute('aria-current', 'step');
      li.append(el('span', 'passo-num', i < passo ? '✓' : String(i)), el('span', 'passo-nome', n));
      ol.append(li);
    });
    corpo.append(ol);
    corpo.append(cartaoFonte(a, false));

    const caixa = el('div', 'cartao formulario');
    corpo.append(caixa);

    if (passo === 2) {
      caixa.append(el('h2', null, 'Onde salvar?'));
      if (!cand.length) {
        caixa.append(el('p', 'alerta alerta-perigo', 'Não há outro disco com letra para guardar a cópia. Conecte um disco externo e toque em Atualizar, ou use “Avançado: digitar caminhos”.'));
        const ac = el('div', 'acoes');
        ac.append(botao('Voltar', 'btn-suave', () => ir(urlAlvo(a))));
        caixa.append(ac);
        return;
      }
      caixa.append(el('p', 'dica', 'Guarde a cópia em outro disco, nunca dentro do que está sendo copiado.'));
      const grupo = el('div', 'opcoes');
      grupo.setAttribute('role', 'radiogroup');
      grupo.setAttribute('aria-label', 'Onde salvar');
      for (const v of cand) {
        const lab = el('label', 'opcao');
        const r = el('input');
        r.type = 'radio';
        r.name = 'destino';
        r.checked = v.hash === img.destino;
        r.addEventListener('change', () => { img.destino = v.hash; render(); });
        const txt = el('span', 'opcao-txt');
        const topo = el('span', 'opcao-topo');
        const nome = el('span', 'opcao-nome', v.titulo);
        nome.title = v.titulo;
        topo.append(nome);
        if (sug && v.hash === sug.hash) topo.append(el('span', 'tag', 'Sugerido'));
        const falta = v.livre < a.tamanho;
        txt.append(topo, el('span', 'fonte-meta' + (falta ? ' falta' : ''), bytes(v.livre) + ' livres' + (falta ? ' · não cabe' : '') + (v.fonte.id === a.fonte.id ? ' · mesmo disco' : '')));
        lab.append(r, txt);
        grupo.append(lab);
      }
      caixa.append(grupo);
      const rot = el('label', null, 'Nome do arquivo');
      rot.htmlFor = 'img-nome';
      caixa.append(rot);
      const inp = el('input');
      inp.type = 'text'; inp.id = 'img-nome'; inp.value = img.nome; inp.autocomplete = 'off'; inp.spellcheck = false;
      caixa.append(inp);
      const alerta = el('p', 'alerta alerta-perigo');
      alerta.id = 'img-aviso';
      alerta.setAttribute('role', 'alert');
      caixa.append(alerta);
      const dl = el('dl', 'dados');
      const dd = el('dd');
      dd.id = 'img-caminho';
      dl.append(el('dt', null, 'A cópia vai ficar em'), dd);
      caixa.append(dl);
      const ac = el('div', 'acoes');
      ac.append(botao('Voltar', 'btn-suave', () => ir(urlAlvo(a))));
      const seguir = botao('Continuar', 'btn-primario', () => ir(urlAlvo(a, 'imagem/confirmar')));
      seguir.id = 'img-continuar';
      ac.append(seguir);
      caixa.append(ac);
      const atualizar = () => {
        img.nome = inp.value;
        const pn = problemaNome(img.nome);
        const falta = dest.livre < a.tamanho;
        const msg = pn || (falta ? 'Não cabe: faltam ' + bytes(a.tamanho - dest.livre) + ' em ' + dest.titulo + '. Escolha outro lugar.' : '');
        alerta.textContent = msg;
        alerta.hidden = !msg;
        dd.textContent = pn ? '—' : destinoCompleto(dest, img.nome);
        dd.title = dd.textContent;
        seguir.disabled = !!msg;
      };
      inp.addEventListener('input', atualizar);
      atualizar();
      return;
    }

    caixa.append(el('h2', null, 'Confirmar'));
    const dl = el('dl', 'dados');
    const linha = (k, v, id) => { const dd = el('dd', null, v); dd.title = v; if (id) dd.id = id; dl.append(el('dt', null, k), dd); };
    linha('Copiar de', a.titulo);
    linha('Salvar em', destinoCompleto(dest, img.nome), 'conf-destino');
    linha('Tamanho da cópia', bytes(a.tamanho));
    caixa.append(dl);
    caixa.append(el('p', 'dica', 'O original não será alterado. Depois de iniciar, pode fechar o programa: a cópia continua em segundo plano.'));
    const erro = el('p', 'alerta alerta-perigo', img.erro);
    erro.setAttribute('role', 'alert');
    erro.hidden = !img.erro;
    erro.id = 'conf-erro';
    caixa.append(erro);
    const ac = el('div', 'acoes');
    ac.append(botao('Voltar', 'btn-suave', () => ir(urlAlvo(a, 'imagem'))));
    const ini = botao('Iniciar cópia', 'btn-primario', async () => {
      if (img.enviando) return;
      img.enviando = true; img.erro = '';
      ini.disabled = true; ini.textContent = 'Iniciando…'; erro.hidden = true;
      try {
        const j = jobDe(await api('POST', '/api/jobs', { tipo: 'imagem', origem: a.caminho, destino: destinoCompleto(dest, img.nome), opcoes: {} }));
        aplicarJob(j);
        img.chave = '';
        if (j && j.id) ir('/jobs/' + encodeURIComponent(j.id));
      } catch (e) {
        img.erro = e.message;
        erro.textContent = e.message; erro.hidden = false;
      } finally {
        img.enviando = false;
        ini.disabled = false; ini.textContent = 'Iniciar cópia';
      }
    });
    ini.id = 'conf-iniciar';
    ac.append(ini);
    caixa.append(ac);
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
  for (const b of document.querySelectorAll('[data-voltar-lista]')) b.addEventListener('click', () => ir('/#lista'));
  $('i-atualizar').addEventListener('click', () => carregarFontes());
  $('i-tentar').addEventListener('click', () => carregarFontes());
  for (const a of [$('avancado-link'), $('ver-lista')]) {
    a.addEventListener('click', (e) => {
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
      e.preventDefault();
      ir(a.getAttribute('href'));
    });
  }
  for (const a of document.querySelectorAll('[data-inicio]')) a.addEventListener('click', (e) => { e.preventDefault(); ir('/'); });
  $('nova-link').addEventListener('click', (e) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    ir('/');
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
  carregarFontes();
  conectar();
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && fontesEstado === 'ok' && Date.now() - fontesQuando > 5000) carregarFontes();
  });
  window.__painel = { // só para leitura nos testes de tela
    estado: () => ({ modo, falhas, emConsulta, abertoId, detEstado, avisandoSaida, jobs: jobs.size }),
    resultados: () => (resComp ? resComp.obj.estado() : null),
  };
})();
