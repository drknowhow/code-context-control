// ─── Main App ─────────────────────────────
const BUILD_TIME = "2026-04-10 UI-v2";
const { useState, useEffect, useCallback, useRef } = React;

const tabs = [
  { id: "dashboard", label: "Dashboard", icon: "gauge" },
  { id: "chat", label: "Chat", icon: "messageSquare" },
  { id: "sessions", label: "Sessions", icon: "clock" },
  { id: "memory", label: "Memory", icon: "bookmark" },
  { id: "edits", label: "Edits", icon: "edit" },
  { id: "instructions", label: "Instructions", icon: "fileText" },
  { id: "settings", label: "Settings", icon: "settings" },
];

function App() {
  const [tab, setTab] = useState("dashboard");
  const [stats, setStats] = useState({});
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [health, setHealth] = useState(null);
  const [healthChecking, setHealthChecking] = useState(false);
  const [notifications, setNotifications] = useState([]);
  const [registry, setRegistry] = useState([]);

  // Sidebar state
  const [sidebarPinned, setSidebarPinned] = useState(() => {
    try { return localStorage.getItem("c3-sidebar-pinned") === "true"; } catch { return true; }
  });
  const [sidebarHover, setSidebarHover] = useState(false);
  const sidebarOpen = sidebarPinned || sidebarHover;

  const toggleSidebarPin = () => {
    const next = !sidebarPinned;
    setSidebarPinned(next);
    try { localStorage.setItem("c3-sidebar-pinned", String(next)); } catch { }
  };

  // Theme toggle
  const [darkMode, setDarkMode] = useState(true);
  T = darkMode ? DARK : LIGHT;
  // Sync data-theme for CSS-based theming (code blocks, markdown)
  React.useEffect(() => {
    document.body.dataset.theme = darkMode ? 'dark' : 'light';
    const dEl = document.getElementById('hljs-theme-dark');
    const lEl = document.getElementById('hljs-theme-light');
    if (dEl) dEl.disabled = !darkMode;
    if (lEl) lEl.disabled = darkMode;
  }, [darkMode]);

  // Data loading
  const loadHealth = useCallback(async () => {
    setHealthChecking(true);
    try { const h = await api.get('/api/health'); setHealth(h); } catch { }
    setHealthChecking(false);
  }, []);

  const loadNotifications = useCallback(async () => {
    try { const n = await api.get('/api/notifications'); setNotifications(Array.isArray(n) ? n : []); } catch { }
  }, []);

  const loadRegistry = useCallback(async () => {
    try { const r = await api.get('/api/registry'); setRegistry(Array.isArray(r) ? r : []); } catch { }
  }, []);

  const ackNotification = async (id) => {
    try { await api.post('/api/notifications/ack', { id }); } catch { }
    loadNotifications();
  };

  const ackAllNotifications = async () => {
    try { await api.post('/api/notifications/ack-all'); } catch { }
    setNotifications([]);
  };

  // Single consolidated poll
  useEffect(() => {
    const load = async () => {
      try {
        const [s] = await Promise.all([api.get('/api/stats')]);
        setStats(s);
        setConnected(true);
      } catch { setConnected(false); }
      setLoading(false);
    };
    load();
    loadHealth();
    loadNotifications();
    loadRegistry();
    const iv = setInterval(() => {
      load();
      loadNotifications();
    }, 15000);
    const hv = setInterval(loadHealth, 30000);
    const rv = setInterval(loadRegistry, 30000);
    return () => { clearInterval(iv); clearInterval(hv); clearInterval(rv); };
  }, [loadNotifications, loadHealth, loadRegistry]);

  const renderPanel = () => {
    // Use display:none instead of unmounting to preserve state
    return (
      <>
        <div style={{ display: tab === "dashboard" ? "block" : "none", height: "100%" }}>
          <Dashboard stats={stats} loading={loading} notifications={notifications}
            ackNotification={ackNotification} ackAllNotifications={ackAllNotifications} />
        </div>
        <div style={{ display: tab === "chat" ? "flex" : "none", height: "100%", flexDirection: "column" }}>
          <ChatPanel />
        </div>
        <div style={{ display: tab === "sessions" ? "block" : "none", height: "100%" }}>
          <SessionsPanel />
        </div>
        <div style={{ display: tab === "memory" ? "block" : "none", height: "100%" }}>
          <Memory />
        </div>
        <div style={{ display: tab === "edits" ? "block" : "none", height: "100%" }}>
          <EditsPanel />
        </div>
        <div style={{ display: tab === "instructions" ? "block" : "none", height: "100%" }}>
          <Instructions />
        </div>
        <div style={{ display: tab === "settings" ? "block" : "none", height: "100%" }}>
          <SettingsPanel stats={stats} />
        </div>
      </>
    );
  };

  return (
    <div style={{ display: "flex", height: "100vh", width: "100vw", background: T.bg, overflow: "hidden" }}>
      <Sidebar
        tab={tab} setTab={setTab} tabs={tabs}
        sidebarOpen={sidebarOpen} sidebarPinned={sidebarPinned}
        toggleSidebarPin={toggleSidebarPin} setSidebarHover={setSidebarHover}
        connected={connected} health={health} healthChecking={healthChecking}
        loadHealth={loadHealth} registry={registry}
      />

      {/* Main content */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        {/* Top bar */}
        <div style={{
          padding: "10px 20px", borderBottom: `1px solid ${T.border}`,
          display: "flex", alignItems: "center", justifyContent: "space-between",
          background: T.surface, flexShrink: 0
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <I name={tabs.find(t => t.id === tab)?.icon || "gauge"} size={16} color={T.accent} />
            <span style={{ fontSize: 14, fontWeight: 600, color: T.text }}>
              {tabs.find(t => t.id === tab)?.label}
            </span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            {notifications.length > 0 && (
              <Badge color={T.warn}>{notifications.length} alerts</Badge>
            )}
            <button onClick={async () => {
                try { const h = await api.get('/api/hub/info'); window.location.href = h.url; }
                catch { window.location.href = 'http://localhost:3330'; }
              }} title="Back to Projects Hub"
              style={{
                height: 28, padding: "0 10px", borderRadius: 6, border: `1px solid ${T.warn}40`,
                background: `${T.warn}12`, color: T.warn, cursor: "pointer",
                display: "flex", alignItems: "center", gap: 4, fontSize: 11,
                fontFamily: "'JetBrains Mono', monospace", fontWeight: 600
              }}>
              Hub
            </button>
            <a href="/nano" title="Switch to Nano view"
              style={{
                height: 28, padding: "0 10px", borderRadius: 6, border: `1px solid ${T.accent}40`,
                background: `${T.accent}12`, color: T.accent, textDecoration: "none",
                display: "flex", alignItems: "center", gap: 4, fontSize: 11,
                fontFamily: "'JetBrains Mono', monospace", fontWeight: 600
              }}>
              Nano
            </a>
            <a href="/edits" title="Edit Ledger — version timeline"
              style={{
                height: 28, padding: "0 10px", borderRadius: 6, border: `1px solid ${T.purple}40`,
                background: `${T.purple}12`, color: T.purple, textDecoration: "none",
                display: "flex", alignItems: "center", gap: 4, fontSize: 11,
                fontFamily: "'JetBrains Mono', monospace", fontWeight: 600
              }}>
              Ledger
            </a>
            <button onClick={() => setDarkMode(!darkMode)} title="Toggle theme"
              style={{
                width: 28, height: 28, borderRadius: 6, border: `1px solid ${T.border}`,
                background: "transparent", cursor: "pointer", display: "flex",
                alignItems: "center", justifyContent: "center"
              }}>
              <I name={darkMode ? "sun" : "moon"} size={13} color={T.textMuted} />
            </button>
            <span className="mono" style={{ fontSize: 9, color: T.textDim }}>{BUILD_TIME}</span>
          </div>
        </div>

        {/* Content area */}
        <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
          {renderPanel()}
        </div>
      </div>
    </div>
  );
}

ReactDOM.render(<App />, document.getElementById("root"));
