// oracle/ui/agents.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Agent Management ──
// ═══════════════════════════════════════════════════════════
function renderAgents() {
  const container = document.getElementById('agentsList');
  const empty = document.getElementById('agentsEmpty');
  const agents = window.oracleAgents || [];
  
  if (agents.length === 0) {
    container.innerHTML = '';
    empty.style.display = 'block';
    return;
  }
  
  empty.style.display = 'none';
  container.innerHTML = agents.map(a => `
    <div class="card" style="display:flex;justify-content:space-between;align-items:center;border-color:${a.active ? 'var(--accent)' : 'var(--border)'};opacity:${a.active ? '1' : '0.6'}">
      <div style="flex:1;min-width:0;margin-right:16px">
        <div style="font-weight:600;font-size:14px;margin-bottom:4px;display:flex;align-items:center;gap:8px">
          ${esc(a.name)}
          <span style="font-size:10px;font-family:monospace;background:var(--bg3);padding:2px 6px;border-radius:4px;color:var(--text2)">${esc(a.backend && a.backend !== 'ollama' ? a.backend : a.model)}</span>
        </div>
        <div style="font-size:12px;color:var(--text2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(a.description)}">${esc(a.description)}</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-shrink:0">
        <button class="btn btn-ghost btn-sm" onclick="editAgent('${esc(a.id)}')">Edit</button>
        <button class="btn btn-danger btn-sm" onclick="deleteAgent('${esc(a.id)}')">Delete</button>
      </div>
    </div>
  `).join('');
}

function openAgentModal(id = null) {
  const m = document.getElementById('agentModal');
  const title = document.getElementById('agentModalTitle');
  const nameInput = document.getElementById('agentName');
  const descInput = document.getElementById('agentDesc');
  const promptInput = document.getElementById('agentPrompt');
  const modelInput = document.getElementById('agentModel');
  const activeInput = document.getElementById('agentActive');
  const idInput = document.getElementById('agentId');

  if (id) {
    const a = window.oracleAgents.find(x => x.id === id);
    if (!a) return;
    title.textContent = 'Edit Agent';
    idInput.value = a.id;
    nameInput.value = a.name || '';
    descInput.value = a.description || '';
    promptInput.value = a.system_prompt || '';
    modelInput.value = a.model || '';
    document.getElementById('agentBackend').value = a.backend || 'ollama';
    activeInput.checked = a.active !== false;
  } else {
    title.textContent = 'Add Custom Agent';
    idInput.value = 'agent_' + Math.random().toString(36).substr(2, 6);
    nameInput.value = '';
    descInput.value = '';
    promptInput.value = '';
    modelInput.value = window.oracleConfig.model || 'gemma4:31b-cloud';
    document.getElementById('agentBackend').value = 'ollama';
    activeInput.checked = true;
  }
  
  m.style.display = 'flex';
}

function closeAgentModal() {
  document.getElementById('agentModal').style.display = 'none';
}

function saveAgent() {
  const id = document.getElementById('agentId').value;
  const name = document.getElementById('agentName').value.trim();
  if (!name) return alert('Name is required');

  const agent = {
    id: id,
    name: name,
    description: document.getElementById('agentDesc').value.trim(),
    system_prompt: document.getElementById('agentPrompt').value.trim(),
    model: document.getElementById('agentModel').value.trim(),
    backend: document.getElementById('agentBackend').value,
    active: document.getElementById('agentActive').checked
  };

  const idx = window.oracleAgents.findIndex(x => x.id === id);
  if (idx >= 0) window.oracleAgents[idx] = agent;
  else window.oracleAgents.push(agent);

  renderAgents();
  closeAgentModal();
  saveSettings();
}

function deleteAgent(id) {
  if (!confirm('Are you sure you want to delete this agent?')) return;
  window.oracleAgents = window.oracleAgents.filter(x => x.id !== id);
  renderAgents();
  saveSettings();
}

function editAgent(id) {
  openAgentModal(id);
}

// ═══════════════════════════════════════════════════════════
