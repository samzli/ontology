/* Onti — shared front-end utilities: API calls, toast, modal, markdown. */
(function () {
  'use strict';

  /* ------------------------------------------------------------- fetch API */

  class ApiError extends Error {
    constructor(message, status, extra) {
      super(message);
      this.status = status;
      this.extra = extra || {};
    }
  }

  async function request(path, options = {}) {
    const opts = Object.assign({ credentials: 'same-origin' }, options);
    opts.headers = Object.assign({}, options.headers || {});
    if (options.body && !(options.body instanceof FormData)) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(options.body);
    }
    let resp;
    try {
      resp = await fetch(path, opts);
    } catch (e) {
      throw new ApiError('Network error — the server may be unXreachable.', 0);
    }
    if (resp.status === 401) {
      // The login/register/logout endpoints themselves use 401 for
      // *authentication failures* (wrong password, unknown user, ...) —
      // NOT session expiry. Those 401s must fall through to the !resp.ok
      // branch below so the server's own error message ("Invalid username
      // or password.") reaches the user, instead of being masked as
      // "Please sign in again." which was very confusing.
      const authEndpoints = new Set([
        '/api/auth/login', '/api/auth/register', '/api/auth/logout',
      ]);
      if (authEndpoints.has(path)) {
        // fall through — let the !resp.ok handler produce the real message
      } else {
        // For every other 401, treat it as a session expiry and bounce
        // the user back to the appropriate sign-in page. Never redirect
        // while already on a sign-in page — that caused an infinite
        // reload loop on /admin/login (the page probes /api/auth/me,
        // which answers 401 when signed out).
        const pagePath = location.pathname;
        const onLoginPage = pagePath === '/login' || pagePath === '/admin/login';
        if (!onLoginPage) {
          const onAdmin = pagePath.startsWith('/admin');
          location.href = onAdmin ? '/admin/login?expired=1' : '/login';
        }
        throw new ApiError('Please sign in again.', 401);
      }
    }
    let data = null;
    const ct = resp.headers.get('content-type') || '';
    if (ct.includes('application/json')) data = await resp.json().catch(() => null);
    if (!resp.ok) {
      const msg = (data && (data.error || data.detail)) ||
        (resp.status === 403 ? 'You do not have permission for this action.' :
          'Request failed (' + resp.status + ').');
      throw new ApiError(msg, resp.status, data || {});
    }
    return data;
  }

  const api = {
    get: (p) => request(p),
    post: (p, body) => request(p, { method: 'POST', body }),
    put: (p, body) => request(p, { method: 'PUT', body }),
    del: (p) => request(p, { method: 'DELETE' }),
    upload: (p, formData, method = 'POST') =>
      request(p, { method, body: formData }),
  };

  /* ----------------------------------------------------------------- toast */

  function ensureToastWrap() {
    let w = document.querySelector('.toast-wrap');
    if (!w) { w = document.createElement('div'); w.className = 'toast-wrap'; document.body.appendChild(w); }
    return w;
  }

  function toast(message, type = '', ms = 3600) {
    const w = ensureToastWrap();
    const el = document.createElement('div');
    el.className = 'toast ' + type;
    const icon = { ok: '✓', err: '✕', warn: '!' }[type] || '•';
    el.innerHTML = '<span style="font-weight:700">' + icon + '</span><span></span>';
    el.querySelector('span:last-child').textContent = message;
    w.appendChild(el);
    setTimeout(() => {
      el.style.opacity = '0'; el.style.transition = 'opacity .25s';
      setTimeout(() => el.remove(), 260);
    }, ms);
    return el;
  }

  /* ----------------------------------------------------------------- modal */

  function openModal({ title, body, footer, wide = false, xwide = false, onClose }) {
    const mask = document.createElement('div');
    mask.className = 'modal-mask open';
    const cls = xwide ? 'xwide' : wide ? 'wide' : '';
    mask.innerHTML =
      '<div class="modal ' + cls + '" role="dialog" aria-modal="true">' +
      '<div class="modal-h"><h3></h3><button class="x" aria-label="Close">✕</button></div>' +
      '<div class="modal-b"></div>' +
      (footer !== null ? '<div class="modal-f"></div>' : '') +
      '</div>';
    mask.querySelector('h3').textContent = title || '';
    const bodyEl = mask.querySelector('.modal-b');
    if (typeof body === 'string') bodyEl.innerHTML = body;
    else if (body instanceof Node) bodyEl.appendChild(body);
    const footEl = mask.querySelector('.modal-f');
    if (footer !== null && footEl) {
      if (typeof footer === 'string') footEl.innerHTML = footer;
      else if (footer instanceof Node) footEl.appendChild(footer);
      else if (Array.isArray(footer)) footer.forEach((n) => n && footEl.appendChild(n));
    }
    const close = () => {
      mask.classList.remove('open'); setTimeout(() => mask.remove(), 120);
      if (onClose) onClose();
    };
    mask.querySelector('.x').addEventListener('click', close);
    mask.addEventListener('click', (e) => { if (e.target === mask) close(); });
    document.addEventListener('keydown', function esc(e) {
      if (e.key === 'Escape' && document.body.contains(mask)) { close(); document.removeEventListener('keydown', esc); }
    });
    document.body.appendChild(mask);
    return { mask, bodyEl, footEl, close };
  }

  function confirmAction(message, onYes, { danger = true, yesText = 'Confirm' } = {}) {
    const cancel = document.createElement('button');
    cancel.className = 'btn btn-ghost btn-sm'; cancel.textContent = 'Cancel';
    const yes = document.createElement('button');
    yes.className = 'btn btn-sm ' + (danger ? 'btn-danger' : 'btn-primary');
    yes.textContent = yesText;
    const m = openModal({
      title: 'Please confirm', body: '<p>' + escHtml(message) + '</p>',
      footer: [cancel, yes]
    });
    cancel.onclick = () => m.close();
    yes.onclick = async () => {
      yes.disabled = true; try { await onYes(); m.close(); }
      catch (e) { toast(e.message, 'err'); yes.disabled = false; }
    };
  }

  /* ------------------------------------------------------------ formatting */

  function escHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fmtDate(s) {
    if (!s) return '—';
    const d = new Date((s || '').replace(' ', 'T'));
    if (isNaN(d)) return s;
    return d.toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit'
    });
  }

  function initials(name) {
    return String(name || '?').trim().slice(0, 2).toUpperCase();
  }

  /* ----------------------------------------------------------- markdown-ish
     Safe, small renderer for model/rule output: headings, bold, italic,
     inline code, fenced code, links (http/https only), lists, tables, hr. */

  function inlineMd(text) {
    let s = escHtml(text);
    // protect inline code
    const codes = [];
    s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return '\0C' + (codes.length - 1) + '\0'; });
    s = s.replace(/!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g,
      (_, alt, url) => '<img src="' + url + '" alt="' + alt + '" style="max-width:100%">');
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|\W)\*([^*\n]+)\*/g, '$1<em>$2</em>');
    s = s.replace(/\0C(\d+)\0/g, (_, i) => '<code>' + codes[+i] + '</code>');
    return s;
  }

  function renderMd(md) {
    if (!md) return '';
    const lines = String(md).replace(/\r\n/g, '\n').split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^```/.test(line)) {
        const buf = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++;
        out.push('<pre style="background:#0E2A47;color:#DDE6F0;padding:12px;border-radius:8px;' +
          'overflow-x:auto;font-size:12px"><code>' + escHtml(buf.join('\n')) + '</code></pre>');
        continue;
      }
      if (/^\s*$/.test(line)) { i++; continue; }
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) { out.push('<h' + h[1].length + '>' + inlineMd(h[2]) + '</h' + h[1].length + '>'); i++; continue; }
      if (/^\s*---+\s*$/.test(line)) { out.push('<hr>'); i++; continue; }
      // table
      if (/^\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        const head = line.split('|').slice(1, -1).map((x) => x.trim());
        i += 2;
        const rows = [];
        while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) {
          rows.push(lines[i].split('|').slice(1, -1).map((x) => x.trim()));
          i++;
        }
        let t = '<div class="table-wrap"><table class="data"><thead><tr>';
        t += head.map((x) => '<th>' + inlineMd(x) + '</th>').join('') + '</tr></thead><tbody>';
        for (const r of rows) t += '<tr>' + r.map((x) => '<td>' + inlineMd(x) + '</td>').join('') + '</tr>';
        out.push(t + '</tbody></table></div>');
        continue;
      }
      // lists
      const ulm = line.match(/^\s*[-*]\s+(.*)$/);
      const olm = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ulm || olm) {
        const ordered = !!olm; const re = ordered ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*]\s+(.*)$/;
        const items = [];
        while (i < lines.length && re.test(lines[i])) { items.push(inlineMd(lines[i].match(re)[1])); i++; }
        out.push((ordered ? '<ol>' : '<ul>') +
          items.map((x) => '<li>' + x + '</li>').join('') + (ordered ? '</ol>' : '</ul>'));
        continue;
      }
      // paragraph (gather until blank/special)
      const buf = [line]; i++;
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^(#{1,4}\s|```|\s*[-*]\s|\s*\d+[.)]\s|\|)/.test(lines[i])) {
        buf.push(lines[i]); i++;
      }
      out.push('<p>' + inlineMd(buf.join(' ')) + '</p>');
    }
    return out.join('\n');
  }

  /* -------------------------------------------------------- small builders */

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') node.className = v;
      else if (k === 'html') node.innerHTML = v;
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) node.setAttribute(k, v);
    }
    for (const c of [].concat(children)) {
      if (c == null) continue;
      node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return node;
  }

  function emptyState(icon, title, sub) {
    return '<div class="empty"><div class="big">' + icon + '</div><div style="font-weight:600;color:var(--ink-2)">' +
      escHtml(title) + '</div>' + (sub ? '<div class="small mt8">' + escHtml(sub) + '</div>' : '') + '</div>';
  }

  /* ----------------------------------------------------------- help center
     Plain-language help for non-technical users. Mounts a floating
     "?" button on both portals; content adapts to the portal. */

  const HELP = {
    app: {
      start: [
        ['1 · View the KPI dashboard', 'The Dashboard shows a scenario banner (which ontology and data sources the numbers are based on) and real KPI cards computed from that data. Use the scenario dropdown in the header to switch packs — the dashboard, graphs, analytics, Ask and Graph studio all follow, and the choice is remembered per user.'],
        ['2 · Ask questions in plain language', 'Open "Ask the ontology", type e.g. <i>What is the total VNB this year?</i> — the green badge means a verified rule answered; blue means the AI model answered.'],
        ['3 · Keep and reuse questions', 'Chips come in four groups: ✨ Recommended for you, ★ My questions (★ Save question stores one; × removes it), 🕘 Recent asks (click to re-ask anything from your history) and Standard questions (set by your admin). Paste a whole list with 📋 Batch — each line is asked in sequence and later questions use earlier answers as memory.'],
        ['4 · Read the impact chain', 'Every answer shows a small graph: the drivers behind the number. Click "Open large view" for path-finding and impact analysis.'],
        ['5 · Upload your own data', 'Data centre → Upload .csv/.xlsx. The ontology instantly includes your data in Q&A.'],
        ['6 · Turn data into a new ontology', 'Graph studio → ✨ Build an ontology from your data: tick tables → 🔍 Preview (tables become entities, columns attributes, *_id links relations) → ✓ Create & activate. A full analytics report is generated automatically, titled with the scenario and its data sources. Tick <b>AI enrichment</b> to let the model write entity descriptions and suggest starter questions.'],
        ['7 · Explore graphs of your sources', 'Graph studio opens on <b>Data lineage</b> — which uploaded table feeds which entity. Table chips show the source file, kind and upload time; the 📍 Node provenance panel names the exact table, column or file behind any node. The <b>Generation history</b> card lists every ontology, preview, upload and report produced here, with one-click switching to past ontologies. Schema and Column mapping views are one click away.'],
        ['8 · Pick or add your own AI model', 'Open <b>Model centre</b> in the sidebar: pick which model answers your questions (system models plus your own), and register any OpenAI-compatible endpoint (name, base URL, API key, model) with ＋ Add my model — it is stored for your account, tested with one click, optionally registered in the server .env, and available next time without any file editing. The button in the Ask view shows the active model and jumps here.'],
        ['9 · Generate a report', 'My reports → pick sections (now including the ontology graph and performance analytics) → Generate. ⚡ <b>Sample report</b> builds a full demo report from the recommended questions in one click. Every report has 🌐 View (in-browser), HTML, PDF and Markdown.'],
        ['10 · Search with instant insight', 'Ontology workspace → search box: matches appear as chips, and below them an <b>Instant insight</b> card shows what the top match is, which tables back it, a graph of the affected neighbourhood (matched nodes + one hop) and a written analysis.'],
      ],
      faq: [
        ['What do the dashboard KPIs mean?', 'Each KPI card is one "metric formula" the active ontology declares: the card label names the metric and the number is computed live from the uploaded tables, never hard-coded or invented. For example the Actuarial Lapse Experience pack shows M1 annualised lapse rate (= 12 × actual lapses ÷ exposure), M2 lapse variance in percentage points, M3 actual-over-expected ratio, M4 average assumed lapse rate, and M5/M6/M7 the IFRS 17 CSM / ISR / EV closing balances ($M). Open "ⓘ What does this mean?" on any card to see its exact formula and the table it reads; the full list is maintained by administrators under Ontology governance → Formulas.'],
        ['The answer says "no matching rule"', 'The question is outside the current ontology. Try a recommended question, or ask the admin to add a rule / upload relevant data.'],
        ['Where do the numbers come from?', 'From the data tables uploaded in the Data centre, computed by transparent formulas — never invented. Hover a node to see its formula source.'],
        ['Can I break something?', 'No. Uploads are versioned, and creating an ontology from data keeps the previous pack — switch back any time from the scenario dropdown, or ask an admin to re-activate it.'],
        ['How do I compare scenarios?', 'Use the scenario dropdown in the header — each option is a pack with its own data, graph and reports. Every view follows the selection, and every report states which scenario and data sources it was built from.'],
        ['Which model am I using?', 'The Ask view button "🤖 Model: …" shows the active model (Default = the admin-configured model); click it to open the Model centre and switch — it only affects your own answers.'],
        ['How is the admin console different?', 'This portal is for using and building (ask, upload, create, report). The admin console (/admin) only governs: users, models, token usage, canonical questions and oversight of ontologies.'],
        ['My dashboard KPI cards are empty', 'KPI cards come from the ontology\'s metric formulas (for example VNB = agent count × productivity × margin). If none appear, the active pack either defines no formulas yet, or the formulas exist but their input tables are still empty. Ask your administrator to define this pack\'s metrics, or upload the data the formulas read.'],
        ['What does the yellow "Compare" button do?', 'It answers each question twice so you can see what the ontology adds. The left column is the normal ontology-grounded answer with its impact chain; the right column is the same model with no knowledge base at all — general industry commentary with no tables, metrics or chain. A green strip reports how many tables, metric definitions and chain nodes/edges the ontology contributed. It costs two model calls per question; press the button again to return to single answers.'],
        ['What does the "Use ontology" switch do?', 'It is the ON / OFF switch in the top bar, next to the scenario dropdown, and it exists in both this portal and the admin console. ON is the default: metrics, graphs and reports come from the active ontology. OFF puts the whole portal into text-only demo mode — KPI cards, the knowledge graph, performance charts and impact chains are hidden behind an "Ontology is OFF" panel, Ask answers from general knowledge with no knowledge base, and reports still generate but drop their graph, chart and analytics sections. Your choice is remembered in this browser only; nobody else is affected. Click ON, or "Turn ontology back on" in the banner, to restore every view.'],
        ['Performance analytics says "not available"', 'That view adapts to the active pack. If your pack declares its own monthly table (a report_month / month column of YYYY-MM values) the page draws a "Monthly trend from this pack" card automatically — pick any numeric column from the dropdown. The "not available" message appears only when the pack has no monthly table at all: the driver, scenario and agent cards need the built-in VNB / APE schema (vnb_monthly, channel_monthly, agent_monthly and agent_master with a month column). Switching, in the header, to a pack that carries that schema fills in the full views.'],
      ],
    },
    admin: {
      start: [
        ['1 · This console is for governance', 'Accounts & access, canonical questions, available models, token usage, and oversight of every ontology. Day-to-day use and data-driven ontology creation happen in the user portal.'],
        ['2 · Create or govern an ontology', 'Ontology governance → "✨ New ontology" (wizard + checklist), or let users generate one from data in Graph studio — it appears here for oversight and can be re-activated any time.'],
        ['3 · Every ontology shows its origin', 'The 🧾 badge next to each pack states exactly where it came from: which user created it in Graph studio (and from which tables), or which admin imported it. The same provenance is visible to users.'],
        ['4 · Keep the two graphs aligned', 'Knowledge graph shows three layers of the SAME ontology — Schema, Impact (what users see), Instance. If Schema and Impact differ, press ⟳ Sync impact graph from ontology.'],
        ['5 · Oversee data and questions', 'Data oversight reviews tables/datasets; Canonical questions are the organisation-wide list. Users keep their own private questions separately.'],
        ['6 · Manage models and watch spend', 'Models & usage: add/disable OpenAI-compatible endpoints, set the default, review the <b>User-created models</b> table (every model a user registered, masked keys) and the <b>User portal settings</b> table — which scenario pack and model each user works with — so support can reproduce any setup.'],
        ['7 · Verify, then activate', '🩺 Health check must show no red items, then press ▶ Activate — users see the new ontology immediately.'],
        ['8 · Stay safe with snapshots', 'Before big edits press 🕘 History → 📸 Save snapshot. Anything breaks? One click rolls back.'],
        ['9 · Review AI proposals', 'After a data upload the model may draft proposals under ✨ AI proposals in the sidebar. A proposal is a suggested addition to the ontology (a new entity, relation, graph node/edge or metric formula) — never a change to program code. Each card states what approving it will add; approval only adds elements, never edits or removes existing ones.'],
      ],
      faq: [
        ['Health check shows amber items', 'Amber = advisory (e.g. entities without attributes). Fix them when convenient; red items block trustworthy answers.'],
        ['Import preview says "Cannot import"', 'The file failed validation (bad slug, duplicate, missing fields). Fix the listed items — nothing was written yet.'],
        ['Why do Schema and Impact graphs look different?', 'They are two layers of the same ontology. Impact can lag after hand edits — click ⟳ Sync impact graph from ontology to rebuild it from entities and relations.'],
        ['What are graph edge colours?', 'Blue solid = structural identity; green dash = operational driver; brown dots = weighting; purple long-dash = cross-link. Hover any edge for details.'],
        ['A user question gets wrong answers', 'Add or fix a rule under Ontology governance → Rules. Run 🩺 Health check — empty-pattern rules never trigger and usually explain misses.'],
        ['A user says their own model fails', 'Check the User-created models table (Models & usage): owner, model name and base URL are listed, keys masked. Ask the user to press Test in their Model centre and share the error.'],
        ['What does an AI proposal actually change?', 'A proposal is a suggested addition to the ontology — a new entity, relation, graph node/edge or metric formula, drafted from an uploaded table. Approving applies it through the normal ontology write path and only ever ADDS elements; it never edits program code and never removes what already exists. Open ✨ AI proposals in the sidebar to review, approve or reject.'],
        ['Why is the user dashboard blank on KPIs?', 'Dashboard KPI cards are the active pack\'s own metric formulas. If a pack has none (or their tables are empty), users see an explanatory empty state. Add formulas under Ontology governance → Formulas, or approve the model\'s formula proposals after a data upload.'],
        ['Why is Performance analytics empty?', 'It adapts to the active pack: a pack with its own monthly table shows an automatic "Monthly trend from this pack" card. Only a pack with no monthly table at all shows the "not available" message, which lists the built-in VNB / APE tables it would need (vnb_monthly, channel_monthly, agent_monthly, agent_master with a month column).'],
      ],
    },
  };

  function helpMount(portal) {
    const fab = document.createElement('button');
    fab.className = 'help-fab';
    fab.setAttribute('aria-label', 'Help');
    fab.title = 'Help';
    fab.innerHTML = '?';
    fab.onclick = () => helpOpen(portal);
    document.body.appendChild(fab);
  }

  function helpOpen(portal) {
    const data = HELP[portal] || HELP.app;
    const isAdmin = portal === 'admin';
    const tabs = [
      ['start', isAdmin ? 'Get started' : 'How to use'],
      ['faq', 'FAQ'],
    ];
    const body = document.createElement('div');
    body.innerHTML =
      '<div class="tabs">' + tabs.map(([k, lab], i) =>
        '<button data-tab="' + k + '"' + (i === 0 ? ' class="active"' : '') + '>' + lab + '</button>').join('') +
      '</div><div id="helpBody"></div>';
    const render = (key) => {
      body.querySelector('#helpBody').innerHTML = data[key].map(([t, d]) =>
        '<div class="card mb8"><div class="card-b" style="padding:10px 14px">' +
        '<b>' + t + '</b>' +
        '<div class="small mt8" style="line-height:1.55">' + d + '</div></div></div>').join('') +
        '<p class="small muted">More detail: see the README and the User Guide ' +
        '(docs/USER_GUIDE.md) in the project folder.</p>';
    };
    body.querySelectorAll('[data-tab]').forEach((b) => {
      b.onclick = () => {
        body.querySelectorAll('[data-tab]').forEach((x) => x.classList.toggle('active', x === b));
        render(b.dataset.tab);
      };
    });
    render('start');
    openModal({ title: '❓ Help', body, wide: true, footer: null });
  }

  /* ------------------------------------------- global ontology mode */

  // One switch, honoured by every module in both portals. ON (the default)
  // grounds metrics, graphs, answers and reports in the active ontology. OFF
  // hides every ontology-derived visual and says so, while text answers and
  // text-only reports are still produced.
  const ONTO_KEY = 'onti.useOntology';

  const ontology = {
    enabled() {
      try { return localStorage.getItem(ONTO_KEY) !== 'off'; } catch (e) { return true; }
    },
    set(on) {
      try { localStorage.setItem(ONTO_KEY, on ? 'on' : 'off'); } catch (e) { /* storage blocked */ }
      document.dispatchEvent(new CustomEvent('onti:ontology', { detail: { on: !!on } }));
    },
    renderBanner() {
      const main = document.querySelector('.main');
      if (!main) return;
      let b = main.querySelector('.onto-off-banner');
      if (ontology.enabled()) {
        if (b) b.remove();
        return;
      }
      if (!b) {
        b = document.createElement('div');
        b.className = 'onto-off-banner';
        const tb = main.querySelector('.topbar');
        if (tb) tb.insertAdjacentElement('afterend', b);
        else main.insertBefore(b, main.firstChild);
      }
      b.innerHTML = '<span class="onto-off-dot"></span><span><b>Ontology is OFF.</b> ' +
        'Text-only mode — ontology metrics, knowledge graphs and impact chains are hidden. ' +
        'Ask still answers from the uploaded data tables, but with no ontology layer; ' +
        'reports are generated text-only.</span>' +
        '<span class="spacer"></span>' +
        '<button class="btn btn-xs" data-onto-restore>Turn ontology on</button>';
      b.querySelector('[data-onto-restore]').onclick = () => ontology.set(true);
    },
    mount() {
      const bar = document.querySelector('.topbar');
      if (bar && !bar.querySelector('.onto-switch')) {
        const box = document.createElement('div');
        box.className = 'onto-switch';
        box.innerHTML =
          '<span class="onto-switch-label">Use ontology</span>' +
          '<button class="onto-switch-btn" data-onto="on" aria-pressed="true">ON</button>' +
          '<button class="onto-switch-btn" data-onto="off" aria-pressed="false">OFF</button>';
        bar.insertBefore(box, bar.querySelector('.userbox') || null);
        box.querySelectorAll('[data-onto]').forEach((btn) => {
          btn.onclick = () => {
            const on = btn.dataset.onto === 'on';
            ontology.set(on);
            toast(on ? 'Ontology is ON' : 'Ontology is OFF — text-only mode',
              on ? 'ok' : 'warn');
          };
        });
      }
      const sync = () => {
        const on = ontology.enabled();
        document.querySelectorAll('.onto-switch [data-onto]').forEach((btn) => {
          const isOn = btn.dataset.onto === 'on';
          btn.classList.toggle('active', isOn === on);
          btn.setAttribute('aria-pressed', String(isOn === on));
        });
        ontology.renderBanner();
      };
      document.addEventListener('onti:ontology', sync);
      sync();
    },
    offPanel(title, body) {
      return '<div class="card onto-off-panel"><div class="card-b">' +
        '<div class="onto-off-title">Ontology is OFF — text-only mode</div>' +
        '<p><b>' + escHtml(title) + '</b></p>' +
        '<p class="small muted">' + escHtml(body) + '</p>' +
        '<button class="btn btn-sm" data-onto-restore>Turn ontology back on</button>' +
        '</div></div>';
    },
    bindRestore(root) {
      (root || document).querySelectorAll('[data-onto-restore]').forEach((b) => {
        b.onclick = () => ontology.set(true);
      });
    },
  };

  window.Onti = {
    api, ApiError, toast, openModal, confirmAction,
    escHtml, renderMd, inlineMd, fmtDate, initials, el, emptyState,
    helpMount, helpOpen, ontology,
  };
})();
