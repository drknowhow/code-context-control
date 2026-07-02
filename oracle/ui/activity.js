// oracle/ui/activity.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Activity Digest ──
// ═══════════════════════════════════════════════════════════
async function loadScheduledDigestBanner() {
  const el = document.getElementById('actScheduled');
  if (!el) return;
  try {
    const latest = await api('/api/activity/digest/latest');
    if (latest && latest.generated_at) {
      el.style.display = '';
      el.textContent = 'Last scheduled digest: ' +
        latest.generated_at.replace('T', ' ').slice(0, 19) + ' UTC';
    } else {
      el.style.display = 'none';
    }
  } catch { el.style.display = 'none'; }
}

async function loadActivity() {
  loadScheduledDigestBanner();
  const dateEl = document.getElementById('actDate');
  if (dateEl && !dateEl.value) dateEl.value = new Date().toISOString().slice(0, 10);
  const date = dateEl ? dateEl.value : '';
  const narrate = document.getElementById('actNarrate')?.checked ? 'true' : 'false';
  const btn = document.getElementById('btnLoadActivity');
  if (btn) btn.disabled = true;
  try {
    const qs = new URLSearchParams({ date, narrate }).toString();
    renderActivity(await api('/api/activity/digest?' + qs));
  } catch (e) {
    renderActivity({ error: (e && e.message) || 'Failed to load activity.' });
  } finally {
    if (btn) btn.disabled = false;
  }
}

function _actCard(label, value) {
  return `<div class="card" style="flex:1;min-width:110px;text-align:center">
    <div style="font-size:22px;font-weight:700">${value}</div>
    <div style="font-size:11px;color:var(--text2);text-transform:uppercase;letter-spacing:.04em">${label}</div>
  </div>`;
}

function renderActivity(data) {
  const summary = document.getElementById('actSummary');
  const projects = document.getElementById('actProjects');
  const empty = document.getElementById('actEmpty');
  const narrEl = document.getElementById('actNarrative');
  if (!data || data.error) {
    summary.innerHTML = ''; projects.innerHTML = ''; narrEl.style.display = 'none';
    empty.style.display = ''; empty.textContent = (data && data.error) || 'Failed to load activity.';
    return;
  }
  const t = data.totals || {};
  summary.innerHTML = [
    _actCard('Projects', t.projects_active || 0),
    _actCard('Sessions', t.sessions || 0),
    _actCard('Tool Calls', t.tool_calls || 0),
    _actCard('Edits', t.edits || 0),
    _actCard('Git', t.git_mutations || 0),
    _actCard('Cost $', (t.cost_usd || 0).toFixed(2)),
  ].join('');

  if (data.narrative) {
    narrEl.style.display = '';
    narrEl.innerHTML = `<div style="font-size:11px;color:var(--text2);text-transform:uppercase;margin-bottom:6px">${esc(data.window && data.window.label || '')}</div>
      <p style="font-size:13px;line-height:1.6;white-space:pre-wrap">${esc(data.narrative)}</p>`;
  } else {
    narrEl.style.display = 'none';
  }

  const rows = data.projects || [];
  if (!rows.length) {
    projects.innerHTML = ''; empty.style.display = '';
    empty.textContent = 'No activity recorded for ' + (data.window && data.window.label || 'this day') + '.';
    return;
  }
  empty.style.display = 'none';
  projects.innerHTML = `
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="text-align:left;color:var(--text2);font-size:11px;text-transform:uppercase">
        <th style="padding:6px">Project</th><th style="padding:6px">Sessions</th>
        <th style="padding:6px">Tools</th><th style="padding:6px">Edits</th>
        <th style="padding:6px">Git</th><th style="padding:6px">Tokens</th>
        <th style="padding:6px">Cost $</th><th style="padding:6px">Last activity</th>
      </tr></thead>
      <tbody>${rows.map(p => `<tr style="border-top:1px solid var(--border)">
        <td style="padding:6px;font-weight:600">${esc(p.name)}</td>
        <td style="padding:6px">${(p.sessions || []).length}</td>
        <td style="padding:6px">${p.tool_calls || 0}</td>
        <td style="padding:6px">${p.edits || 0}</td>
        <td style="padding:6px">${p.git_mutations || 0}</td>
        <td style="padding:6px">${(((p.tokens || {}).input || 0) + ((p.tokens || {}).output || 0)).toLocaleString()}</td>
        <td style="padding:6px">${(p.cost_usd || 0).toFixed(2)}</td>
        <td style="padding:6px;color:var(--text2);font-size:11px">${esc((p.last_activity || '').replace('T', ' ').slice(0, 19))}</td>
      </tr>`).join('')}</tbody>
    </table>`;
}

// ═══════════════════════════════════════════════════════════
