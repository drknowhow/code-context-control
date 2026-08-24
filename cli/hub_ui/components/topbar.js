// ─── Top bar ───────────────────────────────────────────────────
// Sticky header: brand + version + active-session badge on the left,
// global actions (search, guide, oracle, theme, refresh, settings) right.

function TopBar({ version, activeCount, darkMode, hubConfig, mainView, setMainView, onToggleTheme, onOpenSettings, onOpenSearch, onRefresh }) {
  // Single ghost look shared by every top-bar control (buttons and links).
  const ctrl = {
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 6,
    height: 30, minWidth: 30, padding: '0 8px',
    background: 'transparent', border: `1px solid ${T.border}`, borderRadius: 6,
    color: T.textMuted, cursor: 'pointer', fontSize: 12, textDecoration: 'none',
  };

  return (
    <header style={{
      position: 'sticky', top: 0, zIndex: 100, height: 52, flexShrink: 0,
      display: 'flex', alignItems: 'center', gap: 10, padding: '0 18px',
      background: T.surface, borderBottom: `1px solid ${T.border}`,
    }}>
      {/* Brand */}
      <span className="mono" style={{ color: T.accent, fontWeight: 700, fontSize: 15, letterSpacing: 1 }}>C3</span>
      <span style={{ fontSize: 13, fontWeight: 600, color: T.text, whiteSpace: 'nowrap' }}>Project Hub</span>
      {version && <Badge color={T.textMuted}>v{version}</Badge>}
      {activeCount > 0 && (
        <span className="mono" style={{
          display: 'inline-flex', alignItems: 'center', gap: 6,
          padding: '2px 9px', borderRadius: 999, border: `1px solid ${T.border}`,
          fontSize: 11, color: T.accent, whiteSpace: 'nowrap',
        }}>
          <GlowDot color={T.accent} size={7} />
          {activeCount} active
        </span>
      )}

      {/* Projects | Tasks view switcher */}
      {setMainView && (
        <div style={{
          display: 'inline-flex', marginLeft: 10, border: `1px solid ${T.border}`,
          borderRadius: 6, overflow: 'hidden',
        }}>
          {[['projects', 'Projects', 'layers'], ['board', 'Tasks', 'check'], ['ci', 'CI', 'play'], ['creds', 'Credentials', 'lock'], ['tokens', 'Tokens', 'gauge'], ['locks', 'Locks', 'gitBranch'], ['enforce', 'Discipline', 'shield']].map(([id, label, icon]) => (
            <button key={id} onClick={() => setMainView(id)} style={{
              display: 'inline-flex', alignItems: 'center', gap: 6, height: 28,
              padding: '0 12px', border: 'none', cursor: 'pointer', fontSize: 12,
              background: mainView === id ? T.accentDim : 'transparent',
              color: mainView === id ? T.accent : T.textMuted,
              fontWeight: mainView === id ? 700 : 400,
            }}>
              <I name={icon} size={12} color={mainView === id ? T.accent : T.textMuted} />
              {label}
            </button>
          ))}
        </div>
      )}

      <div style={{ flex: 1 }} />

      {/* Actions */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <button onClick={onOpenSearch} title="Search across projects (Ctrl+K)" style={ctrl}>
          <I name="search" size={13} color={T.textMuted} />
          <span className="mono" style={{ fontSize: 11, color: T.textDim }}>Ctrl K</span>
        </button>
        <a href="/guide/" target="_blank" rel="noopener" title="Open the C3 guide" style={ctrl}>
          <I name="fileText" size={13} color={T.textMuted} />
        </a>
        <a href="https://github.com/sponsors/drknowhow" target="_blank" rel="noopener" title="Sponsor C3 development" style={ctrl}>
          <I name="heart" size={13} color="#EA4AAA" />
        </a>
        {/* Goes through the hub, not straight at oracle_url: the hub redeems
            the Oracle's owner-only bootstrap key so the tab lands signed in.
            A raw link opens a read-only dashboard where every write 401s. */}
        {hubConfig && hubConfig.oracle_url && (
          <a href="/api/oracle/open" target="_blank" rel="noopener" title="Open Oracle (signed in)" style={ctrl}>
            <I name="external" size={13} color={T.textMuted} />
          </a>
        )}
        <button onClick={onToggleTheme} style={ctrl}
          title={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}>
          <I name={darkMode ? 'sun' : 'moon'} size={13} color={T.textMuted} />
        </button>
        <button onClick={onRefresh} title="Refresh projects" style={ctrl}>
          <I name="refresh" size={13} color={T.textMuted} />
        </button>
        <button onClick={onOpenSettings} title="Hub settings" style={ctrl}>
          <I name="settings" size={13} color={T.textMuted} />
        </button>
      </div>
    </header>
  );
}
