// ─── Chat Panel ───────────────────────────────────────
const BUILD_TIME_CHAT = "2026-04-10";

// ─── Markdown renderer setup ──────────────────────────
const chatRenderer = new marked.Renderer();
chatRenderer.code = function (code, lang) {
  const language = (lang || '').trim();
  let highlighted;
  try {
    highlighted = language && hljs.getLanguage(language)
      ? hljs.highlight(code, { language }).value
      : hljs.highlightAuto(code).value;
  } catch { highlighted = code.replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  const label = language || 'text';
  return '<div class="chat-code-block" data-lang="' + label + '">'
    + '<div class="chat-code-header"><span>' + label + '</span>'
    + '<button class="chat-code-copy" data-code="' + encodeURIComponent(code) + '">Copy</button></div>'
    + '<pre><code class="hljs">' + highlighted + '</code></pre></div>';
};
marked.setOptions({ renderer: chatRenderer, gfm: true, breaks: true, pedantic: false });

const renderMarkdown = (text) => {
  if (!text) return '';
  try { return marked.parse(text); }
  catch { return '<pre>' + text + '</pre>'; }
};

const highlightSearchInHtml = (html, query) => {
  if (!query || query.length < 2) return html;
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return html.replace(new RegExp('(' + escaped + ')', 'gi'),
    '<mark style="background:#ffb22450;color:inherit;padding:1px 2px;border-radius:2px">$1</mark>');
};

// ─── Utility helpers ──────────────────────────────────
const sourceColors = { claude: '#22c55e', gemini: '#3b82f6', imports: '#a855f7', manual: '#8b949e', api: '#f59e0b' };
const getSourceColor = (src) => sourceColors[(src || '').toLowerCase()] || '#8b949e';

const extractFilesFromTools = (toolCalls) => {
  const files = new Set();
  const pat = /(?:[\w./-]+\/)*[\w.-]+\.\w{1,6}/g;
  for (const tc of (toolCalls || [])) {
    const m = (tc.args || '').match(pat);
    if (m) m.forEach(f => { if (f.length > 3 && f.includes('.')) files.add(f); });
  }
  return [...files].slice(0, 12);
};

const toolSummary = (toolCalls) => {
  const counts = {};
  for (const tc of (toolCalls || [])) {
    const n = tc.tool || 'unknown';
    counts[n] = (counts[n] || 0) + 1;
  }
  return Object.entries(counts).sort((a, b) => b[1] - a[1]);
};

const truncate = (s, max) => s && s.length > max ? s.slice(0, max) + '...' : s;

// ─── ChatAgentActivity ───────────────────────────────
function ChatAgentActivity({ toolCalls, expanded, onToggle }) {
  if (!toolCalls || !toolCalls.length) return null;
  const summary = React.useMemo(() => toolSummary(toolCalls), [toolCalls]);
  const files = React.useMemo(() => extractFilesFromTools(toolCalls), [toolCalls]);

  return React.createElement('div', {
    style: { marginTop: 8, borderTop: '1px solid ' + T.border, paddingTop: 6 }
  },
    React.createElement('div', {
      onClick: onToggle,
      style: {
        display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer',
        fontSize: 11, color: T.textMuted, padding: '4px 0', userSelect: 'none'
      }
    },
      React.createElement(I, { name: 'wrench', size: 12, color: T.textMuted }),
      React.createElement('span', null, 'Agent Activity'),
      React.createElement(Badge, { label: String(toolCalls.length), color: T.accent }),
      React.createElement(I, {
        name: 'chevron', size: 12, color: T.textMuted,
        style: { transform: expanded ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform .15s' }
      })
    ),
    expanded && React.createElement('div', {
      style: { padding: '6px 0 2px 18px', fontSize: 11, lineHeight: 1.7 }
    },
      // Tool call list
      React.createElement('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 6 } },
        summary.map(([name, count]) =>
          React.createElement(Badge, {
            key: name,
            label: name + (count > 1 ? ' x' + count : ''),
            color: (typeof toolColors !== 'undefined' && toolColors[name]) || T.textMuted
          })
        )
      ),
      // Detailed tool calls
      toolCalls.slice(0, 20).map((tc, i) =>
        React.createElement('div', {
          key: i,
          style: { display: 'flex', gap: 6, alignItems: 'baseline', color: T.textDim, marginBottom: 2 }
        },
          React.createElement('span', {
            style: {
              fontFamily: "'JetBrains Mono', monospace", fontSize: 10,
              color: (typeof toolColors !== 'undefined' && toolColors[tc.tool]) || T.accent,
              flexShrink: 0, minWidth: 70
            }
          }, tc.tool || '?'),
          React.createElement('span', {
            style: { fontFamily: "'JetBrains Mono', monospace", fontSize: 10, opacity: 0.7, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }
          }, truncate(tc.args || '', 120))
        )
      ),
      toolCalls.length > 20 && React.createElement('div', { style: { color: T.textDim, fontSize: 10, marginTop: 4 } },
        '... and ' + (toolCalls.length - 20) + ' more tool calls'
      ),
      // Files referenced
      files.length > 0 && React.createElement('div', {
        style: { marginTop: 6, display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }
      },
        React.createElement('span', { style: { color: T.textMuted, fontSize: 10, marginRight: 2 } }, 'Files:'),
        files.map(f => React.createElement('span', {
          key: f,
          style: {
            fontFamily: "'JetBrains Mono', monospace", fontSize: 10, color: T.blue,
            background: T.blue + '12', padding: '1px 5px', borderRadius: 3
          }
        }, f))
      )
    )
  );
}

// ─── ChatMessage ─────────────────────────────────────
const ChatMessage = React.memo(function ChatMessage({ turn, isUser, onCopy, copiedId, expanded, onToggleActivity, searchHighlight }) {
  const msgRef = React.useRef(null);
  const [hovered, setHovered] = React.useState(false);

  const renderedHtml = React.useMemo(() => {
    if (isUser) return null;
    let html = renderMarkdown(turn.text || '');
    if (searchHighlight) html = highlightSearchInHtml(html, searchHighlight);
    return html;
  }, [turn.text, isUser, searchHighlight]);

  const handleClick = React.useCallback((e) => {
    const btn = e.target.closest('.chat-code-copy');
    if (btn) {
      e.preventDefault();
      const code = decodeURIComponent(btn.dataset.code);
      navigator.clipboard.writeText(code);
      btn.textContent = 'Copied!';
      setTimeout(() => btn.textContent = 'Copy', 1500);
    }
  }, []);

  const turnId = turn.id || turn.ts || turn.seq;
  const isCopied = copiedId === turnId;

  const bubbleStyle = isUser ? {
    maxWidth: '80%', marginLeft: 'auto',
    padding: '10px 14px', borderRadius: '12px 12px 4px 12px',
    background: T.accent + '12', border: '1px solid ' + T.accent + '20',
    color: T.text, fontSize: 13, lineHeight: 1.6, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
    position: 'relative'
  } : {
    maxWidth: '88%',
    padding: '10px 14px', borderRadius: '12px 12px 12px 4px',
    background: T.surface, border: '1px solid ' + T.border,
    color: T.text, fontSize: 13, lineHeight: 1.6, position: 'relative'
  };

  return React.createElement('div', {
    ref: msgRef,
    'data-turn-id': turnId,
    style: { marginBottom: 10, display: 'flex', flexDirection: 'column', alignItems: isUser ? 'flex-end' : 'flex-start' },
    onMouseEnter: () => setHovered(true),
    onMouseLeave: () => setHovered(false)
  },
    // Role label
    React.createElement('div', {
      style: { fontSize: 10, color: T.textMuted, marginBottom: 3, paddingLeft: isUser ? 0 : 2, paddingRight: isUser ? 2 : 0 }
    }, isUser ? 'You' : 'Assistant',
      turn.ts && React.createElement('span', {
        style: { marginLeft: 8, fontSize: 10, color: T.textDim }
      }, typeof timeAgo === 'function' ? timeAgo(turn.ts) : '')
    ),
    // Bubble
    React.createElement('div', { style: bubbleStyle, onClick: !isUser ? handleClick : undefined },
      // Copy button on hover
      hovered && React.createElement('button', {
        onClick: (e) => { e.stopPropagation(); onCopy(turnId, turn.text); },
        style: {
          position: 'absolute', top: 6, right: 6, background: T.surfaceAlt,
          border: '1px solid ' + T.border, borderRadius: 4, padding: '2px 6px',
          cursor: 'pointer', fontSize: 10, color: T.textMuted, display: 'flex', alignItems: 'center', gap: 3, zIndex: 2
        },
        title: 'Copy message'
      },
        React.createElement(I, { name: isCopied ? 'check' : 'copy', size: 10, color: isCopied ? T.accent : T.textMuted }),
        isCopied ? 'Copied' : 'Copy'
      ),
      // Content
      isUser
        ? React.createElement('span', null, searchHighlight ? React.createElement('span', { dangerouslySetInnerHTML: { __html: highlightSearchInHtml((turn.text || '').replace(/</g, '&lt;'), searchHighlight) } }) : (turn.text || ''))
        : React.createElement('div', { className: 'chat-md', dangerouslySetInnerHTML: { __html: renderedHtml } }),
      // Agent activity
      !isUser && turn.tool_calls && turn.tool_calls.length > 0 &&
        React.createElement(ChatAgentActivity, { toolCalls: turn.tool_calls, expanded: expanded, onToggle: onToggleActivity })
    ),
    // Token count if available
    turn.tokens && React.createElement('div', {
      style: { fontSize: 9, color: T.textDim, marginTop: 2, paddingLeft: isUser ? 0 : 2 }
    }, turn.tokens.toLocaleString() + ' tokens')
  );
}, (prev, next) => {
  return prev.turn === next.turn && prev.copiedId === next.copiedId
    && prev.expanded === next.expanded && prev.searchHighlight === next.searchHighlight;
});

// ─── ChatToolbar ─────────────────────────────────────
function ChatToolbar({ session, turns, toolbarSearch, onSearchChange, matchCount, matchIdx, onNext, onPrev, onCopyAll, onExport, showJump, onJumpBottom }) {
  const [exportOpen, setExportOpen] = React.useState(false);
  const exportRef = React.useRef(null);

  // Close export dropdown on outside click
  React.useEffect(() => {
    if (!exportOpen) return;
    const handler = (e) => { if (exportRef.current && !exportRef.current.contains(e.target)) setExportOpen(false); };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [exportOpen]);

  const turnCount = (session && session.turns) || (turns && turns.length) || 0;
  const source = (session && session.source) || '?';

  return React.createElement('div', {
    style: {
      display: 'flex', alignItems: 'center', gap: 8, padding: '6px 12px',
      borderBottom: '1px solid ' + T.border, background: T.bg, flexShrink: 0, flexWrap: 'wrap'
    }
  },
    // Session info
    React.createElement('div', { style: { display: 'flex', alignItems: 'center', gap: 6, marginRight: 'auto', minWidth: 0 } },
      React.createElement(Badge, { label: source, color: getSourceColor(source) }),
      React.createElement('span', { style: { fontSize: 11, color: T.textMuted } }, turnCount + ' turns'),
      session && session.started && React.createElement('span', {
        style: { fontSize: 10, color: T.textDim }
      }, typeof localDate === 'function' ? localDate(session.started) : session.started)
    ),
    // In-conversation search
    React.createElement('div', {
      style: { display: 'flex', alignItems: 'center', gap: 4, background: T.surface, borderRadius: 6, padding: '3px 8px', border: '1px solid ' + T.border }
    },
      React.createElement(I, { name: 'search', size: 12, color: T.textMuted }),
      React.createElement('input', {
        type: 'text', value: toolbarSearch, onChange: (e) => onSearchChange(e.target.value),
        placeholder: 'Search in conversation...',
        style: {
          background: 'none', border: 'none', outline: 'none', color: T.text,
          fontSize: 11, width: 140, fontFamily: 'inherit'
        }
      }),
      matchCount > 0 && React.createElement(React.Fragment, null,
        React.createElement('span', { style: { fontSize: 10, color: T.textMuted, whiteSpace: 'nowrap' } },
          (matchIdx + 1) + '/' + matchCount),
        React.createElement('button', {
          onClick: onPrev, style: { background: 'none', border: 'none', cursor: 'pointer', padding: 0 }
        }, React.createElement(I, { name: 'chevron', size: 12, color: T.textMuted, style: { transform: 'rotate(-90deg)' } })),
        React.createElement('button', {
          onClick: onNext, style: { background: 'none', border: 'none', cursor: 'pointer', padding: 0 }
        }, React.createElement(I, { name: 'chevron', size: 12, color: T.textMuted, style: { transform: 'rotate(90deg)' } }))
      )
    ),
    // Copy all
    React.createElement(Btn, { ghost: true, onClick: onCopyAll, style: { padding: '4px 8px', fontSize: 11 } },
      React.createElement(I, { name: 'copy', size: 12 }), ' Copy All'
    ),
    // Export dropdown
    React.createElement('div', { ref: exportRef, style: { position: 'relative' } },
      React.createElement(Btn, {
        ghost: true, onClick: () => setExportOpen(!exportOpen),
        style: { padding: '4px 8px', fontSize: 11 }
      }, React.createElement(I, { name: 'download', size: 12 }), ' Export'),
      exportOpen && React.createElement('div', {
        style: {
          position: 'absolute', top: '100%', right: 0, marginTop: 4, background: T.surface,
          border: '1px solid ' + T.border, borderRadius: 6, overflow: 'hidden', zIndex: 20, minWidth: 130
        }
      },
        ['markdown', 'json'].map(fmt =>
          React.createElement('div', {
            key: fmt,
            onClick: () => { onExport(fmt); setExportOpen(false); },
            style: {
              padding: '8px 14px', fontSize: 12, cursor: 'pointer', color: T.text,
              borderBottom: fmt === 'markdown' ? '1px solid ' + T.border : 'none'
            },
            onMouseEnter: (e) => e.currentTarget.style.background = T.surfaceAlt,
            onMouseLeave: (e) => e.currentTarget.style.background = 'transparent'
          }, 'Export as ' + fmt.charAt(0).toUpperCase() + fmt.slice(1))
        )
      )
    ),
    // Jump to bottom
    showJump && React.createElement(Btn, {
      ghost: true, onClick: onJumpBottom, style: { padding: '4px 8px', fontSize: 11 }
    }, React.createElement(I, { name: 'arrowDown', size: 12 }))
  );
}

// ─── ChatSessionCard ─────────────────────────────────
function ChatSessionCard({ session, selected, onClick }) {
  const [hovered, setHovered] = React.useState(false);
  const title = session.title || session.session_id || 'Untitled';
  const source = session.source || '?';
  const turns = session.turns || 0;
  const started = session.started;

  return React.createElement('div', {
    onClick: onClick,
    onMouseEnter: () => setHovered(true),
    onMouseLeave: () => setHovered(false),
    style: {
      padding: '10px 12px', cursor: 'pointer',
      borderLeft: selected ? '3px solid ' + T.accent : '3px solid transparent',
      background: selected ? T.accent + '08' : hovered ? T.surfaceAlt : 'transparent',
      borderBottom: '1px solid ' + T.border,
      transition: 'background .12s'
    }
  },
    React.createElement('div', {
      style: {
        fontSize: 12, fontWeight: 500, color: T.text, marginBottom: 4,
        overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical'
      }
    }, title),
    React.createElement('div', {
      style: { display: 'flex', alignItems: 'center', gap: 6, fontSize: 10, color: T.textMuted }
    },
      React.createElement('span', {
        style: {
          display: 'inline-block', padding: '1px 5px', borderRadius: 3, fontSize: 9,
          background: getSourceColor(source) + '18', color: getSourceColor(source),
          border: '1px solid ' + getSourceColor(source) + '30'
        }
      }, source),
      React.createElement('span', null, turns + ' turns'),
      started && React.createElement('span', { style: { marginLeft: 'auto', color: T.textDim } },
        typeof timeAgo === 'function' ? timeAgo(started) : started)
    )
  );
}

// ─── ChatSessionList ─────────────────────────────────
function ChatSessionList({ sessions, selectedId, onSelect, loading, searchQuery, onSearchChange, filterSource, onFilterChange, onSync, syncing, stats, searchResults, onSearchSelect }) {
  const displayList = searchResults || sessions;

  return React.createElement('div', {
    style: {
      width: 310, minWidth: 310, height: '100%', display: 'flex', flexDirection: 'column',
      borderRight: '1px solid ' + T.border, background: T.bg
    }
  },
    // Header controls
    React.createElement('div', {
      style: { padding: '8px 10px', borderBottom: '1px solid ' + T.border, display: 'flex', flexDirection: 'column', gap: 6 }
    },
      // Search row
      React.createElement('div', { style: { display: 'flex', gap: 6, alignItems: 'center' } },
        React.createElement('div', {
          style: {
            flex: 1, display: 'flex', alignItems: 'center', gap: 6,
            background: T.surface, borderRadius: 6, padding: '5px 8px', border: '1px solid ' + T.border
          }
        },
          React.createElement(I, { name: 'search', size: 13, color: T.textMuted }),
          React.createElement('input', {
            type: 'text', value: searchQuery, onChange: (e) => onSearchChange(e.target.value),
            placeholder: 'Search conversations...',
            style: { background: 'none', border: 'none', outline: 'none', color: T.text, fontSize: 12, width: '100%', fontFamily: 'inherit' }
          }),
          searchQuery && React.createElement('button', {
            onClick: () => onSearchChange(''),
            style: { background: 'none', border: 'none', cursor: 'pointer', padding: 0 }
          }, React.createElement(I, { name: 'xSmall', size: 12, color: T.textMuted }))
        ),
        React.createElement(Btn, {
          ghost: true, onClick: onSync, disabled: syncing,
          style: { padding: '5px 8px', fontSize: 11, whiteSpace: 'nowrap', opacity: syncing ? 0.5 : 1 }
        }, React.createElement(I, { name: 'refresh', size: 12, style: syncing ? { animation: 'spin 1s linear infinite' } : {} }), ' Sync')
      ),
      // Filter row
      React.createElement('div', { style: { display: 'flex', gap: 6, alignItems: 'center' } },
        React.createElement('select', {
          value: filterSource, onChange: (e) => onFilterChange(e.target.value),
          style: {
            background: T.surface, border: '1px solid ' + T.border, borderRadius: 4,
            color: T.text, fontSize: 11, padding: '3px 6px', outline: 'none', cursor: 'pointer'
          }
        },
          React.createElement('option', { value: '' }, 'All sources'),
          React.createElement('option', { value: 'claude' }, 'Claude'),
          React.createElement('option', { value: 'gemini' }, 'Gemini'),
          React.createElement('option', { value: 'imports' }, 'Imports')
        ),
        stats && React.createElement('span', { style: { fontSize: 10, color: T.textDim, marginLeft: 'auto' } },
          (stats.total_sessions || 0) + ' sessions')
      )
    ),
    // Session list
    React.createElement('div', { style: { flex: 1, overflowY: 'auto' } },
      loading
        ? [1, 2, 3, 4, 5].map(i => React.createElement('div', {
            key: i,
            style: { padding: '12px', borderBottom: '1px solid ' + T.border }
          },
            React.createElement('div', { style: { height: 14, background: T.surface, borderRadius: 4, marginBottom: 8, animation: 'pulse 1.5s infinite', width: '80%' } }),
            React.createElement('div', { style: { height: 10, background: T.surface, borderRadius: 3, animation: 'pulse 1.5s infinite', width: '50%' } })
          ))
        : searchResults
          ? searchResults.length === 0
            ? React.createElement('div', { style: { padding: 20, textAlign: 'center', color: T.textMuted, fontSize: 12 } }, 'No results found')
            : searchResults.map((r, i) => React.createElement('div', {
                key: i,
                onClick: () => onSearchSelect(r),
                style: {
                  padding: '8px 12px', cursor: 'pointer', borderBottom: '1px solid ' + T.border,
                  fontSize: 11
                },
                onMouseEnter: (e) => e.currentTarget.style.background = T.surfaceAlt,
                onMouseLeave: (e) => e.currentTarget.style.background = 'transparent'
              },
                React.createElement('div', { style: { fontWeight: 500, color: T.text, marginBottom: 3 } }, r.session_title || r.session_id),
                React.createElement('div', { style: { color: T.textMuted, fontSize: 10 } }, truncate(r.snippet || r.text || '', 120))
              ))
          : displayList.map(s => React.createElement(ChatSessionCard, {
              key: s.session_id, session: s, selected: s.session_id === selectedId,
              onClick: () => onSelect(s.session_id)
            }))
    ),
    // Stats footer
    stats && !searchResults && React.createElement('div', {
      style: {
        padding: '6px 12px', borderTop: '1px solid ' + T.border,
        fontSize: 10, color: T.textDim, display: 'flex', gap: 10
      }
    },
      React.createElement('span', null, (stats.total_sessions || 0) + ' sessions'),
      React.createElement('span', null, (stats.total_turns || 0) + ' turns')
    )
  );
}

// ─── ChatConversation ────────────────────────────────
function ChatConversation({ turns, turnsLoading, session, onLoadMore, hasMore, scrollToTurnId }) {
  const scrollRef = React.useRef(null);
  const [toolbarSearch, setToolbarSearch] = React.useState('');
  const [copiedId, setCopiedId] = React.useState(null);
  const [expandedActivity, setExpandedActivity] = React.useState({});
  const [showJump, setShowJump] = React.useState(false);
  const [copyAllFlash, setCopyAllFlash] = React.useState(false);

  // Search matching
  const { matchIndices, matchIdx, setMatchIdx } = (() => {
    const [idx, setIdx] = React.useState(0);
    const indices = React.useMemo(() => {
      if (!toolbarSearch || toolbarSearch.length < 2) return [];
      const q = toolbarSearch.toLowerCase();
      return turns.reduce((acc, t, i) => {
        if ((t.text || '').toLowerCase().includes(q)) acc.push(i);
        return acc;
      }, []);
    }, [turns, toolbarSearch]);
    return { matchIndices: indices, matchIdx: Math.min(idx, Math.max(0, indices.length - 1)), setMatchIdx: setIdx };
  })();

  // Auto-scroll to bottom on session change
  React.useEffect(() => {
    if (scrollRef.current && turns.length > 0) {
      setTimeout(() => {
        if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
      }, 50);
    }
  }, [session && session.session_id]);

  // Scroll to search match
  React.useEffect(() => {
    if (matchIndices.length > 0 && scrollRef.current) {
      const turnIdx = matchIndices[matchIdx];
      const el = scrollRef.current.querySelector('[data-turn-id]');
      const allEls = scrollRef.current.querySelectorAll('[data-turn-id]');
      if (allEls[turnIdx]) allEls[turnIdx].scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, [matchIdx, matchIndices]);

  // Scroll to specific turn (from cross-session search)
  React.useEffect(() => {
    if (scrollToTurnId && scrollRef.current) {
      const el = scrollRef.current.querySelector('[data-turn-id="' + scrollToTurnId + '"]');
      if (el) setTimeout(() => el.scrollIntoView({ behavior: 'smooth', block: 'center' }), 100);
    }
  }, [scrollToTurnId]);

  // Track scroll position for jump button
  const handleScroll = React.useCallback(() => {
    if (!scrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    setShowJump(scrollHeight - scrollTop - clientHeight > 200);
  }, []);

  const handleCopy = React.useCallback((turnId, text) => {
    navigator.clipboard.writeText(text || '');
    setCopiedId(turnId);
    setTimeout(() => setCopiedId(null), 2000);
  }, []);

  const handleCopyAll = React.useCallback(() => {
    if (!turns.length) return;
    const text = turns.map(t => {
      const role = t.role === 'user' ? 'User' : 'Assistant';
      let msg = '## ' + role + '\n\n' + (t.text || '');
      if (t.tool_calls && t.tool_calls.length) {
        msg += '\n\nTool calls:\n' + t.tool_calls.map(tc => '- ' + (tc.tool || '?') + ': ' + (tc.args || '')).join('\n');
      }
      return msg;
    }).join('\n\n---\n\n');
    navigator.clipboard.writeText(text);
    setCopyAllFlash(true);
    setTimeout(() => setCopyAllFlash(false), 2000);
  }, [turns]);

  const handleExport = React.useCallback(async (fmt) => {
    if (!session) return;
    try {
      const data = await api.get('/api/conversations/' + session.session_id + '/export?format=' + fmt);
      let blob, ext;
      if (fmt === 'json') {
        blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        ext = '.json';
      } else {
        blob = new Blob([data.markdown || ''], { type: 'text/markdown' });
        ext = '.md';
      }
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = (data.title || session.session_id || 'conversation').replace(/[^a-zA-Z0-9_-]/g, '_') + ext;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) { console.error('Export error:', e); }
  }, [session]);

  const toggleActivity = React.useCallback((turnId) => {
    setExpandedActivity(prev => ({ ...prev, [turnId]: !prev[turnId] }));
  }, []);

  const jumpToBottom = React.useCallback(() => {
    if (scrollRef.current) scrollRef.current.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, []);

  if (!session) {
    return React.createElement('div', {
      style: {
        flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
        color: T.textMuted, gap: 12
      }
    },
      React.createElement(I, { name: 'messageSquare', size: 48, color: T.textDim }),
      React.createElement('div', { style: { fontSize: 16, fontWeight: 500 } }, 'Select a conversation'),
      React.createElement('div', { style: { fontSize: 12, color: T.textDim } }, 'Choose a session from the list or sync new transcripts')
    );
  }

  return React.createElement('div', { style: { flex: 1, display: 'flex', flexDirection: 'column', height: '100%', minWidth: 0 } },
    // Toolbar
    React.createElement(ChatToolbar, {
      session, turns, toolbarSearch, onSearchChange: setToolbarSearch,
      matchCount: matchIndices.length, matchIdx,
      onNext: () => setMatchIdx(i => (i + 1) % matchIndices.length),
      onPrev: () => setMatchIdx(i => (i - 1 + matchIndices.length) % matchIndices.length),
      onCopyAll: handleCopyAll, onExport: handleExport, showJump, onJumpBottom: jumpToBottom
    }),
    // Copy all flash
    copyAllFlash && React.createElement('div', {
      style: { padding: '4px 12px', background: T.accent + '15', color: T.accent, fontSize: 11, textAlign: 'center' }
    }, 'Conversation copied to clipboard'),
    // Messages
    React.createElement('div', {
      ref: scrollRef, onScroll: handleScroll,
      style: { flex: 1, overflowY: 'auto', padding: '16px 20px' }
    },
      // Load more
      hasMore && React.createElement('div', { style: { textAlign: 'center', marginBottom: 12 } },
        React.createElement(Btn, { ghost: true, onClick: onLoadMore, style: { fontSize: 11, padding: '4px 12px' } },
          'Load earlier messages')
      ),
      turnsLoading
        ? [1, 2, 3].map(i => React.createElement('div', {
            key: i,
            style: {
              marginBottom: 16, padding: 14, borderRadius: 12,
              background: T.surface, border: '1px solid ' + T.border, maxWidth: i % 2 === 0 ? '80%' : '85%',
              marginLeft: i % 2 === 0 ? 'auto' : 0
            }
          },
            React.createElement('div', { style: { height: 12, background: T.surfaceAlt, borderRadius: 4, marginBottom: 8, width: '60%', animation: 'pulse 1.5s infinite' } }),
            React.createElement('div', { style: { height: 12, background: T.surfaceAlt, borderRadius: 4, width: '90%', animation: 'pulse 1.5s infinite' } }),
            React.createElement('div', { style: { height: 12, background: T.surfaceAlt, borderRadius: 4, marginTop: 6, width: '40%', animation: 'pulse 1.5s infinite' } })
          ))
        : turns.map((t, i) => {
            const turnId = t.id || t.ts || t.seq || i;
            return React.createElement(ChatMessage, {
              key: turnId, turn: t, isUser: t.role === 'user',
              onCopy: handleCopy, copiedId,
              expanded: !!expandedActivity[turnId],
              onToggleActivity: () => toggleActivity(turnId),
              searchHighlight: toolbarSearch.length >= 2 ? toolbarSearch : null
            });
          }),
      turns.length === 0 && !turnsLoading && React.createElement('div', {
        style: { textAlign: 'center', color: T.textDim, padding: 40, fontSize: 12 }
      }, 'No messages in this conversation')
    )
  );
}

// ─── ChatPanel (top-level) ───────────────────────────
function ChatPanel() {
  const [sessions, setSessions] = React.useState([]);
  const [selectedId, setSelectedId] = React.useState(null);
  const [turns, setTurns] = React.useState([]);
  const [loading, setLoading] = React.useState(true);
  const [turnsLoading, setTurnsLoading] = React.useState(false);
  const [searchQuery, setSearchQuery] = React.useState('');
  const [searchResults, setSearchResults] = React.useState(null);
  const [filterSource, setFilterSource] = React.useState('');
  const [syncing, setSyncing] = React.useState(false);
  const [stats, setStats] = React.useState(null);
  const [hasMore, setHasMore] = React.useState(false);
  const [turnOffset, setTurnOffset] = React.useState(0);
  const [scrollToTurnId, setScrollToTurnId] = React.useState(null);
  const searchTimerRef = React.useRef(null);
  const PAGE_SIZE = 50;

  // Load sessions on mount
  React.useEffect(() => {
    const load = async () => {
      try {
        setLoading(true);
        const [sessData, statsData] = await Promise.all([
          api.get('/api/conversations?limit=100'),
          api.get('/api/conversations/stats')
        ]);
        setSessions(sessData || []);
        setStats(statsData || null);
      } catch (e) { console.error('Failed to load conversations:', e); }
      finally { setLoading(false); }
    };
    load();
  }, []);

  // Load turns when session selected
  React.useEffect(() => {
    if (!selectedId) { setTurns([]); return; }
    const load = async () => {
      setTurnsLoading(true);
      setTurnOffset(0);
      try {
        const data = await api.get('/api/conversations/' + selectedId + '?limit=' + PAGE_SIZE);
        const arr = Array.isArray(data) ? data : (data.turns || []);
        setTurns(arr);
        setHasMore(arr.length >= PAGE_SIZE);
      } catch (e) { console.error('Failed to load turns:', e); setTurns([]); }
      finally { setTurnsLoading(false); }
    };
    load();
  }, [selectedId]);

  // Load more (earlier) turns
  const loadMore = React.useCallback(async () => {
    if (!selectedId) return;
    const newOffset = turnOffset + PAGE_SIZE;
    try {
      const data = await api.get('/api/conversations/' + selectedId + '?offset=' + newOffset + '&limit=' + PAGE_SIZE);
      const arr = Array.isArray(data) ? data : (data.turns || []);
      setTurns(prev => [...arr, ...prev]);
      setTurnOffset(newOffset);
      setHasMore(arr.length >= PAGE_SIZE);
    } catch (e) { console.error('Load more error:', e); }
  }, [selectedId, turnOffset]);

  // Debounced search
  const handleSearchChange = React.useCallback((q) => {
    setSearchQuery(q);
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    if (!q || q.length < 2) { setSearchResults(null); return; }
    searchTimerRef.current = setTimeout(async () => {
      try {
        const data = await api.get('/api/conversations/search?q=' + encodeURIComponent(q) + '&limit=30');
        setSearchResults(data || []);
      } catch (e) { setSearchResults([]); }
    }, 300);
  }, []);

  // Sync transcripts
  const handleSync = React.useCallback(async () => {
    setSyncing(true);
    try {
      await api.get('/api/conversations/sync');
      const [sessData, statsData] = await Promise.all([
        api.get('/api/conversations?limit=100'),
        api.get('/api/conversations/stats')
      ]);
      setSessions(sessData || []);
      setStats(statsData || null);
      setSearchResults(null);
      setSearchQuery('');
    } catch (e) { console.error('Sync error:', e); }
    finally { setSyncing(false); }
  }, []);

  // Filter sessions by source
  const filteredSessions = React.useMemo(() => {
    if (!filterSource) return sessions;
    return sessions.filter(s => (s.source || '').toLowerCase() === filterSource);
  }, [sessions, filterSource]);

  // Handle search result click
  const handleSearchSelect = React.useCallback((result) => {
    const sid = result.session_id;
    setSelectedId(sid);
    setSearchResults(null);
    setSearchQuery('');
    if (result.turn_id) setScrollToTurnId(result.turn_id);
  }, []);

  const selectedSession = React.useMemo(() =>
    sessions.find(s => s.session_id === selectedId) || null
  , [sessions, selectedId]);

  return React.createElement('div', {
    style: { display: 'flex', height: '100%', overflow: 'hidden' }
  },
    React.createElement(ChatSessionList, {
      sessions: filteredSessions, selectedId, onSelect: setSelectedId,
      loading, searchQuery, onSearchChange: handleSearchChange,
      filterSource, onFilterChange: setFilterSource,
      onSync: handleSync, syncing, stats, searchResults,
      onSearchSelect: handleSearchSelect
    }),
    React.createElement(ChatConversation, {
      turns, turnsLoading, session: selectedSession,
      onLoadMore: loadMore, hasMore, scrollToTurnId
    })
  );
}
