// oracle/ui/projects.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Projects ──
// ═══════════════════════════════════════════════════════════
let projectsData = [];
let healthFilter = 'all';
const _detailsCache = {};  // path -> { health, facts, ts }
const _CACHE_TTL = 60000;  // 60s

function _getCached(path) {
  const c = _detailsCache[path];
  if (c && Date.now() - c.ts < _CACHE_TTL) return c;
  return null;
}
function _setCache(path, health, facts) {
  _detailsCache[path] = { health, facts, ts: Date.now() };
}
function _invalidateCache(path) { delete _detailsCache[path]; }

function setHealthFilter(f) {
  healthFilter = f;
  document.querySelectorAll('.filter-chip').forEach(c => c.classList.toggle('active', c.dataset.health === f));
  renderProjects();
}

async function loadProjects() {
  try {
    projectsData = await api('/api/projects');
    _populateTagFilter();
    renderProjects();
  } catch { projectsData = []; renderProjects(); }
}

function _populateTagFilter() {
  const sel = document.getElementById('projTagFilter');
  const current = sel.value;
  const tags = new Set();
  projectsData.forEach(p => (p.tags || []).forEach(t => tags.add(t)));
  const sorted = [...tags].sort();
  sel.innerHTML = '<option value="">All Tags</option>' +
    sorted.map(t => `<option value="${esc(t)}"${t===current?' selected':''}>${esc(t)}</option>`).join('');
}

function _filteredProjects() {
  const q = (document.getElementById('projSearch')?.value || '').toLowerCase();
  const tagFilter = document.getElementById('projTagFilter')?.value || '';
  let list = projectsData;
  if (q) list = list.filter(p => (p.name||'').toLowerCase().includes(q) || (p.path||'').toLowerCase().includes(q) || (p.tags||[]).some(t => t.toLowerCase().includes(q)));
  if (healthFilter !== 'all') list = list.filter(p => (p.health_status || 'unknown') === healthFilter);
  if (tagFilter) list = list.filter(p => (p.tags||[]).some(t => t === tagFilter || t.startsWith(tagFilter + '/')));
  const sort = document.getElementById('projSort')?.value || 'name';
  const healthOrder = { ok: 0, warning: 1, error: 2, unknown: 3 };
  list = [...list].sort((a, b) => {
    if (sort === 'name') return (a.name||'').localeCompare(b.name||'');
    if (sort === 'facts') return (b.fact_count||0) - (a.fact_count||0);
    if (sort === 'reviewed') return (b.last_reviewed||'') < (a.last_reviewed||'') ? -1 : 1;
    if (sort === 'health') return (healthOrder[a.health_status]||3) - (healthOrder[b.health_status]||3);
    return 0;
  });
  return list;
}

function renderProjects() {
  const container = document.getElementById('projectsList');
  const empty = document.getElementById('projectsEmpty');
  const filtered = _filteredProjects();

  // Stats bar
  const total = projectsData.length;
  const totalFacts = projectsData.reduce((s, p) => s + (p.fact_count||0), 0);
  const okCount = projectsData.filter(p => p.health_status === 'ok').length;
  const warnCount = projectsData.filter(p => p.health_status === 'warning').length;
  const errCount = projectsData.filter(p => p.health_status === 'error').length;
  document.getElementById('projStatsBar').innerHTML = `
    <span><span class="stat-val">${total}</span>project${total!==1?'s':''}</span>
    <span><span class="stat-val">${totalFacts}</span>total facts</span>
    <span style="color:var(--green)"><span class="stat-val">${okCount}</span>healthy</span>
    ${warnCount ? `<span style="color:var(--yellow)"><span class="stat-val">${warnCount}</span>warnings</span>` : ''}
    ${errCount ? `<span style="color:var(--red)"><span class="stat-val">${errCount}</span>errors</span>` : ''}
  `;

  if (!projectsData.length) { container.innerHTML = ''; empty.style.display = ''; return; }
  empty.style.display = 'none';

  if (!filtered.length) {
    container.innerHTML = '<div class="empty" style="padding:24px">No projects match filters.</div>';
    return;
  }

  container.innerHTML = filtered.map((p) => {
    const idx = projectsData.indexOf(p);
    const dotColor = p.health_status === 'ok' ? 'var(--green)' : p.health_status === 'warning' ? 'var(--yellow)' : p.health_status === 'error' ? 'var(--red)' : 'var(--muted)';
    const reviewedStr = p.last_reviewed ? timeAgo(new Date(p.last_reviewed).getTime()) : 'never';
    const tagsHtml = (p.tags||[]).map(t => `<span class="tag">${esc(t)}</span>`).join(' ');
    return `
      <div class="proj-card" onclick="toggleDetails(${idx})">
        <div class="proj-card-row">
          <div class="proj-status-dot" style="background:${dotColor}" title="${esc(p.health_status||'unknown')}"></div>
          <div class="proj-info">
            <div class="proj-name">${esc(p.name)}${p.active ? ' <span class="tag tag-active">active</span>' : ''}${p.ide ? ` <span class="tag">${esc(p.ide)}</span>` : ''}</div>
            <div class="proj-path">${esc(p.path)}</div>
            ${tagsHtml ? `<div style="margin-top:3px;display:flex;flex-wrap:wrap;gap:3px">${tagsHtml}</div>` : ''}
          </div>
          <div class="proj-badges">
            <span class="proj-badge proj-badge-facts">${p.fact_count||0} facts</span>
            ${p.health_issues ? `<span class="proj-badge proj-badge-issues">${p.health_issues} issue${p.health_issues!==1?'s':''}</span>` : ''}
            ${!p.has_c3 ? '<span class="proj-badge" style="color:var(--red)">no .c3</span>' : ''}
          </div>
          <div class="proj-reviewed" title="${p.last_reviewed ? 'Reviewed: '+p.last_reviewed : 'Not yet reviewed'}">
            ${reviewedStr}
          </div>
          <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();reviewProject(${idx})" style="flex-shrink:0">Review</button>
        </div>
        <div class="proj-expand" id="projExpand-${idx}"></div>
      </div>
    `;
  }).join('');
}

function _renderDetails(el, p, health, facts) {
  const stats = facts.stats || {};
  const tiers = stats.by_tier || {};
  const cats = stats.by_category || {};
  const catHtml = Object.entries(cats).sort((a,b) => b[1]-a[1]).slice(0,6)
    .map(([k,v]) => `<span class="proj-badge" style="font-size:10px">${esc(k)}: ${v}</span>`).join(' ');
  el.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:12px">
      <div>
        <div style="font-weight:600;margin-bottom:6px;color:var(--text)">Tier Distribution</div>
        <div style="display:flex;gap:10px;margin-bottom:6px">
          <span style="color:var(--accent)">Core: ${tiers.core||0}</span>
          <span style="color:var(--green)">Active: ${tiers.active||0}</span>
          <span style="color:var(--yellow)">Dormant: ${tiers.dormant||0}</span>
          <span style="color:var(--muted)">Ephemeral: ${tiers.ephemeral||0}</span>
        </div>
        <div class="tier-bar" style="margin:4px 0 12px">
          <div class="tier-core" style="flex:${tiers.core||0}"></div>
          <div class="tier-active" style="flex:${tiers.active||0}"></div>
          <div class="tier-dormant" style="flex:${tiers.dormant||0}"></div>
          <div class="tier-ephemeral" style="flex:${tiers.ephemeral||0}"></div>
        </div>
        <div style="font-weight:600;margin-bottom:4px;color:var(--text)">Categories</div>
        <div style="display:flex;flex-wrap:wrap;gap:4px">${catHtml || '<span style="color:var(--text2)">none</span>'}</div>
      </div>
      <div>
        <div style="font-weight:600;margin-bottom:6px;color:var(--text)">Graph</div>
        <div style="color:var(--text2);line-height:1.8">
          ${health.graph_stats?.total_edges||0} edges \u00b7 ${health.graph_stats?.total_nodes||0} nodes<br>
          ${health.graph_stats?.orphaned_edges ? `<span style="color:var(--yellow)">${health.graph_stats.orphaned_edges} orphaned</span>` : '<span style="color:var(--green)">0 orphaned</span>'}
        </div>
        <div style="font-weight:600;margin:8px 0 4px;color:var(--text)">Freshness</div>
        <div style="color:var(--text2)">${health.freshness?.days_since_last_fact != null ? (health.freshness.days_since_last_fact === 0 ? 'Updated today' : health.freshness.days_since_last_fact + ' day' + (health.freshness.days_since_last_fact!==1?'s':'') + ' since last fact') : 'unknown'}</div>
      </div>
    </div>
    ${(health.issues||[]).length ? `<div style="margin-top:10px;font-size:12px"><div style="font-weight:600;margin-bottom:4px;color:var(--text)">Issues</div>` +
      health.issues.map(i => `<div style="padding:2px 0;color:${i.severity==='error'?'var(--red)':i.severity==='warning'?'var(--yellow)':'var(--text2)'}">
        ${i.severity==='error'?'\u2717':i.severity==='warning'?'\u26A0':'\u24D8'} ${esc(i.message)}</div>`).join('') + '</div>' : ''}
    ${p.notes ? `<div style="margin-top:10px;font-size:12px"><div style="font-weight:600;margin-bottom:4px;color:var(--text)">Notes</div><div style="color:var(--text2)">${esc(p.notes)}</div></div>` : ''}
    ${facts.facts?.length ? `<div style="margin-top:10px;font-size:12px"><div style="font-weight:600;margin-bottom:4px;color:var(--text)">Top Facts</div>` +
      facts.facts.slice(0,5).map(f => `<div style="padding:3px 0;border-bottom:1px solid var(--border)">
        <span style="color:var(--accent);font-size:10px;font-weight:600">[${esc(f.category)}]</span> ${esc(f.fact?.substring(0,150))}</div>`).join('') + '</div>' : ''}
  `;
}

async function toggleDetails(idx) {
  const el = document.getElementById('projExpand-' + idx);
  if (!el) return;
  if (el.classList.contains('open')) { el.classList.remove('open'); return; }
  // Close others
  document.querySelectorAll('.proj-expand.open').forEach(e => e.classList.remove('open'));
  el.classList.add('open');
  const p = projectsData[idx];
  const cached = _getCached(p.path);
  if (cached) { _renderDetails(el, p, cached.health, cached.facts); return; }

  el.innerHTML = '<div style="color:var(--text2);font-size:12px">Loading details...</div>';
  busyStart('Loading ' + p.name);
  try {
    const [health, facts] = await Promise.all([
      api('/api/projects/health?path=' + encodeURIComponent(p.path)),
      api('/api/projects/facts?path=' + encodeURIComponent(p.path) + '&limit=10'),
    ]);
    _setCache(p.path, health, facts);
    _renderDetails(el, p, health, facts);
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);font-size:12px">Error loading details: ${esc(e.message)}</div>`;
    toast('Details failed', e.message, 'error');
  } finally {
    busyEnd();
  }
}

async function reviewProject(idx) {
  const p = projectsData[idx];
  if (!p) return;
  _invalidateCache(p.path);
  await tracked('Review: ' + p.name, async () => {
    const result = await api('/api/projects/review', { method: 'POST', body: { path: p.path } });
    const issues = (result.issues || []).length;
    toast('Review: ' + p.name,
      `Health: ${result.status || '?'}` + (issues ? ` \u2014 ${issues} issue(s)` : ' \u2014 no issues'),
      result.status === 'ok' ? 'success' : result.status === 'error' ? 'error' : 'warning', 4000);
  }, { silent: true });
  loadProjects();
}

async function reviewAllProjects() {
  const total = projectsData.length;
  if (!total) return;
  projectsData.forEach(p => _invalidateCache(p.path));
  const progressEl = document.getElementById('reviewAllProgress');
  const barEl = document.getElementById('reviewAllBar');
  const labelEl = document.getElementById('reviewAllLabel');
  const countEl = document.getElementById('reviewAllCount');
  progressEl.style.display = '';
  barEl.style.width = '0%';
  countEl.textContent = `0 / ${total}`;
  labelEl.textContent = 'Reviewing...';
  busyStart('Reviewing all projects');
  let done = 0, errors = 0;
  for (const p of projectsData) {
    labelEl.textContent = `Reviewing ${p.name}...`;
    try {
      await api('/api/projects/review', { method: 'POST', body: { path: p.path } });
    } catch { errors++; }
    done++;
    barEl.style.width = Math.round((done / total) * 100) + '%';
    countEl.textContent = `${done} / ${total}`;
  }
  busyEnd();
  barEl.style.background = errors ? 'var(--yellow)' : 'var(--green)';
  labelEl.textContent = errors ? `Done with ${errors} error(s)` : 'All projects reviewed';
  toast('Review All', `${done} project(s) reviewed` + (errors ? `, ${errors} failed` : ''), errors ? 'warning' : 'success');
  loadProjects();
  setTimeout(() => { progressEl.style.display = 'none'; barEl.style.background = 'var(--accent)'; }, 4000);
}

async function scanProjects() {
  await tracked('Scanning projects', async () => {
    await api('/api/projects/scan', { method: 'POST' });
  }, { successMsg: 'Projects rescanned' });
  loadProjects();
}

// ═══════════════════════════════════════════════════════════
