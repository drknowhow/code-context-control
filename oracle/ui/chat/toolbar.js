// oracle/ui/chat/toolbar.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Copy & Export utilities ──
function chatCopyCode(btn) {
  const code = decodeURIComponent(btn.dataset.code);
  navigator.clipboard.writeText(code);
  btn.textContent = 'Copied!';
  setTimeout(() => btn.textContent = 'Copy', 1500);
}

function chatCopyMsg(btn) {
  const msg = btn.closest('.msg');
  const text = msg ? (msg.dataset.text || msg.innerText) : '';
  navigator.clipboard.writeText(text);
  btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg> Copied`;
  setTimeout(() => {
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg> Copy`;
  }, 1500);
}

function chatCopyAll() {
  const msgs = document.querySelectorAll('#chatMessages .msg');
  const parts = [];
  msgs.forEach(m => {
    const text = m.dataset.text || m.innerText || '';
    if (!text.trim()) return;
    const isUser = m.classList.contains('msg-user');
    const isTool = m.classList.contains('msg-tool');
    if (isTool) {
      const badge = m.querySelector('.tool-badge');
      parts.push('**Tool: ' + (badge ? badge.textContent : 'tool') + '**\n' + text);
    } else {
      parts.push('## ' + (isUser ? 'User' : 'Assistant') + '\n\n' + text);
    }
  });
  if (!parts.length) return;
  navigator.clipboard.writeText(parts.join('\n\n---\n\n'));
  chatShowToast('Conversation copied to clipboard');
}

function chatToggleExport() {
  document.getElementById('exportMenu').classList.toggle('open');
}

async function chatExport(format) {
  document.getElementById('exportMenu').classList.remove('open');
  if (!chatCurrentConvId) { chatShowToast('No conversation selected', 'error'); return; }
  try {
    const msgs = document.querySelectorAll('#chatMessages .msg');
    if (format === 'json') {
      const turns = [];
      msgs.forEach(m => {
        const text = m.dataset.text || m.innerText || '';
        if (!text.trim()) return;
        turns.push({ role: m.classList.contains('msg-user') ? 'user' : 'assistant', text });
      });
      const blob = new Blob([JSON.stringify({ conversation_id: chatCurrentConvId, turns }, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a'); a.href = url; a.download = 'conversation-' + chatCurrentConvId.slice(0, 8) + '.json'; a.click();
      URL.revokeObjectURL(url);
    } else {
      const parts = ['# Conversation ' + chatCurrentConvId.slice(0, 8), ''];
      msgs.forEach(m => {
        const text = m.dataset.text || m.innerText || '';
        if (!text.trim()) return;
        const isUser = m.classList.contains('msg-user');
        parts.push('## ' + (isUser ? 'User' : 'Assistant'), '', text, '', '---', '');
      });
      const blob = new Blob([parts.join('\n')], { type: 'text/markdown' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a'); a.href = url; a.download = 'conversation-' + chatCurrentConvId.slice(0, 8) + '.md'; a.click();
      URL.revokeObjectURL(url);
    }
    chatShowToast('Exported as ' + format);
  } catch (e) { chatShowToast('Export failed: ' + e.message, 'error'); }
}

// Close export dropdown on outside click
document.addEventListener('mousedown', function(e) {
  const menu = document.getElementById('exportMenu');
  if (menu && !e.target.closest('.export-dd')) menu.classList.remove('open');
});

// ── In-conversation search ──
let _chatSearchMatches = [];
let _chatSearchIdx = -1;

let _chatSearchTimer = null;
function chatToolbarSearch(query) {
  clearTimeout(_chatSearchTimer);
  _chatSearchTimer = setTimeout(() => _chatToolbarSearchExec(query), 150);
}

function _chatToolbarSearchExec(query) {
  // Clear previous highlights and merge fragmented text nodes
  document.querySelectorAll('#chatMessages .search-highlight').forEach(el => {
    el.replaceWith(el.textContent);
  });
  document.querySelectorAll('#chatMessages .msg-content, #chatMessages .msg-bubble').forEach(el => {
    el.normalize();
  });
  _chatSearchMatches = [];
  _chatSearchIdx = -1;
  const countEl = document.getElementById('toolbarSearchCount');

  if (!query || query.length < 2) { countEl.textContent = ''; return; }

  const msgs = document.querySelectorAll('#chatMessages .msg');
  const escapedQ = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp('(' + escapedQ + ')', 'gi');

  msgs.forEach(m => {
    const content = m.querySelector('.msg-content') || m.querySelector('.msg-bubble');
    if (!content) return;
    const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode);

    textNodes.forEach(node => {
      if (!re.test(node.textContent)) return;
      re.lastIndex = 0;
      // Build DOM nodes directly — innerHTML would corrupt content with < > & chars
      const parts = node.textContent.split(re);
      const frag = document.createDocumentFragment();
      for (let i = 0; i < parts.length; i++) {
        if (i % 2 === 0) {
          if (parts[i]) frag.appendChild(document.createTextNode(parts[i]));
        } else {
          const mark = document.createElement('mark');
          mark.className = 'search-highlight';
          mark.textContent = parts[i];
          frag.appendChild(mark);
        }
      }
      node.replaceWith(frag);
    });
  });

  _chatSearchMatches = document.querySelectorAll('#chatMessages .search-highlight');
  countEl.textContent = _chatSearchMatches.length ? '0/' + _chatSearchMatches.length : 'No results';
  if (_chatSearchMatches.length) chatToolbarSearchNav(1);
}

function chatToolbarSearchNav(dir) {
  if (!_chatSearchMatches.length) return;
  // Remove active highlight from previous
  if (_chatSearchIdx >= 0 && _chatSearchMatches[_chatSearchIdx]) {
    _chatSearchMatches[_chatSearchIdx].style.background = 'rgba(234,179,8,.25)';
  }
  _chatSearchIdx = (_chatSearchIdx + dir + _chatSearchMatches.length) % _chatSearchMatches.length;
  const el = _chatSearchMatches[_chatSearchIdx];
  if (el) {
    el.style.background = 'rgba(234,179,8,.55)';
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  document.getElementById('toolbarSearchCount').textContent = (_chatSearchIdx + 1) + '/' + _chatSearchMatches.length;
}

// ── Activity summary for assistant messages ──
let _chatPendingToolCalls = [];

// Module-scoped state shared between chatSendMessage stream loop and
// chatFinalizeStreamingMsg — captures meta/done events so we can paint a
// metadata footer on the assistant bubble at finalization time.
let _lastTurnMeta = null;   // stats dict from the `done` event
let _lastStreamModel = null; // model name from the `meta` event

function chatAppendActivitySummary(msgEl) {
  if (!_chatPendingToolCalls.length) return;
  const calls = [..._chatPendingToolCalls];
  _chatPendingToolCalls = [];

  const bubble = msgEl.querySelector('.msg-bubble');
  if (!bubble) return;

  // Build summary
  const counts = {};
  const files = new Set();
  const filePat = /(?:[\w./-]+\/)*[\w.-]+\.\w{1,6}/g;
  calls.forEach(c => {
    const name = c.name || 'tool';
    counts[name] = (counts[name] || 0) + 1;
    const m = (JSON.stringify(c.args || '') || '').match(filePat);
    if (m) m.forEach(f => { if (f.length > 3 && f.includes('.')) files.add(f); });
  });

  const summaryId = 'activity-' + Date.now();
  const completed = calls.filter(c => c.status !== 'running').length;
  const failed = calls.filter(c => c.status === 'error').length;
  const totalMs = calls.reduce((sum, c) => sum + (Number(c.duration_ms) || 0), 0);
  const toolBadges = Object.entries(counts).sort((a, b) => b[1] - a[1])
    .map(([name, ct]) => `<span class="msg-activity-badge neutral">${esc(name)}${ct > 1 ? ' x' + ct : ''}</span>`)
    .join(' ');

  const detailRows = calls.slice(0, 20).map(c => {
    const args = typeof c.args === 'string' ? c.args : JSON.stringify(c.args || {});
    const truncArgs = args.length > 120 ? args.slice(0, 120) + '...' : args;
    const meta = [c.status || 'running'];
    const statusClass = c.status === 'error' ? 'err' : (c.status === 'ok' ? 'ok' : 'neutral');
    if (c.duration_ms) meta.push(c.duration_ms + 'ms');
    return `<div class="activity-tool-row"><span class="activity-tool-name">${esc(c.name || '?')}</span><span class="activity-tool-args">${esc(truncArgs)}</span><span class="msg-activity-badge ${statusClass}">${esc(meta.join(' \u00b7 '))}</span></div>`;
  }).join('');

  const filesBadges = [...files].slice(0, 10).map(f => `<span class="activity-file-badge">${esc(f)}</span>`).join('');

  const div = document.createElement('div');
  div.className = 'msg-activity-summary';
  div.innerHTML = `
    <div class="msg-activity-header" onclick="this.querySelector('.chevron').classList.toggle('open');document.getElementById('${summaryId}').classList.toggle('open')">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/></svg>
      Agent Activity
      <span class="msg-activity-badge ${failed ? 'err' : 'ok'}">${completed}/${calls.length}</span>
      ${totalMs ? `<span class="msg-activity-badge neutral">${totalMs}ms</span>` : ''}
      <span class="chevron">\u25B6</span>
    </div>
    <div class="msg-activity-body" id="${summaryId}">
      <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px">${toolBadges}</div>
      ${detailRows}
      ${calls.length > 20 ? '<div class="activity-summary-line">... and ' + (calls.length - 20) + ' more</div>' : ''}
      ${filesBadges ? '<div class="activity-files" style="margin-top:6px"><span style="color:var(--text2);font-size:10px;margin-right:4px">Files:</span>' + filesBadges + '</div>' : ''}
      <div class="activity-summary-line">Used ${calls.length} tool${calls.length > 1 ? 's' : ''}: ${Object.entries(counts).map(([n, c]) => n + (c > 1 ? ' x' + c : '')).join(', ')}${failed ? ' \u00b7 ' + failed + ' failed' : ''}${totalMs ? ' \u00b7 ' + totalMs + 'ms total' : ''}</div>
    </div>
  `;
  // Insert between bubble and footer for clean layout
  const footer = msgEl.querySelector('.msg-footer');
  if (footer) {
    msgEl.insertBefore(div, footer);
  } else {
    msgEl.appendChild(div);
  }
}

function chatShowToast(message, type = 'info') {
  const existing = document.querySelector('.chat-toast');
  if (existing) existing.remove();
  const div = document.createElement('div');
  div.className = 'chat-toast';
  div.textContent = message;
  if (type === 'error') div.style.borderColor = 'var(--red)';
  document.body.appendChild(div);
  setTimeout(() => div.remove(), 3000);
}

// ── Toolbar visibility ──
function chatShowToolbar() {
  const tb = document.getElementById('chatToolbar');
  if (tb) tb.style.display = 'flex';
}

function chatUpdateToolbarInfo(convId) {
  const srcEl = document.getElementById('toolbarSource');
  const cntEl = document.getElementById('toolbarTurnCount');
  if (srcEl) srcEl.textContent = 'Oracle';
  const msgs = document.querySelectorAll('#chatMessages .msg');
  if (cntEl) cntEl.textContent = msgs.length + ' messages';
}

// ── Scroll management ──
function chatScrollToBottom(force = false) {
  const container = document.getElementById('chatMessages');
  if (force || !chatUserScrolled) {
    container.scrollTop = container.scrollHeight;
  }
}

document.getElementById('chatMessages').addEventListener('scroll', function() {
  const el = this;
  chatUserScrolled = (el.scrollHeight - el.scrollTop - el.clientHeight) > 80;
});

