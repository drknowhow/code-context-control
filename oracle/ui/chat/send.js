// oracle/ui/chat/send.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Main send + SSE consumer ──
async function chatSendMessage() {
  const input = document.getElementById('chatInput');
  const text = input.value.trim();
  if (!text || chatStreaming) return;

  // Hide overlay and ghost
  document.getElementById('chatCmdOverlay').classList.remove('open');
  document.getElementById('chatGhost').innerHTML = '';

  // Slash command interception
  if (text.startsWith('/')) {
    chatAppendUserMsg(text);
    input.value = '';
    input.style.height = 'auto';
    try {
      const result = await api('/api/chat/command', {
        method: 'POST',
        body: { conversation_id: chatCurrentConvId, command: text },
      });
      chatAppendCommandResult(result);
      if (result.conv_id) chatCurrentConvId = result.conv_id;
    } catch (e) {
      chatAppendError('Command failed: ' + e.message);
    }
    return;
  }

  chatStreaming = true;
  chatUserScrolled = false;
  _chatPendingToolCalls = [];
  chatShowToolbar();
  const sendBtn = document.getElementById('chatSendBtn');
  sendBtn.textContent = 'Stop';
  sendBtn.className = 'chat-stop';
  sendBtn.onclick = () => { if (chatAbortController) chatAbortController.abort(); };

  chatAppendUserMsg(text);
  input.value = '';
  input.style.height = 'auto';
  chatScrollToBottom(true);
  chatHideStatus();

  // Clean up any stale streaming element from a previous send BEFORE
  // creating the new one — otherwise chatSetStreamPhase creates a fresh
  // bubble and this line immediately removes it, leaving the user with
  // no visible feedback during slow model first-byte wait.
  const staleStreaming = document.getElementById('chatMessages').querySelector('.msg-streaming');
  if (staleStreaming) staleStreaming.remove();
  _stopStreamTicker();

  chatShowStatus('Connecting', 'Sending to Oracle...');
  chatSetStreamPhase('Connecting to Oracle...');

  chatAbortController = new AbortController();
  let assistantText = '';
  let buffer = '';
  let lastStats = null;
  let gotResponse = false;

  try {
    const resp = await fetch(API + '/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: chatCurrentConvId, message: text }),
      signal: chatAbortController.signal,
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || `Server error: HTTP ${resp.status}`);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        buffer += decoder.decode();  // flush remaining bytes
      } else {
        buffer += decoder.decode(value, { stream: true });
      }

      while (buffer.includes('\n\n')) {
        const idx = buffer.indexOf('\n\n');
        const line = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 2);

        if (line === 'data: [DONE]') continue;
        if (!line.startsWith('data: ')) continue;

        let event;
        try { event = JSON.parse(line.slice(6)); } catch { continue; }

        switch (event.type) {
          case 'meta':
            if (event.conv_id && !chatCurrentConvId) {
              chatCurrentConvId = event.conv_id;
            }
            if (event.state) chatUpdateStatePills(event.state);
            if (event.model) _lastStreamModel = event.model;
            _lastTurnMeta = null;  // reset for new turn
            chatShowStatus('Connected', event.model ? `Model: ${event.model}` : '');
            chatSetStreamPhase(event.model ? `Connected to ${event.model}` : 'Connected to model');
            _chatPushStreamActivity(event.model ? `Connected to ${event.model}` : 'Connected to model');
            break;
          case 'status': {
            const statusMsg = event.message || '';
            const statusDetail = event.detail || '';
            chatShowStatus(statusMsg, statusDetail);
            if (statusMsg.startsWith('Preparing')) chatSetStreamPhase('Preparing context...');
            else if (statusMsg.startsWith('Context ready')) chatSetStreamPhase('Context ready. Waiting for model...');
            else if (statusMsg.startsWith('Streaming')) chatSetStreamPhase('Generating response live...');
            else if (statusMsg.startsWith('Retrying')) chatSetStreamPhase('Retrying visible response...');
            else if (statusMsg.startsWith('Executing')) chatSetStreamPhase(statusMsg + '...');
            else if (statusMsg.startsWith('Continuing')) chatSetStreamPhase('Continuing response generation...');
            if (statusMsg) {
              const trailText = statusDetail ? `${statusMsg} \u2014 ${statusDetail}` : statusMsg;
              _chatPushStreamActivity(trailText);
            }
            break;
          }
          case 'thinking':
            chatUpdateThinking(event.content);
            gotResponse = true;
            break;
          case 'text':
            chatRemoveTypingIndicator();
            chatSetStreamPhase('Writing response...');
            assistantText += event.content;
            chatUpdateStreamingMsg(event.content);
            gotResponse = true;
            break;
          case 'tool_call':
            // Keep the streaming bubble across rounds — accumulate thinking,
            // but reset response text since the prior round's text was just
            // the tool call wrapper. The final round's text will fill it.
            {
              const bubble = document.getElementById('chatMessages').querySelector('.msg-streaming');
              if (bubble) {
                const responseEl = bubble.querySelector('.response-content');
                if (responseEl) {
                  responseEl.textContent = '';
                  // Also reset the memoization counter — otherwise the next
                  // round's tokens fall into the else-branch with a stale
                  // lastLen and `slice(staleN)` stays empty until text grows
                  // past the prior length, making streaming invisible.
                  responseEl.dataset.rendered = '0';
                }
                // Defer the round separator — only insert it if the next
                // round actually emits new thinking tokens. Otherwise we
                // end up with empty "--- next round ---" markers when a
                // round produces only the tool_call wrapper and no
                // visible reasoning.
                if (_streamThinkingText) _streamPendingRoundSep = true;
              }
            }
            _streamResponseText = '';
            _streamInContentThink = false;
            assistantText = '';
            gotResponse = true;
            chatRemoveTypingIndicator();
            _chatPendingToolCalls.push({ name: event.name || event.tool, args: event.args, tool_id: event.tool_id, status: 'running' });
            chatSetStreamPhase(`Using tool: ${event.name || event.tool || 'tool'}...`);
            _chatPushStreamActivity(`Tool call: ${event.name || event.tool || 'tool'}`, 'sa-tool');
            chatAppendToolCall(event, event.tool_id);
            break;
          case 'tool_result': {
            const pendingCall = _chatPendingToolCalls.find(c => c.tool_id === event.tool_id);
            if (pendingCall) {
              pendingCall.duration_ms = event.duration_ms || 0;
              pendingCall.status = event.result && event.result.error ? 'error' : 'ok';
            }
            chatSetStreamPhase(`Tool complete: ${event.name || 'tool'}`);
            const _trDur = event.duration_ms ? ` (${event.duration_ms}ms)` : '';
            _chatPushStreamActivity(`Tool result: ${event.name || 'tool'}${_trDur}`, 'sa-tool');
            const durLabel = event.duration_ms ? ` (${event.duration_ms}ms)` : '';
            chatAppendToolResult(
              event.name,
              typeof event.result === 'string' ? event.result : JSON.stringify(event.result, null, 2),
              event.tool_id,
              durLabel
            );
            break;
          }
          case 'agent_start':
            chatAgentStart(event);
            chatSetStreamPhase(`Sub-agent ${event.agent_id || ''} starting...`);
            break;
          case 'agent_round':
            chatAgentRound(event);
            break;
          case 'agent_thinking':
            chatAgentThinking(event);
            break;
          case 'agent_text':
            chatAgentText(event);
            chatSetStreamPhase(`Sub-agent ${event.agent_id || ''} responding...`);
            break;
          case 'agent_tool_call':
            chatAgentToolCall(event);
            break;
          case 'agent_tool_result':
            chatAgentToolResult(event);
            break;
          case 'agent_done':
            chatAgentDone(event);
            break;
          case 'error':
            chatRemoveTypingIndicator();
            _chatPushStreamActivity(event.message || 'Error', 'sa-err');
            chatAppendError(event.message || 'Unknown error');
            gotResponse = true;
            break;
          case 'done':
            if (event.conv_id) chatCurrentConvId = event.conv_id;
            lastStats = event.stats || null;
            _lastTurnMeta = event.stats || null;
            break;
        }
      }

      if (done) break;
    }
  } catch (e) {
    chatRemoveTypingIndicator();
    _stopStreamTicker();
    if (e.name !== 'AbortError') {
      chatAppendError('Connection error: ' + e.message);
    }
  }

  chatRemoveTypingIndicator();  // safety cleanup
  _stopStreamTicker();  // safety cleanup

  // Finalize streaming message
  if (assistantText) {
    chatFinalizeStreamingMsg(assistantText);
  } else if (_streamThinkingText) {
    // Model produced only thinking with no text response — finalize the bubble
    chatFinalizeStreamingMsg('');
  } else if (!gotResponse) {
    chatAppendError('No response received \u2014 the model may be unavailable.');
  }

  // Annotate the last thinking block with token-level stats
  if (lastStats && (lastStats.thinking_chars || lastStats.eval_tokens)) {
    const msgs = document.getElementById('chatMessages');
    const lastAssistant = msgs.querySelector('.msg-assistant:last-of-type .thinking-block summary');
    if (lastAssistant) {
      const parts = [];
      if (lastStats.thinking_chars) parts.push(_fmtChars(lastStats.thinking_chars) + ' chars');
      if (lastStats.eval_tokens) parts.push(lastStats.eval_tokens + ' tok');
      if (lastStats.tokens_per_sec) parts.push(lastStats.tokens_per_sec + ' tok/s');
      lastAssistant.textContent = 'Thinking \u00b7 ' + parts.join(' \u00b7 ');
    }
  }

  // Show final stats briefly, then hide
  if (lastStats) {
    chatShowStatus('Complete', '');
    chatShowStats(lastStats);
    // Stats persist until next send (chatHideStatus called at start of chatSendMessage)
  } else {
    chatHideStatus();
  }

  chatStreaming = false;
  chatAbortController = null;
  sendBtn.textContent = 'Send';
  sendBtn.className = 'chat-send';
  sendBtn.onclick = chatSendMessage;
  chatLoadConversations();
  chatScrollToBottom(true);
  chatUpdateToolbarInfo(chatCurrentConvId);
}

// ═══════════════════════════════════════════════════════════
