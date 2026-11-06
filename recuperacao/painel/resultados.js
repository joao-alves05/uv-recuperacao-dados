/* Lista de resultados da busca de arquivos (fase 5.4).
   Só mostra e comanda: os achados moram no serviço (GET /api/jobs/<id>/achados, paginado).
   Tudo que vem do serviço entra por textContent; a miniatura é <img src> da rota própria.
   Uso: const r = Resultados.montar(raiz, { jobId, api, bytes, destinos, aoRecuperar }); r.atualizar(job); r.destruir(). */
(() => {
  'use strict';

  const GRUPOS = [['', 'Todos'], ['foto', 'Fotos'], ['video', 'Vídeos'], ['audio', 'Músicas'], ['documento', 'Documentos'],
    ['compactado', 'Compactados'], ['banco', 'Bancos de dados'], ['outro', 'Outros']];
  const TIPOS = {
    foto: ['JPG', 'PNG', 'GIF', 'BMP', 'TIFF', 'CR2', 'NEF', 'ARW', 'DNG', 'HEIC'],
    video: ['MP4', 'MOV', 'AVI'],
    audio: ['MP3', 'WAV'],
    documento: ['PDF', 'DOCX', 'XLSX', 'PPTX', 'ODT', 'DOC', 'XLS', 'RTF'],
    compactado: ['ZIP', 'RAR', '7Z'],
    banco: ['SQLITE'],
    outro: [],
  };
  const TAMANHOS = [['', 'Qualquer tamanho', null, null], ['p', 'Até 100 KB', null, 102400], ['m', '100 KB a 1 MB', 102400, 1048576],
    ['g', '1 MB a 10 MB', 1048576, 10485760], ['x', '10 MB a 100 MB', 10485760, 104857600], ['xx', 'Mais de 100 MB', 104857600, null]];
  const QUAL = { bom: 'Bom', parcial: 'Parcial', ruim: 'Ruim' };
  const QUAL_DICA = {
    bom: 'Arquivo inteiro: deve abrir normalmente.',
    parcial: 'Parte do arquivo se perdeu: pode abrir com defeito.',
    ruim: 'Muito danificado: provavelmente não abre.',
  };
  const COLUNAS = [['nome', 'Nome'], ['tipo', 'Tipo'], ['tamanho', 'Tamanho'], ['data', 'Data']];
  const POR_PAGINA = 100;
  const NS = 'http://www.w3.org/2000/svg';
  const ICONES = {
    foto: ['M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z', 'M3 16l5-5 4 4 3-3 6 6', 'M9 9h.01'],
    video: ['M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z', 'M10 9l5 3-5 3z'],
    audio: ['M9 18V6l10-2v12', 'M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0z', 'M19 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z'],
    documento: ['M7 3h8l4 4v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z', 'M9 12h6M9 16h6'],
    compactado: ['M4 7l8-4 8 4v10l-8 4-8-4z', 'M4 7l8 4 8-4', 'M12 11v10'],
    banco: ['M12 3c4.4 0 8 1.1 8 2.5S16.4 8 12 8 4 6.9 4 5.5 7.6 3 12 3z', 'M4 5.5v13C4 19.9 7.6 21 12 21s8-1.1 8-2.5v-13', 'M4 12c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5'],
    outro: ['M7 3h8l4 4v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z'],
  };

  function el(tag, cls, texto) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (texto != null) e.textContent = texto;
    return e;
  }
  function icone(grupo) {
    const s = document.createElementNS(NS, 'svg');
    s.setAttribute('viewBox', '0 0 24 24');
    s.setAttribute('aria-hidden', 'true');
    for (const d of ICONES[grupo] || ICONES.outro) {
      const p = document.createElementNS(NS, 'path');
      p.setAttribute('d', d);
      s.append(p);
    }
    return s;
  }
  const num = (n) => Number(n).toLocaleString('pt-BR');
  function dataTxt(v) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(v == null ? '' : v));
    return m ? m[3] + '/' + m[2] + '/' + m[1] : '—';
  }
  function botao(texto, cls, fn) {
    const b = el('button', 'btn ' + cls, texto);
    b.type = 'button';
    b.addEventListener('click', fn);
    return b;
  }
  function campo(rotulo, filho) {
    const l = el('label', 'campo-filtro');
    l.append(el('span', null, rotulo), filho);
    return l;
  }
  function seletor(opcoes, aoMudar, rotulo) {
    const s = el('select');
    s.setAttribute('aria-label', rotulo);
    for (const [v, t] of opcoes) { const o = el('option', null, t); o.value = v; s.append(o); }
    s.addEventListener('change', () => aoMudar(s.value));
    return s;
  }

  function montar(raiz, opts) {
    const api = opts.api, bytes = opts.bytes;
    const f = { grupo: '', tipo: '', estado: '', tam: '', de: '', ate: '', busca: '', pasta: '' };
    let ordem = 'nome', sentido = 'asc', pagina = 1;
    let visao = 'lista';
    try { if (localStorage.getItem('painel.visao') === 'grade') visao = 'grade'; } catch (e) { /* padrão */ }
    let dados = { total: 0, itens: [], resumo: { por_grupo: {} } };
    let estado = 'carregando';           // carregando | ok | erro
    let erro = '';
    let seq = 0, ultimoPedido = 0, timerBusca = null, timerAtraso = null, ultChave = '';
    let job = null, vivo = true, temCaminho = false, tamTotalFiltro = null;
    const sel = { modo: 'ids', ids: new Set(), preparando: false };
    const tamanhos = new Map();
    const vistos = {};                   // tipos que apareceram, além dos conhecidos
    let assinatura = '';
    let recuperando = false, avisoRec = '', precisaRefazer = false, refazendo = false;
    const dest = { ix: 0, pasta: 'Recuperados', livre: '', tocou: false };
    const dd = { estado: 'nada', sugerido: '', opcoes: [], aviso: '' };   // nada | carregando | ok | sem | antigo (rota ausente)
    let sugestaoRec = '';

    raiz.replaceChildren();
    raiz.classList.add('resultados');

    /* ---- abas por grupo ---- */
    const abas = el('div', 'abas');
    abas.setAttribute('role', 'tablist');
    const abaBt = new Map();
    for (const [g, nome] of GRUPOS) {
      const b = el('button', 'aba');
      b.type = 'button';
      b.setAttribute('role', 'tab');
      b.dataset.grupo = g;
      b.append(el('span', 'aba-nome', nome), el('span', 'aba-n', ''));
      b.addEventListener('click', () => { f.grupo = g; f.tipo = ''; mudouFiltro(); });
      abaBt.set(g, b);
      abas.append(b);
    }

    /* ---- filtros ---- */
    const filtros = el('div', 'filtros');
    const busca = el('input');
    busca.type = 'search'; busca.placeholder = 'Buscar pelo nome'; busca.autocomplete = 'off'; busca.spellcheck = false;
    busca.setAttribute('aria-label', 'Buscar pelo nome');
    busca.addEventListener('input', () => {
      clearTimeout(timerBusca);
      timerBusca = setTimeout(() => { f.busca = busca.value.trim(); mudouFiltro(); }, 300);
    });
    const selTipo = seletor([['', 'Todos os tipos']], (v) => { f.tipo = v; mudouFiltro(); }, 'Tipo');
    const selTam = seletor(TAMANHOS.map((t) => [t[0], t[1]]), (v) => { f.tam = v; mudouFiltro(); }, 'Tamanho');
    const selEstado = seletor([['', 'Qualquer estado'], ['bom', 'Bom'], ['parcial', 'Parcial'], ['ruim', 'Ruim']], (v) => { f.estado = v; mudouFiltro(); }, 'Estado');
    const de = el('input'); de.type = 'date';
    const ate = el('input'); ate.type = 'date';
    de.setAttribute('aria-label', 'Data a partir de');
    ate.setAttribute('aria-label', 'Data até');
    de.addEventListener('change', () => { f.de = de.value; mudouFiltro(); });
    ate.addEventListener('change', () => { f.ate = ate.value; mudouFiltro(); });
    const limpar = botao('Limpar filtros', 'btn-fantasma', () => {
      Object.assign(f, { tipo: '', estado: '', tam: '', de: '', ate: '', busca: '', pasta: '' });
      busca.value = ''; de.value = ''; ate.value = '';
      mudouFiltro();
    });
    limpar.id = 'r-limpar';
    filtros.append(campo('Nome', busca), campo('Tipo', selTipo), campo('Tamanho', selTam), campo('Estado', selEstado), campo('De', de), campo('Até', ate));

    /* ---- barra de seleção e visão ---- */
    const barra = el('div', 'sel-barra');
    const caixaGrade = el('label', 'marca-pagina');
    const cxG = el('input'); cxG.type = 'checkbox';
    caixaGrade.append(cxG, el('span', null, 'Marcar todos desta página'));
    cxG.addEventListener('change', () => marcarPagina(cxG.checked));
    const selTxt = el('span', 'sel-txt');
    selTxt.setAttribute('role', 'status');
    selTxt.setAttribute('aria-live', 'polite');
    const selTodos = botao('', 'btn-fantasma', () => selecionarFiltro());
    selTodos.id = 'r-sel-filtro';
    const selLimpar = botao('Limpar seleção', 'btn-fantasma', () => { sel.ids.clear(); sel.modo = 'ids'; desenhar(); });
    selLimpar.id = 'r-sel-limpar';
    const ordenarG = el('span', 'ordenar-grade');
    const selOrdem = seletor(COLUNAS.map((c) => [c[0], c[1]]), (v) => { ordem = v; pagina = 1; buscar(); }, 'Ordenar por');
    const btSentido = botao('', 'btn-suave', () => { sentido = sentido === 'asc' ? 'desc' : 'asc'; pagina = 1; buscar(); });
    btSentido.setAttribute('aria-label', 'Inverter a ordem');
    ordenarG.append(selOrdem, btSentido);
    const visaoBt = el('div', 'visao');
    const btLista = botao('Lista', 'btn-suave', () => trocarVisao('lista'));
    const btGrade = botao('Grade', 'btn-suave', () => trocarVisao('grade'));
    btLista.id = 'r-v-lista'; btGrade.id = 'r-v-grade';
    visaoBt.append(btLista, btGrade);
    barra.append(caixaGrade, selTxt, selTodos, selLimpar, ordenarG, limpar, visaoBt);

    /* ---- árvore de pastas (quando o original tem nomes) ---- */
    const arvore = el('aside', 'arvore');
    arvore.setAttribute('aria-label', 'Pastas');
    arvore.hidden = true;
    const alca = el('div', 'alca-arvore');
    alca.setAttribute('role', 'separator');
    alca.setAttribute('aria-orientation', 'vertical');
    alca.setAttribute('aria-label', 'Largura das pastas');
    alca.tabIndex = 0;
    alca.title = 'Arraste para mudar a largura';
    alca.hidden = true;
    const arvCab = el('div', 'arvore-cab');
    arvCab.append(el('h2', null, 'Pastas'));
    const arvRecolhe = botao('Recolher', 'btn-fantasma', () => { pref.c = !pref.c; aplicarPref(); guardarPref(); });
    arvCab.append(arvRecolhe);
    const arvLista = el('ul', 'arvore-lista');
    arvLista.setAttribute('role', 'tree');
    arvore.append(arvCab, arvLista);
    const pref = { l: 240, c: !!(window.matchMedia && window.matchMedia('(max-width:760px)').matches) };
    try { const o = JSON.parse(localStorage.getItem('painel.arvore') || 'null'); if (o && typeof o.l === 'number') { pref.l = Math.max(160, Math.min(480, o.l)); pref.c = !!o.c; } } catch (e) { /* padrão */ }
    function guardarPref() { try { localStorage.setItem('painel.arvore', JSON.stringify(pref)); } catch (e) { /* tudo bem */ } }
    function aplicarPref() {
      corpo.style.setProperty('--arvore-l', (pref.c ? 0 : pref.l) + 'px');
      corpo.classList.toggle('arvore-recolhida', pref.c);
      arvRecolhe.textContent = 'Recolher';
      reabrir.hidden = !pref.c || arvore.hidden;
      alca.setAttribute('aria-valuenow', String(pref.c ? 0 : pref.l));
    }
    alca.addEventListener('pointerdown', (e) => { try { alca.setPointerCapture(e.pointerId); } catch (x) { /* nada */ } alca.dataset.ativo = '1'; });
    alca.addEventListener('pointermove', (e) => {
      if (!alca.dataset.ativo) return;
      const x = e.clientX - corpo.getBoundingClientRect().left;
      if (x < 120) pref.c = true; else { pref.c = false; pref.l = Math.max(160, Math.min(480, Math.round(x))); }
      aplicarPref();
    });
    const soltar = () => { if (alca.dataset.ativo) { delete alca.dataset.ativo; guardarPref(); } };
    alca.addEventListener('pointerup', soltar);
    alca.addEventListener('pointercancel', soltar);
    alca.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      pref.c = false;
      pref.l = Math.max(160, Math.min(480, pref.l + (e.key === 'ArrowRight' ? 16 : -16)));
      aplicarPref(); guardarPref();
    });
    const reabrir = botao('Mostrar pastas', 'btn-suave', () => { pref.c = false; aplicarPref(); guardarPref(); });
    reabrir.hidden = true;

    const noRaiz = { caminho: '', nome: 'Todas as pastas', total: null };
    const nos = new Map();
    function criarNo(n, nivel) {
      const li = el('li', 'no');
      li.setAttribute('role', 'treeitem');
      const linha = el('div', 'no-linha');
      linha.style.paddingLeft = (4 + nivel * 16) + 'px';
      const seta = el('button', 'no-seta', '▸');
      seta.type = 'button';
      seta.setAttribute('aria-label', 'Abrir ou fechar a pasta');
      const nome = el('button', 'no-nome');
      nome.type = 'button';
      nome.append(el('span', 'no-txt', n.nome), el('span', 'no-n', n.total != null ? num(n.total) : ''));
      nome.title = n.nome;
      const filhos = el('ul');
      filhos.setAttribute('role', 'group');
      filhos.hidden = true;
      let carregado = false;
      const abrir = async () => {
        if (!filhos.hidden) { filhos.hidden = true; seta.textContent = '▸'; li.setAttribute('aria-expanded', 'false'); return; }
        filhos.hidden = false; seta.textContent = '▾'; li.setAttribute('aria-expanded', 'true');
        if (carregado) return;
        carregado = true;
        try {
          const d = await api('GET', '/api/jobs/' + encodeURIComponent(opts.jobId) + '/arvore?caminho=' + encodeURIComponent(n.caminho));
          const pastas = Array.isArray(d && d.pastas) ? d.pastas : [];
          filhos.replaceChildren();
          for (const p of pastas) filhos.append(criarNo({ caminho: String(p.caminho == null ? '' : p.caminho), nome: String(p.nome == null ? '' : p.nome), total: p.total }, nivel + 1));
          if (!pastas.length) seta.style.visibility = 'hidden';
        } catch (e) { carregado = false; filhos.hidden = true; seta.textContent = '▸'; }
      };
      seta.addEventListener('click', abrir);
      nome.addEventListener('click', () => {
        f.pasta = n.caminho;
        mudouFiltro();
        for (const [, o] of nos) o.classList.toggle('atual', o === linha);
      });
      linha.append(seta, nome);
      linha.dataset.caminho = n.caminho;
      nos.set(n.caminho, linha);
      li.append(linha, filhos);
      return li;
    }
    function montarArvore() {
      arvLista.replaceChildren();
      nos.clear();
      const li = criarNo(noRaiz, 0);
      arvLista.append(li);
      nos.get('').classList.add('atual');
      li.querySelector('.no-seta').click();
    }

    /* ---- lista ---- */
    const corpo = el('div', 'res-corpo');
    const area = el('div', 'res-area');
    const aviso = el('div', 'vazio');
    aviso.setAttribute('role', 'status');
    const tabelaBox = el('div', 'tabela-box');
    const tabela = el('table', 'tabela');
    const cabeca = el('thead');
    const trh = el('tr');
    const cxT = el('input'); cxT.type = 'checkbox';
    cxT.setAttribute('aria-label', 'Marcar todos desta página');
    cxT.addEventListener('change', () => marcarPagina(cxT.checked));
    const thC = el('th', 'col-marca'); thC.append(cxT);
    const thMini = el('th', 'col-mini'); thMini.setAttribute('aria-label', 'Miniatura');
    trh.append(thC, thMini);
    const ths = new Map();
    for (const [c, nome] of COLUNAS) {
      const th = el('th', 'col-' + c);
      const b = el('button', 'ord');
      b.type = 'button';
      b.append(el('span', null, nome), el('span', 'seta', ''));
      b.addEventListener('click', () => {
        if (ordem === c) sentido = sentido === 'asc' ? 'desc' : 'asc'; else { ordem = c; sentido = 'asc'; }
        pagina = 1; buscar();
      });
      th.append(b);
      ths.set(c, th);
      trh.append(th);
    }
    trh.append(el('th', 'col-estado', 'Estado'));
    cabeca.append(trh);
    const tcorpo = el('tbody');
    tabela.append(cabeca, tcorpo);
    tabelaBox.append(tabela);
    const grade = el('div', 'grade');
    const pager = el('div', 'pager');
    const pgTxt = el('span', 'pg-txt');
    const pgAnt = botao('Anterior', 'btn-suave', () => { pagina--; buscar(); });
    const pgProx = botao('Próxima', 'btn-suave', () => { pagina++; buscar(); });
    pgAnt.id = 'r-ant'; pgProx.id = 'r-prox';
    pager.append(pgAnt, pgTxt, pgProx);
    area.append(aviso, tabelaBox, grade, pager);
    corpo.append(arvore, alca, area);

    /* ---- recuperar selecionados ---- */
    const rec = el('div', 'cartao recuperar');
    rec.append(el('h2', null, 'Recuperar selecionados'));
    const recResumo = el('p', 'rec-resumo');
    const recDest = el('div', 'rec-dest');
    const selVol = el('select');
    selVol.setAttribute('aria-label', 'Disco de destino');
    selVol.addEventListener('change', () => { dest.ix = Number(selVol.value) || 0; atualizarRec(); });
    const inPasta = el('input');
    inPasta.type = 'text'; inPasta.autocomplete = 'off'; inPasta.spellcheck = false; inPasta.value = dest.pasta;
    inPasta.setAttribute('aria-label', 'Nome da pasta de destino');
    inPasta.addEventListener('input', () => { dest.pasta = inPasta.value; atualizarRec(); });
    const inLivre = el('input');                                 // só quando não há disco na lista: caminho completo
    inLivre.type = 'text'; inLivre.autocomplete = 'off'; inLivre.spellcheck = false; inLivre.placeholder = 'Ex.: D:\\Recuperados';
    inLivre.setAttribute('aria-label', 'Pasta de destino');
    inLivre.addEventListener('input', () => { dest.livre = inLivre.value; dest.tocou = true; sugestaoRec = ''; atualizarRec(); });
    recDest.append(campo('Salvar em', selVol), campo('Pasta', inPasta), campo('Pasta de destino', inLivre));
    const recCaminho = el('p', 'dica');
    const recAlerta = el('p', 'alerta alerta-perigo');
    recAlerta.setAttribute('role', 'alert');
    const recOpcoes = el('div', 'rec-opcoes');                 // escolha rápida: um botão por disco
    recOpcoes.setAttribute('role', 'group');
    recOpcoes.setAttribute('aria-label', 'Onde salvar');
    recOpcoes.hidden = true;
    const fixa = el('div', 'sel-fixa');                         // barra no rodapé da tela: leva ao cartão de recuperar
    fixa.setAttribute('role', 'region');
    fixa.setAttribute('aria-label', 'Recuperar o que está marcado');
    const fixaTxt = el('span', 'sel-fixa-txt');
    const fixaBt = botao('Recuperar…', 'btn-primario', () => { rec.scrollIntoView({ block: 'end' }); recBt.focus({ preventScroll: true }); });
    fixaBt.id = 'r-ir-recuperar';
    fixa.append(fixaTxt, fixaBt);
    fixa.hidden = true;
    let recVisivel = false;
    const recObs = 'IntersectionObserver' in window ? new IntersectionObserver((es) => { recVisivel = es[es.length - 1].isIntersecting; atualizarFixa(); }, { threshold: 0, rootMargin: '0px 0px -72px 0px' }) : null;
    const recAvisoDest = el('div', 'aviso-destino');           // nenhum outro disco: diz o que fazer
    recAvisoDest.setAttribute('role', 'status');
    const recAvisoTxt = el('span', 'aviso-destino-txt');
    const recProcurar = botao('Procurar de novo', 'btn-suave', () => carregarDestinos());
    recProcurar.id = 'r-procurar';
    recAvisoDest.append(recAvisoTxt, recProcurar);
    recAvisoDest.hidden = true;
    const recSemDisco = el('p', 'aviso-disco');                       // sem outro disco na lista
    recSemDisco.textContent = 'Não achei outro disco para salvar. Ligue um pen drive ou HD e clique em Atualizar na tela inicial, ou escreva a pasta abaixo (fora do disco que está sendo recuperado).';
    recSemDisco.hidden = true;
    const recAcoes = el('div', 'acoes');
    const recBt = botao('Recuperar selecionados', 'btn-primario', () => recuperar());
    recBt.id = 'r-recuperar';
    const recRefazer = botao('Buscar de novo neste disco', 'btn-primario', () => refazer());
    recRefazer.id = 'r-refazer';
    recRefazer.hidden = true;
    const recSug = botao('', 'btn-primario', () => usarSugestao());
    recSug.id = 'r-usar-sugestao';
    recSug.hidden = true;
    recAcoes.append(recBt, recSug, recRefazer);
    rec.append(recResumo, recSemDisco, recAvisoDest, recOpcoes, recDest, recCaminho, recAlerta, recAcoes);

    raiz.append(abas, filtros, barra, reabrir, corpo, fixa, rec);
    if (recObs) recObs.observe(rec);
    aplicarPref();

    /* ---- dados ---- */
    function params() {
      const q = new URLSearchParams();
      const t = TAMANHOS.find((x) => x[0] === f.tam);
      if (f.grupo) q.set('grupo', f.grupo);
      if (f.tipo) q.set('tipo', f.tipo);
      if (f.estado) q.set('estado', f.estado);
      if (t && t[2] != null) q.set('min', String(t[2]));
      if (t && t[3] != null) q.set('max', String(t[3]));
      if (f.de) q.set('de', f.de);
      if (f.ate) q.set('ate', f.ate);
      if (f.busca) q.set('busca', f.busca);
      if (f.pasta) q.set('pasta', f.pasta);
      return q;
    }
    function filtrosObj() {
      const o = {};
      for (const [k, v] of params()) o[k] = /^(min|max)$/.test(k) ? Number(v) : v;
      return o;
    }
    async function buscar(silencioso) {
      const meu = ++seq;
      ultimoPedido = Date.now();
      if (!silencioso) { estado = dados.itens.length ? estado : 'carregando'; aviso.dataset.ocupado = '1'; }
      const q = params();
      q.set('ordem', ordem); q.set('sentido', sentido); q.set('pagina', String(pagina)); q.set('por_pagina', String(POR_PAGINA));
      try {
        const d = await api('GET', '/api/jobs/' + encodeURIComponent(opts.jobId) + '/achados?' + q.toString());
        if (meu !== seq || !vivo) return;
        const itens = Array.isArray(d && d.itens) ? d.itens : [];
        const total = Math.max(0, parseInt(d && d.total, 10) || 0);
        const maxPag = Math.max(1, Math.ceil(total / POR_PAGINA));
        if (pagina > maxPag) { pagina = maxPag; return buscar(silencioso); }
        for (const a of itens) {
          if (!a || a.id == null) continue;
          tamanhos.set(a.id, Number(a.tamanho) || 0);
          if (a.grupo && a.tipo) { (vistos[a.grupo] = vistos[a.grupo] || new Set()).add(String(a.tipo)); }
        }
        dados = { total, itens, resumo: d && d.resumo && typeof d.resumo === 'object' ? d.resumo : { por_grupo: {} } };
        tamTotalFiltro = dados.resumo && Number.isFinite(Number(dados.resumo.tamanho_total)) ? Number(dados.resumo.tamanho_total) : null;
        if (!temCaminho && itens.some((a) => a && a.caminho)) { temCaminho = true; arvore.hidden = false; alca.hidden = false; corpo.classList.add('com-arvore'); montarArvore(); aplicarPref(); }
        estado = 'ok'; erro = '';
      } catch (e) {
        if (meu !== seq || !vivo) return;
        if (!silencioso || !dados.itens.length) { estado = 'erro'; erro = e.message; }
      }
      desenhar();
    }
    function mudouFiltro() {
      pagina = 1;
      if (sel.modo === 'filtro') { sel.modo = 'ids'; sel.ids.clear(); }
      buscar();
    }
    function trocarVisao(v) {
      visao = v;
      try { localStorage.setItem('painel.visao', v); } catch (e) { /* tudo bem */ }
      desenhar();
    }

    /* ---- seleção ---- */
    const contagem = () => (sel.modo === 'filtro' ? dados.total : sel.ids.size);
    function tamanhoSel() {
      if (sel.modo === 'filtro') return tamTotalFiltro;
      let t = 0;
      for (const id of sel.ids) t += tamanhos.get(id) || 0;
      return t;
    }
    function marcarPagina(on) {
      if (sel.modo === 'filtro') { if (!on) materializar().then(() => { for (const a of dados.itens) sel.ids.delete(a.id); desenhar(); }); return; }
      for (const a of dados.itens) { if (on) sel.ids.add(a.id); else sel.ids.delete(a.id); }
      desenhar();
    }
    function selecionarFiltro() { sel.modo = 'filtro'; sel.ids.clear(); desenhar(); }
    async function materializar() {                       // troca "todos que combinam" pela lista de números, para poder tirar um
      if (sel.modo !== 'filtro') return;
      sel.preparando = true; desenhar();
      const ids = new Set();
      try {
        const q = params(); q.set('ordem', 'nome'); q.set('sentido', 'asc'); q.set('por_pagina', String(POR_PAGINA));
        for (let p = 1; p <= 1000; p++) {
          q.set('pagina', String(p));
          const d = await api('GET', '/api/jobs/' + encodeURIComponent(opts.jobId) + '/achados?' + q.toString());
          const it = Array.isArray(d && d.itens) ? d.itens : [];
          for (const a of it) { ids.add(a.id); tamanhos.set(a.id, Number(a.tamanho) || 0); }
          if (ids.size >= (parseInt(d && d.total, 10) || 0) || !it.length) break;
        }
        sel.ids = ids; sel.modo = 'ids';
      } catch (e) { avisoRec = e.message; }
      sel.preparando = false;
    }
    async function alternar(a, on) {
      if (sel.modo === 'filtro') { await materializar(); }
      if (on) sel.ids.add(a.id); else sel.ids.delete(a.id);
      desenhar();
    }

    /* ---- desenho ---- */
    function desenhar() {
      const total = dados.total;
      const pg = dados.resumo && dados.resumo.por_grupo ? dados.resumo.por_grupo : {};
      let soma = 0;
      for (const [g, b] of abaBt) {
        const n = g ? Number(pg[g]) || 0 : null;
        if (g) soma += n;
        b.setAttribute('aria-selected', String(g === f.grupo));
        b.classList.toggle('ativa', g === f.grupo);
        if (g) { b.querySelector('.aba-n').textContent = num(n); b.hidden = n === 0 && g !== f.grupo; }
      }
      abaBt.get('').querySelector('.aba-n').textContent = num(soma);
      // tipos do grupo
      const uniao = (g) => [...new Set([...(TIPOS[g] || []), ...(vistos[g] ? [...vistos[g]] : [])])].sort();
      const tipos = f.grupo ? uniao(f.grupo) : [...new Set([].concat(...Object.keys(TIPOS).map(uniao)))].sort();
      if (selTipo.dataset.g !== f.grupo + '|' + tipos.length) {
        selTipo.replaceChildren();
        const o0 = el('option', null, 'Todos os tipos'); o0.value = ''; selTipo.append(o0);
        for (const t of tipos) { const o = el('option', null, t); o.value = t; selTipo.append(o); }
        selTipo.dataset.g = f.grupo + '|' + tipos.length;
      }
      selTipo.value = f.tipo;
      selTam.value = f.tam; selEstado.value = f.estado;
      selTipo.disabled = f.grupo === 'outro';
      limpar.disabled = !(f.tipo || f.estado || f.tam || f.de || f.ate || f.busca || f.pasta);

      btLista.setAttribute('aria-pressed', String(visao === 'lista'));
      btGrade.setAttribute('aria-pressed', String(visao === 'grade'));
      btLista.classList.toggle('ligado', visao === 'lista');
      btGrade.classList.toggle('ligado', visao === 'grade');
      tabelaBox.hidden = visao !== 'lista' || !dados.itens.length;
      grade.hidden = visao !== 'grade' || !dados.itens.length;
      caixaGrade.hidden = visao !== 'grade';
      ordenarG.hidden = visao !== 'grade';
      selOrdem.value = ordem;
      btSentido.textContent = sentido === 'asc' ? 'Crescente ▲' : 'Decrescente ▼';

      for (const [c, th] of ths) {
        const ativo = c === ordem;
        th.setAttribute('aria-sort', ativo ? (sentido === 'asc' ? 'ascending' : 'descending') : 'none');
        th.querySelector('.seta').textContent = ativo ? (sentido === 'asc' ? '▲' : '▼') : '';
      }

      // avisos de lista
      const rodando = job && (job.estado === 'rodando' || job.estado === 'na_fila');
      let msg = '';
      if (estado === 'carregando') msg = 'Carregando os arquivos…';
      else if (estado === 'erro') msg = erro || 'Não foi possível carregar a lista.';
      else if (total === 0) {
        const comFiltro = f.tipo || f.estado || f.tam || f.de || f.ate || f.busca || f.pasta || f.grupo;
        msg = comFiltro ? 'Nenhum arquivo combina com os filtros.' : rodando ? 'Procurando… os arquivos aparecem aqui assim que forem achados.' : 'Não achamos nenhum arquivo.';
      }
      aviso.hidden = !msg;
      aviso.textContent = msg;
      aviso.classList.toggle('vazio-erro', estado === 'erro');
      if (estado === 'erro') aviso.append(botao('Tentar de novo', 'btn-suave', () => { estado = 'carregando'; buscar(); }));

      // linhas
      const chave = [visao, ordem, sentido, pagina, f.grupo, dados.itens.map((a) => [a.id, a.estado, a.recuperado_em].join(':')).join(',')].join('|');
      if (chave !== assinatura) {
        assinatura = chave;
        tcorpo.replaceChildren();
        grade.replaceChildren();
        for (const a of dados.itens) {
          if (visao === 'lista') tcorpo.append(linha(a)); else grade.append(cartao(a));
        }
      }
      // marcas
      const marcado = (a) => sel.modo === 'filtro' || sel.ids.has(a.id);
      for (const c of document.querySelectorAll ? raiz.querySelectorAll('input[data-id]') : []) c.checked = sel.modo === 'filtro' || sel.ids.has(Number(c.dataset.id)) || sel.ids.has(c.dataset.id);
      for (const r of raiz.querySelectorAll('[data-linha]')) r.classList.toggle('marcada', r.querySelector('input[data-id]').checked);
      const n = dados.itens.filter(marcado).length;
      const todos = dados.itens.length > 0 && n === dados.itens.length;
      for (const c of [cxT, cxG]) { c.checked = todos; c.indeterminate = n > 0 && !todos; c.disabled = !dados.itens.length || sel.preparando; }

      // barra
      const q = contagem();
      let t;
      if (sel.preparando) t = 'Preparando a seleção…';
      else if (q === 0) t = total ? num(total) + (total === 1 ? ' arquivo' : ' arquivos') + ' na lista' : '';
      else {
        const tam = tamanhoSel();
        t = (sel.modo === 'filtro' ? 'Todos os ' + num(q) + ' arquivos da lista estão selecionados' : num(q) + (q === 1 ? ' selecionado' : ' selecionados')) + (tam ? ' · ' + bytes(tam) : '');
      }
      selTxt.textContent = t;
      selTodos.hidden = !(sel.modo === 'ids' && todos && total > dados.itens.length);
      selTodos.textContent = 'Selecionar os ' + num(total) + ' da lista';
      selLimpar.hidden = q === 0;

      // pager
      const maxPag = Math.max(1, Math.ceil(total / POR_PAGINA));
      pager.hidden = total <= POR_PAGINA;
      const ini = (pagina - 1) * POR_PAGINA + 1;
      pgTxt.textContent = num(ini) + ' a ' + num(Math.min(total, ini + dados.itens.length - 1)) + ' de ' + num(total);
      pgAnt.disabled = pagina <= 1;
      pgProx.disabled = pagina >= maxPag;
      aviso.dataset.ocupado = '';
      aplicarPref();
      atualizarRec();
      if (dlg.open) desenharPrevia();
    }

    /* ---- prévia ampliada e detalhes ---- */
    const PORQUE = {
      bom: 'A estrutura do arquivo está inteira e coerente.',
      parcial: 'Só o começo do arquivo está certo; o resto se perdeu. Ele pode abrir com defeito.',
      ruim: 'O arquivo está inteiro, mas a conferência falhou. Provavelmente está corrompido.',
    };
    const pv = { item: null, opener: null };
    const dlg = el('dialog', 'previa');
    dlg.setAttribute('aria-labelledby', 'pv-titulo');
    const pvTopo = el('div', 'pv-topo');
    const pvTitulo = el('h2', 'pv-titulo');
    pvTitulo.id = 'pv-titulo';
    const pvFecha = botao('Fechar', 'btn-suave', () => dlg.close());
    pvFecha.id = 'pv-fechar';
    pvTopo.append(pvTitulo, pvFecha);
    const pvCorpo = el('div', 'pv-corpo');
    const pvImg = el('div', 'pv-img');
    const pvDet = el('dl', 'pv-det');
    pvCorpo.append(pvImg, pvDet);
    const pvRod = el('div', 'pv-rod');
    const pvAnt = botao('‹ Anterior', 'btn-suave', () => navegarPrevia(-1));
    const pvPos = el('span', 'pg-txt');
    const pvProx = botao('Próximo ›', 'btn-suave', () => navegarPrevia(1));
    const pvMarca = botao('', 'btn-primario', () => {
      if (!pv.item) return;
      const on = !(sel.modo === 'filtro' || sel.ids.has(pv.item.id));
      alternar(pv.item, on).then(() => desenharPrevia());
    });
    pvAnt.id = 'pv-ant'; pvProx.id = 'pv-prox'; pvMarca.id = 'pv-marcar';
    pvRod.append(pvAnt, pvPos, pvProx, pvMarca);
    dlg.append(pvTopo, pvCorpo, pvRod);
    document.body.append(dlg);
    dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
    dlg.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowLeft') { e.preventDefault(); navegarPrevia(-1); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); navegarPrevia(1); }
    });
    dlg.addEventListener('close', () => { pv.item = null; if (pv.opener && pv.opener.isConnected) pv.opener.focus(); pv.opener = null; pvImg.replaceChildren(); });
    function indicePrevia() { return pv.item ? dados.itens.findIndex((x) => x.id === pv.item.id) : -1; }
    function navegarPrevia(d) {
      const i = indicePrevia();
      const n = i + d;
      if (i < 0 || n < 0 || n >= dados.itens.length) return;
      pv.item = dados.itens[n];
      desenharPrevia();
    }
    function abrirPrevia(a, opener) {
      pv.item = a;
      pv.opener = opener || null;
      desenharPrevia();
      if (!dlg.open) dlg.showModal();
    }
    function desenharPrevia() {
      const a = pv.item;
      if (!a) return;
      pvTitulo.textContent = a.nome == null ? '' : String(a.nome);
      pvTitulo.title = nomeCompleto(a);
      const i = indicePrevia();
      pvAnt.disabled = i <= 0;
      pvProx.disabled = i < 0 || i >= dados.itens.length - 1;
      pvPos.textContent = i >= 0 ? (i + 1) + ' de ' + dados.itens.length + ' nesta página' : '';
      const marcado = sel.modo === 'filtro' || sel.ids.has(a.id);
      pvMarca.textContent = marcado ? 'Tirar da seleção' : 'Marcar para recuperar';
      pvMarca.classList.toggle('btn-primario', !marcado);
      pvMarca.classList.toggle('btn-suave', marcado);
      pvImg.replaceChildren();
      const semPrevia = (txt) => { const v = el('div', 'pv-sem'); v.append(icone(a.grupo), el('p', null, txt)); pvImg.replaceChildren(v); };
      if (a.grupo === 'foto' && a.id != null) {
        const im = el('img');
        im.alt = 'Prévia de ' + (a.nome == null ? '' : String(a.nome));
        im.src = '/api/achados/' + encodeURIComponent(a.id) + '/previa';
        im.addEventListener('error', () => semPrevia('Não foi possível mostrar a prévia desta foto.'));
        pvImg.append(im);
      } else semPrevia('Este tipo de arquivo não tem prévia. Veja os detalhes ao lado.');
      pvDet.replaceChildren();
      const linha = (k, v, el2) => { pvDet.append(el('dt', null, k)); const dd = el('dd'); if (el2) dd.append(el2); else dd.textContent = v; pvDet.append(dd); };
      linha('Tipo', [a.tipo, a.grupo].filter(Boolean).map(String).join(' · ') || '—');
      linha('Tamanho', bytes(a.tamanho));
      if (a.largura && a.altura) linha('Dimensões', a.largura + ' × ' + a.altura);
      linha('Data', dataTxt(a.data));
      linha('Pasta de origem', a.caminho ? String(a.caminho) : 'Não se sabe: o arquivo foi achado pelo conteúdo.');
      if (a.apagado === true || a.apagado === false) linha('Apagado', a.apagado ? 'Sim' : 'Não');
      linha('Estado', '', etiquetaQual(a));
      linha('Por quê', PORQUE[a.estado] || '—');
      if (a.pontuacao != null) linha('Confiança', Number(a.pontuacao) + ' de 100');
      if (a.recuperado_em) linha('Recuperado', 'Sim, já foi salvo.');
    }

    function miniatura(a) {
      const caixa = el('button', 'mini');
      caixa.type = 'button';
      caixa.setAttribute('aria-label', 'Ver ' + String(a.nome == null ? '' : a.nome));
      caixa.title = 'Ver em tamanho maior';
      caixa.addEventListener('click', () => abrirPrevia(a, caixa));
      const poe = () => { caixa.replaceChildren(icone(a.grupo)); };
      if (a.grupo === 'foto' && a.id != null) {
        const im = el('img');
        im.alt = '';
        im.decoding = 'async';
        im.dataset.src = '/api/achados/' + encodeURIComponent(a.id) + '/previa';
        im.addEventListener('error', poe);
        caixa.append(im);
        if (obsMini) obsMini.observe(im); else { fila.push(im); andarFila(); }
      } else poe();
      return caixa;
    }
    function etiquetaQual(a) {
      const t = el('span', 'tag q', QUAL[a.estado] || String(a.estado || ''));
      t.dataset.q = a.estado;
      t.title = QUAL_DICA[a.estado] || '';
      return t;
    }
    function caixa(a) {
      const c = el('input');
      c.type = 'checkbox';
      c.dataset.id = String(a.id);
      c.setAttribute('aria-label', 'Selecionar ' + String(a.nome == null ? '' : a.nome));
      c.addEventListener('change', () => alternar(a, c.checked));
      return c;
    }
    function selosApagado() {
      const t = el('span', 'tag apagado', 'Apagado');
      t.title = 'Este arquivo foi apagado do disco; ainda dá para tentar recuperar.';
      return t;
    }
    function nomeCompleto(a) { return (a.caminho ? String(a.caminho) + '/' : '') + String(a.nome == null ? '' : a.nome); }
    function linha(a) {
      const tr = el('tr');
      tr.dataset.linha = String(a.id);
      const tdC = el('td', 'col-marca'); tdC.append(caixa(a));
      const tdM = el('td', 'col-mini'); tdM.append(miniatura(a));
      const tdN = el('td', 'col-nome');
      tdN.append(el('span', 'nome-txt', a.nome == null ? '' : String(a.nome)));
      if (a.caminho) tdN.append(el('span', 'nome-cam', String(a.caminho)));
      tdN.title = nomeCompleto(a);
      const tdT = el('td', 'col-tipo', a.tipo == null ? '' : String(a.tipo));
      const tdS = el('td', 'col-tamanho', bytes(a.tamanho));
      const tdD = el('td', 'col-data', dataTxt(a.data));
      const tdE = el('td', 'col-estado');
      const est = el('div', 'estados');
      est.append(etiquetaQual(a));
      if (a.apagado === true) est.append(selosApagado());
      if (a.recuperado_em) est.append(el('span', 'recuperado', 'Recuperado'));
      tdE.append(est);
      tr.append(tdC, tdM, tdN, tdT, tdS, tdD, tdE);
      tr.addEventListener('click', (e) => {
        if (e.target.closest('input,button,a')) return;
        const c = tdC.querySelector('input');
        c.checked = !c.checked;
        alternar(a, c.checked);
      });
      return tr;
    }
    function cartao(a) {
      const d = el('div', 'item-grade');
      d.dataset.linha = String(a.id);
      const topo = el('div', 'ig-topo');
      topo.append(caixa(a), etiquetaQual(a));
      const nome = el('span', 'ig-nome', a.nome == null ? '' : String(a.nome));
      nome.title = nomeCompleto(a);
      const meta = el('span', 'ig-meta', [a.tipo, bytes(a.tamanho)].filter(Boolean).join(' · '));
      d.append(topo, miniatura(a), nome);
      if (a.caminho) { const cam = el('span', 'ig-meta nome-cam', String(a.caminho)); cam.title = String(a.caminho); d.append(cam); }
      d.append(meta);
      if (a.apagado === true) d.append(selosApagado());
      if (a.recuperado_em) d.append(el('span', 'recuperado', 'Recuperado'));
      d.addEventListener('click', (e) => {
        if (e.target.closest('input,button,a')) return;
        const c = topo.querySelector('input');
        c.checked = !c.checked;
        alternar(a, c.checked);
      });
      return d;
    }

    /* ---- miniaturas: só as que aparecem na tela, no máximo MAX_MINI ao mesmo tempo ---- */
    const MAX_MINI = 4;
    const fila = [];
    let carregandoMini = 0;
    function andarFila() {
      while (vivo && carregandoMini < MAX_MINI && fila.length) {
        const im = fila.shift();
        if (!im.isConnected || im.getAttribute('src')) continue;
        carregandoMini++;
        let fim = false;
        const acabou = () => { if (fim) return; fim = true; carregandoMini--; andarFila(); };
        im.addEventListener('load', acabou, { once: true });
        im.addEventListener('error', acabou, { once: true });
        setTimeout(acabou, 8000);                                 // não deixa a fila presa
        im.src = im.dataset.src;
      }
    }
    const obsMini = 'IntersectionObserver' in window ? new IntersectionObserver((es) => {
      for (const e of es) if (e.isIntersecting) { obsMini.unobserve(e.target); fila.push(e.target); }
      andarFila();
    }, { rootMargin: '120px' }) : null;

    /* ---- recuperar ---- */
    async function carregarDestinos() {                         // o serviço diz onde salvar; sem a rota, vale a lista da tela
      if (!job) return;
      dd.estado = 'carregando'; atualizarRec();
      try {
        const d = await api('GET', '/api/destinos?origem=' + encodeURIComponent(String(job.origem == null ? '' : job.origem)));
        if (!vivo) return;
        dd.opcoes = (Array.isArray(d && d.opcoes) ? d.opcoes : []).filter((o) => o && typeof o.caminho === 'string' && o.caminho)
          .map((o) => ({ caminho: o.caminho, rotulo: String(o.rotulo == null ? o.caminho : o.rotulo), livre: Number(o.livre) }));
        dd.sugerido = d && typeof d.sugerido === 'string' ? d.sugerido : '';
        dd.aviso = d && typeof d.aviso === 'string' ? d.aviso : '';
        dd.estado = dd.opcoes.length || dd.sugerido ? 'ok' : 'sem';
        if (!dest.tocou) dest.livre = dd.sugerido;
      } catch (e) {
        if (!vivo) return;
        dd.estado = 'antigo';
      }
      atualizarRec();
    }
    function atualizarFixa() {
      const q = contagem(), tam = tamanhoSel();
      fixaTxt.textContent = q ? num(q) + (q === 1 ? ' selecionado' : ' selecionados') + (tam ? ' · ' + bytes(tam) : '') : '';
      fixa.hidden = !q || recVisivel || precisaRefazer;
    }
    function opAtual() { return dd.opcoes.find((o) => o.caminho === dest.livre.trim()) || null; }
    function textoNaoCabe(tam, o) {
      const outra = dd.opcoes.filter((x) => x !== o && x.livre >= tam).sort((a, b) => b.livre - a.livre)[0];
      return 'Faltam ' + bytes(tam - o.livre) + '. ' + (outra ? 'Escolha o ' + outra.rotulo + ' ou outro com mais espaço.' : 'Marque menos arquivos ou ligue um disco com mais espaço.');
    }
    function mostrarAviso() { recAcoes.scrollIntoView({ block: 'center' }); }
    function usarSugestao() {
      if (!sugestaoRec) return;
      dest.livre = sugestaoRec; dest.tocou = true;
      sugestaoRec = ''; avisoRec = '';
      recuperar();
    }
    function destinoAtual() {
      if (dd.estado === 'ok' || dd.estado === 'sem' || dd.estado === 'carregando') return { livre: true, caminho: dest.livre.trim(), vol: null };
      const l = opts.destinos ? opts.destinos() : [];
      if (!l.length) return { livre: true, caminho: dest.livre.trim(), vol: null };
      const v = l[Math.min(dest.ix, l.length - 1)];
      const pasta = dest.pasta.trim();
      return { livre: false, caminho: v.base + (pasta ? '\\' + pasta : ''), vol: v, pasta };
    }
    function atualizarRec() {
      const novo = dd.estado === 'ok' || dd.estado === 'sem' || dd.estado === 'carregando';
      const l = novo ? [] : (opts.destinos ? opts.destinos() : []);
      if (selVol.dataset.n !== String(l.length) + '|' + l.map((v) => v.rotulo).join(',')) {
        selVol.replaceChildren();
        l.forEach((v, i) => { const o = el('option', null, v.rotulo + (v.sugerido ? ' (sugerido)' : '')); o.value = String(i); selVol.append(o); });
        selVol.dataset.n = String(l.length) + '|' + l.map((v) => v.rotulo).join(',');
        if (dest.ix >= l.length || dest.ixAuto !== true) { const s = l.findIndex((v) => v.sugerido); dest.ix = s >= 0 ? s : 0; dest.ixAuto = true; }
      }
      selVol.value = String(Math.min(dest.ix, Math.max(0, l.length - 1)));
      selVol.parentElement.hidden = !l.length;
      inPasta.parentElement.hidden = !l.length;
      inLivre.parentElement.hidden = novo ? dd.estado !== 'ok' : l.length > 0;
      recSemDisco.hidden = novo || l.length > 0;
      recAvisoDest.hidden = !(dd.estado === 'sem' || dd.estado === 'carregando');
      recAvisoTxt.textContent = dd.estado === 'carregando' ? 'Procurando onde salvar…' : (dd.aviso || 'Ligue um pendrive ou HD externo para guardar os arquivos.');
      recProcurar.hidden = dd.estado === 'carregando';
      recOpcoes.hidden = !(dd.estado === 'ok' && dd.opcoes.length);
      if (dd.estado === 'ok') {
        const chaveOp = dd.opcoes.map((o) => o.caminho + o.rotulo + o.livre).join('|');
        if (recOpcoes.dataset.chave !== chaveOp) {
          recOpcoes.dataset.chave = chaveOp;
          recOpcoes.replaceChildren();
          for (const o of dd.opcoes) {
            const b = botao(o.rotulo + (Number.isFinite(o.livre) ? ' · ' + bytes(o.livre) + ' livres' : ''), 'btn-suave', () => {
              dest.livre = o.caminho; dest.tocou = true; avisoRec = ''; sugestaoRec = ''; atualizarRec();
            });
            b.dataset.caminho = o.caminho;
            b.title = o.caminho;
            recOpcoes.append(b);
          }
        }
        for (const b of recOpcoes.children) b.setAttribute('aria-pressed', String(b.dataset.caminho === dest.livre.trim()));
        if (inLivre.value !== dest.livre) inLivre.value = dest.livre;
      }
      const d = destinoAtual();
      const q = contagem(), tam = tamanhoSel();
      recResumo.textContent = q ? num(q) + (q === 1 ? ' arquivo selecionado' : ' arquivos selecionados') + (tam ? ' · ' + bytes(tam) : '') + '.' : 'Marque os arquivos que você quer recuperar.';
      recCaminho.hidden = !d.caminho;
      recCaminho.textContent = d.caminho ? 'Os arquivos vão para ' + d.caminho + ', separados por tipo (Fotos, Vídeos…).' : '';
      let problema = '';
      if (d.vol && /[\\/:*?"<>|]/.test(d.pasta || '')) problema = 'O nome da pasta não pode ter \\ / : * ? " < > |';
      else if (d.vol && tam && d.vol.livre != null && tam > d.vol.livre) problema = 'Não cabe: faltam ' + bytes(tam - d.vol.livre) + ' em ' + d.vol.rotulo + '. Escolha outro disco ou marque menos arquivos.';
      else if (dd.estado === 'ok' && tam && opAtual() && opAtual().livre < tam) problema = textoNaoCabe(tam, opAtual());
      else if (d.livre && q && !d.caminho && (dd.estado === 'ok' || dd.estado === 'antigo' || dd.estado === 'nada')) problema = 'Escreva a pasta de destino.';
      const msg = avisoRec || problema;
      recAlerta.textContent = msg;
      recAlerta.hidden = !msg;
      const calmo = precisaRefazer || !!sugestaoRec;                       // "disco mudou" e "destino recusado" não são erro grave: tom de aviso
      recAlerta.classList.toggle('alerta-perigo', !calmo);
      if (calmo) recAlerta.dataset.tipo = 'aviso'; else delete recAlerta.dataset.tipo;
      recSug.hidden = !sugestaoRec || precisaRefazer;
      recSug.textContent = 'Usar ' + sugestaoRec;
      recSug.title = sugestaoRec;
      recSug.disabled = recuperando;
      recRefazer.hidden = !precisaRefazer;
      recRefazer.disabled = refazendo;
      recRefazer.textContent = refazendo ? 'Buscando…' : 'Buscar de novo neste disco';
      recBt.hidden = precisaRefazer;
      recBt.disabled = !q || !!problema || recuperando || sel.preparando || !d.caminho || dd.estado === 'carregando';
      atualizarFixa();
      recBt.textContent = recuperando ? 'Iniciando…' : q ? 'Recuperar selecionados (' + num(q) + ')' : 'Recuperar selecionados';
    }
    async function recuperar() {
      if (recuperando) return;
      const d = destinoAtual();
      const corpoPost = sel.modo === 'filtro' ? { ids: 'todos', filtros: filtrosObj(), destino: d.caminho } : { ids: [...sel.ids], destino: d.caminho };
      recuperando = true; avisoRec = ''; sugestaoRec = ''; atualizarRec();
      try {
        const j = await api('POST', '/api/jobs/' + encodeURIComponent(opts.jobId) + '/recuperar', corpoPost);
        recuperando = false;
        if (opts.aoRecuperar) opts.aoRecuperar(j && j.job && typeof j.job === 'object' ? j.job : j, j);
      } catch (e) {
        recuperando = false;
        if (e && e.status === 409) {
          precisaRefazer = true;
          avisoRec = 'O disco não está igual a quando a busca foi feita, então esta lista pode estar errada. Busque de novo neste disco para continuar.';
        } else if (e && e.status === 400 && e.dados && typeof e.dados.sugestao === 'string' && e.dados.sugestao) {
          sugestaoRec = e.dados.sugestao;
          avisoRec = e.message;
        } else avisoRec = e.message;
      }
      atualizarRec();
      mostrarAviso();
    }
    async function refazer() {
      if (refazendo || !opts.aoRefazer) return;
      refazendo = true; atualizarRec();
      try { await opts.aoRefazer(); } catch (e) { avisoRec = e.message; }
      refazendo = false;
      atualizarRec();
    }
    for (const c of [inPasta, inLivre]) c.addEventListener('input', () => { avisoRec = ''; });
    selVol.addEventListener('change', () => { avisoRec = ''; });

    /* ---- ao vivo ---- */
    function atualizar(j) {
      job = j;
      if (!j) return;
      if (dd.estado === 'nada') carregarDestinos();
      const chave = j.estado + '|' + Math.floor((Number(j.progresso) || 0) * 100);
      if (chave === ultChave) return;
      ultChave = chave;
      const agora = Date.now();
      clearTimeout(timerAtraso);
      if (document.hidden) return;                       // aba escondida: sem consulta; ao voltar, busca de novo
      if (agora - ultimoPedido >= 1500) buscar(true);
      else timerAtraso = setTimeout(() => vivo && buscar(true), 1500 - (agora - ultimoPedido));
      if (estado === 'ok') desenhar();
    }
    const aoVoltar = () => { if (!document.hidden && vivo) { ultChave = ''; if (job) atualizar(job); } };
    document.addEventListener('visibilitychange', aoVoltar);
    function destruir() { document.removeEventListener('visibilitychange', aoVoltar); if (obsMini) obsMini.disconnect(); if (recObs) recObs.disconnect(); fila.length = 0; if (dlg.open) dlg.close(); dlg.remove(); vivo = false; clearTimeout(timerBusca); clearTimeout(timerAtraso); seq++; raiz.replaceChildren(); raiz.classList.remove('resultados'); }

    desenhar();
    buscar();
    return { atualizar, destruir, estado: () => ({ total: dados.total, linhas: dados.itens.length, modo: sel.modo, selecionados: contagem(), ordem, sentido, pagina, filtros: Object.assign({}, f) }) };
  }

  window.Resultados = { montar };
})();
