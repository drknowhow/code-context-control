// oracle/ui/settings.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Settings & Agents ──
// ═══════════════════════════════════════════════════════════
window.oracleConfig = {};
window.oracleAgents = [];

async function loadSettings() {
  try {
    const [cfg, ollama] = await Promise.all([api('/api/config'), api('/api/ollama/status')]);
    window.oracleConfig = cfg;
    window.oracleAgents = cfg.agents || [];
    renderAgents();
    
    document.getElementById('cfgOllamaUrl').value = cfg.ollama_base_url || '';
    const keyInput = document.getElementById('cfgApiKey');
    const keyCheck = document.getElementById('cfgApiKeyEdit');
    const hasKey = !!cfg.ollama_api_key;
    keyInput.value = hasKey ? cfg.ollama_api_key : '';
    keyInput.placeholder = hasKey ? '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022 (key set)' : 'OLLAMA_API_KEY (or set env var)';
    keyInput.disabled = hasKey;
    keyCheck.checked = false;
    keyCheck.parentElement.style.display = hasKey ? '' : 'none';
    document.getElementById('cfgHubUrl').value = cfg.hub_url || '';
    document.getElementById('cfgInterval').value = Math.round((cfg.review_interval_seconds || 1800) / 60);
    document.getElementById('cfgDigestEnabled').checked = !!cfg.digest_enabled;
    document.getElementById('cfgDigestInterval').value = Math.round((cfg.digest_interval_seconds || 86400) / 3600);
    applyTheme(cfg.theme || 'dark');
    // Hub link
    const hubUrl = cfg.hub_url || 'http://localhost:3330';
    const hubEl = document.getElementById('hubLink');
    hubEl.href = hubUrl;
    hubEl.style.display = '';

    const modelInput = document.getElementById('cfgModel');
    const datalist = document.getElementById('cfgModelList');
    datalist.innerHTML = '';
    const models = ollama.models || [];
    // Always include current model in suggestions
    const currentModel = cfg.model || 'gemma4:31b-cloud';
    if (currentModel && !models.includes(currentModel)) models.unshift(currentModel);
    models.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m;
      datalist.appendChild(opt);
    });
    modelInput.value = currentModel;
  } catch { /* ignore */ }
}

function toggleApiKeyEdit() {
  const keyInput = document.getElementById('cfgApiKey');
  const checked = document.getElementById('cfgApiKeyEdit').checked;
  keyInput.disabled = !checked;
  if (checked) {
    keyInput.value = '';
    keyInput.placeholder = 'Enter new API key';
    keyInput.focus();
  } else {
    // Revert — reload will restore the masked value
    loadSettings();
  }
}

async function saveSettings() {
  const keyInput = document.getElementById('cfgApiKey');
  const keyEditing = document.getElementById('cfgApiKeyEdit').checked;
  const apiKeyVal = keyEditing ? keyInput.value : '';
  const cfg = {
    model: document.getElementById('cfgModel').value,
    ollama_base_url: document.getElementById('cfgOllamaUrl').value,
    hub_url: document.getElementById('cfgHubUrl').value,
    review_interval_seconds: parseInt(document.getElementById('cfgInterval').value) * 60,
    digest_enabled: document.getElementById('cfgDigestEnabled').checked,
    digest_interval_seconds: parseInt(document.getElementById('cfgDigestInterval').value) * 3600,
    agents: window.oracleAgents
  };
  if (apiKeyVal) cfg.ollama_api_key = apiKeyVal;
  await tracked('Saving settings', async () => {
    await api('/api/config', { method: 'POST', body: cfg });
    window.oracleConfig = cfg;
    // Update hub link
    const hubEl = document.getElementById('hubLink');
    hubEl.href = cfg.hub_url;
    refreshHeader();
  }, { successMsg: 'Configuration saved' });
}

async function testConnection() {
  await tracked('Testing Ollama', async () => {
    const result = await api('/api/ollama/test', { method: 'POST' });
    const resp = result.response || 'No response';
    toast('Ollama response', resp, resp.includes('No response') ? 'warning' : 'success', 6000);
  }, { silent: true });
}

// ── Discovery API token management ──
window._discoveryKey = null;
window._dkRevealed = false;

function _renderDiscoveryKey(s) {
  window._discoveryKey = s;
  const tok = document.getElementById('dkToken');
  if (!tok) return;
  const reveal = document.getElementById('dkReveal');
  const genBtn = document.getElementById('dkGenBtn');
  const status = document.getElementById('dkStatus');
  if (s.exists) {
    tok.value = s.key || '';
    tok.type = window._dkRevealed ? 'text' : 'password';
    tok.placeholder = '';
    reveal.style.display = '';
    reveal.textContent = window._dkRevealed ? 'Hide' : 'Reveal';
    genBtn.style.display = 'none';
    status.textContent = s.require_auth
      ? 'Token active — required on all /api/discovery requests and the MCP transport.'
      : 'Token set, but auth is DISABLED (api_require_auth=false in config).';
  } else {
    tok.value = '';
    tok.type = 'password';
    tok.placeholder = 'No token yet — click Generate';
    reveal.style.display = 'none';
    genBtn.style.display = '';
    status.textContent = s.require_auth
      ? 'No token yet. Generate one so external LLMs can authenticate.'
      : 'Auth is disabled — external requests are accepted without a token.';
  }
  document.getElementById('dkMcpUrl').value = s.mcp_enabled ? s.mcp_url : s.mcp_url + '  (MCP server disabled)';
  document.getElementById('dkRestBase').value = s.rest_base;
  document.getElementById('dkOpenapi').value = s.openapi_url;
  document.getElementById('dkSnippet').textContent = JSON.stringify(s.snippet, null, 2);
}

async function loadDiscoveryKey() {
  try { _renderDiscoveryKey(await api('/api/apikey')); } catch { /* ignore */ }
}

function toggleDiscoveryReveal() {
  window._dkRevealed = !window._dkRevealed;
  if (window._discoveryKey) _renderDiscoveryKey(window._discoveryKey);
}

async function generateDiscoveryKey() {
  await tracked('Generating token', async () => {
    const s = await api('/api/apikey/generate', { method: 'POST' });
    window._dkRevealed = true;
    _renderDiscoveryKey(s);
  }, { successMsg: 'Discovery API token generated' });
}

async function rotateDiscoveryKey() {
  if (!confirm('Rotate the Discovery API token? Clients using the old token will stop working until updated.')) return;
  await tracked('Rotating token', async () => {
    const s = await api('/api/apikey/rotate', { method: 'POST' });
    window._dkRevealed = true;
    _renderDiscoveryKey(s);
  }, { successMsg: 'Token rotated — update your clients' });
}

async function clearDiscoveryKey() {
  if (!confirm('Delete the Discovery API token? External LLMs cannot authenticate until you generate a new one.')) return;
  await tracked('Clearing token', async () => {
    const s = await api('/api/apikey/clear', { method: 'POST' });
    window._dkRevealed = false;
    _renderDiscoveryKey(s);
  }, { successMsg: 'Token cleared' });
}

async function copyDiscoveryToken() {
  const s = window._discoveryKey;
  if (!s || !s.exists || !s.key) { toast('No token', 'Generate a token first.', 'warning'); return; }
  await _dkCopy(s.key, 'Token copied');
}

async function copyMcpSnippet() {
  const s = window._discoveryKey;
  if (!s) return;
  await _dkCopy(JSON.stringify(s.snippet, null, 2), '.mcp.json copied');
}

async function _dkCopy(text, okMsg) {
  try { await navigator.clipboard.writeText(text); toast(okMsg, '', 'success', 2000); }
  catch { toast('Copy failed', 'Select the field and copy manually.', 'error'); }
}

// ═══════════════════════════════════════════════════════════
