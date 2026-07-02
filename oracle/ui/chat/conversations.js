// oracle/ui/chat/conversations.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Conversation list ──
async function chatLoadConversations() {
  try {
    const data = await api('/api/chat/conversations');
    const list = document.getElementById('convList');
    const convs = data.conversations || [];
    if (convs.length === 0) {
      list.innerHTML = '<div class="empty" style="padding:20px;font-size:11px">No conversations yet</div>';
      return;
    }
    let html = '';
    let lastGroup = '';
    convs.forEach(c => {
      const group = _convDateGroup(c.updated);
      if (group !== lastGroup) {
        html += `<div class="conv-group-label">${esc(group)}</div>`;
        lastGroup = group;
      }
      html += `
      <div class="conv-item ${c.id === chatCurrentConvId ? 'active' : ''}" onclick="chatLoadConversation('${c.id}')">
        <span class="conv-title">${esc(c.title || 'Untitled')}</span>
        <span class="conv-time">${c.updated ? timeAgo(new Date(c.updated).getTime()) : (c.message_count || 0) + ' msg'}</span>
        <button class="conv-rename" onclick="event.stopPropagation();chatRenameConversation('${c.id}',this)" title="Rename">&#9998;</button>
        <button class="conv-del" onclick="event.stopPropagation();chatDeleteConversation('${c.id}')" title="Delete">\u2715</button>
      </div>`;
    });
    list.innerHTML = html;
  } catch (e) {
    console.error('Failed to load conversations:', e);
  }
}

function _convDateGroup(isoStr) {
  if (!isoStr) return 'Older';
  const d = new Date(isoStr);
  const now = new Date();
  const diffMs = now - d;
  const diffDays = Math.floor(diffMs / 86400000);
  const isToday = d.toDateString() === now.toDateString();
  if (isToday) return 'Today';
  const yesterday = new Date(now); yesterday.setDate(yesterday.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return 'Yesterday';
  if (diffDays < 7) return 'This Week';
  return 'Older';
}

function chatFilterConversations() {
  const q = (document.getElementById('convSearch').value || '').toLowerCase();
  document.querySelectorAll('#convList .conv-item').forEach(el => {
    const title = (el.querySelector('.conv-title')?.textContent || '').toLowerCase();
    el.style.display = !q || title.includes(q) ? '' : 'none';
  });
  // Hide empty group labels
  document.querySelectorAll('#convList .conv-group-label').forEach(label => {
    let next = label.nextElementSibling;
    let hasVisible = false;
    while (next && !next.classList.contains('conv-group-label')) {
      if (next.classList.contains('conv-item') && next.style.display !== 'none') hasVisible = true;
      next = next.nextElementSibling;
    }
    label.style.display = hasVisible ? '' : 'none';
  });
}

async function chatRenameConversation(convId, btn) {
  const item = btn.closest('.conv-item');
  const titleEl = item.querySelector('.conv-title');
  const origTitle = titleEl.textContent;
  const input = document.createElement('input');
  input.className = 'conv-title-input';
  input.value = origTitle;
  titleEl.replaceWith(input);
  input.focus();
  input.select();

  const finish = async (save) => {
    if (input._done) return; input._done = true;
    const newTitle = input.value.trim();
    if (save && newTitle && newTitle !== origTitle) {
      try {
        await api(`/api/chat/conversations/${convId}/title`, { method: 'PUT', body: { title: newTitle } });
      } catch (e) { chatShowToast('Rename failed: ' + e.message, 'error'); }
    }
    chatLoadConversations();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') finish(false);
  });
  input.addEventListener('blur', () => finish(true));
}

async function chatNewConversation() {
  try {
    const data = await api('/api/chat/conversations', { method: 'POST', body: {} });
    chatCurrentConvId = data.id;
    chatClearMessages();
    chatUpdateStatePills({});
    chatLoadConversations();
  } catch (e) {
    toast('Error', 'Failed to create conversation', 'error');
  }
}

async function chatLoadConversation(convId) {
  chatCurrentConvId = convId;
  chatLoadConversations();
  try {
    const [convData, stateData] = await Promise.all([
      api(`/api/chat/conversations/${convId}`),
      api(`/api/chat/conversations/${convId}/state`).catch(() => ({ state: {} })),
    ]);
    chatClearMessages();
    chatUpdateStatePills(stateData.state || {});
    const msgs = convData.messages || [];
    for (const msg of msgs) {
      if (msg.role === 'user') chatAppendUserMsg(msg.content, msg.timestamp);
      else if (msg.role === 'assistant') chatAppendAssistantMsg(msg.content, msg.timestamp, msg.metadata);
      else if (msg.role === 'tool_call') chatAppendToolCall(JSON.parse(msg.content || '{}'), msg.tool_id);
      else if (msg.role === 'tool_result') chatAppendToolResult(msg.tool_name, msg.content, msg.tool_id);
    }
    chatScrollToBottom(true);
    chatShowToolbar();
    chatUpdateToolbarInfo(convId);
  } catch (e) {
    toast('Error', 'Failed to load conversation', 'error');
  }
}

async function chatDeleteConversation(convId) {
  try {
    await api(`/api/chat/conversations/${convId}`, { method: 'DELETE' });
    if (convId === chatCurrentConvId) {
      chatCurrentConvId = null;
      chatClearMessages();
    }
    chatLoadConversations();
  } catch (e) {
    toast('Error', 'Failed to delete conversation', 'error');
  }
}

function formatMsgTime(isoStr) {
  if (!isoStr) return '';
  const d = new Date(isoStr);
  const now = new Date();
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) return hm;
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ', ' + hm;
}

