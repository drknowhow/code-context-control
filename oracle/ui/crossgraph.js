// oracle/ui/crossgraph.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Cross-Graph (federated memory graph) ──
// ═══════════════════════════════════════════════════════════
let _cgCy = null;
const CG_PALETTE = ["#4c8bf5","#b388ff","#ef5350","#ffb74d","#66bb6a","#26c6da","#ec407a","#ab47bc","#8d6e63","#78909c","#ffca28","#ff7043"];
const _cgProjectColor = {};
function cgColorFor(slug) {
  if (!(slug in _cgProjectColor)) {
    _cgProjectColor[slug] = CG_PALETTE[Object.keys(_cgProjectColor).length % CG_PALETTE.length];
  }
  return _cgProjectColor[slug];
}
function cgToggleHelp() {
  const el = document.getElementById('cgHelp');
  el.style.display = el.style.display === 'none' ? 'grid' : 'none';
}
function cgRelayout() {
  if (!_cgCy) return;
  _cgCy.resize();
  try { _cgCy.layout({ name: 'fcose', animate: true, quality: 'default', nodeRepulsion: 4500 }).run(); }
  catch (e) { _cgCy.layout({ name: 'cose', animate: true }).run(); }
  setTimeout(() => { if (_cgCy) _cgCy.fit(undefined, 40); }, 50);
}
function cgFit() { if (_cgCy) { _cgCy.resize(); _cgCy.fit(undefined, 40); } }

async function cgLoad(force) {
  const minSim = parseFloat(document.getElementById('cgMinSim').value);
  const topK = parseInt(document.getElementById('cgTopK').value, 10);
  const status = document.getElementById('cgStatus');
  status.textContent = 'Loading federated graph...';
  const busyLabel = force ? 'Rebuilding federated graph (embedding facts)...' : 'Loading federated graph...';
  const busyId = oracleBusy(busyLabel, { panel: 'crossgraph' });
  let data;
  try {
    const qs = `?min_sim=${minSim}&top_k=${topK}` + (force ? '&force=1' : '');
    data = await api('/api/graph/federated' + qs);
  } catch (e) { status.textContent = 'Graph load failed: ' + e; oracleIdle(busyId); return; }

  const s = data.stats || {};
  status.textContent = `${s.projects || 0} projects · ${s.total_nodes || 0} nodes · within=${s.within_project || 0}, cross=${s.cross_similar || 0}, insight=${s.linked_via_insight || 0} · sim=${s.similarity_method || 'n/a'}`;

  // Legend
  const leg = document.getElementById('cgProjectLegend');
  leg.innerHTML = '';
  (data.projects || []).forEach(p => {
    const col = cgColorFor(p.slug);
    const chip = document.createElement('div');
    chip.style.cssText = 'display:flex;align-items:center;gap:4px;font-size:10px;background:var(--bg3,var(--bg2));padding:2px 6px;border-radius:10px';
    chip.innerHTML = `<span style="width:8px;height:8px;border-radius:50%;background:${col};display:inline-block"></span><span>${p.name} (${p.fact_count})</span>`;
    leg.appendChild(chip);
  });

  const elements = [
    ...(data.nodes || []).map(n => ({
      data: {
        id: n.id,
        label: n.label || n.id,
        project: n.project,
        category: n.category,
        relevance: n.relevance || 0,
      },
    })),
    ...(data.edges || [])
      .filter(e => e.src && e.dst && !e.src.startsWith('project:') && !e.dst.startsWith('project:'))
      .map((e, i) => ({
        data: {
          id: 'e' + i,
          source: e.src,
          target: e.dst,
          type: e.type,
          scope: e.scope,
          weight: e.weight || 1,
        },
      })),
  ];
  // Drop edges referencing missing nodes (defensive — e.g. orphaned refs)
  const _nodeIds = new Set(elements.filter(x => !x.data.source).map(x => x.data.id));
  for (let i = elements.length - 1; i >= 0; i--) {
    const d = elements[i].data;
    if (d.source && (!_nodeIds.has(d.source) || !_nodeIds.has(d.target))) elements.splice(i, 1);
  }

  if (!_cgCy && window.cytoscape) {
    _cgCy = cytoscape({
      container: document.getElementById('cgCanvas'),
      elements,
      style: [
        { selector: 'node', style: {
          'background-color': ele => cgColorFor(ele.data('project')),
          'label': 'data(label)',
          'color': '#e8eaed', 'font-size': 9,
          'text-wrap': 'ellipsis', 'text-max-width': 120,
          'text-valign': 'bottom', 'text-margin-y': 4,
          'width': ele => 12 + Math.min(22, (ele.data('relevance') || 0) * 2),
          'height': ele => 12 + Math.min(22, (ele.data('relevance') || 0) * 2),
          'border-width': 1, 'border-color': '#1a1a1a',
        }},
        { selector: 'node:selected', style: { 'border-width': 3, 'border-color': '#ffd54f' } },
        { selector: 'edge', style: {
          'curve-style': 'bezier',
          'width': ele => Math.max(0.5, Math.min(4, (ele.data('weight') || 1) * 1.5)),
          'opacity': 0.6,
          'line-color': ele => {
            const sc = ele.data('scope');
            if (sc === 'cross_similar') return '#b388ff';
            if (sc === 'linked_via_insight') return '#4c8bf5';
            return '#9aa0a6';
          },
          'line-style': ele => ele.data('scope') === 'cross_similar' ? 'dashed' : 'solid',
          'target-arrow-shape': 'none',
        }},
      ],
    });
    _cgCy.on('tap', 'node', evt => cgShowDetail(evt.target.data('id')));
  } else if (_cgCy) {
    _cgCy.elements().remove();
    _cgCy.add(elements);
  }
  cgRelayout();
  oracleIdle(busyId);
}

async function cgShowDetail(nodeId) {
  const el = document.getElementById('cgDetail');
  el.innerHTML = '<div style="color:var(--text2)">Loading...</div>';
  try {
    const d = await api('/api/graph/federated/node/' + encodeURIComponent(nodeId));
    const n = d.node || {};
    const col = cgColorFor(n.project);
    const neighbors = d.neighbors || [];
    const crossN = neighbors.filter(x => x.scope === 'cross_similar');
    const withinN = neighbors.filter(x => x.scope === 'within_project');
    el.innerHTML = `
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
        <span style="width:10px;height:10px;border-radius:50%;background:${col};display:inline-block"></span>
        <span style="font-size:10px;color:var(--text2)">${n.project || ''}</span>
        <span style="margin-left:auto;font-size:10px;color:var(--text3,var(--text2))">${n.category || 'general'}</span>
      </div>
      <div style="color:var(--text);font-size:12px;line-height:1.45;margin-bottom:8px">${(n.text || n.label || '').replace(/</g,'&lt;')}</div>
      <div style="font-family:monospace;font-size:10px;color:var(--text3,var(--text2));margin-bottom:10px">recalls=${n.relevance || 0} · confidence=${(n.confidence ?? 1).toFixed(2)}</div>
      <div style="font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--text2);margin-bottom:4px">Cross-project similar (${crossN.length})</div>
      <div style="display:flex;flex-direction:column;gap:3px;margin-bottom:10px;max-height:180px;overflow-y:auto">
        ${crossN.map(x => `<div style="padding:4px 6px;background:var(--bg3,var(--bg));border-radius:4px;cursor:pointer" onclick="cgShowDetail('${x.id}')">
          <div style="font-size:10px;color:${cgColorFor(x.project)}">${x.project || ''}</div>
          <div style="font-size:11px">${(x.label || '').replace(/</g,'&lt;')}</div>
          <div style="font-family:monospace;font-size:9px;color:var(--text3,var(--text2))">w=${(x.weight || 0).toFixed(2)}</div>
        </div>`).join('') || '<div style="font-size:10px;color:var(--text3)">None above threshold.</div>'}
      </div>
      <div style="font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--text2);margin-bottom:4px">Within-project (${withinN.length})</div>
      <div style="font-size:10px;color:var(--text3,var(--text2))">${withinN.length ? withinN.length + ' edges' : 'none'}</div>
    `;
  } catch (e) {
    el.textContent = 'Detail failed: ' + e;
  }
}

async function cgRebuild() {
  const btn = event?.target; if (btn) btn.disabled = true;
  await withBusy('Rebuilding federated graph...', async () => {
    try { await api('/api/graph/federated/rebuild', { method: 'POST', body: {} }); await cgLoad(true); }
    catch (e) { alert('Rebuild failed: ' + e); }
  }, { panel: 'crossgraph' });
  if (btn) btn.disabled = false;
}

async function cgGenerateInsights() {
  const btn = document.getElementById('cgGenBtn');
  btn.disabled = true; btn.textContent = 'Generating...';
  await withBusy('Oracle is thinking — generating cross-project insights via LLM...', async () => {
    try {
      const r = await api('/api/insights/cross', { method: 'POST', body: {} });
      alert(`Generated ${r.generated || 0} cross-project insights.`);
    } catch (e) { alert('Generation failed: ' + e); }
  }, { panel: 'crossgraph' });
  btn.disabled = false; btn.textContent = 'Generate Cross-Insights';
}

document.getElementById('cgMinSim').addEventListener('change', () => cgLoad());
document.getElementById('cgTopK').addEventListener('change', () => cgLoad());
// Hide main initially since chat is the default tab
document.querySelector('main').style.display = 'none';

// ═══════════════════════════════════════════════════════════
