// Strategy Builder — canvas view (CANVAS_DESIGN.md, Phase 1).
//
// Renders /studio/{id}/canvas/graph as SVG. No node-editor library: the graph
// is derived from the schema server-side, so this file only lays it out and
// moves the viewBox. Layout is recomputed on every render — nothing about the
// view is persisted, which is what keeps the canvas out of StrategyDefinition.

const NS = 'http://www.w3.org/2000/svg';
const NODE_W = 210, NODE_H = 60, COL_GAP = 96, ROW_GAP = 22, PAD = 60;

const el = (id) => document.getElementById(id);
const svg = el('cv-svg');
const gEdges = el('cv-edges');
const gNodes = el('cv-nodes');
const embed = document.querySelector('.cv-embed');
const strategyId = embed && embed.dataset.strategy;

let graph = { nodes: [], edges: [], meta: {} };
let placed = new Map();          // node id -> {x, y, node}
let vb = { x: 0, y: 0, w: 1200, h: 700 };
let contentBox = null;           // bbox of the laid-out graph, for fit()

// ── viewBox plumbing ───────────────────────────────────────────────────
function applyViewBox() {
  svg.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  const label = el('cv-zoom-label');
  if (!label) return;
  const r = svg.getBoundingClientRect();
  if (r.width && vb.w) label.textContent = Math.round((r.width / vb.w) * 100) + '%';
}

function fit(minScale = 0) {
  if (!contentBox) return;
  const r = svg.getBoundingClientRect();
  const aspect = (r.height && r.width) ? r.height / r.width : 0.6;
  let w = contentBox.w + PAD * 2;
  let h = contentBox.h + PAD * 2;
  // preserveAspectRatio="meet" letterboxes, so grow the short side instead of
  // letting the fit depend on which dimension happens to bind.
  if (h / w < aspect) h = w * aspect; else w = h / aspect;
  // A wide, short graph in a near-square pane fits at a scale nobody can read.
  // The opening view therefore clamps: fit, but never below `minScale`, anchored
  // at the start of the flow so the first thing on screen is where it begins.
  const anchorLeft = r.width && w > r.width / minScale;
  if (minScale > 0 && r.width && r.height) {
    w = Math.min(w, r.width / minScale);
    h = Math.min(h, r.height / minScale);
  }
  vb = {
    x: anchorLeft ? contentBox.x - PAD : contentBox.x - (w - contentBox.w) / 2,
    y: contentBox.y - (h - contentBox.h) / 2,
    w, h,
  };
  applyViewBox();
}

function zoomAt(factor, clientX, clientY) {
  const ctm = svg.getScreenCTM();
  if (!ctm) return;
  const p = new DOMPoint(clientX, clientY).matrixTransform(ctm.inverse());
  const k = Math.min(Math.max(factor, 0.2), 5);
  const w = vb.w * k, h = vb.h * k;
  // Bounds keep the wheel from zooming into numeric mush or out to nothing.
  if (w < 200 || w > 40000) return;
  vb = { x: p.x - (p.x - vb.x) * k, y: p.y - (p.y - vb.y) * k, w, h };
  applyViewBox();
}

function zoomCenter(factor) {
  const r = svg.getBoundingClientRect();
  zoomAt(factor, r.left + r.width / 2, r.top + r.height / 2);
}

svg.addEventListener('wheel', (e) => {
  e.preventDefault();
  zoomAt(e.deltaY > 0 ? 1.12 : 1 / 1.12, e.clientX, e.clientY);
}, { passive: false });

let drag = null;
let dragMoved = false;
svg.addEventListener('mousedown', (e) => {
  if (e.button !== 0) return;
  const ctm = svg.getScreenCTM();
  if (!ctm) return;
  dragMoved = false;
  // Units-per-pixel is captured once: recomputing it mid-drag against a
  // viewBox that is itself moving makes the pan drift away from the cursor.
  drag = { x: e.clientX, y: e.clientY, vx: vb.x, vy: vb.y, u: 1 / ctm.a };
  svg.classList.add('panning');
});
window.addEventListener('mousemove', (e) => {
  if (!drag) return;
  if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) > 4) dragMoved = true;
  vb.x = drag.vx - (e.clientX - drag.x) * drag.u;
  vb.y = drag.vy - (e.clientY - drag.y) * drag.u;
  applyViewBox();
});
window.addEventListener('mouseup', () => { drag = null; svg.classList.remove('panning'); });

// Pinch zoom: two-finger distance drives the same zoomAt as the wheel.
let pinch = null;
const dist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
svg.addEventListener('touchstart', (e) => {
  if (e.touches.length === 2) pinch = dist(e.touches);
}, { passive: true });
svg.addEventListener('touchmove', (e) => {
  if (e.touches.length !== 2 || !pinch) return;
  e.preventDefault();
  const d = dist(e.touches);
  const mid = {
    x: (e.touches[0].clientX + e.touches[1].clientX) / 2,
    y: (e.touches[0].clientY + e.touches[1].clientY) / 2,
  };
  if (d > 0) zoomAt(pinch / d, mid.x, mid.y);
  pinch = d;
}, { passive: false });
svg.addEventListener('touchend', () => { pinch = null; });

document.addEventListener('click', (e) => {
  const b = e.target.closest('.cv-tool');
  if (!b) return;
  if (b.dataset.zoom === 'in') zoomCenter(1 / 1.25);
  else if (b.dataset.zoom === 'out') zoomCenter(1.25);
  else fit();
});

window.addEventListener('resize', applyViewBox);

// ── layout ─────────────────────────────────────────────────────────────
function layout(nodes) {
  const columns = new Map();
  nodes.forEach(n => {
    if (!columns.has(n.layer)) columns.set(n.layer, []);
    columns.get(n.layer).push(n);
  });
  const tallest = Math.max(1, ...[...columns.values()].map(c => c.length));
  const fullH = tallest * NODE_H + (tallest - 1) * ROW_GAP;
  const out = new Map();
  for (const [layer, col] of columns) {
    const colH = col.length * NODE_H + (col.length - 1) * ROW_GAP;
    const top = (fullH - colH) / 2;                 // centre each column
    col.forEach((n, i) => out.set(n.id, {
      node: n,
      x: layer * (NODE_W + COL_GAP),
      y: top + i * (NODE_H + ROW_GAP),
    }));
  }
  const layers = columns.size;
  contentBox = { x: 0, y: 0, w: Math.max(NODE_W, layers * NODE_W + (layers - 1) * COL_GAP), h: fullH };
  return out;
}

// ── render ─────────────────────────────────────────────────────────────
function mk(tag, attrs, text) {
  const node = document.createElementNS(NS, tag);
  for (const k in attrs) node.setAttribute(k, attrs[k]);
  if (text != null) node.textContent = text;
  return node;
}

function clip(text, max) {
  return text && text.length > max ? text.slice(0, max - 1) + '…' : (text || '');
}

function edgePath(a, b) {
  const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
  const x2 = b.x, y2 = b.y + NODE_H / 2;
  const dx = Math.max(30, (x2 - x1) / 2);
  return `M${x1} ${y1} C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}`;
}

function render() {
  placed = layout(graph.nodes);
  gEdges.textContent = '';
  gNodes.textContent = '';

  graph.edges.forEach(e => {
    const a = placed.get(e.source), b = placed.get(e.target);
    if (!a || !b) return;   // a derived graph should never dangle; skip if it does
    gEdges.appendChild(mk('path', {
      d: edgePath(a, b),
      class: 'cv-edge cv-edge-' + e.kind,
      'marker-end': 'url(#cv-arrow)',
    }));
  });

  for (const { node, x, y } of placed.values()) {
    const g = mk('g', {
      class: 'cv-node cv-' + node.kind,
      transform: `translate(${x} ${y})`,
      'data-node': node.id,
    });
    g.appendChild(mk('rect', { class: 'cv-box', width: NODE_W, height: NODE_H, rx: 9 }));
    g.appendChild(mk('rect', { class: 'cv-accent', width: 3, height: NODE_H, rx: 1.5 }));
    if (node.badge) g.appendChild(mk('text', { class: 'cv-badge', x: 14, y: 17 }, clip(node.badge, 18)));
    g.appendChild(mk('text', { class: 'cv-label', x: 14, y: 35 }, clip(node.label, 26)));
    g.appendChild(mk('text', { class: 'cv-detail', x: 14, y: 50 }, clip(node.detail, 34)));
    if (node.badges.includes('opt')) {
      g.appendChild(mk('circle', { class: 'cv-opt', cx: NODE_W - 14, cy: 16, r: 4.5 }));
    }
    // Hover tooltip: the untruncated text, so clipping never hides information.
    g.appendChild(mk('title', {}, `${node.label}\n${node.detail}`));
    gNodes.appendChild(g);
  }
  applyViewBox();
}

// ── selection + inspector ──────────────────────────────────────────────
// The inspector is the form view's own partial in a narrow column; its hx-*
// attributes are untouched, so editing here posts exactly where editing there
// posts. This file only decides WHICH partial to load.
let selected = null;
const inspector = el('cv-inspector');

const EMPTY_INSPECTOR =
  '<div class="cv-pane-title">Inspector</div>' +
  '<p class="cv-hint">Click a node to edit it here. <kbd>Esc</kbd> clears the ' +
  'selection.</p>';

function paintSelection() {
  gNodes.querySelectorAll('.cv-node').forEach(g =>
    g.classList.toggle('selected', g.dataset.node === selected));
}

function select(id) {
  if (!strategyId || !inspector) return;
  selected = id;
  paintSelection();
  htmx.ajax('GET', `/studio/${strategyId}/canvas/inspector/${encodeURIComponent(id)}`,
            { target: '#cv-inspector', swap: 'innerHTML' });
}

function clearSelection() {
  selected = null;
  paintSelection();
  if (inspector) inspector.innerHTML = EMPTY_INSPECTOR;
}

svg.addEventListener('click', (e) => {
  if (dragMoved) return;              // that was a pan, not a click
  const g = e.target.closest('.cv-node');
  if (g) select(g.dataset.node);
  else clearSelection();
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && selected) clearSelection();
});

// Highlight the inspected rule inside the block partial it came with.
document.body.addEventListener('htmx:afterSwap', (e) => {
  if (!inspector || !inspector.contains(e.target)) return;
  const insp = inspector.querySelector('.cv-insp');
  const focus = insp && insp.dataset.focus;
  if (focus) {
    const rule = inspector.querySelector('#rule-' + CSS.escape(focus));
    if (rule) rule.classList.add('cv-focus');
  }
});

// ── data ───────────────────────────────────────────────────────────────
async function refresh({ keepView = true } = {}) {
  if (!strategyId) return;
  const r = await fetch(`/studio/${strategyId}/canvas/graph`, {
    headers: { Accept: 'application/json' },
  });
  if (!r.ok) return;
  graph = await r.json();
  render();
  paintSelection();                   // survives a re-render; the node id is stable
  if (!keepView) fit(OPEN_MIN_SCALE);
}

// Every mutation still goes through the form view's endpoints. Those were left
// untouched, so they do not announce themselves — the canvas instead re-reads
// the graph after any successful write it just made. (The named trigger is
// honoured too, in case the endpoints ever start sending it.)
document.body.addEventListener('studio:changed', () => refresh());
document.body.addEventListener('htmx:afterRequest', (e) => {
  const verb = (e.detail.requestConfig && e.detail.requestConfig.verb) || 'get';
  if (verb !== 'get' && e.detail.successful) refresh();
});

const OPEN_MIN_SCALE = 0.62;   // readable enough to see what the nodes say

if (svg) {
  refresh({ keepView: false });
  window.NauCanvas = { refresh, fit, get graph() { return graph; } };
}
