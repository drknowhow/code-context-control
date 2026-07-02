// oracle/ui/suggestions.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Suggestions ──
// ═══════════════════════════════════════════════════════════
async function loadSuggestions() {
  try {
    const data = await api('/api/suggestions');
    renderSuggestions(data);
  } catch { renderSuggestions([]); }
}

function renderSuggestions(suggestions) {
  const el = document.getElementById('suggestionsList');
  const empty = document.getElementById('suggestionsEmpty');
  if (!suggestions.length) { el.innerHTML = ''; empty.style.display = ''; return; }
  empty.style.display = 'none';
  el.innerHTML = suggestions.map(s => `
    <div class="card suggestion">
      <div class="card-header">
        <span class="card-title">${esc(s.type)} \u2014 ${esc(s.project_path?.split(/[/\\]/).pop() || '?')}</span>
        <span class="card-meta">${timeAgo(new Date(s.created_at).getTime())}</span>
      </div>
      <pre style="font-size:11px;color:var(--text2);white-space:pre-wrap;max-height:120px;overflow:auto">${esc(JSON.stringify(s.data, null, 2))}</pre>
      <div class="suggestion-actions">
        <button class="btn btn-primary btn-sm" onclick="approveSuggestion('${s.id}')">Approve</button>
        <button class="btn btn-ghost btn-sm" onclick="dismissSuggestion('${s.id}')">Dismiss</button>
      </div>
    </div>
  `).join('');
}

async function approveSuggestion(id) {
  await tracked('Approving suggestion', async () => {
    const result = await api('/api/suggestions/approve', { method: 'POST', body: { id } });
    if (result.error) throw new Error(result.error);
    loadSuggestions();
  }, { successMsg: 'Suggestion applied to project memory' });
}

async function dismissSuggestion(id) {
  await tracked('Dismiss suggestion', async () => {
    await api('/api/suggestions/dismiss', { method: 'POST', body: { id } });
    loadSuggestions();
  }, { successMsg: 'Suggestion dismissed' });
}

// ═══════════════════════════════════════════════════════════
