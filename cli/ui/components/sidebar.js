// ─── Sidebar ─────────────────────────────
// Switch between ACTIVE projects: other running UI servers (direct jump)
// plus projects with a live agent session but no UI yet (launch, then
// jump). The cheap polled /api/registry prop only seeds the count hint;
// the authoritative list (/api/registry/active — it probes ports) loads
// on demand when the menu opens, never on a poll.
function ProjectSwitcher({ registry }) {
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState(null);   // null = scanning
  const [busyPath, setBusyPath] = useState(null); // path being launched
  const ref = React.useRef(null);
  const myPort = parseInt(window.location.port) || 3333;

  useEffect(() => {
    if (!open) return;
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const entryName = (e) => {
    if (e.project_name && e.project_name.trim()) return e.project_name.trim();
    if (e.project_path) {
      const parts = e.project_path.replace(/\\/g, "/").split("/").filter(Boolean);
      return parts[parts.length - 1] || "Unknown";
    }
    return "Unknown";
  };

  const others = (list) => (list || []).filter(e => e.port !== myPort);

  const loadActive = async () => {
    setEntries(null);
    try {
      const r = await api.get('/api/registry/active');
      setEntries(others(Array.isArray(r) ? r : []));
    } catch { setEntries(others(registry)); }
  };

  const toggle = () => { const next = !open; setOpen(next); if (next) loadActive(); };

  // Portless entry = live agent session without a UI server: launch one,
  // poll until it registers a port, then navigate this tab to it.
  const jump = async (e) => {
    if (busyPath) return;
    setBusyPath(e.project_path);
    try {
      await api.post('/api/registry/launch', { path: e.project_path });
      const poll = async (tries) => {
        let list = [];
        try { list = await api.get('/api/registry/active'); } catch { }
        const row = (Array.isArray(list) ? list : []).find(x =>
          (x.project_path || '').toLowerCase() === (e.project_path || '').toLowerCase() && x.port);
        if (row) { window.location.href = `http://localhost:${row.port}`; return; }
        if (tries >= 15) { setBusyPath(null); return; }
        setTimeout(() => poll(tries + 1), 1500);
      };
      setTimeout(() => poll(0), 1200);
    } catch { setBusyPath(null); }
  };

  const hint = others(registry).length;

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button onClick={toggle} title="Switch to another active project"
        style={{
          width: "100%", padding: "4px 6px", borderRadius: 4, border: `1px solid ${T.border}`,
          background: "transparent", cursor: "pointer", display: "flex", alignItems: "center",
          gap: 5, fontSize: 10, color: T.textDim
        }}>
        <I name="shuffle" size={10} color={T.textDim} />
        <span>Switch project</span>
        {hint > 0 && <span className="mono" style={{ marginLeft: "auto", color: T.textMuted }}>{hint}</span>}
      </button>
      {open && (
        <div style={{
          position: "absolute", bottom: "100%", left: 0, marginBottom: 4, zIndex: 100,
          background: T.surface, border: `1px solid ${T.border}`, borderRadius: 6,
          boxShadow: "0 4px 12px rgba(0,0,0,0.3)", minWidth: 220, overflow: "hidden"
        }}>
          {entries === null && (
            <div style={{ padding: "8px 12px", fontSize: 11, color: T.textDim }}>Scanning active sessions…</div>
          )}
          {entries !== null && entries.length === 0 && (
            <div style={{ padding: "8px 12px", fontSize: 11, color: T.textDim }}>No other active projects.</div>
          )}
          {(entries || []).map(e => (
            <a key={e.project_path || e.port}
              href={e.port ? `http://localhost:${e.port}` : '#'}
              onClick={(ev) => { if (!e.port) { ev.preventDefault(); jump(e); } }}
              style={{
                display: "flex", alignItems: "center", gap: 8, padding: "8px 12px",
                color: T.text, textDecoration: "none", fontSize: 12,
                borderBottom: `1px solid ${T.border}20`, transition: "background 0.1s",
                opacity: busyPath && busyPath !== e.project_path ? 0.5 : 1
              }}
              onMouseEnter={ev => ev.currentTarget.style.background = T.surfaceAlt}
              onMouseLeave={ev => ev.currentTarget.style.background = "transparent"}>
              <GlowDot color={e.port ? T.accent : T.warn} size={5} />
              <span style={{ flex: 1 }}>{entryName(e)}</span>
              <span className="mono" style={{ fontSize: 9, color: T.textDim }}>
                {e.port ? `:${e.port}` : (busyPath === e.project_path ? 'starting…' : 'session')}
              </span>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

const Sidebar = ({ tab, setTab, tabs, sidebarOpen, sidebarPinned, toggleSidebarPin, setSidebarHover, connected, health, healthChecking, loadHealth, registry }) => (
  <div style={{
    width: sidebarOpen ? 210 : 54, flexShrink: 0, background: T.surface, borderRight: `1px solid ${T.border}`,
    display: "flex", flexDirection: "column", transition: "width 0.25s ease", overflow: "hidden"
  }}
    onMouseEnter={() => setSidebarHover(true)}
    onMouseLeave={() => setSidebarHover(false)}>
    <div style={{
      padding: sidebarOpen ? "16px 14px" : "16px 10px", borderBottom: `1px solid ${T.border}`,
      display: "flex", alignItems: "center", gap: 10
    }}>
      <div style={{
        width: 32, height: 32, borderRadius: 8, background: `${T.accent}18`, border: `1px solid ${T.accent}40`,
        display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0
      }}>
        <I name="terminal" size={16} color={T.accent} />
      </div>
      {sidebarOpen && (
        <>
          <div>
            <div style={{ fontSize: 15, fontWeight: 700, color: T.text, letterSpacing: -0.5, lineHeight: 1 }}>
              C<span style={{ color: T.accent }}>3</span>
            </div>
            <div style={{ fontSize: 9, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1.5, marginTop: 2 }}>Context Control</div>
          </div>
          <button onClick={toggleSidebarPin} title={sidebarPinned ? "Unpin sidebar" : "Pin sidebar open"}
            style={{
              marginLeft: "auto", width: 22, height: 22, borderRadius: 4, border: "none",
              background: "transparent", cursor: "pointer", display: "flex", alignItems: "center",
              justifyContent: "center", flexShrink: 0
            }}>
            <I name="pin" size={12} color={sidebarPinned ? T.accent : T.textDim} />
          </button>
        </>
      )}
    </div>

    <nav style={{ padding: "8px 6px", flex: 1, display: "flex", flexDirection: "column", gap: 2 }}>
      {tabs.map(t => {
        const active = tab === t.id;
        return (
          <button key={t.id} onClick={() => setTab(t.id)}
            style={{
              display: "flex", alignItems: "center", gap: 10, width: "100%",
              padding: sidebarOpen ? "9px 12px" : "9px 0", justifyContent: sidebarOpen ? "flex-start" : "center",
              borderRadius: 6, border: "none", cursor: "pointer",
              background: active ? T.accentDim : "transparent", color: active ? T.accent : T.textMuted,
              fontSize: 13, fontWeight: active ? 600 : 400, transition: "all 0.15s", position: "relative"
            }}>
            {active && <div style={{ position: "absolute", left: 0, top: "20%", bottom: "20%", width: 3, borderRadius: "0 2px 2px 0", background: T.accent }} />}
            <I name={t.icon} size={16} />
            {sidebarOpen && <span>{t.label}</span>}
          </button>
        );
      })}
    </nav>

    {sidebarOpen && (
      <div style={{ padding: "10px 14px", borderTop: `1px solid ${T.border}`, fontSize: 10, color: T.textDim }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
          <GlowDot color={connected ? T.accent : T.error} size={5} />
          <span style={{ color: T.textMuted }}>{connected ? "Connected" : "Disconnected"}</span>
          {health?.session && (
            <span className="mono" style={{ marginLeft: "auto", color: T.textDim, fontSize: 9 }}>
              {health.session.tool_calls} calls
            </span>
          )}
          <button onClick={loadHealth} disabled={healthChecking} title="Check connections"
            style={{
              marginLeft: health?.session ? 0 : "auto", padding: "1px 4px", borderRadius: 3,
              border: `1px solid ${T.border}`, background: "transparent",
              cursor: healthChecking ? "default" : "pointer", display: "flex", alignItems: "center",
              opacity: healthChecking ? 0.5 : 1
            }}>
            <I name="refresh" size={9} color={T.textDim}
              style={healthChecking ? { animation: "spin 0.6s linear infinite" } : {}} />
          </button>
        </div>
        {health?.sources && (
          <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginBottom: 3 }}>
            {Object.entries(health.sources).map(([name, ok]) => (
              <span key={name} style={{
                padding: "1px 5px", borderRadius: 3, fontSize: 9, fontFamily: "'JetBrains Mono', monospace",
                background: ok ? `${T.accent}18` : `${T.error}18`,
                color: ok ? T.accent : T.error,
                border: `1px solid ${ok ? T.accent : T.error}30`,
              }}>{name}</span>
            ))}
          </div>
        )}
        <ProjectSwitcher registry={registry} />
      </div>
    )}
  </div>
);
