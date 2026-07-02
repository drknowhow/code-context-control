// oracle/ui/chat/input.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Slash command system ──
let chatCmdRegistry = {};
let chatCmdOverlayIdx = -1;
let chatConvState = {};

async function chatLoadCommands() {
  try {
    const data = await api('/api/chat/commands');
    chatCmdRegistry = data.commands || {};
  } catch { /* use empty */ }
}

function chatOnInput(el) {
  chatAutoResize(el);
  chatUpdateCmdOverlay();
  chatUpdateGhostHint();
}

function chatAutoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}

function chatUpdateCmdOverlay() {
  const input = document.getElementById('chatInput');
  const overlay = document.getElementById('chatCmdOverlay');
  const text = input.value;

  if (!text.startsWith('/') || text.includes('\n') || chatStreaming) {
    overlay.classList.remove('open');
    chatCmdOverlayIdx = -1;
    return;
  }

  const typed = text.slice(1).split(' ')[0].toLowerCase();
  const hasSpace = text.indexOf(' ') > 0;

  // If command is fully typed with space (entering args), hide overlay
  if (hasSpace && chatCmdRegistry[typed]) {
    overlay.classList.remove('open');
    chatCmdOverlayIdx = -1;
    return;
  }

  // Build list of all items (commands + team)
  const allItems = [];
  
  // Standard commands
  Object.entries(chatCmdRegistry).forEach(([name, info]) => {
    if (!typed || name.startsWith(typed)) {
      allItems.push({ type: 'cmd', name, info });
    }
  });

  // Team agents
  if (window.oracleAgents) {
    window.oracleAgents.forEach(a => {
      if (a.active && (!typed || a.id.startsWith(typed) || a.name.toLowerCase().startsWith(typed))) {
        allItems.push({ type: 'agent', name: a.id, info: { args: '', desc: 'Delegate to ' + a.name } });
      }
    });
  }

  if (allItems.length === 0) {
    overlay.classList.remove('open');
    chatCmdOverlayIdx = -1;
    return;
  }

  // Sort and render with groups
  allItems.sort((a, b) => a.name.localeCompare(b.name));
  
  let html = '';
  let lastType = null;
  allItems.forEach((item, i) => {
    if (item.type !== lastType) {
      const label = item.type === 'cmd' ? 'Commands' : 'Active Team';
      html += `<div class="cmd-group">${label}</div>`;
      lastType = item.type;
    }
    html += `
      <div class="cmd-row ${i === chatCmdOverlayIdx ? 'active' : ''}"
           onmousedown="chatSelectCmd('${item.name}', '${item.type}')" 
           onmouseenter="chatCmdOverlayIdx=${i};chatUpdateCmdOverlay()">
        <span class="cmd-name">/${item.name}</span>
        <span class="cmd-args">${esc(item.info.args || '')}</span>
        <span class="cmd-desc">${esc(item.info.desc || '')}</span>
      </div>
    `;
  });

  overlay.innerHTML = html;
  overlay.classList.add('open');
}

function chatSelectCmd(name, type = 'cmd') {
  const input = document.getElementById('chatInput');
  if (type === 'agent') {
    input.value = `Ask ${name} to `;
  } else {
    const info = chatCmdRegistry[name];
    input.value = '/' + name + (info && info.args ? ' ' : '');
  }
  input.focus();
  chatUpdateCmdOverlay();
  chatUpdateGhostHint();
}

function chatUpdateGhostHint() {
  const input = document.getElementById('chatInput');
  const ghost = document.getElementById('chatGhost');
  const text = input.value;

  if (!text.startsWith('/') || !text.includes(' ')) {
    ghost.innerHTML = '';
    return;
  }

  const parts = text.split(' ');
  const cmdName = parts[0].slice(1).toLowerCase();
  const info = chatCmdRegistry[cmdName];
  if (!info || !info.args) {
    ghost.innerHTML = '';
    return;
  }

  const argTyped = parts.slice(1).join(' ');
  if (argTyped.length > 0) {
    ghost.innerHTML = '';
    return;
  }

  // Show ghost hint: invisible text matching input + faint hint
  ghost.innerHTML = `<span class="chat-ghost-visible">${esc(text)}</span><span class="chat-ghost-hint">${esc(info.args)}</span>`;
}

function chatInputKeydown(e) {
  const overlay = document.getElementById('chatCmdOverlay');
  const isOpen = overlay.classList.contains('open');

  if (isOpen) {
    const rows = overlay.querySelectorAll('.cmd-row');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      chatCmdOverlayIdx = Math.min(chatCmdOverlayIdx + 1, rows.length - 1);
      chatUpdateCmdOverlay();
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      chatCmdOverlayIdx = Math.max(chatCmdOverlayIdx - 1, 0);
      chatUpdateCmdOverlay();
      return;
    }
    if ((e.key === 'Enter' || e.key === 'Tab') && chatCmdOverlayIdx >= 0 && chatCmdOverlayIdx < rows.length) {
      e.preventDefault();
      const row = rows[chatCmdOverlayIdx];
      const name = row.querySelector('.cmd-name').textContent.slice(1);
      
      // Determine type from context
      let type = 'cmd';
      let prev = row.previousElementSibling;
      while (prev) {
        if (prev.classList.contains('cmd-group')) {
          if (prev.textContent.includes('Team')) type = 'agent';
          break;
        }
        prev = prev.previousElementSibling;
      }
      
      chatSelectCmd(name, type);
      return;
    }
    if (e.key === 'Escape') {
      overlay.classList.remove('open');
      chatCmdOverlayIdx = -1;
      return;
    }
  }

  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    chatSendMessage();
  }
}

// ── State pills ──
function chatUpdateStatePills(state) {
  chatConvState = state || {};
  const bar = document.getElementById('chatStateBar');
  const pills = [];

  const focused = state.focused_projects || [];
  if (focused.length > 0) {
    const names = focused.map(p => esc(p.name)).join(', ');
    pills.push(`<span class="state-pill state-pill-accent"><span class="pill-label">project</span> ${names}</span>`);
  }
  if (state.model) {
    pills.push(`<span class="state-pill"><span class="pill-label">model</span> ${esc(state.model)}</span>`);
  }
  const depth = state.depth || 'normal';
  const depthNext = { brief: 'normal', normal: 'deep', deep: 'brief' };
  pills.push(`<span class="state-pill" style="cursor:pointer" onclick="chatCycleDepth('${depthNext[depth] || 'normal'}')" title="Click to cycle depth">
    <span class="pill-label">depth</span> ${esc(depth)}
  </span>`);

  if (pills.length > 0) {
    bar.innerHTML = pills.join('');
    bar.classList.add('active');
  } else {
    bar.innerHTML = '';
    bar.classList.remove('active');
  }
}

async function chatCycleDepth(depth) {
  if (!chatCurrentConvId) { chatShowToast('Start a conversation first', 'error'); return; }
  try {
    const result = await api('/api/chat/command', {
      method: 'POST',
      body: { conversation_id: chatCurrentConvId, command: '/depth ' + depth },
    });
    if (result.state) chatUpdateStatePills(result.state);
    chatShowToast('Depth set to ' + depth);
  } catch (e) {
    chatShowToast('Failed: ' + e.message, 'error');
  }
}

// ── Command result rendering ──
function chatAppendCommandResult(result) {
  chatHideWelcome();
  const container = document.getElementById('chatMessages');

  if (result.command === 'health' && result.results) {
    // Render health cards
    const div = document.createElement('div');
    div.className = 'msg msg-command';
    let html = '<div class="cmd-title">/health</div><div class="cmd-body">';
    for (const r of result.results) {
      const name = r.project_path ? r.project_path.split(/[\\/]/).pop() : '?';
      const statusColor = r.status === 'ok' ? 'var(--green)' : r.status === 'warning' ? 'var(--yellow)' : 'var(--red)';
      const issues = (r.issues || []).filter(i => i.severity !== 'info');
      html += `<div class="health-card">
        <div class="health-card-header">
          <span style="width:8px;height:8px;border-radius:50%;background:${statusColor};display:inline-block"></span>
          <span class="health-card-name">${esc(name)}</span>
          <span class="health-card-status" style="color:${statusColor}">${esc(r.status || 'unknown')}</span>
        </div>
        <div style="font-size:11px;color:var(--text2)">Facts: ${r.fact_stats?.total || 0} &middot; Edges: ${r.graph_stats?.total_edges || 0}</div>
        ${issues.length ? '<div class="health-card-issues">' + issues.map(i => `<div>&bull; ${esc(i.message)}</div>`).join('') + '</div>' : ''}
      </div>`;
    }
    html += '</div>';
    div.innerHTML = html;
    container.appendChild(div);
  } else if (result.command === 'clear' && result.ok) {
    chatClearMessages();
    if (result.state) chatUpdateStatePills(result.state);
    chatScrollToBottom(true);
    return;
  } else {
    const div = document.createElement('div');
    div.className = 'msg msg-command';
    const icon = result.ok ? '\u2713' : '\u2717';
    const color = result.ok ? 'var(--green)' : 'var(--red)';
    div.innerHTML = `
      <div class="cmd-title" style="color:${color}">${icon} /${esc(result.command || '?')}</div>
      <div class="cmd-body">${renderMarkdown(result.message || '')}</div>
    `;
    container.appendChild(div);
  }

  if (result.state) chatUpdateStatePills(result.state);
  chatScrollToBottom(true);
}

function chatSendSuggested(text) {
  document.getElementById('chatInput').value = text;
  chatSendMessage();
}

