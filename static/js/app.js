/* Onti — user portal: chat with inline impact chain, library, reports. */
(function () {
  'use strict';
  const { api, toast, openModal, confirmAction, renderMd, escHtml, fmtDate, el, emptyState } = Onti;

  const state = {
    mode: 'auto',
    compare: false,
    config: null,
    fullGraph: null,
    classGraph: null,
    graphLayer: 'visual',
    qaLog: [],
    pinned: [],
    busy: false,
  };

  /* ------------------------------------------------------------ navigation */

  const titles = {
    dashboard: 'Dashboard', analytics: 'Performance analytics',
    chat: 'Ask the ontology',
    data: 'Data centre', workspace: 'Ontology workspace',
    graphstudio: 'Graph studio', history: 'History',
    library: 'Knowledge library', reports: 'My reports',
    models: 'Model centre'
  };
  function switchView(name) {
    state.view = name;
    document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + name));
    document.querySelectorAll('[data-view]').forEach((b) => b.classList.toggle('active', b.dataset.view === name));
    document.getElementById('pageTitle').textContent = titles[name] || '';
    if (name === 'dashboard') loadDashboard();
    if (name === 'analytics') loadAnalytics();
    if (name === 'library') loadLibrary();
    if (name === 'reports') loadReports();
    if (name === 'data') loadDataCentre();
    if (name === 'history') renderHistory();
    if (name === 'workspace') initOntoSearch();
    if (name === 'graphstudio') loadGraphStudio();
    if (name === 'models') loadModelsView();
  }
  document.querySelectorAll('[data-view]').forEach((b) =>
    b.addEventListener('click', () => switchView(b.dataset.view)));

  document.getElementById('logoutBtn').addEventListener('click', async () => {
    await api.post('/api/auth/logout', {});
    location.href = '/login';
  });

  if (new URLSearchParams(location.search).get('forbidden')) {
    setTimeout(() => toast('That area is restricted to administrators.', 'warn'), 300);
  }

  /* ------------------------------------------------------------------ init */

  async function init() {
    try {
      state.config = await api.get('/api/config');
      Onti.ontology.mount();
      const p = state.config.pack;
      const pill = document.getElementById('packPill');
      pill.innerHTML = '<span>' + (p && p.name
        ? '📦 ' + escHtml(p.name) : '⚠️ No active ontology pack') + '</span>';
      pill.title = p && p.name
        ? p.name + (state.config.model.configured ? ' · ' + state.config.model.name : '')
        : 'No active ontology pack';
      if (state.config.model.configured)
        pill.innerHTML +=
          '<span>· 🤖 ' + escHtml(state.config.model.name) + '</span>';
      loadPackSelect();
      const [q] = await Promise.all([
        api.get('/api/questions'),
        loadMyQuestionChips(),
      ]);
      renderChips((q.questions || []).slice(0, 8), 'standard');
      updateAskModelName();
      const g = await api.get('/api/graph');
      state.fullGraph = g.graph;
      loadDashboard();
    } catch (e) {
      if (e.status !== 401) toast(e.message, 'err');
    }
  }

  /* ------------------------------------------- scenario (pack) selection */

  async function loadPackSelect() {
    const sel = document.getElementById('packSelect');
    if (!sel) return;
    try {
      const d = await api.get('/api/packs');
      if (!d.packs.length) {
        sel.innerHTML = '<option value="">No ontology packs</option>';
        sel.disabled = true;
        return;
      }
      sel.disabled = false;
      sel.innerHTML = '<option value="">System default (globally active pack)</option>' +
        d.packs.map((pk) =>
          '<option value="' + pk.id + '"' + (pk.selected ? ' selected' : '') + '>' +
          escHtml(pk.name) + (pk.is_active ? '  [system default]' : '') + '</option>').join('');
      if (!sel._bound) {
        sel._bound = true;
        sel.addEventListener('change', async () => {
          const v = sel.value ? +sel.value : null;
          try {
            await api.post('/api/my/pack', { pack_id: v });
            toast('Scenario switched — reloading your workspace…', 'ok', 2500);
            await refreshAfterPackSwitch();
          } catch (e) { toast(e.message, 'err', 5000); }
        });
      }
    } catch (e) { /* header select is best-effort */ }
  }

  async function refreshAfterPackSwitch() {
    state.config = await api.get('/api/config');
    const p = state.config.pack;
    const pill = document.getElementById('packPill');
    pill.innerHTML = '<span>' + (p && p.name
      ? '📦 ' + escHtml(p.name) : '⚠️ No active ontology pack') + '</span>';
    pill.title = p && p.name
      ? p.name + (state.config.model.configured ? ' · ' + state.config.model.name : '')
      : 'No active ontology pack';
    if (state.config.model.configured)
      pill.innerHTML +=
        '<span>· 🤖 ' + escHtml(state.config.model.name) + '</span>';
    const g = await api.get('/api/graph');
    state.fullGraph = g.graph;
    dashRendered = false;
    ana.loaded = false;
    gsState.sources = null;
    document.getElementById('gsGraphHost').dataset.built = '';
    const q = await api.get('/api/questions');
    renderChips((q.questions || []).slice(0, 8), 'standard');
    await loadMyQuestionChips();
    loadPackSelect();
    loadDashboard();
    if (document.getElementById('view-analytics').classList.contains('active')) loadAnalytics();
    if (document.getElementById('view-graphstudio').classList.contains('active')) loadGraphStudio();
  }

  /* Suggestion chips — three groups: AI recommendations from the
     ontology, the user's own saved questions, and the admin-curated
     canonical list. */

  function chipGroup(id, label) {
    let g = document.getElementById('cg-' + id);
    if (!g) {
      g = document.createElement('div');
      g.id = 'cg-' + id;
      g.className = 'chip-group';
      g.innerHTML = '<div class="chip-group-label">' + label + '</div>' +
        '<div class="chips"></div>';
      const row = document.getElementById('chipRow');
      row.appendChild(g);
    }
    return g.querySelector('.chips');
  }

  function renderChips(questions, group) {
    const host = chipGroup(group,
      group === 'recommended' ? '✨ Recommended for you'
        : group === 'mine' ? '★ My questions'
          : group === 'history' ? '🕘 Recent asks (click to re-ask)'
            : 'Standard questions');
    host.innerHTML = '';
    if (!questions || !questions.length) {
      host.parentElement.style.display = 'none';
      return;
    }
    host.parentElement.style.display = '';
    questions.forEach((item) => {
      const q = typeof item === 'string' ? item : item.question;
      const b = document.createElement('button');
      b.className = 'chip';
      b.textContent = q;
      b.onclick = () => { document.getElementById('askInput').value = q; send(); };
      if (group === 'mine' && typeof item === 'object') {
        const x = document.createElement('span');
        x.className = 'chip-x';
        x.textContent = '×';
        x.title = 'Remove from my questions';
        x.onclick = async (e) => {
          e.stopPropagation();
          try {
            await api.del('/api/my-questions/' + item.id);
            await loadMyQuestionChips();
          } catch (err) { toast(err.message, 'err'); }
        };
        b.appendChild(x);
      }
      host.appendChild(b);
    });
  }

  async function loadMyQuestionChips() {
    try {
      const [mine, rec] = await Promise.all([
        api.get('/api/my-questions'),
        api.get('/api/recommended-questions'),
      ]);
      renderChips(mine.questions || [], 'mine');
      renderChips((rec.questions || []).slice(0, 8), 'recommended');
    } catch (e) { /* chips are optional */ }
    try {
      const h = await api.get('/api/ask/history');
      renderChips((h.history || []).slice(0, 6), 'history');
    } catch (e) { /* history chips are optional */ }
  }

  async function saveCurrentQuestion() {
    const inp = document.getElementById('askInput');
    const q = inp.value.trim();
    if (!q) { toast('Type a question first, then save it.', 'warn'); return; }
    try {
      await api.post('/api/my-questions', { question: q });
      toast('Saved to your question list.', 'ok', 2200);
      await loadMyQuestionChips();
    } catch (e) { toast(e.message, 'err'); }
  }
  document.getElementById('saveQBtn').addEventListener('click', saveCurrentQuestion);

  /* -------------------------------------------------------- batch questions */

  document.getElementById('batchQBtn').addEventListener('click', openBatchModal);

  function openBatchModal() {
    const body = document.createElement('div');
    body.innerHTML =
      '<p class="small muted">Paste one question per line (max 20). Each is asked in <b>' +
      escHtml(state.mode) + '</b> mode, answered in sequence, and recorded in your ask ' +
      'history — later questions use earlier answers as memory for follow-ups.</p>' +
      '<label class="fld"><span>Questions — one per line</span>' +
      '<textarea id="bqText" rows="8" placeholder="What drives the VNB gap?&#10;Which channel grew fastest last month?"></textarea></label>' +
      '<label class="switch" style="margin-top:8px;font-weight:400;color:var(--ink-2)">' +
      '<input type="checkbox" id="bqSave" checked><span class="track"></span>' +
      'Save all questions to my question list</label>';
    const run = el('button', { class: 'btn btn-primary' }, 'Ask all');
    const cancel = el('button', { class: 'btn btn-ghost' }, 'Cancel');
    const m = openModal({ title: '📋 Batch questions', body, footer: [cancel, run] });
    cancel.onclick = () => m.close();
    run.onclick = async () => {
      const lines = m.mask.querySelector('#bqText').value.split('\n')
        .map((s) => s.trim()).filter(Boolean).slice(0, 20);
      if (!lines.length) { toast('Paste at least one question.', 'warn'); return; }
      const saveAll = m.mask.querySelector('#bqSave').checked;
      m.close();
      switchView('chat');
      document.getElementById('chatIntro').style.display = 'none';
      for (const q of lines) await askCore(q);
      if (saveAll) {
        try {
          for (const q of lines) await api.post('/api/my-questions', { question: q });
          await loadMyQuestionChips();
        } catch (e) { /* saving is optional */ }
      }
      toast('Batch finished — ' + lines.length + ' question(s) answered. Package the ' +
        'results with 📄 Generate report or ⚡ Sample report.', 'ok', 4500);
    };
  }

  /* ----------------------------------------------------- ontology search */

  let _ontoSearchTimer = null;
  function initOntoSearch() {
    const inp = document.getElementById('ontoSearch');
    if (!inp || inp.dataset.bound) return;
    inp.dataset.bound = '1';
    inp.addEventListener('input', () => {
      clearTimeout(_ontoSearchTimer);
      const q = inp.value.trim();
      const box = document.getElementById('ontoSearchResults');
      const ins = document.getElementById('ontoInsight');
      if (!q) { box.innerHTML = ''; ins.innerHTML = ''; return; }
      _ontoSearchTimer = setTimeout(async () => {
        try {
          const d = await api.get('/api/search/insight?q=' + encodeURIComponent(q));
          renderOntoResults(box, ins, q, d);
        } catch (e) { box.innerHTML = ''; ins.innerHTML = ''; }
      }, 220);
    });
  }

  function renderOntoResults(box, ins, q, d) {
    if (!d.results.length) {
      box.innerHTML = '<span class="muted">No matches for "' + escHtml(q) + '".</span>';
      ins.innerHTML = '';
      return;
    }
    box.innerHTML = d.results.map((r) =>
      '<span class="chip" style="font-size:12px;margin:3px"><b>' +
      escHtml(r.kind) + '</b>: ' + escHtml(r.label) +
      ' <span class="muted">(' + escHtml(r.key) + ')</span></span>').join('');
    if (!ins) return;
    const a = d.analysis;
    const sub = d.subgraph || { nodes: [], links: [] };
    const hasGraph = sub.nodes && sub.nodes.length;
    if (!a && !hasGraph) { ins.innerHTML = ''; return; }
    const facts = a && a.facts && a.facts.length
      ? '<div class="grid grid-2" style="gap:6px 18px;margin:8px 0">' +
      a.facts.map((f) =>
        '<div class="small"><b class="muted">' + escHtml(f[0]) + ':</b> ' +
        escHtml(f[1]) + '</div>').join('') + '</div>'
      : '';
    ins.innerHTML =
      '<div class="card" style="border-left:3px solid var(--red)">' +
      '<div class="card-b">' +
      '<div class="flex" style="align-items:center;gap:8px;flex-wrap:wrap">' +
      '<span class="badge red">Instant insight</span>' +
      (a ? '<b>' + escHtml(a.title) + '</b>' : '') +
      (a && a.kind ? '<span class="badge neutral">' + escHtml(a.kind) + '</span>' : '') +
      '</div>' +
      (a && a.description ? '<p class="small muted" style="margin:6px 0 0">' +
        escHtml(a.description) + '</p>' : '') +
      facts +
      (hasGraph
        ? '<div class="small muted" style="margin:10px 0 4px"><b>Affected graph ' +
        'neighbourhood</b> — matched nodes plus one hop (' + sub.nodes.length +
        ' nodes · ' + sub.links.length + ' links):</div>' +
        '<div id="ontoInsightGraph" style="min-height:300px"></div>'
        : '') +
      (a ? '<div class="small" style="margin-top:8px">' +
        renderMd(a.paragraph) + '</div>' : '') +
      '</div></div>';
    if (hasGraph) {
      Onti.Graph.render(document.getElementById('ontoInsightGraph'),
        Object.assign({}, sub, { highlight: d.node_keys }),
        { compact: true, legend: true });
    }
  }

  /* --------------------------------------------------------- model centre */

  async function updateAskModelName() {
    const elName = document.getElementById('askModelName');
    if (!elName) return;
    try {
      const d = await api.get('/api/models');
      const cur = d.models.find((m) => m.id === d.selected);
      elName.textContent = cur ? cur.name
        : (d.models.find((m) => m.is_default) || { name: 'Default' }).name;
    } catch (e) { elName.textContent = 'Default'; }
  }

  async function selectModel(id) {
    try {
      const r = await api.post('/api/models/select', { config_id: id || null });
      toast(r.selected
        ? 'Your questions now use: ' + r.name
        : 'Back to the default model.', 'ok', 2200);
    } catch (e) {
      toast(e.message, 'err');
    }
    await loadModelsView();
    updateAskModelName();
  }

  async function loadModelsView() {
    const body = document.getElementById('modelsBody');
    if (!body) return;
    const addBtn = document.getElementById('mcAddBtn');
    if (addBtn && !addBtn.dataset.bound) {
      addBtn.dataset.bound = '1';
      addBtn.addEventListener('click', () => openModelCentre(() => loadModelsView()));
    }
    body.innerHTML = '<div class="empty">Loading models…</div>';
    let sys, mine;
    try {
      [sys, mine] = await Promise.all([
        api.get('/api/models'), api.get('/api/my/models')
      ]);
    } catch (e) {
      body.innerHTML = emptyState('⚠️', 'Could not load models', e.message);
      return;
    }
    const sysModels = sys.models.filter((m) => !m.mine);
    const myModels = mine.models || [];
    const sel = sys.selected || '';

    function card(inner) {
      return '<div class="card" style="padding:12px 16px;display:flex;' +
        'align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px">' +
        inner + '</div>';
    }

    function rowActions(m) {
      const inUse = sel === m.id;
      return '<span class="spacer"></span>' +
        (inUse ? '<span class="badge rules">✓ In use</span>' : '') +
        (m.is_default ? '<span class="badge neutral">System default</span>' : '') +
        (inUse ? '' : '<button class="btn btn-primary btn-xs" data-act="sel" data-id="' +
          escHtml(String(m.id)) + '">Select</button>');
    }

    let html = '<h3 style="margin:4px 0 10px">System models</h3>';
    const defSys = sysModels.find((m) => m.is_default);
    html += card(
      '<div><b>Default</b> <span class="muted small">— follow the system default' +
      (defSys ? ' (' + escHtml(defSys.name) + ')' : '') +
      '</span></div>' +
      (sel
        ? '<span class="spacer"></span><button class="btn btn-ghost btn-xs" data-act="sel" data-id="">Use default</button>'
        : '<span class="badge rules">✓ In use</span>'));
    sysModels.forEach((m) => {
      html += card(
        '<div><b>' + escHtml(m.name) + '</b> <span class="badge neutral">' +
        escHtml(m.model) + '</span></div>' + rowActions(m));
    });

    html += '<h3 style="margin:18px 0 10px">My models</h3>';
    if (!myModels.length) {
      html += '<div class="empty">No personal models yet — click "＋ Add my model" ' +
        'to register an OpenAI-compatible endpoint (OpenAI, Azure OpenAI, ' +
        'Ollama, vLLM…).</div>';
    } else {
      myModels.forEach((m) => {
        html += card(
          '<div><b>' + escHtml(m.name) + '</b> <span class="badge neutral">' +
          escHtml(m.model) + '</span></div>' +
          rowActions(m) +
          '<button class="btn btn-ghost btn-xs" data-act="test" data-id="' +
          m.id + '">Test</button>' +
          '<button class="btn btn-ghost btn-xs" data-act="edit" data-id="' +
          m.id + '">Edit</button>' +
          '<button class="btn btn-ghost btn-xs" data-act="del" data-id="' +
          m.id + '">Delete</button>');
      });
    }

    body.innerHTML = html;
    body.querySelectorAll('button[data-act]').forEach((b) => {
      b.onclick = async () => {
        const act = b.dataset.act;
        const id = b.dataset.id;
        if (act === 'sel') { await selectModel(id || null); return; }
        const m = myModels.find((z) => String(z.id) === String(id));
        if (!m) return;
        if (act === 'test') {
          b.disabled = true; b.textContent = 'Testing…';
          try {
            const r = await api.post('/api/my/models/test', { config_id: 'my:' + m.id });
            if (r.ok) toast('"' + m.name + '" connected successfully.', 'ok', 3000);
            else toast('Test failed: ' + r.error + (r.hint ? ' — ' + r.hint : ''), 'err', 8000);
          } catch (e) {
            const hint = (e.extra && e.extra.hint) ? ' — ' + e.extra.hint : '';
            toast('Test failed: ' + e.message + hint, 'err', 6500);
          }
          finally { b.disabled = false; b.textContent = 'Test'; }
        } else if (act === 'del') {
          confirmAction('Delete model "' + m.name + '"?', async () => {
            try {
              await api.del('/api/my/models/' + m.id);
              toast('Model deleted.', 'ok', 2200);
              await loadModelsView();
              updateAskModelName();
            } catch (e) { toast(e.message, 'err'); }
          });
        } else if (act === 'edit') {
          mcEditInline(m);
        }
      };
    });

    function mcEditInline(m) {
      const host = document.createElement('div');
      host.innerHTML = '<b style="display:block;margin:6px 0">Edit "' +
        escHtml(m.name) + '"</b><div id="mcEditForm"></div>';
      const form = host.querySelector('#mcEditForm');
      form.innerHTML = mcFormFields(m);
      const save = el('button', { class: 'btn btn-primary btn-sm' }, 'Save model');
      const cancel = el('button', { class: 'btn btn-ghost btn-sm' }, 'Cancel');
      const bar = el('div', { class: 'flex', style: 'gap:8px;margin:6px 0 14px' }, [save, cancel]);
      form.appendChild(bar);
      body.insertBefore(host, body.firstChild);
      cancel.onclick = () => host.remove();
      save.onclick = async () => {
        const payload = mcReadForm(form);
        payload.write_env = false; // keep the existing env registration untouched
        save.disabled = true;
        try {
          await api.put('/api/my/models/' + m.id, payload);
          toast('Model saved.', 'ok', 2200);
          await loadModelsView();
          updateAskModelName();
        } catch (e) { toast(e.message, 'err', 5000); }
        finally { save.disabled = false; }
      };
      host.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }

  /* --------------------------------------------------- model centre modal */

  function mcFormFields(f) {
    return '<label class="fld"><span>Display name *</span>' +
      '<input id="mcName" type="text" placeholder="e.g. My Azure GPT" value="' +
      escHtml(f.name || '') + '"></label>' +
      '<label class="fld"><span>Base URL (OpenAI-compatible) *</span>' +
      '<input id="mcBase" type="text" placeholder="https://api.openai.com/v1" value="' +
      escHtml(f.base_url || '') + '"></label>' +
      '<label class="fld"><span>API key' + (f.id ? ' (leave blank to keep the saved key)' : ' *') + '</span>' +
      '<input id="mcKey" type="password" autocomplete="new-password" spellcheck="false" ' +
      'name="onti-model-api-key" placeholder="' +
      (f.api_key_masked ? escHtml(f.api_key_masked) : 'sk-...') + '"></label>' +
      '<label class="fld"><span>Model name *</span>' +
      '<input id="mcModel" type="text" placeholder="e.g. gpt-4o-mini" value="' +
      escHtml(f.model || '') + '"></label>' +
      '<div class="grid grid-3" style="gap:8px;margin-bottom:10px">' +
      '<label class="fld"><span>Temperature</span><input id="mcTemp" type="number" min="0" max="2" step="0.1" value="' +
      (f.temperature != null ? f.temperature : 0.3) + '"></label>' +
      '<label class="fld"><span>Max tokens</span><input id="mcMax" type="number" min="64" step="64" value="' +
      (f.max_tokens != null ? f.max_tokens : 1024) + '"></label>' +
      '<label class="fld"><span>Timeout (s)</span><input id="mcTo" type="number" min="10" step="5" value="' +
      (f.timeout != null ? f.timeout : 90) + '"></label>' +
      '</div>' +
      '<label class="switch" style="font-weight:400;color:var(--ink-2)">' +
      '<input type="checkbox" id="mcVerifySSL"' + (f.verify_ssl !== false ? ' checked' : '') + '><span class="track"></span>' +
      'Verify SSL certificate' +
      '<span class="hint" style="margin-left:8px">Uncheck for self-signed gateways (e.g. Deloitte Nova)</span></label>' +
      '<label class="switch" style="font-weight:400;color:var(--ink-2)">' +
      '<input type="checkbox" id="mcEnv" checked><span class="track"></span>' +
      'Also register in the server .env file (survives DB resets)</label>';
  }

  function mcReadForm(mask) {
    return {
      name: mask.querySelector('#mcName').value.trim(),
      base_url: mask.querySelector('#mcBase').value.trim(),
      api_key: mask.querySelector('#mcKey').value,
      model: mask.querySelector('#mcModel').value.trim(),
      temperature: +mask.querySelector('#mcTemp').value || 0.3,
      max_tokens: +mask.querySelector('#mcMax').value || 1024,
      timeout: +mask.querySelector('#mcTo').value || 90,
      verify_ssl: mask.querySelector('#mcVerifySSL').checked,
      write_env: mask.querySelector('#mcEnv').checked,
    };
  }

  function openModelCentre(onClose) {
    const body = document.createElement('div');
    body.innerHTML =
      '<p class="small muted">Add your own OpenAI-compatible model (OpenAI, Azure ' +
      'OpenAI, Ollama, vLLM, …). It is stored for your account only — no .env editing ' +
      'required — and appears in the model picker next to the system models.</p>' +
      '<div id="mcList" class="small"><div class="muted">Loading…</div></div>' +
      '<hr style="border:none;border-top:1px solid #e8ecf2;margin:12px 0">' +
      '<div id="mcFormWrap"></div>';
    const m = openModal({ title: '⚙ Model centre', body, footer: [] });
    const done = () => { try { onClose && onClose(); } catch (e) { /* noop */ } };

    async function renderList() {
      const host = body.querySelector('#mcList');
      try {
        const d = await api.get('/api/my/models');
        if (!d.models.length) {
          host.innerHTML = '<div class="muted small">You have no personal models yet — add one below.</div>';
          return;
        }
        host.innerHTML = d.models.map((x) =>
          '<div style="display:flex;gap:8px;align-items:center;margin:6px 0;padding:8px;' +
          'background:var(--blue-soft,#F2F6FB);border-radius:8px;flex-wrap:wrap">' +
          '<b>' + escHtml(x.name) + '</b>' +
          '<span class="badge neutral">' + escHtml(x.model) + '</span>' +
          (d.selected === 'my:' + x.id ? '<span class="badge rules">✓ in use</span>' : '') +
          '<span class="spacer"></span>' +
          '<button class="btn btn-ghost btn-xs" data-act="test" data-id="' + x.id + '">Test</button>' +
          '<button class="btn btn-ghost btn-xs" data-act="edit" data-id="' + x.id + '">Edit</button>' +
          '<button class="icon-btn danger" data-act="del" data-id="' + x.id + '" title="Delete">🗑</button>' +
          '</div>').join('');
        host.querySelectorAll('button[data-act]').forEach((b) => {
          b.onclick = () => {
            const x = d.models.find((z) => z.id === +b.dataset.id);
            if (b.dataset.act === 'test') mcTest(x, b);
            else if (b.dataset.act === 'edit') mcEdit(x);
            else mcDelete(x);
          };
        });
      } catch (e) { host.innerHTML = '<div class="muted small">' + escHtml(e.message) + '</div>'; }
    }

    async function mcTest(x, btn) {
      btn.disabled = true; btn.textContent = 'Testing…';
      try {
        const r = await api.post('/api/my/models/test', { config_id: 'my:' + x.id });
        if (r.ok) toast('"' + x.name + '" connected successfully.', 'ok', 3000);
        else toast('Test failed: ' + r.error + (r.hint ? ' — ' + r.hint : ''), 'err', 8000);
      } catch (e) {
        const hint = (e.extra && e.extra.hint) ? ' — ' + e.extra.hint : '';
        toast('Test failed: ' + e.message + hint, 'err', 6500);
      }
      finally { btn.disabled = false; btn.textContent = 'Test'; }
    }

    function mcDelete(x) {
      confirmAction('Delete model "' + x.name + '"?', async () => {
        try {
          await api.del('/api/my/models/' + x.id);
          toast('Model deleted.', 'ok', 2200);
          await renderList(); done();
        } catch (e) { toast(e.message, 'err'); }
      });
    }

    function mcForm(title, f, submit) {
      const wrap = body.querySelector('#mcFormWrap');
      wrap.innerHTML = '<b>' + escHtml(title) + '</b><div style="margin-top:8px">' +
        mcFormFields(f) + '</div>';
      const save = el('button', { class: 'btn btn-primary btn-sm' }, 'Save model');
      const cancel = el('button', { class: 'btn btn-ghost btn-sm' }, 'Cancel');
      const bar = el('div', { class: 'flex', style: 'gap:8px;margin-top:4px' }, [save, cancel]);
      wrap.appendChild(bar);
      cancel.onclick = () => { wrap.innerHTML = ''; };
      save.onclick = async () => {
        const payload = mcReadForm(wrap);
        save.disabled = true;
        try {
          await submit(payload);
          toast('Model saved.', 'ok', 2200);
          wrap.innerHTML = '';
          await renderList(); done();
        } catch (e) { toast(e.message, 'err', 5000); }
        finally { save.disabled = false; }
      };
    }

    mcForm('Add a model', {}, async (p) => { await api.post('/api/my/models', p); });

    function mcEdit(x) {
      mcForm('Edit "' + x.name + '"', x, async (p) => {
        if (!p.api_key) p.api_key = '__unchanged__';
        await api.put('/api/my/models/' + x.id, p);
      });
    }

    m.mask.addEventListener('click', (e) => {
      if (e.target === m.mask) done();
    });
  }

  /* --------------------------------------------------------- graph studio */

  const gsState = { sources: null, view: 'lineage' };

  const GS_VIEW_HINTS = {
    schema: 'The ontology class structure — entities and how they relate. Small and easy to read.',
    lineage: 'Which data tables feed which entities (explicit mapping or inferred from matched columns).',
    columns: 'One node per unique column concept — columns shared by several tables are merged into one node with all source tables listed.',
  };

  async function loadGraphStudio() {
    if (!Onti.ontology.enabled()) {
      document.getElementById('gsSources').innerHTML = Onti.ontology.offPanel(
        'Graph studio is hidden because ontology mode is off.',
        'Schema, data-lineage and column-mapping graphs are ontology views. Turn '
        + 'ontology back on to browse them.');
      const host = document.getElementById('gsGraphHost');
      host.innerHTML = '';
      delete host.dataset.built;
      const oi = document.getElementById('oiGraphHost');
      if (oi) oi.innerHTML = '';
      Onti.ontology.bindRestore(document.getElementById('view-graphstudio'));
      return;
    }
    try {
      const d = await api.get('/api/graph-studio/sources');
      gsState.sources = d;
      renderGsSources();
      renderOiSources();
      bindGsViewSwitch();
      // Auto-build the compact schema graph on first entry.
      if (!document.getElementById('gsGraphHost').dataset.built) {
        document.getElementById('gsGraphHost').dataset.built = '1';
        gsBuild();
      }
      loadGsHistory();
    } catch (e) {
      document.getElementById('gsSources').innerHTML = emptyState('📦',
        'No ontology pack is active',
        'Ask an administrator to create or activate a pack.');
    }
  }

  function tableChip(t, cls) {
    const origin = t.dataset_file
      ? ' · 📄 ' + escHtml(t.dataset_file) + ' <span title="Dataset: ' +
      escHtml(t.dataset_name || '-') + '">' +
      escHtml(t.dataset_kind || 'uploaded') +
      (t.dataset_updated ? ' · ' + escHtml(t.dataset_updated) : '') + '</span>'
      : '';
    return '<label style="display:flex;gap:8px;align-items:center;margin:4px 0">' +
      '<input type="checkbox" class="' + cls + '" value="' + t.id + '" checked> ' +
      escHtml(t.label) + ' <span class="mono muted small">' + escHtml(t.key) +
      '</span> <span class="muted small">· ' + t.row_count + ' rows · ' +
      t.columns.length + ' columns' + origin + '</span></label>';
  }

  function renderGsSources() {
    const d = gsState.sources;
    const tables = (d && d.tables) || [];
    const host = document.getElementById('gsSources');
    host.innerHTML = tables.length
      ? tables.map((t) => tableChip(t, 'gsTbl')).join('')
      : '<div class="muted small">No data tables yet — ask your administrator to import data first.</div>';
    document.getElementById('gsViewHint').textContent = GS_VIEW_HINTS[gsState.view];
    const needTables = gsState.view !== 'schema';
    document.getElementById('gsTablePicker').style.display = needTables ? '' : 'none';
    document.getElementById('gsUnmatchedWrap').style.display =
      gsState.view === 'columns' ? 'flex' : 'none';
    document.getElementById('gsUnmatched').checked = true;
  }

  function bindGsViewSwitch() {
    const wrap = document.getElementById('gsViewSwitch');
    if (wrap.dataset.bound) return;
    wrap.dataset.bound = '1';
    wrap.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
      gsState.view = b.dataset.v;
      wrap.querySelectorAll('button').forEach((x) =>
        x.setAttribute('aria-pressed', x === b ? 'true' : 'false'));
      renderGsSources();
      gsBuild();
    }));
    document.getElementById('gsSelAll').addEventListener('click', () => {
      document.querySelectorAll('.gsTbl').forEach((c) => c.checked = true);
    });
    document.getElementById('gsSelNone').addEventListener('click', () => {
      document.querySelectorAll('.gsTbl').forEach((c) => c.checked = false);
    });
  }

  function gsShowProvenance(raw) {
    const box = document.getElementById('gsProvenance');
    const s = raw.source || {};
    box.innerHTML =
      '<div style="font-weight:700;margin-bottom:6px">' + escHtml(raw.label || raw.id) + '</div>' +
      '<div class="badge neutral" style="margin-bottom:8px">' + escHtml(s.kind || 'node') + '</div>' +
      '<div class="mt8">' + escHtml(s.detail || raw.desc || '') + '</div>' +
      (raw.value ? '<div class="mt8 muted">Value: ' + escHtml(String(raw.value)) + '</div>' : '');
  }

  function gsRender(graph) {
    Onti.Graph.render(document.getElementById('gsGraphHost'), graph, {
      compact: graph.nodes.length > 24,
      onNodeClick: (raw) => gsShowProvenance(raw),
    });
    document.getElementById('gsStats').textContent =
      graph.nodes.length + ' nodes · ' + graph.links.length + ' edges · ' +
      graph.view + ' view';
    const legend = document.getElementById('gsLegend');
    const colors = { entity: '#0E2A47', instance: '#3D7DCC', assumption: '#2B9BD4', mix: '#7A4FB0' };
    legend.innerHTML = Object.entries(graph.groups || {}).map(([g, lab]) =>
      '<span style="display:inline-flex;align-items:center;gap:6px">' +
      '<span style="width:10px;height:10px;border-radius:3px;display:inline-block;background:' +
      (colors[g] || '#A9B8C9') + '"></span>' + escHtml(lab) + '</span>').join('');
    document.getElementById('gsSourceList').innerHTML =
      (graph.sources_used || []).map((s) =>
        '<div style="margin:3px 0">• ' + escHtml(s) + '</div>').join('') ||
      '<span class="muted">—</span>';
  }

  async function gsBuild() {
    if (!gsState.sources) return;
    try {
      const tableIds = [...document.querySelectorAll('.gsTbl:checked')].map((c) => +c.value);
      const graph = await api.post('/api/graph-studio/build', {
        view: gsState.view,
        table_ids: tableIds,
        include_unmatched: !!(document.getElementById('gsUnmatched') || {}).checked,
      });
      gsRender(graph);
    } catch (e) { toast(e.message, 'err'); }
  }

  document.getElementById('gsBuildBtn').addEventListener('click', gsBuild);

  /* -------------------------------------------- generation history list */

  const GS_ACTION_META = {
    ontology: { label: 'Ontology created', color: '#86BC25' },
    preview: { label: 'Ontology preview', color: '#2B9BD4' },
    'data-upload': { label: 'Graph from data file', color: '#B7791F' },
    'ontology-upload': { label: 'Graph from ontology file', color: '#7A4FB0' },
    report: { label: 'Auto report', color: '#ED1B2E' },
  };

  async function loadGsHistory() {
    const host = document.getElementById('gsHistory');
    if (!host) return;
    try {
      const d = await api.get('/api/graph-studio/history');
      const items = d.history || [];
      if (!items.length) {
        host.innerHTML = '<span class="muted">Nothing generated yet — build a ' +
          'graph, upload a file, or create an ontology and it will be listed here.</span>';
        return;
      }
      host.innerHTML = items.map((h) => {
        const m = GS_ACTION_META[h.action] || { label: h.action, color: '#A9B8C9' };
        const reportLink = h.action === 'report' && h.detail && h.detail.length <= 40
          ? ' <a href="/api/reports/' + escHtml(h.detail) + '/view" target="_blank">open report</a>'
          : '';
        const switchBtn = h.action === 'ontology' && h.pack_id
          ? ' <button class="btn btn-ghost btn-xs" data-switchpack="' + h.pack_id +
          '" title="Make this ontology your working scenario">Switch to it</button>'
          : '';
        return '<div style="display:flex;gap:10px;align-items:baseline;margin:6px 0;' +
          'border-bottom:1px solid #eef1f6;padding-bottom:6px">' +
          '<span class="badge neutral" style="color:' + m.color + ';white-space:nowrap">' +
          escHtml(m.label) + '</span>' +
          '<span style="flex:1"><b>' + escHtml(h.title) + '</b>' +
          ' <span class="muted small">· ' + escHtml(h.pack_name || '') + '</span>' +
          '<div class="muted small">' + escHtml(h.detail || '') + '</div></span>' +
          '<span class="muted small" style="white-space:nowrap">' +
          escHtml((h.created_at || '').replace('T', ' ').slice(0, 16)) + '</span>' +
          reportLink + switchBtn + '</div>';
      }).join('');
      host.querySelectorAll('[data-switchpack]').forEach((b) => {
        b.onclick = async () => {
          try {
            await api.post('/api/my/pack', { pack_id: +b.dataset.switchpack });
            toast('Scenario switched to that ontology.', 'ok', 2500);
            await refreshAfterPackSwitch();
          } catch (e) { toast(e.message, 'err', 5000); }
        };
      });
    } catch (e) {
      host.innerHTML = '<span class="muted">History unavailable.</span>';
    }
  }
  document.getElementById('gsHistoryRefresh').addEventListener('click', loadGsHistory);

  document.getElementById('gsUploadInput').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      const fd = new FormData();
      fd.append('file', file);
      const graph = await api.upload('/api/graph-studio/upload', fd);
      gsRender(graph);
      loadGsHistory();
      toast('Graph generated from ' + file.name + ' (preview only — nothing stored).', 'ok', 3500);
    } catch (err) { toast(err.message, 'err', 5000); }
    finally { e.target.value = ''; }
  });

  /* ------------------------------------- smart ontology generation (infer) */

  function renderOiSources() {
    const host = document.getElementById('oiSources');
    if (!host) return;
    const tables = (gsState.sources && gsState.sources.tables) || [];
    host.innerHTML = tables.length
      ? tables.map((t) => tableChip(t, 'oiTbl')).join('')
      : '<div class="muted small">No data tables yet — ask your administrator to import a CSV/XLSX file first.</div>';
  }

  function oiSelected() {
    return [...document.querySelectorAll('.oiTbl:checked')].map((c) => +c.value);
  }

  document.getElementById('oiSelAll').addEventListener('click', () =>
    document.querySelectorAll('.oiTbl').forEach((c) => c.checked = true));
  document.getElementById('oiSelNone').addEventListener('click', () =>
    document.querySelectorAll('.oiTbl').forEach((c) => c.checked = false));

  document.getElementById('oiCancelBtn').addEventListener('click', () => {
    document.getElementById('oiPreview').style.display = 'none';
  });

  document.getElementById('oiPreviewBtn').addEventListener('click', async () => {
    const tableIds = oiSelected();
    if (!tableIds.length) { toast('Pick at least one table.', 'warn'); return; }
    const name = document.getElementById('oiName').value.trim();
    const useAI = !!(document.getElementById('oiAI') || {}).checked;
    const btn = document.getElementById('oiPreviewBtn');
    btn.disabled = true;
    if (useAI) btn.textContent = '🔍 Preview (AI enriching…)';
    try {
      const d = await api.post('/api/graph-studio/infer', {
        table_ids: tableIds, name, ai_enrich: useAI,
      });
      document.getElementById('oiCntEnt').textContent = d.counts.entities + ' entities';
      document.getElementById('oiCntAttr').textContent = d.counts.attributes + ' attributes';
      document.getElementById('oiCntRel').textContent = d.counts.relations + ' relations';
      document.getElementById('oiEntList').innerHTML =
        '<div class="small muted" style="margin-bottom:4px"><b>Entities</b></div>' +
        d.entities.map((e) => '<div style="margin:3px 0">• <b>' + escHtml(e.label) +
          '</b> <span class="muted">' + escHtml(e.key) + '</span>' +
          (e.description ? ' — ' + escHtml(e.description) : '') + '</div>').join('');
      document.getElementById('oiRelList').innerHTML =
        '<div class="small muted" style="margin-bottom:4px"><b>Relations</b></div>' +
        (d.relations.length
          ? d.relations.map((r) => '<div style="margin:3px 0">• ' + escHtml(r.source) +
            ' → ' + escHtml(r.target) + ' <span class="badge neutral">' +
            escHtml(r.cardinality) + '</span></div>').join('')
          : '<div class="muted">No foreign-key style relations detected (add *_id columns to link tables).</div>');
      const aiHost = document.getElementById('oiAiQs');
      if (d.ai_questions && d.ai_questions.length) {
        aiHost.innerHTML =
          '<div class="firstrun-banner" style="margin-bottom:6px">🤖 <b>AI-suggested starter questions</b>' +
          ' — these will be added to the new ontology:</div>' +
          d.ai_questions.map((q) => '<div style="margin:3px 0">• ' + escHtml(q) + '</div>').join('');
      } else {
        aiHost.innerHTML = useAI
          ? '<div class="muted small">AI enrichment returned no questions (no model configured or the model call failed) — the ontology is built from your data structure alone.</div>'
          : '';
      }
      Onti.Graph.render(document.getElementById('oiGraphHost'), d.graph, { compact: true });
      document.getElementById('oiPreview').style.display = '';
      document.getElementById('oiPreview').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      loadGsHistory();
    } catch (e) { toast(e.message, 'err', 5000); }
    finally { btn.disabled = false; btn.textContent = '🔍 Preview inferred ontology'; }
  });

  document.getElementById('oiCreateBtn').addEventListener('click', async () => {
    const tableIds = oiSelected();
    const name = document.getElementById('oiName').value.trim();
    if (!name) { toast('Give the new ontology a name.', 'warn'); return; }
    if (!confirm('Create the ontology "' + name + '" with ' + tableIds.length +
      ' table(s), copy the current data in, and activate it?')) return;
    const btn = document.getElementById('oiCreateBtn');
    btn.disabled = true;
    try {
      const r = await api.post('/api/graph-studio/create-ontology',
        { table_ids: tableIds, name, ai_enrich: !!(document.getElementById('oiAI') || {}).checked });
      const rep = r.report && r.report.uid
        ? ' <a href="/api/reports/' + escHtml(r.report.uid) + '/view" target="_blank" style="text-decoration:underline">Open the auto-generated report</a>'
        : '';
      toast('Ontology created and activated — a default report was generated. ' + rep, 'ok', 8000);
      // The active pack changed — refresh everything that depends on it.
      state.config = await api.get('/api/config');
      const p = state.config.pack;
      const pill = document.getElementById('packPill');
      pill.innerHTML = '<span>' + (p && p.name
        ? '📦 ' + escHtml(p.name) : '⚠️ No active ontology pack') + '</span>';
      pill.title = p && p.name
        ? p.name + (state.config.model.configured ? ' · ' + state.config.model.name : '')
        : 'No active ontology pack';
      if (state.config.model.configured)
        pill.innerHTML +=
          '<span>· 🤖 ' + escHtml(state.config.model.name) + '</span>';
      const g = await api.get('/api/graph');
      state.fullGraph = g.graph;
      gsState.sources = null;
      ana.loaded = false;
      dashRendered = false;
      document.getElementById('gsGraphHost').dataset.built = '';
      document.getElementById('oiPreview').style.display = 'none';
      document.getElementById('oiName').value = '';
      await loadMyQuestionChips();
      loadPackSelect();
      loadGsHistory();
      loadDashboard();
      switchView('chat');
    } catch (e) { toast(e.message, 'err', 5000); }
    finally { btn.disabled = false; }
  });

  /* ------------------------------------------------------------- mode switch */

  const modeNotes = {
    auto: '<b>Auto</b> — rules engine first, AI only when no rule matches.',
    rules: '<b>Rules engine</b> — deterministic, auditable answers from ontology rules. Unmatched questions return clickable suggestions.',
    ai: '<b>AI mode</b> — the configured model answers, grounded in the ontology and active data.',
  };
  document.querySelectorAll('#modeSwitch button').forEach((b) => {
    b.addEventListener('click', () => {
      state.mode = b.dataset.mode;
      document.querySelectorAll('#modeSwitch button').forEach((x) => {
        const on = x === b;
        x.setAttribute('aria-pressed', on);
        x.className = on ? 'mode-' + x.dataset.mode : '';
      });
      document.getElementById('modeNote').innerHTML = modeNotes[state.mode];
      toast('Mode: ' + b.textContent.trim(), '', 1800);
    });
  });

  /* "With / without ontology" comparison toggle (the yellow demo switch). */
  const compareBtn = document.getElementById('compareBtn');
  compareBtn.onclick = () => {
    state.compare = !state.compare;
    compareBtn.setAttribute('aria-pressed', state.compare);
    document.getElementById('sendBtn').textContent = state.compare ? 'Compare' : 'Send';
    document.getElementById('modeNote').innerHTML = state.compare
      ? '<b>Compare</b> — each question is answered twice: once grounded in the ' +
      'active ontology (with its impact chain) and once with no ontology at all. ' +
      'This costs two model calls per question.'
      : modeNotes[state.mode];
    toast(state.compare ? 'Comparison mode on' : 'Comparison mode off', '', 1800);
  };

  /* ------------------------------------------------------------------- send */

  const input = document.getElementById('askInput');
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 160) + 'px';
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  document.getElementById('sendBtn').onclick = send;
  document.getElementById('clearBtn').onclick = () => {
    document.getElementById('thread').innerHTML = '';
    state.qaLog = [];
    document.getElementById('chatIntro').style.display = '';
  };

  function addUserCard(question) {
    return el('div', { class: 'msg-user' }, [el('div', { class: 'q' }, question)]);
  }

  function addAICard() {
    const card = el('div', { class: 'msg-ai' });
    const head = el('div', { class: 'msg-head' });
    const badge = el('span', { class: 'badge neutral' }, '…');
    const meta = el('span', { class: 'meta' }, '');
    head.appendChild(badge); head.appendChild(meta);
    const body = el('div', { class: 'msg-body cursor-blink' });
    const footer = el('div', { class: 'msg-footer', style: 'display:none;margin-top:10px;padding-top:8px;border-top:1px solid #eef0f4' });
    const pinBtn = el('button', { class: 'btn btn-ghost btn-xs' }, '📌 Pin to dashboard');
    footer.appendChild(pinBtn);
    card.appendChild(head); card.appendChild(body); card.appendChild(footer);
    return { card, badge, meta, body, footer, pinBtn };
  }

  async function send() {
    const question = input.value.trim();
    if (!question || state.busy) return;
    await (state.compare ? compareCore(question) : askCore(question));
    input.value = '';
    input.style.height = 'auto';
    input.focus();
  }

  /* Answer with no ontology at all — used whenever the global switch is OFF. */
  async function askWithoutOntology(question) {
    if (!question || state.busy) return null;
    state.busy = true;
    document.getElementById('sendBtn').disabled = true;
    document.getElementById('chatIntro').style.display = 'none';

    const thread = document.getElementById('thread');
    thread.appendChild(addUserCard(question));
    const ui = addAICard();
    ui.badge.className = 'badge neutral';
    ui.badge.textContent = '⚪ Without ontology';
    ui.meta.textContent = 'ontology OFF · raw data tables only, no impact chain';
    thread.appendChild(ui.card);

    try {
      const resp = await fetch('/api/ask/compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, mode: 'ai' }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.error || 'Answer failed (' + resp.status + ')');
      const b = data.without_ontology || {};
      ui.body.classList.remove('cursor-blink');
      ui.body.innerHTML = renderMd(b.answer || '') +
        '<p class="small muted" style="margin-top:10px">Ontology is OFF — this answer uses ' +
        'the uploaded data tables, but no metric definitions, no entity graph and no ' +
        'impact chain. Turn <b>Use ontology</b> back on in ' +
        'the header to answer from the active pack and see its impact chain.</p>';
      if (b.model) ui.meta.textContent = 'model: ' + b.model;
      state.qaLog.push({
        question, answer: (b.answer || '').slice(0, 1200), source: 'no-ontology',
        matched: null, latency_ms: 0, graph: null, suggestions: [],
      });
    } catch (e) {
      ui.body.classList.remove('cursor-blink');
      ui.body.innerHTML = '<p>' + escHtml(e.message) + '</p>';
      toast(e.message, 'err');
    } finally {
      state.busy = false;
      document.getElementById('sendBtn').disabled = false;
      ui.card.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }
    return null;
  }

  /* Ask one question programmatically (chips, batch mode, history replay).
     Renders into the chat thread exactly like a manual send. */
  async function askCore(question) {
    if (!question || state.busy) return null;
    if (!Onti.ontology.enabled()) return askWithoutOntology(question);
    if (!state.config || !state.config.pack.id) {
      toast('No active ontology pack — contact an administrator.', 'warn'); return null;
    }
    state.busy = true;
    document.getElementById('sendBtn').disabled = true;
    document.getElementById('chatIntro').style.display = 'none';

    const thread = document.getElementById('thread');
    thread.appendChild(addUserCard(question));
    const ui = addAICard();
    thread.appendChild(ui.card);

    const record = {
      question, answer: '', source: state.mode, matched: null,
      latency_ms: 0, graph: null, suggestions: []
    };
    let modeLabel = 'AI';

    try {
      const resp = await fetch('/api/ask-stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question, mode: state.mode,
          history: state.qaLog.slice(-6).map((x) =>
            ({ role: 'user', content: x.question }))
        }),
      });
      if (!resp.ok || !resp.body) throw new Error('Stream failed (' + resp.status + ')');
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      let finished = false;

      const handle = (evt) => {
        if (evt.type === 'meta') {
          modeLabel = evt.source === 'rules' ? 'Rules engine' : 'AI mode';
          ui.badge.className = 'badge ' + (evt.source === 'rules' ?
            (evt.matched ? 'rules' : 'miss') : 'ai');
          ui.badge.textContent = evt.source === 'rules' ?
            (evt.matched ? '✓ Rules engine' : '✕ No rule matched') : '🤖 AI mode';
          if (evt.model) ui.meta.textContent = 'model: ' + evt.model;
        } else if (evt.type === 'token') {
          ui.body.classList.remove('cursor-blink');
          ui.body.innerHTML = renderMd((ui.body._raw = (ui.body._raw || '') + evt.content));
        } else if (evt.type === 'done') {
          finished = true;
          record.source = evt.source;
          record.matched = evt.matched;
          record.latency_ms = evt.latency_ms || 0;
          record.graph = evt.graph;
          record.suggestions = evt.suggestions || [];
          ui.meta.textContent = (evt.source === 'ai' && evt.latency_ms ?
            evt.latency_ms.toFixed(0) + ' ms · ' : '') + (evt.model || '');
          ui.body.innerHTML = renderMd(ui.body._raw || '');
          if (evt.graph && evt.graph.nodes && evt.graph.nodes.length) renderChain(ui.card, evt.graph);
          if (evt.matched === false && evt.suggestions && evt.suggestions.length)
            renderSuggestions(ui.card, evt.suggestions);
        } else if (evt.type === 'error') {
          finished = true;
          ui.badge.className = 'badge miss';
          ui.badge.textContent = '⚠ Unavailable';
          ui.body.classList.remove('cursor-blink');
          ui.body.innerHTML = '<p>' + escHtml(evt.error) + '</p>';
          record.answer = evt.error;
          toast(evt.error, 'err', 5000);
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const parts = buf.split('\n\n');
        buf = parts.pop();
        for (const part of parts) {
          const line = part.split('\n').find((l) => l.startsWith('data: '));
          if (line) handle(JSON.parse(line.slice(6)));
        }
      }
      if (!finished) {
        ui.body.classList.remove('cursor-blink');
        ui.body.innerHTML = renderMd(ui.body._raw || '');
      }
      record.answer = (ui.body._raw || '').replace(/[*_`#]/g, '').slice(0, 1200);
      state.qaLog.push(record);
    } catch (e) {
      ui.body.classList.remove('cursor-blink');
      ui.body.innerHTML = '<p>' + escHtml(e.message) + '</p>';
      toast(e.message, 'err');
    } finally {
      state.busy = false;
      document.getElementById('sendBtn').disabled = false;
      ui.card.scrollIntoView({ behavior: 'smooth', block: 'end' });
      // Show "Pin to dashboard" button once the answer is complete
      if (record.answer && ui.footer) {
        ui.footer.style.display = '';
        ui.pinBtn.onclick = () => {
          state.pinned.push({ question: record.question, answer: record.answer });
          toast('Pinned to dashboard.', 'ok', 1500);
          ui.pinBtn.textContent = '✓ Pinned';
          ui.pinBtn.disabled = true;
        };
      }
    }
    return record;
  }

  /* ------------------------------------------------- with / without ontology */

  const CMP_SOURCE_LABEL = {
    rules: 'rules engine · deterministic',
    ai: 'AI mode · grounded in raw data only',
    none: 'no rule matched',
    error: 'model error',
    unavailable: 'no model configured',
  };

  async function compareCore(question) {
    if (!question || state.busy) return null;
    // With the ontology switched off there is nothing to compare against.
    if (!Onti.ontology.enabled()) return askWithoutOntology(question);
    if (!state.config || !state.config.pack.id) {
      toast('No active ontology pack — contact an administrator.', 'warn'); return null;
    }
    state.busy = true;
    document.getElementById('sendBtn').disabled = true;
    document.getElementById('chatIntro').style.display = 'none';

    const thread = document.getElementById('thread');
    thread.appendChild(addUserCard(question));
    const card = el('div', { class: 'msg-ai' });
    const head = el('div', { class: 'msg-head' });
    head.appendChild(el('span', { class: 'badge neutral' }, '◐ With vs without ontology'));
    head.appendChild(el('span', { class: 'meta' }, 'same question · two answers'));
    const body = el('div', { class: 'msg-body cursor-blink' }, 'Comparing both answers…');
    card.appendChild(head);
    card.appendChild(body);
    thread.appendChild(card);

    try {
      const resp = await fetch('/api/ask/compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, mode: state.mode }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.error || 'Comparison failed (' + resp.status + ')');
      body.classList.remove('cursor-blink');
      body.innerHTML = '';
      renderCompareCard(body, data);
      state.qaLog.push({
        question,
        answer: (data.with_ontology.answer || '').slice(0, 1200),
        source: data.with_ontology.source,
        matched: data.with_ontology.source === 'rules',
        latency_ms: 0,
        graph: data.with_ontology.graph,
        suggestions: [],
      });
    } catch (e) {
      body.classList.remove('cursor-blink');
      body.innerHTML = '<p>' + escHtml(e.message) + '</p>';
      toast(e.message, 'err');
    } finally {
      state.busy = false;
      document.getElementById('sendBtn').disabled = false;
      card.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }
  }

  function renderCompareCard(host, data) {
    const w = data.with_ontology || {};
    const b = data.without_ontology || {};
    const wg = w.grounding || {};

    const grid = el('div', { class: 'cmp-grid' });

    const withCol = el('div', { class: 'cmp-col cmp-with' });
    withCol.appendChild(el('div', { class: 'cmp-head' }, [
      '🟢 ' + escHtml(w.label || 'With ontology'),
      el('span', { class: 'cmp-src' }, '· ' + escHtml(CMP_SOURCE_LABEL[w.source] || w.source || '')),
    ]));
    const withAns = el('div', { class: 'cmp-answer' });
    withAns.innerHTML = renderMd(w.answer || '');
    withCol.appendChild(withAns);
    const withChain = el('div', { class: 'cmp-chain' });
    withCol.appendChild(withChain);

    const withoutCol = el('div', { class: 'cmp-col cmp-without' });
    withoutCol.appendChild(el('div', { class: 'cmp-head' }, [
      '⚪ ' + escHtml(b.label || 'Without ontology'),
      el('span', { class: 'cmp-src' }, '· ' + escHtml(CMP_SOURCE_LABEL[b.source] || b.source || '')),
    ]));
    const withoutAns = el('div', { class: 'cmp-answer' });
    withoutAns.innerHTML = renderMd(b.answer || '');
    withoutCol.appendChild(withoutAns);

    grid.appendChild(withCol);
    grid.appendChild(withoutCol);
    host.appendChild(grid);

    // The grounded side keeps its real impact chain; the baseline has none.
    if (w.graph && state.fullGraph && state.fullGraph.nodes && wg.graph_nodes) {
      const chain = Onti.Graph.fromEvidence(state.fullGraph, w.graph);
      if (chain.nodes.length) {
        const wrap = el('div', { class: 'chain-card' });
        const chead = el('div', { class: 'chain-head' }, [
          '🔗 Impact chain',
          el('span', { class: 'spacer' }),
        ]);
        const svgWrap = el('div', { class: 'chain-svg-wrap' });
        const openBtn = el('button', { class: 'btn btn-ghost btn-xs' }, 'Open full graph ↗');
        openBtn.onclick = openFullGraph;
        chead.appendChild(openBtn);
        wrap.appendChild(chead);
        wrap.appendChild(svgWrap);
        withChain.appendChild(wrap);
        requestAnimationFrame(() =>
          Onti.Graph.render(svgWrap, chain, { compact: true, legend: false }));
      }
    }
  }

  /* ------------------------------------------------------- chain & sugg UI */

  function renderChain(card, evidence) {
    if (!state.fullGraph || !state.fullGraph.nodes) return;
    const data = Onti.Graph.fromEvidence(state.fullGraph, evidence);
    if (!data.nodes.length) return;
    const wrap = el('div', { class: 'chain-card' });
    const head = el('div', { class: 'chain-head' }, [
      '🔗 Impact chain',
      el('span', { class: 'spacer' }),
    ]);
    const svgWrap = el('div', { class: 'chain-svg-wrap' });
    const openBtn = el('button', { class: 'btn btn-ghost btn-xs' }, 'Open full graph ↗');
    openBtn.onclick = openFullGraph;
    head.appendChild(openBtn);
    wrap.appendChild(head);
    wrap.appendChild(svgWrap);
    card.appendChild(wrap);
    requestAnimationFrame(() => Onti.Graph.render(svgWrap, data, { compact: true, legend: false }));
  }

  function renderSuggestions(card, suggestions) {
    const wrap = el('div', { class: 'suggest-row' });
    wrap.appendChild(el('div', { class: 'lab' }, 'Did you mean one of these? Click to ask:'));
    const chips = el('div', { class: 'chips', style: 'justify-content:flex-start' });
    suggestions.forEach((s) => {
      const b = el('button', { class: 'chip' }, s.question);
      b.onclick = () => { input.value = s.question; send(); };
      chips.appendChild(b);
    });
    wrap.appendChild(chips);
    card.appendChild(wrap);
  }

  function openFullGraph() {
    const layer = state.graphLayer || 'visual';
    const graphData = layer === 'class'
      ? (state.classGraph || state.fullGraph)
      : state.fullGraph;
    const m = openModal({
      title: layer === 'class'
        ? 'Class relationship graph — ontology structure'
        : 'Knowledge graph — full ontology',
      body:
        '<div class="graph-toolbar flex wrap" style="gap:8px;align-items:center;margin-bottom:10px">' +
        '<b class="small">🔗 Path</b>' +
        '<select id="gtFrom" class="gt-select"></select><span class="muted">→</span>' +
        '<select id="gtTo" class="gt-select"></select>' +
        '<button class="btn btn-ghost btn-xs" id="gtPathBtn">Find</button>' +
        '<span style="width:1px;height:22px;background:#dde3ec"></span>' +
        '<b class="small">🌊 Impact</b>' +
        '<select id="gtNode" class="gt-select"></select>' +
        '<select id="gtHops" class="gt-select" style="max-width:70px">' +
        [1, 2, 3, 4].map((h) => '<option value="' + h + '"' + (h === 2 ? ' selected' : '') + '>' + h + ' hop</option>').join('') +
        '</select>' +
        '<select id="gtDir" class="gt-select" style="max-width:120px">' +
        '<option value="downstream">downstream</option><option value="upstream">upstream</option></select>' +
        '<button class="btn btn-ghost btn-xs" id="gtImpactBtn">Analyse</button>' +
        '<button class="btn btn-ghost btn-xs" id="gtReset">Reset</button>' +
        '</div>' +
        '<div id="gtResult" class="small muted" style="margin-bottom:8px;display:none"></div>' +
        '<div id="fullGraphHost" style="min-height:420px"></div>',
      wide: true,
      footer: null,
    });
    const host = m.mask.querySelector('#fullGraphHost');
    const result = m.mask.querySelector('#gtResult');
    const nodeOpts = (sel) => (graphData?.nodes || [])
      .map((n) => '<option value="' + escHtml(n.id) + '">' + escHtml(n.label || n.id) + '</option>').join('');
    m.mask.querySelector('#gtFrom').innerHTML = nodeOpts();
    m.mask.querySelector('#gtTo').innerHTML = nodeOpts();
    m.mask.querySelector('#gtNode').innerHTML = nodeOpts();

    const showErr = (r) => {
      result.style.display = '';
      result.innerHTML = '⚠️ ' + escHtml(r.reason || 'Not found.');
    };
    const renderWith = (hl) => {
      Onti.Graph.render(host, { ...graphData, highlight: hl }, {});
    };
    const pathChips = (keys, labels) => keys.map((k) =>
      '<code class="chip">' + escHtml(labels?.[k] || k) + '</code>').join(' <b>→</b> ');

    m.mask.querySelector('#gtPathBtn').onclick = async () => {
      const s = m.mask.querySelector('#gtFrom').value;
      const t = m.mask.querySelector('#gtTo').value;
      if (s === t) { showErr({ reason: 'Pick two different nodes.' }); return; }
      const r = await api.get('/api/graph/path?source=' + encodeURIComponent(s) +
        '&target=' + encodeURIComponent(t));
      if (!r.found) { showErr(r); return; }
      result.style.display = '';
      result.innerHTML = '🔗 Shortest path (' + (r.path.length - 1) + ' step(s)): ' +
        pathChips(r.path);
      renderWith({ nodes: r.path, edge_ids: r.edge_ids });
    };
    m.mask.querySelector('#gtImpactBtn').onclick = async () => {
      const n = m.mask.querySelector('#gtNode').value;
      const hops = m.mask.querySelector('#gtHops').value;
      const dir = m.mask.querySelector('#gtDir').value;
      const r = await api.get('/api/graph/impact?node=' + encodeURIComponent(n) +
        '&hops=' + hops + '&direction=' + dir);
      if (!r.found) { showErr(r); return; }
      result.style.display = '';
      result.innerHTML = '🌊 ' + (dir === 'upstream' ? 'Upstream' : 'Downstream') +
        ' impact of <b>' + escHtml(r.labels?.[n] || n) + '</b> — ' +
        (r.nodes.length - 1) + ' node(s) within ' + r.hops + ' hop(s):<br>' +
        r.levels.map((lvl, i) => '<span class="muted">L' + i + ':</span> ' +
          pathChips(lvl, r.labels)).join('<br>');
      renderWith({ nodes: r.nodes, edge_ids: r.edge_ids });
    };
    m.mask.querySelector('#gtReset').onclick = () => {
      result.style.display = 'none';
      Onti.Graph.render(host, graphData || { nodes: [], links: [] }, {});
    };
    Onti.Graph.render(host, graphData || { nodes: [], links: [] }, {});
  }

  /* --------------------------------------------------------- graph layer */

  function renderDashGraph() {
    const host = document.getElementById('dashGraphHost');
    if (!host) return;
    const layer = state.graphLayer || 'visual';
    const data = layer === 'class'
      ? (state.classGraph || state.fullGraph)
      : state.fullGraph;
    if (!data || !data.nodes) return;
    Onti.Graph.render(host, data, {});
  }

  async function switchGraphLayer(layer) {
    state.graphLayer = layer;
    document.querySelectorAll('#graphLayer button').forEach((b) => {
      const on = b.dataset.l === layer;
      b.setAttribute('aria-pressed', on);
      b.className = on ? 'mode-' + layer : '';
    });
    if (layer === 'class' && !state.classGraph) {
      try {
        const d = await api.get('/api/class-graph');
        state.classGraph = d.graph;
      } catch (e) { toast(e.message, 'err'); return; }
    }
    renderDashGraph();
    toast('Graph layer: ' + (layer === 'class' ? 'Class relationships' : 'Impact graph'), '', 1500);
  }

  /* -------------------------------------------------------------- dashboard */

  let dashRendered = false;
  async function loadDashboard() {
    const graphCard = document.getElementById('dashGraphCard');
    if (!Onti.ontology.enabled()) {
      if (graphCard) graphCard.style.display = 'none';
      document.getElementById('kpiRow').innerHTML =
        '<div style="grid-column:1/-1">' + Onti.ontology.offPanel(
          'Dashboard metrics and the knowledge graph are hidden because ontology mode is off.',
          'No KPI cards, metric formulas or graph are being used. Turn ontology back on to '
          + 'see the active pack\'s metrics and graph. Text answers and text-only reports '
          + 'still work in this mode.') + '</div>';
      document.getElementById('kpiGroups').innerHTML = '';
      document.getElementById('dashBannerBody').innerHTML =
        '<span class="badge warm">Ontology OFF</span>' +
        '<span class="small">Text-only mode — no scenario metrics or knowledge graph.</span>';
      const pill = document.getElementById('dashPackPill');
      pill.innerHTML = '⚠️ Ontology OFF';
      pill.title = 'Ontology mode is off in this browser.';
      Onti.ontology.bindRestore(document.getElementById('view-dashboard'));
      return;
    }
    if (graphCard) graphCard.style.display = '';
    if (!state.fullGraph || !state.fullGraph.nodes) return;
    const host = document.getElementById('dashGraphHost');
    const packPill = document.getElementById('dashPackPill');
    // Scenario banner + computed KPIs come from /api/dashboard (the pack's
    // own data), the graph layer keeps using the knowledge graph.
    try {
      const d = await api.get('/api/dashboard');
      if (d.pack) {
        packPill.innerHTML = '📦 ' + escHtml(d.pack.name);
        const src = (d.sources || []).map((s) =>
          '<span class="badge neutral" title="' + escHtml(s.filename || '') +
          ' · ' + escHtml(s.created_at || '') + '">' +
          (s.kind === 'seed' ? '🌱 ' : '📄 ') + escHtml(s.name) +
          ' · ' + s.row_count + ' rows</span>').join(' ');
        document.getElementById('dashBannerBody').innerHTML =
          '<div style="min-width:230px;flex:1">' +
          '<div style="font-weight:700;font-size:15px">Scenario: ' +
          escHtml(d.pack.name) + '</div>' +
          '<div class="small muted" style="margin-top:2px">' +
          escHtml(d.pack.description || '') + '</div>' +
          (d.auto_report
            ? '<div class="small" style="margin-top:8px"><a class="btn btn-primary btn-xs" ' +
            'target="_blank" href="/api/reports/' + escHtml(d.auto_report.uid) +
            '/view">📄 Full analytics report</a> <span class="muted small">' +
            escHtml(d.auto_report.title) + ' · generated ' +
            escHtml((d.auto_report.created_at || '').slice(0, 10)) + '</span></div>'
            : '') +
          '</div>' +
          '<div style="flex:2;min-width:280px">' +
          '<div class="small muted" style="margin-bottom:4px"><b>Based on data '
          + (d.sources || []).length + ' dataset(s):</b></div>' +
          '<div class="flex wrap" style="gap:6px">' + (src || '<span class="muted small">—</span>') +
          '</div></div>';
        const kpis = d.kpis || [];
        const kpiHint = d.formula_count > 0
          ? 'This ontology declares ' + d.formula_count + ' metric formula(s), but the '
          + 'tables they read are still empty — upload or extend that data.'
          : 'This ontology has no metric formulas yet. Every KPI card comes from a '
          + 'formula (for example VNB = agent count × productivity × margin). Add one '
          + 'in the Ontology workspace, or ask your administrator to define this pack\'s metrics.';
        // Each card explains itself — the number, the formula behind it and the
        // table it reads — so "what is this KPI?" is answerable on the page (the
        // full list lives in the admin's Ontology governance -> Formulas).
        const kpiIntro = kpis.length
          ? '<div class="small muted" style="margin:0 0 10px">Showing ' + kpis.length +
          ' KPI card(s). Each card is one metric formula declared by this ontology (' +
          '<b>' + escHtml(d.pack.name) + '</b>). Open <b>ⓘ What does this mean?</b> on a ' +
          'card to see its formula and the table it reads. See <b>Help → "What do the ' +
          'dashboard KPIs mean?"</b> for a plain-language guide.</div>'
          : '';
        document.getElementById('kpiRow').innerHTML = kpis.length
          ? kpis.slice(0, 12).map((k) => {
            const def = (k.definition || '').trim();
            const src = (k.source_tables || []).filter(Boolean);
            const info = (def || src.length)
              ? '<details style="margin-top:6px"><summary class="small muted" ' +
              'style="cursor:pointer">ⓘ What does this mean?</summary>' +
              '<div class="small muted" style="margin-top:4px">' +
              (src.length ? 'Reads table: <code>' + escHtml(src.join(', ')) + '</code><br>' : '') +
              (def ? 'Formula: <code>' + escHtml(def) + '</code>' : '') +
              '</div></details>'
              : '';
            return '<div class="card"><div class="card-b" style="padding:14px 16px">' +
              '<div class="small muted" style="margin-bottom:4px">' + escHtml(k.label) +
              (k.unit ? ' (' + escHtml(k.unit) + ')' : '') + '</div>' +
              '<div style="font-size:21px;font-weight:700;color:var(--navy)">' +
              escHtml(k.value || '—') + '</div>' + info + '</div></div>';
          }).join('')
          : '<div class="card"><div class="card-b">' +
          '<div style="font-weight:600;color:var(--ink-2)">No KPI cards to show yet</div>' +
          '<div class="small muted mt8">' + escHtml(kpiHint) + '</div></div></div>';
        document.getElementById('kpiGroups').innerHTML = kpiIntro;
      } else {
        packPill.innerHTML = '⚠️ No active ontology pack';
        document.getElementById('dashBannerBody').innerHTML =
          '<span class="muted small">No scenario selected — create or ' +
          'activate an ontology pack first.</span>';
        document.getElementById('kpiRow').innerHTML = '';
        document.getElementById('kpiGroups').innerHTML = '';
      }
    } catch (e) { /* banner is best-effort; graph still renders */ }
    const nodes = state.fullGraph.nodes;
    // Full graph
    if (!dashRendered) {
      renderDashGraph();
      dashRendered = true;
    }
    // Graph layer switcher
    const layerSwitch = document.getElementById('graphLayer');
    if (layerSwitch && !layerSwitch._bound) {
      layerSwitch._bound = true;
      layerSwitch.querySelectorAll('button').forEach((b) => {
        b.addEventListener('click', () => switchGraphLayer(b.dataset.l));
      });
    }
    // Graph legend by group
    const legendColors = { financial: '#ED1B2E', operational: '#0e7c5f', mix: '#b45309', cross: '#7c3aed' };
    const legendEl = document.getElementById('graphLegend');
    const seen = new Set();
    legendEl.innerHTML = nodes.map((n) => n.group).filter((g) => {
      if (seen.has(g)) return false; seen.add(g); return true;
    }).map((g) => '<span style="display:flex;align-items:center;gap:5px">' +
      '<span style="width:12px;height:12px;border-radius:50%;background:' + (legendColors[g] || '#888') + ';display:inline-block"></span>' +
      g.charAt(0).toUpperCase() + g.slice(1) + '</span>').join('') +
      '<span class="small muted" style="margin-left:8px">— click a node to inspect; edges show structural/operational/mix/cross relationships</span>';
    // Pinned insights
    renderPinned();
  }

  function renderPinned() {
    const card = document.getElementById('pinnedCard');
    const body = document.getElementById('pinnedBody');
    if (!state.pinned.length) { card.style.display = 'none'; return; }
    card.style.display = '';
    body.innerHTML = state.pinned.map((p, i) =>
      '<div class="card" style="margin-bottom:10px;border-left:3px solid var(--red)"><div class="card-b">' +
      '<div class="small muted" style="margin-bottom:4px">Q: ' + escHtml(p.question) + '</div>' +
      '<div>' + renderMd(p.answer) + '</div>' +
      '<div style="margin-top:8px"><button class="btn btn-ghost btn-xs" data-unpin="' + i + '">Remove</button></div>' +
      '</div></div>').join('');
    body.querySelectorAll('[data-unpin]').forEach((b) => b.onclick = () => {
      state.pinned.splice(+b.dataset.unpin, 1); renderPinned();
    });
  }

  document.getElementById('clearPinnedBtn').onclick = () => {
    state.pinned = []; renderPinned(); toast('Pinned insights cleared.', '', 1500);
  };

  document.getElementById('dashFullGraphBtn').onclick = openFullGraph;
  document.getElementById('dashGoChatBtn').onclick = () => switchView('chat');

  const dashInput = document.getElementById('dashAskInput');
  document.getElementById('dashSendBtn').onclick = () => {
    const q = dashInput.value.trim();
    if (!q) return;
    input.value = q;
    switchView('chat');
    setTimeout(send, 80);
  };
  dashInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); document.getElementById('dashSendBtn').click(); }
  });

  /* ----------------------------------------------------------- data centre */

  async function loadDataCentre() {
    const body = document.getElementById('dataBody');
    body.innerHTML = '<div class="empty">Loading data tables…</div>';
    try {
      const d = await api.get('/api/library');
      if (!d.pack) {
        body.innerHTML = emptyState('📦', 'No ontology pack is active',
          'Ask an administrator to create or activate a pack.'); return;
      }
      const tables = d.tables || [];
      if (!tables.length) {
        body.innerHTML = emptyState('🗂️', 'No data tables yet',
          'Upload a CSV or XLSX file above to import data into the ontology.');
        return;
      }
      body.innerHTML = '<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(460px,1fr))">' +
        tables.map((t) => {
          const head = '<tr>' + t.columns.map((c) => '<th>' + escHtml(c.label) + '</th>').join('') + '</tr>';
          const rows = (t.preview || []).slice(0, 12).map((r) =>
            '<tr>' + t.columns.map((c) => '<td>' +
              (r[c.key] == null ? '<span class="muted">—</span>' : escHtml(String(r[c.key]))) +
              '</td>').join('') + '</tr>').join('');
          return '<div class="card" id="dcCard' + t.id + '"><div class="card-h"><h3>' + escHtml(t.label) +
            ' <span class="mono" style="font-weight:400;font-size:11px">' + escHtml(t.key) + '</span></h3>' +
            '<span class="spacer"></span><span class="badge neutral">' + t.row_count + ' rows</span>' +
            (t.row_count > 12
              ? '<button class="btn btn-ghost btn-xs" data-full="' + t.id + '">View full dataset</button>'
              : '') + '</div>' +
            '<div class="card-b tight table-wrap"><table class="data">' + head + '<tbody>' + rows +
            '</tbody></table></div>' +
            (t.row_count > 12
              ? '<div class="card-b small muted" style="padding-top:6px">Preview — first ' +
              Math.min(12, (t.preview || []).length) + ' of ' + t.row_count + ' rows.</div>'
              : '') + '</div>';
        }).join('') + '</div>';
      body.querySelectorAll('[data-full]').forEach((b) => {
        b.onclick = () => openFullTable(+b.dataset.full, tables.find((t) => t.id === +b.dataset.full));
      });
    } catch (e) { toast(e.message, 'err'); }
  }

  function openFullTable(tableId, table) {
    const card = document.getElementById('dcCard' + tableId);
    if (!card || !table) return;
    card.style.gridColumn = '1 / -1';
    const PAGE = 50;
    const head = '<tr>' + table.columns.map((c) => '<th>' + escHtml(c.label) + '</th>').join('') + '</tr>';
    const drawRows = (data, offset) => {
      const start = offset + 1;
      const end = offset + data.rows.length;
      const box = card.querySelector('.dcRows');
      box.innerHTML = '<div class="table-wrap"><table class="data">' + head + '<tbody>' +
        data.rows.map((r) => '<tr>' + table.columns.map((c) => '<td>' +
          (r[c.key] == null ? '<span class="muted">—</span>' : escHtml(String(r[c.key]))) +
          '</td>').join('') + '</tr>').join('') + '</tbody></table></div>' +
        (data.total > PAGE
          ? '<div class="flex" style="gap:10px;align-items:center;padding:8px 14px">' +
          '<button class="btn btn-ghost btn-xs dcPrev"' + (offset === 0 ? ' disabled' : '') + '>‹ Prev</button>' +
          '<span class="small muted">Rows ' + start + '–' + end + ' of ' + data.total + '</span>' +
          '<button class="btn btn-ghost btn-xs dcNext"' + (end >= data.total ? ' disabled' : '') + '>Next ›</button></div>'
          : '<div class="small muted" style="padding:8px 14px">All ' + data.total + ' rows shown.</div>');
      const prev = card.querySelector('.dcPrev');
      const next = card.querySelector('.dcNext');
      if (prev) prev.onclick = () => loadRows(offset - PAGE);
      if (next) next.onclick = () => loadRows(offset + PAGE);
    };
    const loadRows = async (offset) => {
      offset = Math.max(0, offset);
      const box = card.querySelector('.dcRows');
      box.innerHTML = '<div class="empty">Loading…</div>';
      try {
        const data = await api.get('/api/data/rows?table_id=' + tableId +
          '&limit=' + PAGE + '&offset=' + offset);
        drawRows(data, offset);
      } catch (e) { toast(e.message, 'err'); loadDataCentre(); }
    };
    card.innerHTML = '<div class="card-h"><h3>' + escHtml(table.label) +
      ' <span class="mono" style="font-weight:400;font-size:11px">' + escHtml(table.key) + '</span></h3>' +
      '<span class="spacer"></span><span class="badge neutral">' + table.row_count + ' rows</span>' +
      '<button class="btn btn-ghost btn-xs dcBack">‹ Back to preview</button></div>' +
      '<div class="card-b tight dcRows"></div>';
    card.querySelector('.dcBack').onclick = () => loadDataCentre();
    loadRows(0);
  }

  /* --------------------------------------------------------------- history */

  function renderHistory() {
    const body = document.getElementById('historyBody');
    if (!state.qaLog.length) {
      body.innerHTML = emptyState('🕘', 'No history yet',
        'Ask a question in the Dashboard or Ask view — your session Q&A will appear here.');
      return;
    }
    body.innerHTML = state.qaLog.map((r, i) => {
      const src = r.source === 'rules' ? (r.matched ? '✓ Rules' : '✕ No rule') : '🤖 AI';
      const cls = r.source === 'rules' ? (r.matched ? 'rules' : 'miss') : 'ai';
      return '<div class="card" style="margin-bottom:12px"><div class="card-h">' +
        '<span class="badge ' + cls + '">' + src + '</span>' +
        '<span class="spacer"></span>' +
        '<button class="btn btn-ghost btn-xs" data-req="' + i + '">Re-ask</button></div>' +
        '<div class="card-b"><div style="font-weight:600;margin-bottom:6px">Q: ' + escHtml(r.question) + '</div>' +
        '<div class="muted">' + escHtml((r.answer || '').slice(0, 400)) + '</div></div></div>';
    }).join('');
    body.querySelectorAll('[data-req]').forEach((b) => b.addEventListener('click', () => {
      const r = state.qaLog[+b.dataset.req];
      input.value = r.question; switchView('chat'); setTimeout(send, 80);
    }));
  }

  document.getElementById('clearHistoryBtn').onclick = () => {
    state.qaLog = []; renderHistory();
    document.getElementById('thread').innerHTML = '';
    document.getElementById('chatIntro').style.display = '';
    toast('History cleared.', '', 1800);
  };

  /* --------------------------------------------------------------- library */

  async function loadLibrary() {
    const body = document.getElementById('libraryBody');
    body.innerHTML = '<div class="empty">Loading library…</div>';
    try {
      const d = await api.get('/api/library');
      if (!d.pack) {
        body.innerHTML = emptyState('📦', 'No ontology pack is active',
          'Ask an administrator to create or activate a pack.'); return;
      }

      const byEntity = {};
      d.attributes.forEach((a) => { (byEntity[a.entity_key] ||= []).push(a); });
      const entCards = (d.entities || []).map((e) => {
        const attrs = (byEntity[e.key] || []).map((a) =>
          '<tr><td><b>' + escHtml(a.label_en || a.key) + '</b> <span class="mono">' +
          escHtml(a.key) + '</span></td><td>' + escHtml(a.data_type) +
          (a.unit ? ' ' + escHtml(a.unit) : '') + '</td><td class="wrap muted">' +
          escHtml(a.description || '') + '</td></tr>').join('');
        return '<div class="card"><div class="card-h"><h3>' + escHtml(e.label_en || e.key) +
          ' <span class="mono" style="font-weight:400;font-size:11px">' + escHtml(e.key) +
          '</span></h3></div><div class="card-b tight"><div class="table-wrap"><table class="data">' +
          '<thead><tr><th style="min-width:180px">Attribute</th><th style="width:110px">Type</th>' +
          '<th>Meaning</th></tr></thead><tbody>' + (attrs || '<tr><td colspan="3" class="muted">No attributes defined.</td></tr>') +
          '</tbody></table></div></div>' +
          (e.description ? '<div class="card-b small muted" style="padding-top:8px">' +
            escHtml(e.description) + '</div>' : '') + '</div>';
      }).join('');

      const rels = (d.relations || []).map((r) =>
        '<tr><td>' + escHtml(r.label_en || r.key) + '</td><td class="mono">' + escHtml(r.source_entity_key) +
        '</td><td>→</td><td class="mono">' + escHtml(r.target_entity_key) + '</td>' +
        '<td class="wrap muted">' + escHtml(r.description || '') + '</td></tr>').join('');

      const formulas = (d.formulas || []).map((f) =>
        '<tr><td><b>' + escHtml(f.label || f.key) + '</b></td><td><code>' +
        escHtml(f.expression) + '</code></td><td class="wrap muted">' +
        escHtml(f.description || '') + '</td></tr>').join('');

      const tables = (d.tables || []).map((t) => {
        const cols = t.columns.map((c) => '<span class="badge neutral" style="margin:2px">' +
          escHtml(c.label) + ' <span class="mono">' + escHtml(c.data_type) + '</span>' +
          (c.derived ? ' ∑' : '') + '</span>').join('');
        const head = '<tr>' + t.columns.map((c) => '<th>' + escHtml(c.label) + '</th>').join('') + '</tr>';
        const rows = t.preview.slice(0, 10).map((r) =>
          '<tr>' + t.columns.map((c) => '<td>' +
            (r[c.key] == null ? '<span class="muted">—</span>' : escHtml(String(r[c.key]))) +
            '</td>').join('') + '</tr>').join('');
        return '<div class="card"><div class="card-h"><h3>' + escHtml(t.label) +
          ' <span class="mono" style="font-weight:400;font-size:11px">' + escHtml(t.key) +
          '</span></h3><span class="spacer"></span>' +
          '<span class="badge neutral">' + t.row_count + ' rows</span></div>' +
          '<div class="card-b small muted" style="padding-bottom:6px">' + (t.dataset ? 'Dataset: ' + escHtml(t.dataset) : 'No active dataset') +
          (t.description ? ' · ' + escHtml(t.description) : '') + '</div>' +
          '<div class="card-b tight table-wrap"><table class="data">' + head + '<tbody>' +
          (rows || '<tr><td class="muted">No rows imported yet.</td></tr>') +
          '</tbody></table></div></div>';
      }).join('');

      body.innerHTML =
        '<div class="card mb16"><div class="card-b"><h3 style="margin-bottom:4px">' + escHtml(d.pack.name) + '</h3>' +
        '<div class="muted small">' + escHtml(d.pack.description || 'Active ontology pack') + '</div></div></div>' +
        '<div class="tabs"><button class="active" data-tab="entities">Entities (' + d.entities.length + ')</button>' +
        '<button data-tab="relations">Relations (' + d.relations.length + ')</button>' +
        '<button data-tab="formulas">Formulas (' + d.formulas.length + ')</button>' +
        '<button data-tab="data">Data tables (' + d.tables.length + ')</button></div>' +
        '<div data-pane="entities" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(420px,1fr))">' + entCards + '</div>' +
        '<div data-pane="relations" style="display:none" class="card"><div class="card-b tight"><div class="table-wrap"><table class="data">' +
        '<thead><tr><th>Relation</th><th>From</th><th></th><th>To</th><th>Meaning</th></tr></thead><tbody>' +
        (rels || '<tr><td colspan="5" class="muted">No relations defined.</td></tr>') + '</tbody></table></div></div></div>' +
        '<div data-pane="formulas" style="display:none" class="card"><div class="card-b tight"><div class="table-wrap"><table class="data">' +
        '<thead><tr><th style="min-width:160px">Metric</th><th>Expression</th><th>Notes</th></tr></thead><tbody>' +
        (formulas || '<tr><td colspan="3" class="muted">No formulas defined.</td></tr>') + '</tbody></table></div></div></div>' +
        '<div data-pane="data" style="display:none;grid-template-columns:repeat(auto-fit,minmax(460px,1fr))" class="grid">' + tables + '</div>';

      body.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => {
        body.querySelectorAll('.tabs button').forEach((x) => x.classList.toggle('active', x === b));
        body.querySelectorAll('[data-pane]').forEach((p) =>
          p.style.display = p.dataset.pane === b.dataset.tab ? '' : 'none');
      }));
    } catch (e) { toast(e.message, 'err'); }
  }

  /* --------------------------------------------------------------- reports */

  document.getElementById('reportBtn').onclick = () => openReportModal('session');
  document.getElementById('reportBtn2').onclick = () => openReportModal('pack');

  document.getElementById('sampleReportBtn').onclick = async () => {
    const btn = document.getElementById('sampleReportBtn');
    btn.disabled = true;
    toast('Building the sample report — answering the recommended questions first…',
      'ok', 3500);
    try {
      await api.post('/api/reports/sample', { title: '', include_ai: false });
      toast('Sample report ready — HTML, PDF and Markdown generated.', 'ok', 4200);
      switchView('reports');
    } catch (e) { toast(e.message, 'err', 5000); }
    finally { btn.disabled = false; }
  };

  function openReportModal(scope) {
    const qaCount = state.qaLog.length;
    const onto = Onti.ontology.enabled();
    const body = document.createElement('div');
    body.innerHTML =
      (onto ? '' :
        '<div class="onto-off-banner" style="margin-bottom:12px;border-radius:8px;border:1px solid #F0D79A">' +
        '<span class="onto-off-dot"></span><span><b>Ontology is OFF.</b> This report will ' +
        'be text-only — the ontology graph, impact chains, charts and analytics sections ' +
        'are removed. It is still generated and downloadable.</span></div>') +
      (scope === 'session' && qaCount === 0
        ? '<div class="firstrun-banner" style="background:var(--blue-soft);border-color:#C3D8EF;color:var(--blue)">' +
        'Your session has no Q&amp;A yet. The report will cover the ontology, KPIs and charts only.</div>'
        : scope === 'session'
          ? '<p class="small muted">The report will include the ' + qaCount +
          ' question(s) from your current session, their answers and impact chains.</p>'
          : '<p class="small muted">Generate an ontology-wide analytics report from the active pack data.</p>') +
      '<label class="fld"><span>Report title</span><input type="text" id="rpTitle" ' +
      'placeholder="e.g. Q3 VNB / APE Analytics Report"></label>' +
      '<label class="fld"><span>Sections</span></label>' +
      '<div class="grid grid-2" style="gap:6px 18px;margin-bottom:14px">' +
      sectionBox('summary', 'Executive summary', true) +
      sectionBox('qa', 'Q&A record', scope === 'session' && qaCount > 0) +
      sectionBox('evidence', 'Ontology evidence & chain', onto) +
      sectionBox('kpi', 'KPI and numbers', onto) +
      sectionBox('charts', 'Chart analysis', onto) +
      sectionBox('ontology', 'Ontology knowledge graph', onto) +
      sectionBox('analytics', 'Performance analytics', onto) +
      sectionBox('appendix', 'Appendix & methodology', true) +
      '</div>' +
      '<label class="switch"><input type="checkbox" id="rpAI"><span class="track"></span>' +
      ' Add AI business interpretation (uses the configured model)</label>';
    const gen = el('button', { class: 'btn btn-primary' }, 'Generate report');
    const cancel = el('button', { class: 'btn btn-ghost' }, 'Cancel');
    const m = openModal({ title: 'Generate analytics report', body, footer: [cancel, gen] });
    cancel.onclick = () => m.close();
    gen.onclick = async () => {
      gen.disabled = true; gen.textContent = 'Rendering PDF…';
      try {
        const sections = [...m.mask.querySelectorAll('input[type=checkbox][data-sec]:checked')]
          .map((x) => x.dataset.sec);
        const { report } = await api.post('/api/reports', {
          title: m.mask.querySelector('#rpTitle').value,
          scope, sections,
          include_ai: m.mask.querySelector('#rpAI').checked,
          use_ontology: onto,
          qa_items: scope === 'session' ? state.qaLog.map((x) => ({
            question: x.question, answer: x.answer, source: x.source,
            latency_ms: x.latency_ms, matched: x.matched, graph: x.graph,
          })) : [],
        });
        m.close();
        toast('Report generated — HTML, PDF and Markdown are ready.', 'ok', 4200);
        switchView('reports');
      } catch (e) { toast(e.message, 'err', 5000); gen.disabled = false; gen.textContent = 'Generate report'; }
    };
  }

  function sectionBox(key, label, checked) {
    return '<label class="switch" style="font-weight:400;color:var(--ink-2)"><input type="checkbox" ' +
      'data-sec="' + key + '"' + (checked ? ' checked' : '') + '><span class="track"></span>' + label + '</label>';
  }

  async function loadReports() {
    const body = document.getElementById('reportsBody');
    body.innerHTML = '<div class="empty">Loading reports…</div>';
    try {
      const { reports } = await api.get('/api/reports');
      if (!reports.length) {
        body.innerHTML = emptyState('📄', 'No reports yet',
          'A full analytics report for the active scenario is generated automatically the first time you open the Dashboard. You can also ask questions and generate a report from the session.');
        return;
      }
      body.className = 'grid';
      body.style.gridTemplateColumns = 'repeat(auto-fill,minmax(340px,1fr))';
      body.innerHTML = '';
      reports.forEach((r) => {
        const card = el('div', { class: 'card' });
        card.innerHTML =
          '<div class="card-b"><h3 style="font-size:14.5px;margin-bottom:2px">' + escHtml(r.title) + '</h3>' +
          '<div class="small muted">' + fmtDate(r.created_at) + ' · scope: ' + escHtml(r.scope) + '</div>' +
          '<div class="flex wrap mt8">' +
          '<a class="btn btn-primary btn-xs" target="_blank" href="/api/reports/' + r.uid + '/view">🌐 View</a>' +
          '<a class="btn btn-navy btn-xs" target="_blank" href="/api/reports/' + r.uid + '/download/html">Preview HTML</a>' +
          '<a class="btn btn-ghost btn-xs" href="/api/reports/' + r.uid + '/download/pdf">PDF</a>' +
          '<a class="btn btn-ghost btn-xs" href="/api/reports/' + r.uid + '/download/md">Markdown</a>' +
          '<span class="spacer"></span>' +
          '<button class="icon-btn danger" title="Delete">🗑</button>' +
          '</div></div>';
        card.querySelector('.icon-btn').onclick = () =>
          confirmAction('Delete report "' + r.title + '" and all its files?', async () => {
            await api.del('/api/reports/' + r.uid);
            toast('Report deleted.', 'ok'); loadReports();
          });
        body.appendChild(card);
      });
    } catch (e) { toast(e.message, 'err'); }
  }

  /* ------------------------------------------------------- perf analytics */

  const ana = { ts: null, month: null, loaded: false };

  const fMoneyM = (x, d = 2) => (x == null || isNaN(x) ? '—' : '$' + (+x).toFixed(d) + 'M');
  const fMoneyK = (x) => (x == null || isNaN(x) ? '—' : '$' + (+x).toFixed(2) + 'k');
  const fPct = (x, d = 1) => (x == null || isNaN(x) ? '—' : (+x * 100).toFixed(d) + '%');
  const fSPct = (x, d = 0) => {
    if (x == null || isNaN(x)) return '—';
    const v = (+x * 100).toFixed(d);
    return (v > 0 ? '+' : '') + v + '%';
  };
  const fS3 = (x) => {
    if (x == null || isNaN(x)) return '—';
    const v = (+x).toFixed(3);
    return (x > 0 ? '+' : '') + v;
  };

  /* Generic monthly trend — used when the pack is not the VNB/APE schema but
     does declare its own monthly table (a lapse experience study, say). */

  function genericFmt(v, agg) {
    if (v == null || isNaN(v)) return '—';
    if (agg === 'rate') return (+v * 100).toFixed(2) + '%';
    return agg === 'mean'
      ? (+v).toFixed(3)
      : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 });
  }

  function genericChartSvg(g, measure) {
    const rows = g.series;
    const m = (g.measures || []).find((x) => x.key === measure) || { agg: 'sum' };
    const vals = rows.map((r) => +r[measure] || 0);
    const W = 760, H = 240, L = 60, R = 18, T = 18, B = 34;
    const vmax = Math.max(...vals, 0), vmin = Math.min(...vals, 0);
    const span = (vmax - vmin) || 1;
    const x = (i) => L + (rows.length === 1 ? (W - L - R) / 2 : i * (W - L - R) / (rows.length - 1));
    const y = (v) => T + (vmax - v) * (H - T - B) / span;
    let grid = '';
    for (let k = 0; k <= 4; k++) {
      const gv = vmin + span * k / 4, gy = y(gv);
      grid += '<line x1="' + L + '" y1="' + gy + '" x2="' + (W - R) + '" y2="' + gy + '" stroke="#eef0f4"/>' +
        '<text x="' + (L - 7) + '" y="' + (gy + 3.5) + '" text-anchor="end" font-size="10" fill="#8a93a2">' +
        escHtml(genericFmt(gv, m.agg)) + '</text>';
    }
    const pts = rows.map((r, i) => x(i).toFixed(1) + ',' + y(+r[measure] || 0).toFixed(1)).join(' ');
    const dots = rows.map((r, i) =>
      '<circle cx="' + x(i) + '" cy="' + y(+r[measure] || 0) + '" r="3.4" fill="#0E2A47">' +
      '<title>' + escHtml(r.label + ' · ' + genericFmt(r[measure], m.agg)) + '</title></circle>').join('');
    const step = Math.ceil(rows.length / 12);
    const xlabels = rows.map((r, i) => (i % step)
      ? '' : '<text x="' + x(i) + '" y="' + (H - 12) + '" text-anchor="middle" font-size="10" fill="#8a93a2">' +
      escHtml(r.label) + '</text>').join('');
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" height="' + H + '">' + grid +
      '<polyline fill="none" stroke="#0E2A47" stroke-width="2.4" points="' + pts + '"/>' + dots + xlabels + '</svg>';
  }

  function genericTableHtml(g, measure) {
    const m = (g.measures || []).find((x) => x.key === measure) || { key: measure, agg: 'sum' };
    return '<div class="table-wrap"><table class="data"><thead><tr><th>Month</th><th>' +
      escHtml(String(m.key || '').replace(/_/g, ' ')) + '</th><th>Records</th></tr></thead><tbody>' +
      g.series.map((r) => '<tr><td>' + escHtml(r.label) + '</td><td>' +
        escHtml(genericFmt(r[measure], m.agg)) + '</td><td>' + (r.records || 0) + '</td></tr>').join('') +
      '</tbody></table></div>';
  }

  function genericCardHtml(g) {
    const measure = ana.genericMeasure || g.default_measure;
    const opts = (g.measures || []).map((m) =>
      '<option value="' + escHtml(m.key) + '"' + (m.key === measure ? ' selected' : '') + '>' +
      escHtml(m.agg === 'rate' ? m.label : m.label + ' (' + m.agg + ')') + '</option>').join('');
    return '<div class="card"><div class="card-h">' +
      '<h3>Monthly trend from this pack</h3><span class="spacer"></span>' +
      '<select id="genMeasure" class="input" style="max-width:260px;font-size:12px">' + opts + '</select>' +
      '</div><div class="card-b"><div id="genChart">' + genericChartSvg(g, measure) + '</div>' +
      '<div id="genTable" style="margin-top:10px">' + genericTableHtml(g, measure) + '</div></div></div>';
  }

  function genericNoteHtml() {
    return '<div class="card" style="margin-top:12px"><div class="card-b">' +
      '<p class="small muted" style="margin:0">This pack does not use the built-in VNB / APE monthly schema, ' +
      'so the driver-attribution, scenario and agent views stay hidden. The trend above is generated ' +
      'automatically from the monthly table declared by the active pack and the data it holds. To unlock the ' +
      'full VNB / APE views, switch the <b>Scenario</b> selector to a pack that carries that schema.</p>' +
      '</div></div>';
  }

  async function loadAnalytics() {
    const body = document.getElementById('analyticsBody');
    if (!Onti.ontology.enabled()) {
      body.innerHTML = Onti.ontology.offPanel(
        'Performance analytics is hidden because ontology mode is off.',
        'Monthly trends, driver attribution and portfolio charts all read the active '
        + 'ontology\'s data. Turn ontology back on to render them; in this mode only '
        + 'text answers and text-only reports are produced.');
      Onti.ontology.bindRestore(body);
      return;
    }
    if (ana.loaded) return;
    body.innerHTML = '<div class="empty">Loading analytics…</div>';
    try {
      const ts = await api.get('/api/analytics/timeseries');
      if (!ts.company || !ts.company.length) {
        if (ts.generic && ts.generic.series && ts.generic.series.length) {
          ana.ts = ts; ana.loaded = true; ana.generic = ts.generic;
          body.innerHTML = genericCardHtml(ts.generic) + genericNoteHtml();
          const sel = document.getElementById('genMeasure');
          if (sel) sel.onchange = () => {
            ana.genericMeasure = sel.value;
            document.getElementById('genChart').innerHTML = genericChartSvg(ts.generic, sel.value);
            document.getElementById('genTable').innerHTML = genericTableHtml(ts.generic, sel.value);
          };
          return;
        }
        const req = ts.requirements || {};
        const need = (req.required_tables || ['vnb_monthly', 'channel_monthly']).join(', ');
        const have = (req.present_tables || []).join(', ') || 'no data tables';
        const miss = (req.missing_tables || []).join(', ') || '—';
        const keys = req.key_columns || {};
        const purpose = req.table_purpose || {};
        const schemaRows = (req.required_tables || []).map((t) =>
          '<tr><td><code>' + escHtml(t) + '</code></td><td>' + escHtml(keys[t] || 'month') +
          '</td><td>' + escHtml(purpose[t] || '') + '</td></tr>').join('');
        body.innerHTML = emptyState('📈', 'Monthly analytics not available',
          'This is not an error — the active pack does not model monthly VNB / APE, which is all this page reads.') +
          '<div class="card" style="margin-top:12px"><div class="card-b">' +
          '<h3 style="font-size:14px">Why this page is empty</h3>' +
          '<p class="small muted" style="margin:4px 0 0">Performance analytics — the trend chart, the ' +
          'month-on-month driver attribution, the scenario view and the agent table — runs on a fixed ' +
          'monthly schema: tables named <code>' + escHtml(need) + '</code>, each carrying a ' +
          '<code>month</code> column. This pack provides: <b>' + escHtml(have) + '</b>. ' +
          'Still missing: <b>' + escHtml(miss) + '</b>.</p>' +
          '<h3 style="font-size:14px;margin-top:16px">How to make it light up</h3>' +
          '<ol class="small muted" style="margin:6px 0 0 18px;padding:0">' +
          '<li><b>Switch scenario</b> — choose a pack that carries the monthly schema from the ' +
          '<b>Scenario</b> selector at the top of the page (the built-in VNB / APE sample pack is one). ' +
          'This page then fills in automatically.</li>' +
          '<li><b>Upload the tables</b> — open <b>Data centre</b> and upload a workbook containing the ' +
          'tables listed below; sheets are matched to tables by name. The key columns are what let the ' +
          'views group by month, channel and agent.</li>' +
          '<li><b>Your data may not be VNB / APE at all</b> — then this view does not apply. Read the ' +
          'metrics declared by the active pack on the <b>Dashboard</b>, and explore how they connect in ' +
          'the <b>Knowledge graph</b> / <b>Graph studio</b>.</li>' +
          '</ol>' +
          '<div class="table-wrap" style="margin-top:12px"><table class="data"><thead><tr>' +
          '<th>Required table</th><th>Key columns</th><th>What it powers</th></tr></thead><tbody>' +
          schemaRows + '</tbody></table></div>' +
          '</div></div>';
        return;
      }
      ana.ts = ts;
      ana.month = ts.months[ts.months.length - 1];
      ana.loaded = true;
      document.getElementById('anaMonthPill').textContent =
        'Latest month: ' + ts.company[ts.company.length - 1].label;
      body.innerHTML =
        trendCardHtml() + attributionCardHtml(ts) +
        scenarioCardHtml() + agentCardHtml();
      bindAnalyticsControls(ts);
      const [att, ag] = await Promise.all([
        api.get('/api/analytics/attribution?month=' + encodeURIComponent(ana.month)),
        api.get('/api/analytics/agent-stats'),
      ]);
      renderAttribution(att.attribution);
      renderAgents(ag.agents);
      runScenario();
    } catch (e) {
      body.innerHTML = '<div class="empty">' + escHtml(e.message) + '</div>';
    }
  }

  function trendCardHtml() {
    const rows = ana.ts.company;
    const W = 760, H = 268, L = 50, R = 18, T = 18, B = 30;
    const ymax = Math.max(...rows.map((r) => r.total_vnb)) * 1.12;
    const x = (i) => L + (rows.length === 1 ? (W - L - R) / 2 : i * (W - L - R) / (rows.length - 1));
    const y = (v) => T + (ymax - v) * (H - T - B) / ymax;
    const line = (key, color) => {
      const pts = rows.map((r, i) => x(i).toFixed(1) + ',' + y(r[key]).toFixed(1)).join(' ');
      const dots = rows.map((r, i) =>
        '<circle cx="' + x(i) + '" cy="' + y(r[key]) + '" r="3.4" fill="' + color + '">' +
        '<title>' + escHtml(r.label) + ' · ' + fMoneyM(r[key]) + '</title></circle>').join('');
      return '<polyline fill="none" stroke="' + color + '" stroke-width="2.4" points="' + pts + '"/>' + dots;
    };
    let grid = '';
    for (let g = 0; g <= 4; g++) {
      const gv = ymax * g / 4, gy = y(gv);
      grid += '<line x1="' + L + '" y1="' + gy + '" x2="' + (W - R) + '" y2="' + gy +
        '" stroke="#eef0f4"/><text x="' + (L - 7) + '" y="' + (gy + 3.5) +
        '" text-anchor="end" font-size="10" fill="#8a93a5">$' + gv.toFixed(2) + 'M</text>';
    }
    const xlabels = rows.map((r, i) =>
      '<text x="' + x(i) + '" y="' + (H - 9) + '" text-anchor="middle" font-size="10.5" fill="#5a6478">' +
      escHtml(r.label.split(' ')[0]) + '</text>').join('');
    const latest = rows[rows.length - 1];
    const tiles = [
      ['Total VNB ' + latest.label, fMoneyM(latest.total_vnb), fSPct(latest.mom_vnb) + ' MoM', '#ED1B2E'],
      ['Agency VNB', fMoneyM(latest.agency_vnb), fPct(latest.agency_share, 0) + ' of total', '#0e7c5f'],
      ['Total APE', fMoneyM(latest.total_ape), fMoneyM(latest.agency_ape) + ' Agency APE', '#1d4280'],
    ];
    return '<div class="card" style="margin-bottom:18px"><div class="card-h"><h3>📈 Monthly VNB trend</h3>' +
      '<span class="spacer"></span><span style="font-size:12px;color:#5a6478">' +
      '<span style="color:#ED1B2E">●</span> Total VNB &nbsp; <span style="color:#0e7c5f">●</span> Agency VNB</span></div>' +
      '<div class="card-b">' +
      '<div class="grid grid-3" style="margin-bottom:14px">' + tiles.map((t) =>
        '<div style="border-left:3px solid ' + t[3] + ';padding:8px 12px;background:#f7f8fb;border-radius:6px">' +
        '<div class="small muted">' + escHtml(t[0]) + '</div>' +
        '<div style="font-size:20px;font-weight:700;color:var(--navy)">' + escHtml(t[1]) + '</div>' +
        '<div class="small" style="color:' + t[3] + ';font-weight:600">' + escHtml(t[2]) + '</div></div>').join('') +
      '</div>' +
      '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto;display:block">' +
      grid + xlabels + line('agency_vnb', '#0e7c5f') + line('total_vnb', '#ED1B2E') + '</svg>' +
      (latest.narrative ? '<p class="small muted" style="margin-top:10px">📝 ' +
        escHtml(latest.narrative) + '</p>' : '') +
      '</div></div>';
  }

  function attributionCardHtml(ts) {
    const opts = ts.months.slice(1).map((m, i) => {
      const label = ts.company[i + 1].label;
      return '<option value="' + escHtml(m) + '"' + (m === ana.month ? ' selected' : '') +
        '>' + escHtml(label) + '</option>';
    }).join('');
    return '<div class="card" style="margin-bottom:18px"><div class="card-h"><h3>🔍 MoM driver attribution</h3>' +
      '<span class="spacer"></span><label class="small muted" style="font-weight:600">Month&nbsp;' +
      '<select id="attrMonth" class="inp" style="padding:4px 8px">' + opts + '</select></label></div>' +
      '<div class="card-b" id="attrResult"><div class="empty">Loading attribution…</div></div></div>';
  }

  function factorBar(label, eff, maxAbs, color) {
    const w = maxAbs ? Math.abs(eff) / maxAbs * 50 : 0;
    const pos = eff >= 0;
    const bar = '<div style="position:absolute;top:1px;height:12px;width:' + w.toFixed(1) +
      '%;left:50%;background:' + color + ';opacity:.85;border-radius:3px;transform:translateX(' +
      (pos ? '0' : '-100%') + ')"></div>';
    return '<div style="display:grid;grid-template-columns:175px 1fr 64px;gap:10px;align-items:center;margin:7px 0">' +
      '<span class="small">' + escHtml(label) + '</span>' +
      '<div style="position:relative;height:14px;background:#f1f2f6;border-radius:4px">' +
      '<div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:#c3c9d6"></div>' +
      bar + '</div>' +
      '<span class="mono small" style="text-align:right;color:' + (pos ? '#0e7c5f' : '#c0392b') +
      ';font-weight:600">' + fS3(eff) + '</span></div>';
  }

  function renderAttribution(a) {
    const host = document.getElementById('attrResult');
    if (!host) return;
    if (!a) { host.innerHTML = '<p class="muted small">No attribution available for this month.</p>'; return; }
    const f = a.factors;
    const maxAbs = Math.max(Math.abs(f.agent_effect), Math.abs(f.productivity_effect), Math.abs(f.mix_effect)) || 1;
    const rows = (a.channels || []).map((c) =>
      '<tr><td><b>' + escHtml(c.channel_name) + '</b></td>' +
      '<td class="mono">' + fS3(c.delta_vnb) + '</td>' +
      '<td class="mono">' + fS3(c.agent_effect) + '</td>' +
      '<td class="mono">' + fS3(c.productivity_effect) + '</td>' +
      '<td class="mono">' + fS3(c.mix_effect) + '</td></tr>').join('');
    host.innerHTML =
      '<p style="margin-bottom:10px">Total VNB <b>' + fMoneyM(a.prev_total_vnb) + '</b> → <b>' +
      fMoneyM(a.cur_total_vnb) + '</b> in ' + escHtml(a.label) +
      ' &nbsp; <span class="badge ' + (a.delta_total_vnb >= 0 ? 'rules' : 'miss') + '">' +
      fSPct(a.mom_pct, 1) + ' MoM · ' + fS3(a.delta_total_vnb) + ' $M</span></p>' +
      '<div style="max-width:640px;margin-bottom:14px">' +
      factorBar('Active-agent effect', f.agent_effect, maxAbs, '#0e7c5f') +
      factorBar('Productivity effect', f.productivity_effect, maxAbs, '#2563eb') +
      factorBar('Mix / margin effect', f.mix_effect, maxAbs, '#b45309') +
      '<div style="display:grid;grid-template-columns:175px 1fr 64px;gap:10px;margin-top:4px;padding-top:8px;border-top:1px solid #e6e9f0">' +
      '<span class="small"><b>Total ΔVNB</b></span><span></span>' +
      '<span class="mono small" style="text-align:right;font-weight:700">' + fS3(f.delta_vnb) + '</span></div></div>' +
      '<div class="table-wrap"><table class="data"><thead><tr><th>Channel</th><th>ΔVNB ($M)</th>' +
      '<th>Agents</th><th>Productivity</th><th>Mix/margin</th></tr></thead><tbody>' + rows +
      '</tbody></table></div>' +
      (a.narrative ? '<p class="small muted" style="margin-top:10px">📝 ' +
        escHtml(a.narrative) + '</p>' : '');
  }

  function scenarioCardHtml() {
    return '<div class="card" style="margin-bottom:18px"><div class="card-h"><h3>🎯 Target back-solve (scenario)</h3></div>' +
      '<div class="card-b">' +
      '<div class="flex wrap" style="gap:14px;align-items:flex-end;margin-bottom:6px">' +
      '<label class="fld" style="margin:0"><span class="small muted">Target (× current VNB)</span>' +
      '<select id="scMult" class="inp" style="padding:6px 10px">' +
      [1.25, 1.5, 2, 2.5, 3].map((m) =>
        '<option value="' + m + '"' + (m === 2 ? ' selected' : '') + '>' + m + '×</option>').join('') +
      '</select></label>' +
      '<label class="fld" style="margin:0"><span class="small muted">Planned active agents (optional)</span>' +
      '<input id="scPlanned" type="number" min="1" class="inp" style="padding:6px 10px;width:170px" placeholder="leave blank = current"></label>' +
      '<button class="btn btn-primary btn-sm" id="scRun">Back-solve</button>' +
      '<span class="small muted">Holds non-Agency channels and current product mix flat.</span></div>' +
      '<div id="scenarioResult" style="margin-top:14px"></div></div></div>';
  }

  function leverTile(title, sub, color, lines) {
    return '<div style="border:1px solid #e6e9f0;border-top:3px solid ' + color +
      ';border-radius:8px;padding:12px 14px;background:#fcfdff">' +
      '<div class="small" style="color:' + color + ';font-weight:700">' + escHtml(title) + '</div>' +
      '<div class="small muted" style="margin-bottom:8px">' + escHtml(sub) + '</div>' +
      lines.map((l) => '<div style="display:flex;justify-content:space-between;gap:10px;margin:4px 0">' +
        '<span class="small muted">' + escHtml(l[0]) + '</span><b class="small">' + escHtml(l[1]) + '</b></div>').join('') +
      '</div>';
  }

  function renderScenario(s) {
    const host = document.getElementById('scenarioResult');
    if (!host) return;
    if (!s) { host.innerHTML = '<p class="muted small">Scenario data not available.</p>'; return; }
    const lh = s.lever_headcount, lp = s.lever_productivity;
    host.innerHTML =
      '<div class="grid grid-3" style="margin-bottom:14px">' +
      [['Current total VNB', fMoneyM(s.current_total_vnb), '#5a6478'],
      ['Target total VNB', fMoneyM(s.target_total_vnb), '#ED1B2E'],
      ['Gap to close', '+' + fMoneyM(s.gap_total_vnb), '#0e7c5f']].map((t) =>
        '<div style="padding:10px 14px;background:#f7f8fb;border-radius:6px"><div class="small muted">' +
        escHtml(t[0]) + '</div><div style="font-size:19px;font-weight:700;color:' + t[2] + '">' +
        escHtml(t[1]) + '</div></div>').join('') + '</div>' +
      '<p style="margin-bottom:12px">Non-Agency held flat at <b>' + fMoneyM(s.non_agency_vnb_held) +
      '</b> → Agency VNB must rise <b>' + fMoneyM(s.current_agency_vnb) + '</b> → <b style="color:#0e7c5f">' +
      fMoneyM(s.required_agency_vnb) + '</b> (' + fSPct(s.agency_vnb_uplift) + '), i.e. Agency APE ' +
      fMoneyM(s.current_agency_ape) + ' → <b>' + fMoneyM(s.required_agency_ape) +
      '</b> at the current ' + fPct(s.agency_margin) + ' weighted margin.</p>' +
      '<div class="grid grid-2" style="gap:12px">' +
      leverTile('👥 Headcount lever', 'Productivity held at ' + fMoneyK(lh.current_prod), '#0e7c5f', [
        ['Active agents', lh.current_active + ' → ' + Math.round(lh.required_active)],
        ['Headcount uplift', fSPct(lh.active_uplift)],
      ]) +
      leverTile('⚡ Productivity lever', 'Headcount held at ' + lp.planned_active + ' active', '#2563eb', [
        ['Avg productivity', fMoneyK(lp.current_prod) + ' → ' + fMoneyK(lp.required_prod)],
        ['Productivity uplift', fSPct(lp.prod_uplift)],
      ]) + '</div>' +
      '<p class="small muted" style="margin-top:10px">A third lever is shifting product mix to lift the ' +
      'weighted VNB margin; the decomposition and identity are documented in KPI Linkage.</p>';
  }

  async function runScenario() {
    const mult = parseFloat(document.getElementById('scMult').value) || 2;
    const plannedRaw = document.getElementById('scPlanned').value.trim();
    const planned = plannedRaw ? parseInt(plannedRaw, 10) : null;
    const host = document.getElementById('scenarioResult');
    if (host) host.innerHTML = '<p class="small muted">Calculating…</p>';
    try {
      let url = '/api/analytics/scenario?multiplier=' + mult;
      if (planned) url += '&planned_active=' + planned;
      const res = await api.get(url);
      renderScenario(res.scenario);
    } catch (e) {
      if (host) host.innerHTML = '<p class="small" style="color:#c0392b">' + escHtml(e.message) + '</p>';
    }
  }

  function distBar(counts, colors) {
    const total = Object.values(counts).reduce((a, b) => a + b, 0) || 1;
    const segs = Object.entries(counts).map(([k, v]) =>
      '<div title="' + escHtml(k) + ': ' + v + '" style="width:' + (v / total * 100).toFixed(1) +
      '%;background:' + (colors[k] || '#9aa2b1') + '"></div>').join('');
    const legend = Object.entries(counts).map(([k, v]) =>
      '<span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px">' +
      '<span style="width:10px;height:10px;border-radius:2px;background:' + (colors[k] || '#9aa2b1') +
      ';display:inline-block"></span>' + escHtml(k) + ' <b>' + v + '</b></span>').join('');
    return '<div style="display:flex;height:16px;border-radius:4px;overflow:hidden;margin:6px 0 8px">' +
      segs + '</div><div class="small muted" style="margin-bottom:14px">' + legend + '</div>';
  }

  function hbarRows(buckets, color) {
    const total = Object.values(buckets).reduce((a, b) => a + b, 0) || 1;
    return Object.entries(buckets).map(([k, v]) =>
      '<div style="display:grid;grid-template-columns:90px 1fr 40px;gap:10px;align-items:center;margin:5px 0">' +
      '<span class="small muted">' + escHtml(k) + '</span>' +
      '<div style="background:#f1f2f6;border-radius:4px;height:12px">' +
      '<div style="height:12px;border-radius:4px;background:' + color + ';width:' +
      (v / total * 100).toFixed(1) + '%"></div></div>' +
      '<span class="small mono" style="text-align:right">' + v + '</span></div>').join('');
  }

  function agentCardHtml() {
    return '<div class="card"><div class="card-h"><h3>🧑‍💼 Agent salesforce health</h3>' +
      '<span class="spacer"></span><span class="small muted" id="agMonth"></span></div>' +
      '<div class="card-b" id="agentResult"><div class="empty">Loading agent analytics…</div></div></div>';
  }

  function renderAgents(ag) {
    const host = document.getElementById('agentResult');
    if (!host) return;
    if (!ag) { host.innerHTML = '<p class="muted small">Agent-level analytics not available.</p>'; return; }
    document.getElementById('agMonth').textContent = ag.label;
    const high = ag.churn_risk.High || 0;
    const tiles = [
      ['Active agents', ag.total_active + ' / ' + ag.total_registered, fPct(ag.active_active || ag.active_ratio, 0) + ' active rate', '#0e7c5f'],
      ['High churn risk', high, fPct(high / (ag.total_active || 1), 0) + ' of active', '#c0392b'],
      ['Registered in master', ag.master_count, 'agent roster', '#1d4280'],
    ];
    const chRows = (ag.by_channel || []).map((c) =>
      '<tr><td class="mono"><b>' + escHtml(c.channel_id) + '</b></td><td>' + c.registered +
      '</td><td>' + c.active + '</td><td>' + fPct(c.active_ratio, 0) +
      '</td><td>' + fMoneyK(c.avg_prod) + '</td></tr>').join('');
    const topCerts = Object.entries(ag.certification || {}).slice(0, 5)
      .map(([k, v]) => '<span class="badge neutral" style="margin:2px">' + escHtml(k) + ' · ' + v + '</span>').join('');
    host.innerHTML =
      '<div class="grid grid-3" style="margin-bottom:16px">' + tiles.map((t) =>
        '<div style="border-left:3px solid ' + t[3] + ';padding:8px 12px;background:#f7f8fb;border-radius:6px">' +
        '<div class="small muted">' + escHtml(t[0]) + '</div>' +
        '<div style="font-size:19px;font-weight:700;color:var(--navy)">' + escHtml(String(t[1])) + '</div>' +
        '<div class="small" style="color:' + t[3] + ';font-weight:600">' + escHtml(t[2]) + '</div></div>').join('') + '</div>' +
      '<div class="grid grid-2" style="gap:18px;align-items:start"><div>' +
      '<div class="small" style="font-weight:600;margin-bottom:2px">Churn risk (active agents)</div>' +
      distBar(ag.churn_risk, { Low: '#0e7c5f', Medium: '#d9941f', High: '#c0392b' }) +
      '<div class="small" style="font-weight:600;margin-bottom:2px">Tenure mix (active)</div>' +
      hbarRows(ag.tenure_buckets, '#1d4280') +
      (topCerts ? '<div class="small" style="font-weight:600;margin:10px 0 4px">Top certifications</div>' + topCerts : '') +
      '</div><div class="table-wrap"><table class="data"><thead><tr><th>Channel</th><th>Registered</th>' +
      '<th>Active</th><th>Active %</th><th>Avg prod.</th></tr></thead><tbody>' + chRows +
      '</tbody></table></div></div>';
  }

  function bindAnalyticsControls() {
    document.getElementById('attrMonth').addEventListener('change', async (e) => {
      ana.month = e.target.value;
      const host = document.getElementById('attrResult');
      host.innerHTML = '<div class="empty">Loading…</div>';
      try {
        const res = await api.get('/api/analytics/attribution?month=' + encodeURIComponent(ana.month));
        renderAttribution(res.attribution);
      } catch (err) { host.innerHTML = '<p class="small" style="color:#c0392b">' + escHtml(err.message) + '</p>'; }
    });
    document.getElementById('scRun').addEventListener('click', runScenario);
  }

  // A mode change invalidates cached ontology views, so re-render whatever
  // the user is currently looking at.
  document.addEventListener('onti:ontology', () => {
    dashRendered = false;
    ana.loaded = false;
    const host = document.getElementById('analyticsBody');
    if (host) host.innerHTML = '';
    const active = document.querySelector('.view.active');
    switchView(state.view || (active ? active.id.replace('view-', '') : 'dashboard'));
  });

  init();
  Onti.helpMount('app');
})();
