// oracle/ui/header.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Header status ──
// ═══════════════════════════════════════════════════════════
function toggleStatusDD() {
  const dd = document.getElementById('statusDD');
  dd.classList.toggle('open');
}
// Close dropdown on outside click
document.addEventListener('click', (e) => {
  const wrap = document.querySelector('.status-wrap');
  if (wrap && !wrap.contains(e.target)) document.getElementById('statusDD').classList.remove('open');
});

async function refreshHeader() {
  let ollamaOk = false, reviewOk = false, hubOk = false;
  let model = '?', modelVerified = false;

  // Fetch all status from Oracle's own endpoints (no cross-origin)
  try {
    const [health, ollama, review] = await Promise.all([
      api('/api/health'),
      api('/api/ollama/status').catch(() => ({})),
      api('/api/review/status').catch(() => ({})),
    ]);

    // Ollama
    ollamaOk = !!health.ollama_available;
    model = health.model || '?';
    // model_verified is a tri-state: null=verifying, true=ok, false=failed
    const mvRaw = health.model_verified;
    const mvStatus = mvRaw === true ? 'verified' : mvRaw === false ? 'failed' : 'verifying';
    modelVerified = mvRaw === true;
    document.getElementById('ddOllamaDot').className = 'dot ' + (ollamaOk ? 'dot-ok' : 'dot-err');
    document.getElementById('ddOllamaVal').textContent = ollamaOk ? 'connected' : 'unreachable';
    const mvSuffix = !ollamaOk ? '' : mvStatus === 'verified' ? ' \u2713' : mvStatus === 'failed' ? ' \u2717 (verify failed)' : ' (verifying...)';
    document.getElementById('ddModelInfo').textContent = 'Model: ' + model + mvSuffix;

    // Review agent
    reviewOk = !!review.running;
    document.getElementById('ddReviewDot').className = 'dot ' + (reviewOk ? 'dot-ok' : 'dot-off');
    document.getElementById('ddReviewVal').textContent = reviewOk ? 'running' : 'stopped';
    document.getElementById('ddReviewInfo').textContent = review.last_run
      ? 'Last run: ' + timeAgo(new Date(review.last_run).getTime()) + ' \u00b7 ' + (review.projects_tracked||0) + ' project(s)'
      : 'No review yet';

    // Hub (checked server-side by Oracle, no CORS issue)
    hubOk = !!health.hub_available;
    document.getElementById('ddHubDot').className = 'dot ' + (hubOk ? 'dot-ok' : 'dot-err');
    document.getElementById('ddHubVal').textContent = hubOk ? 'connected' : 'unreachable';
  } catch { /* ignore */ }

  // Aggregate status pill
  const okCount = [ollamaOk, reviewOk, hubOk].filter(Boolean).length;
  const dot = document.getElementById('statusDot');
  const label = document.getElementById('statusLabel');
  if (okCount === 3) {
    dot.className = 'dot dot-ok';
    label.textContent = 'All systems OK';
  } else if (okCount === 0) {
    dot.className = 'dot dot-err';
    label.textContent = 'All offline';
  } else {
    dot.className = 'dot dot-warn';
    label.textContent = okCount + '/3 online';
  }
}

// ═══════════════════════════════════════════════════════════
