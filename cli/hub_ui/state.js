// ─── Hub state helpers & constants ─────────────────────────────
const IDE_LABELS = {
  'claude-code': 'Claude Code CLI',
  'claude-app': 'Claude Code App',
  'vscode': 'VS Code',
  'cursor': 'Cursor',
  'codex': 'Codex CLI',
  'gemini': 'Gemini CLI',
  'antigravity': 'Antigravity',
};

const IDE_OPTIONS = [
  { id: 'claude-code', name: 'Claude Code CLI', icon: '\u{1F916}', cmd: 'claude' },
  { id: 'claude-app', name: 'Claude Code App', icon: '\u{1F4D0}', cmd: 'claude-app' },
  { id: 'codex', name: 'Codex CLI', icon: '\u{1F9E0}', cmd: 'codex' },
  { id: 'gemini', name: 'Gemini CLI', icon: '\u{1F48E}', cmd: 'gemini' },
  { id: 'antigravity', name: 'Antigravity', icon: '\u{1F680}', cmd: 'antigravity' },
  { id: 'vscode', name: 'VS Code', icon: '\u{1F4BB}', cmd: 'code' },
  { id: 'cursor', name: 'Cursor', icon: '⚡', cmd: 'cursor' },
  { id: 'custom', name: 'Custom', icon: '⌨', cmd: '...' },
];

const ideLabel = (ide) => IDE_LABELS[ide] || ide || 'unknown';

// Repeating poll that pauses while the tab is hidden.
const usePoll = (fn, ms) => {
  React.useEffect(() => {
    const iv = setInterval(() => {
      if (!document.hidden) fn();
    }, ms);
    return () => clearInterval(iv);
  }, [fn, ms]);
};

// ── Project filtering (sidebar group + search box) ─────────────
const projectMatchesFilter = (p, filter) => {
  if (!filter || filter === 'all') return true;
  if (filter === 'active') return !!p.active;
  if (filter === 'idle') return !p.active;
  if (filter.startsWith('tag:')) {
    const tag = filter.slice(4).toLowerCase();
    return (p.tags || []).some(t => (t || '').toLowerCase() === tag ||
      (t || '').toLowerCase().startsWith(tag + '/'));
  }
  return true;
};

const projectMatchesSearch = (p, search) => {
  const q = (search || '').trim().toLowerCase();
  if (!q) return true;
  return [(p.name || ''), (p.path || ''), (p.ide || '')]
    .some(v => v.toLowerCase().includes(q));
};

// Filter, keeping parents visible when any of their children match
// (and vice versa) so the tree never renders orphaned rows.
const filterProjects = (projects, filter, search) => {
  const pass = p => projectMatchesFilter(p, filter) && projectMatchesSearch(p, search);
  const byPath = {};
  projects.forEach(p => { byPath[(p.path || '').toLowerCase()] = p; });
  const keep = new Set();
  projects.forEach(p => {
    if (!pass(p)) return;
    keep.add(p.path);
    if (p.parent_path) {
      const parent = byPath[(p.parent_path || '').toLowerCase()];
      if (parent) keep.add(parent.path);
    }
  });
  projects.forEach(p => {
    if (p.parent_path && keep.has(p.path)) return;
    if (!p.parent_path && keep.has(p.path)) {
      projects.forEach(c => {
        if (c.parent_path && (c.parent_path || '').toLowerCase() === (p.path || '').toLowerCase()
          && projectMatchesSearch(c, search)) keep.add(c.path);
      });
    }
  });
  return projects.filter(p => keep.has(p.path));
};

// ── Tree building ──────────────────────────────────────────────
// Groups registry entries by parent_path (feature-detected: entries
// without the field render flat). Returns [{project, children: [...]}].
const buildProjectTree = (projects) => {
  const byPath = {};
  projects.forEach(p => { byPath[(p.path || '').toLowerCase()] = p; });
  const roots = [];
  const childrenOf = {};
  projects.forEach(p => {
    const parentKey = (p.parent_path || '').toLowerCase();
    if (parentKey && byPath[parentKey]) {
      p.parent_name = byPath[parentKey].name || '';
      (childrenOf[parentKey] = childrenOf[parentKey] || []).push(p);
    } else {
      // Child rendered as a root (parent filtered out or unregistered):
      // keep parent context visible on the card via a path-derived name.
      if (parentKey) p.parent_name = (p.parent_path || '').split(/[\\/]/).filter(Boolean).pop() || '';
      roots.push(p);
    }
  });
  return roots.map(p => ({
    project: p,
    children: childrenOf[(p.path || '').toLowerCase()] || [],
  }));
};

// Rollup chips for a parent card, computed client-side from child rows.
const treeRollup = (children) => ({
  count: children.length,
  active: children.filter(c => c.active).length,
  alerts: children.reduce((n, c) => n + (c.notification_count || 0), 0),
});

// ── Tag tree for the sidebar (hierarchical tags via "/") ───────
const buildTagTree = (projects) => {
  const root = {};
  projects.forEach(p => (p.tags || []).forEach(tag => {
    if (!tag) return;
    let node = root;
    let prefix = '';
    tag.split('/').forEach(part => {
      prefix = prefix ? `${prefix}/${part}` : part;
      node.children = node.children || {};
      node.children[part] = node.children[part] || { full: prefix, count: 0 };
      node = node.children[part];
    });
    node.count = (node.count || 0) + 1;
  }));
  return root.children || {};
};

// ── Confirm dialog (shared by destructive actions) ─────────────
const ConfirmDialog = ({ title, message, confirmLabel = 'Confirm', danger, onConfirm, onCancel }) => (
  <div onClick={onCancel} style={{
    position: 'fixed', inset: 0, background: '#00000090', zIndex: 300,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
  }}>
    <div onClick={e => e.stopPropagation()} className="fade-up" style={{
      background: T.surface, border: `1px solid ${T.border}`, borderRadius: 10,
      padding: 24, width: 420, maxWidth: '90vw',
    }}>
      <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 8, color: T.text }}>{title}</div>
      <div style={{ fontSize: 12, color: T.textMuted, marginBottom: 18, lineHeight: 1.5 }}>{message}</div>
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <Btn variant="ghost" onClick={onCancel}>Cancel</Btn>
        <Btn color={danger ? T.error : T.accent} onClick={onConfirm}>{confirmLabel}</Btn>
      </div>
    </div>
  </div>
);
