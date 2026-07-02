// oracle/ui/chat/stream_renderer.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Message rendering ──
function chatClearMessages() {
  const container = document.getElementById('chatMessages');
  container.innerHTML = `
    <div class="chat-welcome" id="chatWelcome">
      <div style="font-size:40px;opacity:.3">&#128300;</div>
      <h2>Oracle Chat</h2>
      <p>Ask about your projects, memory patterns, health, or cross-project insights. Type <code>/</code> for commands.</p>
      <div class="chat-suggestions">
        <button onclick="chatSendSuggested('What projects do I have?')">&#128193; List my projects</button>
        <button onclick="chatSendSuggested('Show memory health across all projects')">&#128153; Memory health overview</button>
        <button onclick="chatSendSuggested('Find patterns and insights across my projects')">&#128270; Cross-project patterns</button>
        <button onclick="chatSendSuggested('Which projects have stale or duplicate facts?')">&#128214; Stale/duplicate facts</button>
      </div>
      <div style="font-size:11px;color:var(--text2);margin-top:12px">Enter to send &middot; Shift+Enter for newline &middot; / for commands</div>
    </div>
  `;
}

function chatHideWelcome() {
  const welcome = document.getElementById('chatWelcome');
  if (welcome) welcome.remove();
}

const _svgCopy = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';
const _svgRetry = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 4v6h6"/><path d="M3.51 15a9 9 0 102.13-9.36L1 10"/></svg>';

function _msgFooter(timestamp, opts = {}) {
  const ts = timestamp ? formatMsgTime(timestamp) : '';
  const actions = [];
  actions.push(`<button class="msg-footer-btn" onclick="chatCopyMsg(this)" title="Copy">${_svgCopy} Copy</button>`);
  if (opts.retry) {
    actions.push(`<button class="msg-footer-btn" onclick="chatRetryLast()" title="Retry">${_svgRetry} Retry</button>`);
  }
  return `<div class="msg-footer">
    <span class="msg-footer-ts">${ts ? esc(ts) : ''}</span>
    <span class="msg-footer-actions">${actions.join('')}</span>
  </div>`;
}

function chatAppendUserMsg(text, timestamp) {
  chatHideWelcome();
  const container = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = 'msg msg-user';
  div.dataset.text = text;
  div.innerHTML = `<div class="msg-bubble">${esc(text)}</div>${_msgFooter(timestamp)}`;
  container.appendChild(div);
}

function _fmtTokens(n) {
  if (!n && n !== 0) return '';
  n = Number(n);
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(n);
}

function _fmtDuration(ms) {
  if (!ms && ms !== 0) return '';
  ms = Number(ms);
  if (ms < 1000) return ms + 'ms';
  const s = ms / 1000;
  if (s < 60) return s.toFixed(s < 10 ? 2 : 1) + 's';
  const m = Math.floor(s / 60);
  const rem = Math.round(s - m * 60);
  return `${m}m${rem}s`;
}

function _msgMeta(_meta) {
  // Per-message metadata footer disabled — stats are available via the stats panel.
  return '';
}

function chatAppendAssistantMsg(text, timestamp, metadata) {
  chatHideWelcome();
  const container = document.getElementById('chatMessages');
  const existing = container.querySelector('.msg-streaming');
  if (existing) existing.remove();

  const div = document.createElement('div');
  div.className = 'msg msg-assistant';
  div.dataset.text = text;
  div.innerHTML = `<div class="msg-bubble"><div class="msg-content">${renderMarkdown(text)}</div></div>${_msgMeta(metadata)}${_msgFooter(timestamp, { retry: true })}`;
  container.appendChild(div);
}

// Accumulated text for the current streaming turn
window.__oracleStreamVersion = 'v2-live-trail-2026-04-11';
console.log('[oracle] stream UI', window.__oracleStreamVersion);
let _streamThinkingText = '';
let _streamResponseText = '';
let _streamInContentThink = false;  // tracks <think> tags embedded in content field
let _streamStartTime = 0;
let _streamTicker = null;
let _streamPendingRoundSep = false;  // set on tool_call, flushed on next thinking chunk
let _streamRenderScheduled = false;
let _streamThinkRenderScheduled = false;

function _scheduleStreamRender() {
  if (_streamRenderScheduled) return;
  _streamRenderScheduled = true;
  requestAnimationFrame(() => {
    _streamRenderScheduled = false;
    _renderStreamNow();
  });
}

function _renderStreamNow() {
  const container = document.getElementById('chatMessages');
  const el = container && container.querySelector('.msg-streaming');
  if (!el) return;
  const responseEl = el.querySelector('.response-content');
  if (!responseEl) return;
  // Strip in-progress <tool_call> blocks so users don't see raw JSON.
  const displayText = _streamResponseText
    .replace(/<tool_call>[\s\S]*?<\/tool_call>/g, '')
    .replace(/<tool_call>[\s\S]*$/, '');
  const lastLen = parseInt(responseEl.dataset.rendered || '0', 10);
  if (displayText.length === lastLen) return;
  if (!displayText.length) {
    responseEl.innerHTML = '';
    responseEl.dataset.rendered = '0';
    return;
  }
  responseEl.innerHTML = renderMarkdown(displayText);
  responseEl.dataset.rendered = String(displayText.length);
  chatScrollToBottom();
}

function _scheduleThinkRender() {
  if (_streamThinkRenderScheduled) return;
  _streamThinkRenderScheduled = true;
  requestAnimationFrame(() => {
    _streamThinkRenderScheduled = false;
    _renderThinkNow();
  });
}

function _renderThinkNow() {
  const container = document.getElementById('chatMessages');
  const el = container && container.querySelector('.msg-streaming');
  if (!el) return;
  const thinkBlock = el.querySelector('.thinking-block');
  if (!thinkBlock) return;
  thinkBlock.style.display = '';
  thinkBlock.setAttribute('open', '');
  const contentEl = thinkBlock.querySelector('.thinking-content');
  if (contentEl && contentEl.textContent !== _streamThinkingText) {
    contentEl.textContent = _streamThinkingText;
    contentEl.scrollTop = contentEl.scrollHeight;
  }
  const summaryEl = thinkBlock.querySelector('summary');
  if (summaryEl) summaryEl.textContent = 'Thinking \u00b7 ' + _fmtChars(_streamThinkingText.length) + ' chars';
  chatScrollToBottom();
}

function _fmtElapsed(ms) {
  const s = ms / 1000;
  if (s < 60) return s.toFixed(1) + 's';
  const m = Math.floor(s / 60);
  const rem = Math.floor(s % 60);
  return m + ':' + String(rem).padStart(2, '0');
}

function _updateStreamElapsed() {
  const container = document.getElementById('chatMessages');
  const el = container && container.querySelector('.msg-streaming');
  if (!el || !_streamStartTime) return;
  const ticker = el.querySelector('.stream-elapsed');
  if (!ticker) return;
  const parts = [_fmtElapsed(performance.now() - _streamStartTime)];
  if (_streamResponseText.length) parts.push(_fmtChars(_streamResponseText.length) + ' chars');
  else if (_streamThinkingText.length) parts.push(_fmtChars(_streamThinkingText.length) + ' think');
  ticker.textContent = parts.join(' \u00b7 ');
}

function _startStreamTicker() {
  if (_streamTicker) return;
  _streamStartTime = performance.now();
  _streamTicker = setInterval(_updateStreamElapsed, 100);
}

function _stopStreamTicker() {
  if (_streamTicker) { clearInterval(_streamTicker); _streamTicker = null; }
}

function _chatPushStreamActivity(text, cls) {
  const container = document.getElementById('chatMessages');
  const el = container && container.querySelector('.msg-streaming');
  if (!el) return;
  const trail = el.querySelector('.stream-activity');
  if (!trail) return;
  // Mark previous active row as done
  const prev = trail.querySelector('.stream-activity-row.sa-active');
  if (prev) { prev.classList.remove('sa-active'); prev.classList.add('sa-done'); }
  const row = document.createElement('div');
  row.className = 'stream-activity-row sa-active' + (cls ? ' ' + cls : '');
  const elapsed = _streamStartTime ? _fmtElapsed(performance.now() - _streamStartTime) : '0.0s';
  row.innerHTML = '<span class="sa-time">' + esc(elapsed) + '</span><span class="sa-text">' + esc(text) + '</span>';
  trail.appendChild(row);
  trail.scrollTop = trail.scrollHeight;
}

function _chatFinishStreamActivity() {
  const container = document.getElementById('chatMessages');
  const el = container && container.querySelector('.msg-streaming');
  if (!el) return;
  const trail = el.querySelector('.stream-activity');
  if (!trail) return;
  const prev = trail.querySelector('.stream-activity-row.sa-active');
  if (prev) { prev.classList.remove('sa-active'); prev.classList.add('sa-done'); }
}

function _ensureStreamingEl() {
  const container = document.getElementById('chatMessages');
  let el = container.querySelector('.msg-streaming');
  if (!el) {
    _streamThinkingText = '';
    _streamResponseText = '';
    _streamInContentThink = false;
    _streamPendingRoundSep = false;
    el = document.createElement('div');
    el.className = 'msg msg-assistant msg-streaming';
    el.innerHTML = '<div class="msg-bubble"><div class="msg-content">'
      + '<details class="thinking-block streaming" style="display:none" open>'
      + '<summary>Thinking</summary><div class="thinking-content"></div></details>'
      + '<div class="stream-status" data-state="connecting">'
      +   '<span class="stream-phase">Connecting</span>'
      +   '<span class="stream-detail">Connecting to model...</span>'
      +   '<span class="stream-elapsed">0.0s</span>'
      + '</div>'
      + '<div class="stream-activity"></div>'
      + '<div class="response-content"></div>'
      + '<span class="streaming-cursor"></span>'
      + '</div></div>';
    container.appendChild(el);
    _startStreamTicker();
  }
  return el;
}

function chatStreamState(text) {
  const value = (text || '').toLowerCase();
  if (value.includes('thinking')) return 'thinking';
  if (value.includes('tool') || value.includes('executing')) return 'tool';
  if (value.includes('retry')) return 'retry';
  if (value.includes('writing') || value.includes('response')) return 'writing';
  return 'connecting';
}

function chatStreamLabel(text, state) {
  if (!text) return '';
  if (state === 'thinking') return 'Thinking';
  if (state === 'tool') return 'Using tools';
  if (state === 'retry') return 'Retrying';
  if (state === 'writing') return text.includes('Writing') ? 'Writing' : 'Generating';
  const lower = text.toLowerCase();
  if (lower.includes('preparing')) return 'Preparing';
  if (lower.includes('context ready')) return 'Ready';
  if (lower.includes('connected')) return 'Connected';
  if (lower.includes('finaliz')) return 'Finalizing';
  return 'Connecting';
}

function chatSetStreamPhase(text) {
  chatHideWelcome();
  chatRemoveTypingIndicator();
  const el = _ensureStreamingEl();
  const statusEl = el.querySelector('.stream-status');
  if (statusEl) {
    const state = chatStreamState(text);
    statusEl.dataset.state = state;
    statusEl.classList.toggle('hidden', !text);
    const phaseEl = statusEl.querySelector('.stream-phase');
    const detailEl = statusEl.querySelector('.stream-detail');
    if (phaseEl) phaseEl.textContent = chatStreamLabel(text, state);
    if (detailEl) detailEl.textContent = text || '';
  }
  chatScrollToBottom();
}

function chatUpdateThinking(chunk) {
  chatHideWelcome();
  chatRemoveTypingIndicator();
  _ensureStreamingEl();
  chatSetStreamPhase('Thinking live...');
  // Flush a deferred round separator only now that real new thinking
  // content is arriving — avoids empty "--- next round ---" markers.
  if (_streamPendingRoundSep) {
    _streamThinkingText += '\n\n--- next round ---\n\n';
    _streamPendingRoundSep = false;
  }
  _streamThinkingText += chunk;
  _scheduleThinkRender();
}

function chatFinishThinking() {
  const container = document.getElementById('chatMessages');
  const el = container.querySelector('.msg-streaming');
  if (!el) return;
  const thinkBlock = el.querySelector('.thinking-block');
  if (thinkBlock) {
    thinkBlock.classList.remove('streaming');
    thinkBlock.removeAttribute('open');
  }
}

function chatUpdateStreamingMsg(text) {
  chatHideWelcome();
  chatRemoveTypingIndicator();
  const el = _ensureStreamingEl();

  // Handle <think> tags embedded in content (models without thinking field)
  let remaining = text;
  while (remaining) {
    if (_streamInContentThink) {
      const closeIdx = remaining.indexOf('</think>');
      if (closeIdx === -1) {
        // Still in thinking — send entire chunk to thinking
        chatUpdateThinking(remaining);
        remaining = '';
      } else {
        // Thinking ends mid-chunk
        chatUpdateThinking(remaining.slice(0, closeIdx));
        chatFinishThinking();
        _streamInContentThink = false;
        remaining = remaining.slice(closeIdx + 8);
      }
    } else {
      const openIdx = remaining.indexOf('<think>');
      if (openIdx === -1) {
        // Normal response text
        _streamResponseText += remaining;
        remaining = '';
      } else {
        // Response text before <think>, then enter thinking
        if (openIdx > 0) _streamResponseText += remaining.slice(0, openIdx);
        _streamInContentThink = true;
        remaining = remaining.slice(openIdx + 7);
      }
    }
  }

  // Once real response text has arrived, hide the stream-status overlay so
  // the user never sees a stale "Connecting" label sitting on top of an
  // empty bubble. Also do a synchronous render as a safety net — rAF can
  // be deferred indefinitely in background tabs or during heavy work.
  if (_streamResponseText) {
    const statusEl = el.querySelector('.stream-status');
    if (statusEl) statusEl.classList.add('hidden');
    _renderStreamNow();
  }

  // Coalesced markdown re-render for the next frame — handles any trailing
  // chunks that arrive in the same tick.
  _scheduleStreamRender();
}

function chatFinalizeStreamingMsg(text) {
  const container = document.getElementById('chatMessages');
  const el = container.querySelector('.msg-streaming');
  if (el) {
    el.classList.remove('msg-streaming');
    el.dataset.text = text;
    // Add metadata footer (model, time, tokens) before the actions footer.
    // Source of truth: _lastTurnMeta from the `done` event, with model name
    // falling back to _lastStreamModel captured on the `meta` event.
    if (!el.querySelector('.msg-meta')) {
      const meta = { ...(_lastTurnMeta || {}) };
      if (!meta.model && _lastStreamModel) meta.model = _lastStreamModel;
      const metaHtml = _msgMeta(meta);
      if (metaHtml) {
        const wrap = document.createElement('div');
        wrap.innerHTML = metaHtml;
        el.appendChild(wrap.firstElementChild);
      }
    }
    // Add footer (timestamp + actions) if not already present
    if (!el.querySelector('.msg-footer')) {
      const footer = document.createElement('div');
      footer.innerHTML = _msgFooter(new Date().toISOString(), { retry: true });
      el.appendChild(footer.firstElementChild);
    }
    // Finalize thinking block — collapse now that streaming is done
    const thinkBlock = el.querySelector('.thinking-block');
    if (thinkBlock) {
      thinkBlock.classList.remove('streaming');
      if (!_streamThinkingText) {
        thinkBlock.style.display = 'none';
      } else {
        thinkBlock.removeAttribute('open');  // collapse on finalize
        const summary = thinkBlock.querySelector('summary');
        if (summary) {
          const chars = _streamThinkingText.length;
          summary.textContent = `Thinking \u00b7 ${_fmtChars(chars)} chars`;
        }
      }
    }
    // Render response as markdown and remove cursor
    _stopStreamTicker();
    _chatFinishStreamActivity();
    const responseEl = el.querySelector('.response-content');
    const statusEl = el.querySelector('.stream-status');
    if (statusEl) statusEl.remove();
    const activityEl = el.querySelector('.stream-activity');
    if (activityEl) activityEl.remove();
    let cleanText = text
      .replace(/<tool_call>[\s\S]*?<\/tool_call>/g, '')
      .replace(/<think>[\s\S]*?<\/think>/g, '')
      .trim();
    if (!cleanText) cleanText = 'No visible response was returned.';
    el.dataset.text = cleanText;
    if (responseEl) responseEl.innerHTML = renderMarkdown(cleanText);
    const cursor = el.querySelector('.streaming-cursor');
    if (cursor) cursor.remove();
    // Reset accumulators
    _streamThinkingText = '';
    _streamResponseText = '';
    _streamInContentThink = false;
    _streamPendingRoundSep = false;
    // Append activity summary if tool calls were made
    chatAppendActivitySummary(el);
  }
}

function _toolCallSummary(call) {
  const name = call.name || 'tool';
  const args = call.args || {};
  // Extract the most meaningful arg value for a one-line summary
  const keyPriority = ['query', 'project_path', 'path', 'action', 'fact_ids'];
  for (const k of keyPriority) {
    if (args[k]) {
      const v = typeof args[k] === 'string' ? args[k] : JSON.stringify(args[k]);
      const short = v.length > 50 ? v.slice(0, 47) + '...' : v;
      return short;
    }
  }
  // Fallback: first string arg
  for (const v of Object.values(args)) {
    if (typeof v === 'string' && v.length > 0) {
      return v.length > 50 ? v.slice(0, 47) + '...' : v;
    }
  }
  return 'running...';
}

function chatRetryLast() {
  if (chatStreaming) return;
  // Find the last user message
  const userMsgs = document.querySelectorAll('#chatMessages .msg-user');
  if (!userMsgs.length) return;
  const lastUser = userMsgs[userMsgs.length - 1];
  const text = lastUser.dataset.text;
  if (!text) return;
  const input = document.getElementById('chatInput');
  input.value = text;
  chatSendMessage();
}

function chatInsertTypingIndicator() {
  chatRemoveTypingIndicator();
  const container = document.getElementById('chatMessages');
  const el = document.createElement('div');
  el.className = 'msg msg-assistant msg-typing';
  el.style.animation = 'none'; // don't fade-in the dots
  el.innerHTML = '<div class="msg-bubble"><div class="typing-indicator"><span></span><span></span><span></span></div></div>';
  container.appendChild(el);
  chatScrollToBottom(true);
}

function chatRemoveTypingIndicator() {
  const el = document.querySelector('.msg-typing');
  if (el) el.remove();
}

function chatAppendToolCall(call, toolId) {
  chatHideWelcome();
  const container = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = 'msg msg-tool';
  div.id = `tool-${toolId}`;
  const argsStr = JSON.stringify(call.args || {}, null, 2);
  div.innerHTML = `
    <div class="tool-header" onclick="this.nextElementSibling.classList.toggle('open');this.querySelector('.tool-chevron').classList.toggle('open')">
      <span class="tool-badge">${esc(call.name || 'tool')}</span>
      <span class="tool-label">${esc(_toolCallSummary(call))}<span class="tool-spinner"> \u25CF</span></span>
      <span class="tool-chevron open">\u25B6</span>
    </div>
    <div class="tool-body open">
      <div class="tool-section-label">Arguments</div>
      <pre>${esc(argsStr)}</pre>
      <div class="tool-result-area" id="tool-result-${toolId}"></div>
    </div>
  `;
  container.appendChild(div);
  chatScrollToBottom();
}

function chatAppendToolResult(name, resultStr, toolId, durLabel = '') {
  // Try to find the tool call element and add result
  const area = document.getElementById(`tool-result-${toolId}`);
  const displayStr = typeof resultStr === 'string' ? resultStr : JSON.stringify(resultStr, null, 2);
  if (area) {
    const toolEl = area.closest('.msg-tool');
    const label = toolEl.querySelector('.tool-label');
    if (label) label.innerHTML = `<span class="tool-status-ok">\u2713</span> Done${esc(durLabel)}`;
    area.innerHTML = `
      <div class="tool-section-label" style="margin-top:8px">Result</div>
      <pre>${esc(displayStr)}</pre>
    `;
    // Keep the body open if it contains a sub-agent activity block so the
    // user can review what the agent streamed. Otherwise collapse.
    const hasAgent = toolEl.querySelector('.agent-activity');
    const body = toolEl.querySelector('.tool-body');
    const chevron = toolEl.querySelector('.tool-chevron');
    if (!hasAgent) {
      if (body) body.classList.remove('open');
      if (chevron) chevron.classList.remove('open');
    }
  } else {
    // Fallback: append as standalone
    const container = document.getElementById('chatMessages');
    const div = document.createElement('div');
    div.className = 'msg msg-tool';
    div.innerHTML = `
      <div class="tool-header">
        <span class="tool-badge">${esc(name || 'tool')}</span>
        <span class="tool-label"><span class="tool-status-ok">\u2713</span> Result${esc(durLabel)}</span>
      </div>
      <div class="tool-body open">
        <pre>${esc(displayStr)}</pre>
      </div>
    `;
    container.appendChild(div);
  }
  chatScrollToBottom();
}

function chatAppendError(msg) {
  const container = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = 'msg-error';
  div.textContent = msg;
  container.appendChild(div);
  chatScrollToBottom();
}

// ── Sub-agent activity (nested inside delegate_task tool blocks) ──

function chatAgentEnsureBlock(toolId, agentId) {
  const toolEl = document.getElementById(`tool-${toolId}`);
  if (!toolEl) return null;
  const body = toolEl.querySelector('.tool-body');
  if (!body) return null;
  let block = body.querySelector('.agent-activity');
  if (!block) {
    block = document.createElement('div');
    block.className = 'agent-activity';
    if (agentId) block.dataset.agentId = agentId;
    block.innerHTML = `
      <div class="agent-header">
        <span class="agent-badge">${esc(agentId || 'agent')}</span>
        <span class="agent-round"></span>
        <span class="agent-spinner">\u25CF</span>
      </div>
      <details class="thinking-block agent-thinking streaming" style="display:none" open>
        <summary>Thinking</summary>
        <div class="thinking-content"></div>
      </details>
      <div class="agent-tools"></div>
      <div class="agent-response"></div>
      <div class="agent-footer"></div>
    `;
    // Ensure the tool body is open so users see live activity
    body.classList.add('open');
    const chevron = toolEl.querySelector('.tool-chevron');
    if (chevron) chevron.classList.add('open');
    body.appendChild(block);
  }
  return block;
}

function chatAgentStart(event) {
  const block = chatAgentEnsureBlock(event.tool_id, event.agent_id);
  if (!block) return;
  block.dataset.text = '';
  block.dataset.thinkingText = '';
  const label = document.querySelector(`#tool-${event.tool_id} .tool-label`);
  if (label) label.innerHTML = `Delegating to <strong>${esc(event.agent_id || 'agent')}</strong><span class="tool-spinner"> \u25CF</span>`;
  chatScrollToBottom();
}

function chatAgentRound(event) {
  const block = chatAgentEnsureBlock(event.tool_id);
  if (!block) return;
  const roundEl = block.querySelector('.agent-round');
  if (roundEl) roundEl.textContent = `round ${event.round}`;
}

function chatAgentThinking(event) {
  const block = chatAgentEnsureBlock(event.tool_id);
  if (!block) return;
  const thinkBlock = block.querySelector('.thinking-block');
  if (!thinkBlock) return;
  thinkBlock.style.display = '';
  thinkBlock.setAttribute('open', '');
  const contentEl = thinkBlock.querySelector('.thinking-content');
  const prev = block.dataset.thinkingText || '';
  const next = prev + (event.content || '');
  block.dataset.thinkingText = next;
  contentEl.textContent = next;
  contentEl.scrollTop = contentEl.scrollHeight;
  const summary = thinkBlock.querySelector('summary');
  if (summary) summary.textContent = 'Thinking \u00b7 ' + _fmtChars(next.length) + ' chars';
  chatScrollToBottom();
}

function chatAgentText(event) {
  const block = chatAgentEnsureBlock(event.tool_id);
  if (!block) return;
  const respEl = block.querySelector('.agent-response');
  if (!respEl) return;
  const prev = block.dataset.text || '';
  const next = prev + (event.content || '');
  block.dataset.text = next;
  // Strip any tool_call/think wrappers for live preview
  const clean = next
    .replace(/<tool_call>[\s\S]*?<\/tool_call>/g, '\u2026')
    .replace(/<think>[\s\S]*?<\/think>/g, '');
  respEl.textContent = clean;
  chatScrollToBottom();
}

function chatAgentToolCall(event) {
  const block = chatAgentEnsureBlock(event.tool_id);
  if (!block) return;
  const toolsEl = block.querySelector('.agent-tools');
  if (!toolsEl) return;
  const subId = event.sub_tool_id;
  const argsStr = JSON.stringify(event.args || {}, null, 2);
  const card = document.createElement('div');
  card.className = 'agent-sub-tool';
  card.id = `agent-sub-${subId}`;
  card.innerHTML = `
    <div class="agent-sub-header" onclick="this.nextElementSibling.classList.toggle('open')">
      <span class="agent-sub-badge">${esc(event.name || 'tool')}</span>
      <span class="agent-sub-label">${esc(_toolCallSummary({ name: event.name, args: event.args }))}<span class="tool-spinner"> \u25CF</span></span>
    </div>
    <div class="agent-sub-body">
      <div class="tool-section-label">Arguments</div>
      <pre>${esc(argsStr)}</pre>
      <div class="agent-sub-result" id="agent-sub-result-${subId}"></div>
    </div>
  `;
  toolsEl.appendChild(card);
  chatScrollToBottom();
}

function chatAgentToolResult(event) {
  const subId = event.sub_tool_id;
  const card = document.getElementById(`agent-sub-${subId}`);
  if (!card) return;
  const label = card.querySelector('.agent-sub-label');
  const durLabel = event.duration_ms ? ` (${event.duration_ms}ms)` : '';
  const hasError = event.result && typeof event.result === 'object' && 'error' in event.result;
  if (label) {
    label.innerHTML = hasError
      ? `<span class="tool-status-err">\u2717</span> Error${esc(durLabel)}`
      : `<span class="tool-status-ok">\u2713</span> Done${esc(durLabel)}`;
  }
  const resultArea = document.getElementById(`agent-sub-result-${subId}`);
  if (resultArea) {
    const display = typeof event.result === 'string' ? event.result : JSON.stringify(event.result, null, 2);
    resultArea.innerHTML = `<div class="tool-section-label" style="margin-top:6px">Result</div><pre>${esc(display)}</pre>`;
  }
}

function chatAgentDone(event) {
  const block = chatAgentEnsureBlock(event.tool_id, event.agent_id);
  if (!block) return;
  const spinner = block.querySelector('.agent-spinner');
  if (spinner) spinner.remove();
  const footer = block.querySelector('.agent-footer');
  if (footer) {
    const parts = [];
    if (event.rounds) parts.push(`${event.rounds} round${event.rounds > 1 ? 's' : ''}`);
    if (event.result_chars) parts.push(`${_fmtChars(event.result_chars)} chars`);
    if (event.duration_ms) parts.push(_fmtDuration(event.duration_ms));
    if (event.error) parts.push(`\u26A0 ${event.error}`);
    footer.innerHTML = parts.map(p => `<span>${esc(p)}</span>`).join('<span class="msg-meta-sep">\u00b7</span>');
  }
  // Finalize thinking block — collapse it
  const thinkBlock = block.querySelector('.thinking-block');
  if (thinkBlock) {
    thinkBlock.classList.remove('streaming');
    thinkBlock.removeAttribute('open');
  }
  block.classList.add('done');
}

// ── Status bar (activity trail) ──
let _statusTrailSteps = [];

function chatShowStatus(msg, detail = '') {
  const bar = document.getElementById('chatStatusBar');
  bar.classList.add('active');
  const trail = document.getElementById('chatStatusTrail');

  // Deduplicate consecutive identical messages
  if (_statusTrailSteps.length && _statusTrailSteps[_statusTrailSteps.length - 1] === msg) {
    document.getElementById('chatStatusDetail').textContent = detail;
    return;
  }

  _statusTrailSteps.push(msg);

  // Rebuild trail: previous steps faded, current step highlighted
  let html = '';
  const visible = _statusTrailSteps.length > 6 ? _statusTrailSteps.slice(-6) : _statusTrailSteps;
  visible.forEach((s, i) => {
    if (i > 0) html += '<span class="chat-status-sep">\u203A</span>';
    html += `<span class="chat-status-step${i === visible.length - 1 ? ' active' : ''}">${esc(s)}</span>`;
  });
  trail.innerHTML = html;
  document.getElementById('chatStatusDetail').textContent = detail;
}

function chatHideStatus() {
  document.getElementById('chatStatusBar').classList.remove('active');
  document.getElementById('chatStatusTrail').innerHTML = '';
  document.getElementById('chatStatusStats').innerHTML = '';
  _statusTrailSteps = [];
}

function chatShowStats(stats) {
  const el = document.getElementById('chatStatusStats');
  if (!stats) { el.innerHTML = ''; return; }
  const parts = [];
  if (stats.total_ms) parts.push(`${(stats.total_ms / 1000).toFixed(1)}s`);
  // Token breakdown: thinking + response
  if (stats.eval_tokens) {
    parts.push(`${stats.eval_tokens} tok`);
  }
  if (stats.thinking_chars) {
    parts.push(`${_fmtChars(stats.thinking_chars)} thinking`);
  }
  if (stats.response_chars) {
    parts.push(`${_fmtChars(stats.response_chars)} response`);
  }
  if (stats.prompt_tokens) {
    parts.push(`${stats.prompt_tokens} prompt`);
  }
  if (stats.tokens_per_sec) {
    parts.push(`${stats.tokens_per_sec} tok/s`);
  }
  if (stats.tool_calls) parts.push(`${stats.tool_calls} tool${stats.tool_calls > 1 ? 's' : ''}`);
  if (stats.rounds > 1) parts.push(`${stats.rounds} rounds`);
  el.innerHTML = parts.map(p => `<span>${esc(p)}</span>`).join('');
}

function _fmtChars(n) {
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

