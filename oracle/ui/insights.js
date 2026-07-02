// oracle/ui/insights.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Insights ──
// ═══════════════════════════════════════════════════════════
async function loadInsights() {
  try {
    const data = await api('/api/insights');
    renderInsights(data.insights || []);
  } catch { renderInsights([]); }
}

function renderInsights(insights) {
  const el = document.getElementById('insightsList');
  const empty = document.getElementById('insightsEmpty');
  if (!insights.length) { el.innerHTML = ''; empty.style.display = ''; return; }
  empty.style.display = 'none';
  el.innerHTML = insights.map(i => `
    <div class="card">
      <div class="card-header">
        <span class="insight-type type-${i.type||'pattern'}">${i.type||'pattern'}</span>
        <span class="card-meta">confidence: ${(i.confidence||0).toFixed(2)}</span>
      </div>
      <p style="font-size:13px;line-height:1.5">${esc(i.text)}</p>
      <div style="margin-top:6px;font-size:11px;color:var(--text2)">
        Projects: ${(i.source_projects||[]).map(p => esc(p.split(/[/\\]/).pop())).join(', ')}
        ${i.tags?.length ? ' \u00b7 Tags: ' + i.tags.join(', ') : ''}
      </div>
      <div style="margin-top:8px"><button class="btn btn-ghost btn-sm" onclick="dismissInsight('${i.id}')">Dismiss</button></div>
    </div>
  `).join('');
}

async function generateInsights() {
  const btn = document.getElementById('btnGenInsights');
  btn.disabled = true;
  await tracked('Generating insights', async () => {
    const result = await api('/api/insights/generate', { method: 'POST' });
    if (result.error) throw new Error(result.error);
    loadInsights();
    return result;
  }, { successMsg: 'Insights generated' }).catch(() => {});
  btn.disabled = false;
}

async function dismissInsight(id) {
  await tracked('Dismiss insight', async () => {
    await api('/api/insights/dismiss', { method: 'POST', body: { id } });
    loadInsights();
  }, { successMsg: 'Insight dismissed', silent: false });
}

// ═══════════════════════════════════════════════════════════
