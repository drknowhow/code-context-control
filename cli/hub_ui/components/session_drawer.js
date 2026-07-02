// ─── Session drawer (bottom-docked) ────────────────────────────
// One project at a time. Logs tab: POST /api/projects/activity
// {path, limit} -> {events, latest_timestamp, project}. Notifications
// tab: POST /api/projects/notifications {path, limit} -> {notifications}.
// Polls every 4s while open (usePoll pauses when the tab is hidden).

const drawerSummarize = (ev) => {
  if (!ev) return 'Unknown event';
  if (ev.type === 'tool_call' && ev.tool_name) return `Tool call: ${ev.tool_name}`;
  if (ev.type === 'decision' && ev.reasoning) return ev.reasoning;
  if (ev.type === 'file_change' && ev.path) return `Changed ${ev.path}`;
  if (ev.type === 'fact_stored' && ev.fact) return `Stored fact: ${ev.fact}`;
  if (ev.type === 'session_start') return 'Session started';
  if (ev.type === 'session_save') return 'Session saved';
  return ev.summary || ev.message || ev.note || `Recorded ${ev.type || 'event'}`;
};

const drawerSevColor = (sev) => {
  const s = (sev || 'info').toLowerCase();
  if (s === 'error' || s === 'critical') return T.error;
  if (s === 'warn' || s === 'warning') return T.warn;
  return T.blue;
};

function SessionDrawer({ project, onClose }) {
  const [tab, setTab] = useState('logs');
  const [events, setEvents] = useState([]);
  const [notifs, setNotifs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [live, setLive] = useState({ active: !!project.active, port: project.port || null });

  const path = project.path;

  const refreshLogs = useCallback(async () => {
    try {
      const d = await api.post('/api/projects/activity', { path, limit: 120 });
      setEvents(Array.isArray(d.events) ? d.events : []);
      setLive({
        active: !!(d.project && d.project.active),
        port: (d.project && d.project.port) || null,
      });
      setError(null);
    } catch (e) { setError(e.message); }
    setLoading(false);
  }, [path]);

  const refreshNotifs = useCallback(async () => {
    try {
      const d = await api.post('/api/projects/notifications', { path, limit: 50 });
      setNotifs(Array.isArray(d.notifications) ? d.notifications : []);
    } catch (e) { /* keep last good list */ }
  }, [path]);

  // Reset + initial load whenever the drawer switches project.
  useEffect(() => {
    setEvents([]); setNotifs([]); setLoading(true); setError(null);
    refreshLogs();
    refreshNotifs();
  }, [path]);

  const poll = useCallback(() => {
    refreshLogs();
    if (tab === 'notifications') refreshNotifs();
  }, [refreshLogs, refreshNotifs, tab]);
  usePoll(poll, 4000);

  const clearAll = async () => {
    try {
      await api.post('/api/projects/notifications/clear', { path });
      notify('Notifications cleared');
      setNotifs([]);
    } catch (e) { notify('Clear failed: ' + e.message, 'err'); }
  };

  let body;
  if (tab === 'logs') {
    if (loading) {
      body = <div style={{ color: T.textMuted, fontSize: 12, padding: '14px 0' }}>Loading session activity…</div>;
    } else if (error) {
      body = <div style={{ color: T.error, fontSize: 12, padding: '14px 0' }}>Failed to load activity: {error}</div>;
    } else if (!events.length) {
      body = <div style={{ color: T.textMuted, fontSize: 12, padding: '14px 0' }}>No activity captured yet for this project.</div>;
    } else {
      body = events.map((ev, i) => (
        <div key={`${ev.timestamp || ''}-${i}`} className="mono" style={{
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '5px 0', borderBottom: `1px solid ${T.border}`, minWidth: 0,
        }}>
          <span style={{ fontSize: 11, color: T.textDim, width: 66, flexShrink: 0 }}>
            {localTime(ev.timestamp)}
          </span>
          <Badge color={typeColors[ev.type] || T.textMuted}>{ev.type || 'event'}</Badge>
          <span style={{
            fontSize: 11, color: T.textMuted,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>{drawerSummarize(ev)}</span>
        </div>
      ));
    }
  } else {
    if (!notifs.length) {
      body = <div style={{ color: T.textMuted, fontSize: 12, padding: '14px 0' }}>No pending notifications.</div>;
    } else {
      body = notifs.map((n, i) => (
        <div key={`${n.timestamp || ''}-${i}`} style={{
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '6px 0', borderBottom: `1px solid ${T.border}`, minWidth: 0,
        }}>
          <Badge color={drawerSevColor(n.severity)}>{n.severity || 'info'}</Badge>
          <span style={{
            fontSize: 12, color: T.text, flex: 1,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>{n.title || n.message || ''}</span>
          {n.agent && (
            <span className="mono" style={{ fontSize: 10, color: T.textDim, flexShrink: 0 }}>{n.agent}</span>
          )}
          <span className="mono" style={{ fontSize: 10, color: T.textDim, flexShrink: 0 }}>
            {localTime(n.timestamp)}
          </span>
        </div>
      ));
    }
  }

  return (
    <div className="fade-up" style={{
      position: 'fixed', bottom: 0, left: 0, right: 0, height: 300, zIndex: 200,
      background: T.surface, borderTop: `1px solid ${T.border}`,
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10, padding: '10px 16px',
        borderBottom: `1px solid ${T.border}`, flexShrink: 0, minWidth: 0,
      }}>
        <GlowDot color={live.active ? T.accent : T.textDim} />
        <span style={{ fontSize: 13, fontWeight: 600, color: T.text, whiteSpace: 'nowrap' }}>
          {project.name}
        </span>
        {live.port && (
          <a className="mono" href={`http://127.0.0.1:${live.port}`} target="_blank" rel="noopener noreferrer"
            style={{ fontSize: 11, color: T.accent, textDecoration: 'none' }}>:{live.port}</a>
        )}
        <span className="mono" title={path} style={{
          fontSize: 11, color: T.textDim, flex: 1, minWidth: 0,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>{path}</span>
        {['logs', 'notifications'].map(id => (
          <span key={id}
            onClick={() => { setTab(id); if (id === 'notifications') refreshNotifs(); }}
            style={{
              fontSize: 12, fontWeight: 600, cursor: 'pointer', padding: '4px 10px',
              borderRadius: 6, userSelect: 'none', whiteSpace: 'nowrap',
              color: tab === id ? T.text : T.textMuted,
              background: tab === id ? T.surfaceAlt : 'transparent',
            }}>
            {id === 'logs' ? 'Logs' : `Notifications${notifs.length ? ` (${notifs.length})` : ''}`}
          </span>
        ))}
        {tab === 'notifications' && notifs.length > 0 && (
          <Btn variant="ghost" style={{ padding: '4px 10px', fontSize: 11 }} onClick={clearAll}>
            Clear all
          </Btn>
        )}
        <button onClick={onClose} title="Close" style={{
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          width: 26, height: 26, padding: 0, borderRadius: 6, cursor: 'pointer',
          border: 'none', background: 'transparent',
        }}>
          <I name="xSmall" size={14} color={T.textMuted} />
        </button>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: '6px 16px 12px' }}>
        {body}
      </div>
    </div>
  );
}
