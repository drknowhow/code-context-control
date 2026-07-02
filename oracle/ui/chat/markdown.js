// oracle/ui/chat/markdown.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Chat system ──
// ═══════════════════════════════════════════════════════════
let chatCurrentConvId = null;
let chatStreaming = false;
let chatAbortController = null;
let chatUserScrolled = false;

// ── Markdown renderer (marked.js + highlight.js) ──
const _mdRenderer = new marked.Renderer();
_mdRenderer.code = function(code, lang) {
  const language = (lang || '').trim();
  let highlighted;
  try {
    highlighted = language && hljs.getLanguage(language)
      ? hljs.highlight(code, { language }).value
      : hljs.highlightAuto(code).value;
  } catch { highlighted = esc(code); }
  const label = language || 'text';
  return '<div class="code-block-wrap">'
    + '<div class="code-block-header"><span>' + esc(label) + '</span>'
    + '<button class="code-copy-btn" onclick="chatCopyCode(this)" data-code="' + encodeURIComponent(code) + '">Copy</button></div>'
    + '<pre><code class="hljs">' + highlighted + '</code></pre></div>';
};
marked.setOptions({ renderer: _mdRenderer, gfm: true, breaks: true, pedantic: false });

function renderMarkdown(text) {
  if (!text) return '';
  try { return marked.parse(text); }
  catch { return '<pre>' + esc(text) + '</pre>'; }
}

