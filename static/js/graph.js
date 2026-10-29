/* Onti — layered left-to-right DAG SVG graph renderer (v13).
 * Orthogonal edge routing that avoids crossing through nodes.
 * Used for: inline answer chain, full-graph modal, admin knowledge graph.
 *
 * data: { nodes: [{id,label,grp|group,value,is_core}],
 *          links: [{id,source,target,label,type}],
 *          highlight: {nodes:[ids], edge_ids:[ids]} }
 */
(function () {
  'use strict';

  const SVGNS = 'http://www.w3.org/2000/svg';
  const GROUP_COLORS = {
    financial: '#ED1B2E', operational: '#0E2A47', product: '#86BC25',
    assumption: '#2B9BD4', mix: '#7A4FB0', cross: '#B7791F',
    entity: '#0E2A47', instance: '#3D7DCC',
  };

  const EDGE_STYLES = {
    STRUCTURAL: { color: '#5B8DEF', dash: 'none', width: 1.5 },
    OPERATIONAL: { color: '#0E7C5F', dash: 'none', width: 1.5 },
    MIX: { color: '#B45309', dash: 'none', width: 1.5 },
    CROSS_LINK: { color: '#7C3AED', dash: 'none', width: 1.5 },
    RELATION: { color: '#2563EB', dash: 'none', width: 1.5 },
  };
  const EDGE_FALLBACK = { color: '#A9B8C9', dash: 'none', width: 1.3 };

  function edgeStyle(type) { return EDGE_STYLES[type] || EDGE_FALLBACK; }
  function grpColor(g) { return GROUP_COLORS[g || 'operational'] || '#0E2A47'; }

  function svgEl(tag, attrs) {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    return n;
  }

  function wrapText(text, maxChars) {
    const words = String(text || '').split(/\s+/);
    const lines = [];
    let cur = '';
    for (const w of words) {
      const long = w.length > maxChars;
      if (cur && (cur.length + 1 + w.length > maxChars)) { lines.push(cur); cur = ''; }
      if (long) {
        if (cur) { lines.push(cur); cur = ''; }
        for (let i = 0; i < w.length; i += maxChars) lines.push(w.slice(i, i + maxChars));
      } else cur = cur ? cur + ' ' + w : w;
    }
    if (cur) lines.push(cur);
    return lines.slice(0, 4);
  }

  /* Layout: longest-path layering + barycenter ordering.
     Limits columns to maxCols; overflow nodes spill into wrapped bands. */
  function layout(nodes, links, boxW, boxH, gapX, gapY, maxCols) {
    const ids = new Set(nodes.map((n) => n.id));
    const out = new Map(), inc = new Map();
    nodes.forEach((n) => { out.set(n.id, []); inc.set(n.id, []); });
    const real = links.filter((l) => ids.has(l.source) && ids.has(l.target) && l.source !== l.target);
    real.forEach((l) => { out.get(l.source).push(l.target); inc.get(l.target).push(l.source); });

    const level = new Map();
    let frontier = nodes.filter((n) => inc.get(n.id).length === 0).map((n) => n.id);
    if (!frontier.length) frontier = [nodes[0].id];
    frontier.forEach((id) => level.set(id, 0));
    const indeg = new Map(nodes.map((n) => [n.id, inc.get(n.id).length]));
    const q = frontier.slice();
    while (q.length) {
      const id = q.shift();
      for (const t of out.get(id)) {
        level.set(t, Math.max(level.get(t) ?? 0, (level.get(id) ?? 0) + 1));
        indeg.set(t, indeg.get(t) - 1);
        if (indeg.get(t) <= 0) q.push(t);
      }
    }
    nodes.forEach((n, idx) => { if (!level.has(n.id)) level.set(n.id, idx); });

    const cols = new Map();
    nodes.forEach((n) => {
      const lv = level.get(n.id);
      if (!cols.has(lv)) cols.set(lv, []);
      cols.get(lv).push(n);
    });
    const pos = new Map();
    const refresh = () => {
      pos.clear();
      [...cols.entries()].forEach(([, arr]) => arr.forEach((n, i) => pos.set(n.id, i)));
    };
    refresh();
    const center = (ids2) => {
      const p = ids2.map((x) => pos.get(x)).filter((x) => x != null);
      return p.length ? p.reduce((a, b) => a + b, 0) / p.length : 0;
    };
    for (let sweep = 0; sweep < 6; sweep++) {
      const levels = [...cols.keys()].sort((a, b) => a - b);
      levels.slice(1).forEach((lv) => {
        cols.get(lv).sort((a, b) => center(inc.get(a.id)) - center(inc.get(b.id)) ||
          a.label.localeCompare(b.label));
      });
      refresh();
      levels.slice(0, -1).reverse().forEach((lv) => {
        cols.get(lv).sort((a, b) => center(out.get(a.id)) - center(out.get(b.id)) ||
          a.label.localeCompare(b.label));
      });
      refresh();
    }

    /* Cap rows per column: if a column has too many nodes, the graph
       becomes very tall.  Split into side-by-side sub-columns. */
    const maxRowsPerCol = 5;
    const colEntries = [...cols.entries()].sort((a, b) => a[0] - b[0]);
    const positions = new Map();
    const stepX = boxW + gapX;
    const stepY = boxH + gapY;
    let xPos = 0;
    let maxH = 0;
    let actualCols = 0;

    colEntries.forEach(([, arr]) => {
      const chunks = [];
      for (let i = 0; i < arr.length; i += maxRowsPerCol) {
        chunks.push(arr.slice(i, i + maxRowsPerCol));
      }
      const chunkWidth = chunks.length * stepX;
      chunks.forEach((chunk, ci) => {
        const cx = xPos + ci * stepX;
        chunk.forEach((n, yi) => {
          positions.set(n.id, {
            x: cx,
            y: yi * stepY + (maxRowsPerCol - chunk.length) * stepY * 0.5 + gapY * 0.4,
          });
        });
        if (chunk.length * stepY > maxH) maxH = chunk.length * stepY;
      });
      xPos += chunkWidth;
      actualCols += chunks.length;
    });

    const totalW = xPos;
    return {
      positions, width: totalW, height: maxH,
      links: real, actualCols,
    };
  }

  /* Orthogonal routing: source-right → gap → vertical → target-left.
     Returns an SVG path "d" string and the midpoint for label placement. */
  function routeEdge(x0, y0, x1, y1, colGap) {
    const midX = x0 + (x1 - x0) / 2;
    /* If same Y, simple horizontal line with slight curve */
    if (Math.abs(y0 - y1) < 2) {
      const d = `M${x0},${y0} L${x1 - 1},${y1}`;
      return { d, labelX: midX, labelY: y0 };
    }
    /* Orthogonal: go right to mid, then vertical, then right to target */
    const d = `M${x0},${y0} L${midX},${y0} L${midX},${y1} L${x1 - 1},${y1}`;
    return { d, labelX: midX, labelY: (y0 + y1) / 2 };
  }

  function render(container, data, opts = {}) {
    const compact = !!opts.compact;
    const boxW = compact ? 148 : 210;
    const boxH = compact ? 50 : 70;
    const gapX = compact ? 36 : 50;
    const gapY = compact ? 18 : 26;
    const pad = compact ? 10 : 20;
    const maxCols = compact ? 6 : 8;

    container.innerHTML = '';
    const stage = document.createElement('div');
    stage.className = 'graph-stage';
    container.appendChild(stage);

    const nodes = (data.nodes || []).map((n) => ({
      id: String(n.id ?? n.key),
      label: n.label || n.name || n.id || n.key || '?',
      grp: n.grp || n.group || '',
      value: n.value != null ? String(n.value) : '',
      core: !!n.is_core,
      raw: n,
    }));
    if (!nodes.length) {
      stage.innerHTML = '<div class="empty" style="padding:24px">No graph nodes to display yet.</div>';
      return;
    }
    const links = (data.links || data.edges || []).map((l) => ({
      id: String(l.id || l.key || (l.source + '>' + l.target)),
      source: String((l.source ?? l.source_key ?? '').split(',')[0]).trim(),
      target: String(l.target ?? l.target_key ?? ''),
      label: l.label || l.name || '',
      type: l.type || l.edge_type || '',
    }));

    const hlNodes = new Set((data.highlight?.nodes || []).map(String));
    const hlEdges = new Set((data.highlight?.edge_ids || []).map(String));
    const { positions, width, height, links: realLinks } =
      layout(nodes, links, boxW, boxH, gapX, gapY, maxCols);

    const W = width + boxW + gapX * 0.8;
    const H = height + boxH + gapY * 0.8;
    const svg = svgEl('svg', {
      width: W + pad * 2,
      height: Math.max(140, H + pad * 2),
      viewBox: `0 0 ${W + pad * 2} ${H + pad * 2}`,
      preserveAspectRatio: 'xMinYMin meet',
    });

    // arrow markers
    const defs = svgEl('defs');
    const markerColors = new Map([['arr', '#A9B8C9'], ['arr-hl', '#ED1B2E']]);
    realLinks.forEach((l) => {
      const s = edgeStyle(l.type);
      if (!markerColors.has(s.color)) markerColors.set(s.color + '-arr', s.color);
    });
    markerColors.forEach((color, id) => {
      const m = svgEl('marker', {
        id, viewBox: '0 0 10 10', refX: 9, refY: 5,
        markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse'
      });
      m.appendChild(svgEl('path', { d: 'M0,0 L10,5 L0,10 z', fill: color }));
      defs.appendChild(m);
    });
    svg.appendChild(defs);

    /* Layer order: edges → nodes → labels (labels on top, always visible) */
    const edgeLayer = svgEl('g');
    const nodeLayer = svgEl('g');
    const labelLayer = svgEl('g');
    svg.appendChild(edgeLayer);
    svg.appendChild(nodeLayer);
    svg.appendChild(labelLayer);

    const dim = hlNodes.size > 0 || hlEdges.size > 0;
    const adjOut = new Map(), adjIn = new Map();
    realLinks.forEach((l) => {
      if (!adjOut.has(l.source)) adjOut.set(l.source, []);
      if (!adjIn.has(l.target)) adjIn.set(l.target, []);
      adjOut.get(l.source).push(l);
      adjIn.get(l.target).push(l);
    });
    let hoverLocked = false;

    /* Label t-offsets to spread along edges */
    const _tOffsets = [0.50, 0.35, 0.65, 0.25, 0.75, 0.40, 0.60, 0.30, 0.70];
    let _lblIdx = 0;

    realLinks.forEach((l) => {
      const p0 = positions.get(l.source), p1 = positions.get(l.target);
      if (!p0 || !p1) return;
      const x0 = p0.x + pad + boxW, y0 = p0.y + pad + boxH / 2;
      const x1 = p1.x + pad, y1 = p1.y + pad + boxH / 2;

      /* Orthogonal routing avoids crossing through node boxes */
      const route = routeEdge(x0, y0, x1, y1, gapX);

      const hot = hlEdges.has(l.id);
      const touch = hlNodes.has(l.source) && hlNodes.has(l.target);
      const s = edgeStyle(l.type);
      const useColor = hot || touch ? '#ED1B2E' : s.color;
      const useWidth = hot || touch ? 2.2 : s.width;
      const useDash = hot || touch ? 'none' : s.dash;

      const path = svgEl('path', {
        d: route.d,
        fill: 'none',
        class: 'g-link' + (hot || touch ? ' hl' : dim ? ' dim' : ''),
        'marker-end': `url(#${hot || touch ? 'arr-hl' : s.color + '-arr'})`
      });
      path.style.stroke = useColor;
      path.style.strokeWidth = String(useWidth);
      if (useDash !== 'none') path.style.strokeDasharray = useDash;
      path.style.strokeLinejoin = 'round';
      path.style.strokeLinecap = 'round';
      l._path = path;

      const title = svgEl('title');
      let tip = l.label || l.type || 'edge';
      if (l.type) tip += ' [' + l.type + ']';
      if (l.description) tip += '\n\n' + l.description;
      title.textContent = tip;
      path.appendChild(title);

      path.addEventListener('mouseenter', () => {
        if (hoverLocked) return;
        path.classList.add('hl');
        path.style.stroke = '#ED1B2E';
        path.style.strokeWidth = '2.2';
        path.style.strokeDasharray = 'none';
        path.setAttribute('marker-end', 'url(#arr-hl)');
      });
      path.addEventListener('mouseleave', () => {
        if (hoverLocked) return;
        path.classList.remove('hl');
        path.style.stroke = useColor;
        path.style.strokeWidth = String(useWidth);
        if (useDash !== 'none') path.style.strokeDasharray = useDash;
        else path.style.strokeDasharray = '';
        path.setAttribute('marker-end', `url(#${hot || touch ? 'arr-hl' : s.color + '-arr'})`);
      });
      if (opts.onEdgeClick) {
        path.style.cursor = 'pointer';
        path.addEventListener('click', (e) => {
          e.stopPropagation();
          opts.onEdgeClick(l);
        });
      }
      edgeLayer.appendChild(path);
      /* Edge label is in the <title> tooltip — hover to see it.
         No text rendered on the SVG to keep the graph clean. */
    });

    nodes.forEach((n) => {
      const p = positions.get(n.id);
      const color = grpColor(n.grp);
      const matched = hlNodes.has(n.id);
      /* Auto-fit: measure text lines first, then set box height */
      const labelLines = wrapText(n.label, compact ? 16 : 22);
      const hasValue = n.value && !compact;
      const dynBoxH = hasValue
        ? Math.max(boxH, labelLines.length * 14 + 28)
        : Math.max(boxH, labelLines.length * 14 + 14);
      const g = svgEl('g', {
        class: 'g-node' + (n.core ? ' core' : '') + (matched ? ' matched' : '') +
          (dim && !matched ? ' dim' : ''),
        transform: `translate(${p.x + pad},${p.y + pad})`
      });
      const rect = svgEl('rect', {
        width: boxW, height: dynBoxH, rx: 8, ry: 8,
        stroke: color, 'stroke-width': n.core ? 2.2 : 1.5, fill: '#fff'
      });
      g.appendChild(rect);
      g.appendChild(svgEl('rect', { width: 5, height: dynBoxH, rx: 2, fill: color }));
      const startY = hasValue ? 16 : dynBoxH / 2 - (labelLines.length - 1) * 7 + 2;
      labelLines.forEach((line, i) => {
        const t = svgEl('text', { x: 13, y: startY + i * 14, class: 'g-label' });
        t.textContent = line;
        g.appendChild(t);
      });
      if (hasValue) {
        const t = svgEl('text', { x: 13, y: dynBoxH - 8, class: 'g-value' });
        t.textContent = String(n.value).length > 28 ? String(n.value).slice(0, 27) + '…' : n.value;
        g.appendChild(t);
      }
      const title = svgEl('title');
      let tip = n.label;
      if (n.value) tip += ' — ' + n.value;
      if (n.desc) tip += '\n\n' + n.desc;
      title.textContent = tip;
      g.appendChild(title);

      const connected = (adjOut.get(n.id) || []).concat(adjIn.get(n.id) || []);
      const neighbours = new Set();
      connected.forEach((l) => {
        if (l.source !== n.id) neighbours.add(l.source);
        if (l.target !== n.id) neighbours.add(l.target);
      });
      const allPaths = realLinks.map((l) => l._path).filter(Boolean);
      const allNodes = nodeLayer.querySelectorAll('g.g-node');
      g.addEventListener('mouseenter', () => {
        if (hoverLocked || hlNodes.size) return;
        allPaths.forEach((p) => { if (!connected.some((c) => c._path === p)) p.classList.add('dim'); });
        allNodes.forEach((ng) => {
          const nid = ng.getAttribute('data-nid');
          if (nid !== n.id && !neighbours.has(nid)) ng.classList.add('dim');
        });
      });
      g.addEventListener('mouseleave', () => {
        if (hoverLocked || hlNodes.size) return;
        allPaths.forEach((p) => p.classList.remove('dim'));
        allNodes.forEach((ng) => ng.classList.remove('dim'));
      });
      g.setAttribute('data-nid', n.id);

      if (opts.onNodeClick) {
        g.style.cursor = 'pointer';
        g.addEventListener('click', () => opts.onNodeClick(n.raw));
      }
      nodeLayer.appendChild(g);
    });

    stage.appendChild(svg);
    if (opts.legend !== false) {
      const legend = document.createElement('div');
      legend.className = 'legend';
      legend.style.padding = '8px 12px';
      legend.innerHTML =
        '<span><i style="background:#ED1B2E"></i>Financial</span>' +
        '<span><i style="background:#0E2A47"></i>Operational</span>' +
        '<span><i style="background:#86BC25"></i>Product</span>' +
        '<span><i style="background:#2B9BD4"></i>Assumption</span>' +
        (hlNodes.size ? '<span><i style="background:#fff;border:2px solid #ED1B2E"></i>Answer evidence</span>' : '');
      const et = data.edge_types || {};
      const etKeys = Object.keys(et);
      if (etKeys.length) {
        legend.innerHTML += '<div style="width:100%;border-top:1px solid #e0e4ec;margin-top:6px;padding-top:6px;display:flex;flex-wrap:wrap;gap:12px;font-size:11px;color:#6B7787">' +
          '<span style="font-weight:600">Edge types:</span> ' + etKeys.join(' · ') +
          ' <span style="color:#93A0AF">(hover a line to see its label)</span></div>';
      }
      container.appendChild(legend);
    }
  }

  function fromEvidence(fullGraph, evidence) {
    const evNodes = new Set(evidence?.nodes || []);
    const evEdges = new Set(evidence?.edge_ids || []);
    const links = (fullGraph.links || []).filter((l) =>
      evEdges.has(l.id) || (evNodes.has(l.source) && evNodes.has(l.target)));
    const keep = new Set();
    links.forEach((l) => { keep.add(l.source); keep.add(l.target); });
    (evidence?.matched || []).forEach((x) => keep.add(String(x)));
    const nodes = (fullGraph.nodes || []).filter((n) => keep.size === 0 || keep.has(String(n.id)));
    return {
      nodes, links,
      edge_types: fullGraph.edge_types,
      edge_descriptions: fullGraph.edge_descriptions,
      highlight: {
        nodes: evidence?.matched || evidence?.nodes || [],
        edge_ids: evidence?.edge_ids || []
      }
    };
  }

  window.Onti = window.Onti || {};
  window.Onti.Graph = { render, fromEvidence, grpColor, edgeStyle };
})();
