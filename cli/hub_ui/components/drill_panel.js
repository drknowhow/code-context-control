// ─── Drill panel: right slide-over with per-project tabs ───────
// Shared helpers below (DrillCenter/DrillMsg/DrillKV/DrillSection/
// DrillNeedsInit/drillFieldStyle) are used by drill_views.js,
// drill_health.js, config_editor.js and mcp_manager.js.

const DRILL_PANEL_TABS = [
  ['overview', 'Overview'],
  ['tasks', 'Tasks'],
  ['artifacts', 'Artifacts'],
  ['memory', 'Memory'],
  ['ledger', 'Ledger'],
  ['sessions', 'Sessions'],
  ['health', 'Health'],
  ['budget', 'Budget'],
  ['config', 'Config'],
  ['mcp', 'MCP'],
];

function drillFieldStyle(extra) {
  return Object.assign({
    background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 6,
    color: T.text, fontSize: 12, padding: '7px 10px', outline: 'none', minWidth: 0,
  }, extra || {});
}

function DrillCenter({ children }) {
  return (
    <div className="fade-up" style={{
      display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
      gap: 10, padding: '60px 20px', textAlign: 'center',
    }}>{children}</div>
  );
}

function DrillMsg({ text, color }) {
  return (
    <div style={{ padding: '28px 0', textAlign: 'center', fontSize: 12, color: color || T.textMuted }}>
      {text}
    </div>
  );
}

// Two grid cells (label + value) for a `gridTemplateColumns: '110px 1fr'` grid.
function DrillKV({ label, mono, title, children }) {
  const empty = children == null || children === '';
  return (
    <React.Fragment>
      <div style={{
        color: T.textMuted, fontSize: 11, fontWeight: 600, textTransform: 'uppercase',
        letterSpacing: 1, paddingTop: 2,
      }}>{label}</div>
      <div className={mono ? 'mono' : undefined} title={title} style={{
        color: T.text, fontSize: mono ? 11 : 12, minWidth: 0, overflowWrap: 'anywhere', lineHeight: 1.5,
      }}>
        {empty ? <span style={{ color: T.textDim }}>&mdash;</span> : children}
      </div>
    </React.Fragment>
  );
}

function DrillSection({ label, children, style }) {
  return (
    <div style={Object.assign({ marginTop: 26 }, style || {})}>
      <div style={{
        fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 1,
        color: T.textMuted, marginBottom: 10,
      }}>{label}</div>
      {children}
    </div>
  );
}

// Centered CTA rendered by drill views when the backend answers
// HTTP 409 {needs_init:true} (project has no .c3 workspace yet).
function DrillNeedsInit({ project, onReady }) {
  const [running, setRunning] = useState(false);
  const run = async () => {
    if (running) return;
    setRunning(true);
    try {
      const d = await api.post('/api/projects/run-init', { path: project.path });
      if (d && d.success) {
        notify('c3 init completed', 'ok');
        if (onReady) onReady();
      } else {
        const tail = d && d.output ? ': ' + String(d.output).slice(-140) : '';
        notify('c3 init failed' + tail, 'err');
      }
    } catch (e) {
      notify('c3 init failed: ' + e.message, 'err');
    }
    setRunning(false);
  };
  return (
    <DrillCenter>
      <I name="folder" size={22} color={T.textDim} />
      <div style={{ fontSize: 13, fontWeight: 700, color: T.text }}>This project isn't initialized</div>
      <div style={{ fontSize: 12, color: T.textMuted, maxWidth: 340, lineHeight: 1.5 }}>
        No <span className="mono">.c3</span> workspace found at this path.
        Run init to index the project and enable drill-in views.
      </div>
      <Btn onClick={run} disabled={running}>
        {running ? 'Running c3 init…' : 'Run c3 init'}
      </Btn>
    </DrillCenter>
  );
}

function DrillPanel({ project, tab, setTab, onClose, onChanged, onOpenModal }) {
  const renderTab = () => {
    switch (tab) {
      case 'tasks': return <DrillTasks project={project} onChanged={onChanged} />;
      case 'artifacts': return <DrillArtifacts project={project} onChanged={onChanged} />;
      case 'memory': return <DrillMemory project={project} />;
      case 'ledger': return <DrillLedger project={project} />;
      case 'sessions': return <DrillSessions project={project} />;
      case 'health': return <DrillHealth project={project} onChanged={onChanged} />;
      case 'budget': return <DrillBudget project={project} />;
      case 'config': return <ConfigEditor project={project} />;
      case 'mcp': return <McpManager project={project} onChanged={onChanged} />;
      default: return <DrillOverview project={project} onChanged={onChanged} setTab={setTab} />;
    }
  };
  return (
    <React.Fragment>
      <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: '#00000060', zIndex: 249 }} />
      <div style={{
        position: 'fixed', top: 0, right: 0, bottom: 0, width: 'min(720px, 92vw)',
        background: T.surface, borderLeft: `1px solid ${T.border}`, zIndex: 250,
        display: 'flex', flexDirection: 'column', animation: 'slideInRight 0.25s ease',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '16px 20px 8px' }}>
          <GlowDot color={project.active ? T.accent : T.textDim} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{
              fontSize: 15, fontWeight: 700, color: T.text,
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
            }}>{project.name || project.path}</div>
            <div className="mono" title={project.path} style={{
              fontSize: 11, color: T.textMuted,
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
            }}>{project.path}</div>
          </div>
          <button onClick={onClose} title="Close" style={{
            background: 'none', border: 'none', cursor: 'pointer', padding: 6,
            display: 'flex', alignItems: 'center',
          }}>
            <I name="xSmall" size={14} color={T.textMuted} />
          </button>
        </div>
        <div style={{
          display: 'flex', gap: 2, padding: '4px 20px 0',
          borderBottom: `1px solid ${T.border}`, overflowX: 'auto', flexShrink: 0,
        }}>
          {DRILL_PANEL_TABS.map(([id, label]) => (
            <button key={id} onClick={() => setTab(id)} style={{
              background: 'none', border: 'none', cursor: 'pointer', padding: '8px 10px',
              fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 1,
              color: tab === id ? T.accent : T.textMuted, whiteSpace: 'nowrap',
              borderBottom: `2px solid ${tab === id ? T.accent : 'transparent'}`,
            }}>{label}</button>
          ))}
        </div>
        <div style={{ flex: 1, overflowY: 'auto', padding: 20 }}>
          {renderTab()}
        </div>
      </div>
    </React.Fragment>
  );
}
