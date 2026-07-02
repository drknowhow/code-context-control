// ─── Drill views: Overview / Memory / Ledger / Sessions ────────
// All data comes from POST /api/projects/inspect {path, view, ...};
// a 409 {needs_init:true} renders the shared DrillNeedsInit CTA.

function drillCatColor(cat) {
  const palette = [T.accent, T.blue, T.purple, T.warn];
  const s = String(cat || '');
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h * 31) + s.charCodeAt(i)) >>> 0;
  return palette[h % palette.length];
}

function drillFactText(f) {
  if (typeof f === 'string') return f;
  if (!f) return '';
  return f.fact || f.text || f.content || JSON.stringify(f);
}

// ── Overview ───────────────────────────────────────────────────
function DrillOverview({ project, onChanged, setTab }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [needsInit, setNeedsInit] = useState(false);
  const [subs, setSubs] = useState(null);

  const load = async () => {
    setErr(null);
    try {
      const d = await api.post('/api/projects/inspect', { path: project.path, view: 'overview' });
      setData(d);
      setNeedsInit(false);
    } catch (e) {
      if (e.status === 409) setNeedsInit(true);
      else setErr(e.message);
    }
  };
  useEffect(() => { setData(null); setNeedsInit(false); load(); }, [project.path]);
  useEffect(() => {
    setSubs(null);
    if (!project.is_parent) return;
    api.get('/api/projects/subprojects?parent=' + encodeURIComponent(project.path))
      .then(setSubs).catch(() => {});
  }, [project.path, project.is_parent]);

  if (needsInit) {
    return <DrillNeedsInit project={project} onReady={() => { load(); if (onChanged) onChanged(); }} />;
  }
  if (err) return <DrillMsg text={'Failed to load overview: ' + err} color={T.error} />;

  const counts = (data && data.counts) || {};
  const info = (data && data.project) || {};
  const loading = !data;
  const stats = [
    ['tasks_open', 'Tasks', T.blue, 'tasks'],
    ['facts', 'Facts', T.accent, 'memory'],
    ['edits', 'Edits', T.blue, 'ledger'],
    ['sessions', 'Sessions', T.purple, 'sessions'],
    ['notifications', 'Alerts', T.warn, 'health'],
  ];
  const rollup = subs && subs.rollup;

  return (
    <div className="fade-up">
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        {stats.map(([key, label, color, target]) => (
          <div key={key} onClick={() => setTab(target)} style={{ flex: 1, minWidth: 140, cursor: 'pointer' }}>
            <StatBox label={label} color={color} loading={loading}
              value={counts[key] != null ? counts[key] : '—'} />
          </div>
        ))}
      </div>

      <DrillSection label="Details">
        <div style={{ display: 'grid', gridTemplateColumns: '110px 1fr', rowGap: 10, columnGap: 14 }}>
          <DrillKV label="IDE">{ideLabel(info.ide || project.ide)}</DrillKV>
          <DrillKV label="Version" mono>{info.c3_version || project.c3_version}</DrillKV>
          <DrillKV label="Last session">
            {project.last_session
              ? `${timeAgo(project.last_session)} · ${localDate(project.last_session)} ${localTime(project.last_session)}`
              : ''}
          </DrillKV>
          <DrillKV label="Path" mono title={project.path}>{project.path}</DrillKV>
          <DrillKV label="Tags">
            {(project.tags || []).length
              ? <span style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {project.tags.map(t => <Badge key={t} color={T.blue}>{t}</Badge>)}
                </span>
              : ''}
          </DrillKV>
          <DrillKV label="Notes">{project.notes}</DrillKV>
        </div>
      </DrillSection>

      {project.is_parent && (
        <DrillSection label="Sub-projects">
          {!subs ? <DrillMsg text="Loading sub-projects…" /> : (
            <React.Fragment>
              {(subs.children || []).length === 0 && <DrillMsg text="No sub-projects designated." />}
              {(subs.children || []).map(c => (
                <div key={c.path || c.rel_path} style={{
                  display: 'flex', alignItems: 'center', gap: 10, padding: '9px 0',
                  borderBottom: `1px solid ${T.border}`,
                }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, color: T.text }}>{c.name || c.rel_path}</div>
                    <div className="mono" title={c.path} style={{
                      fontSize: 11, color: T.textDim,
                      whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                    }}>{c.rel_path || c.path}</div>
                  </div>
                  {c.facts_count != null &&
                    <span className="mono" style={{ fontSize: 11, color: T.textMuted }}>{c.facts_count} facts</span>}
                  {c.notification_count > 0 && <Badge color={T.warn}>{c.notification_count} alerts</Badge>}
                  <Badge color={c.status === 'ok' ? T.accent
                    : (c.status === 'missing' || c.status === 'error') ? T.error : T.warn}>
                    {c.status || '?'}
                  </Badge>
                </div>
              ))}
              {rollup && (
                <div className="mono" style={{ fontSize: 11, color: T.textMuted, paddingTop: 10 }}>
                  {rollup.children} sub-project{rollup.children === 1 ? '' : 's'}
                  {' · '}{rollup.notifications} alert{rollup.notifications === 1 ? '' : 's'}
                  {' · '}{rollup.issues} issue{rollup.issues === 1 ? '' : 's'}
                </div>
              )}
            </React.Fragment>
          )}
        </DrillSection>
      )}
    </div>
  );
}

// ── Memory ─────────────────────────────────────────────────────
function DrillMemory({ project }) {
  const [query, setQuery] = useState('');
  const [items, setItems] = useState(null);
  const [total, setTotal] = useState(0);
  const [needsInit, setNeedsInit] = useState(false);
  const [err, setErr] = useState(null);

  const load = async () => {
    setErr(null);
    const q = query.trim();
    try {
      const body = { path: project.path, view: 'memory' };
      if (q) { body.query = q; body.limit = 20; }
      const d = await api.post('/api/projects/inspect', body);
      const list = q ? (d.results || []) : (d.facts || []);
      setItems(list);
      setTotal(q ? list.length : (d.total != null ? d.total : list.length));
      setNeedsInit(false);
    } catch (e) {
      if (e.status === 409) setNeedsInit(true);
      else setErr(e.message);
    }
  };
  useEffect(() => {
    const t = setTimeout(load, query.trim() ? 350 : 0);
    return () => clearTimeout(t);
  }, [project.path, query]);

  if (needsInit) return <DrillNeedsInit project={project} onReady={load} />;

  const shown = (items || []).slice(0, 200);
  return (
    <div className="fade-up">
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
        <I name="search" size={13} color={T.textMuted} />
        <input value={query} onChange={e => setQuery(e.target.value)}
          placeholder="Recall project memory…"
          style={drillFieldStyle({ flex: 1 })} />
      </div>
      {err && <DrillMsg text={'Failed to load memory: ' + err} color={T.error} />}
      {!err && items === null && <DrillMsg text="Loading facts…" />}
      {!err && items !== null && shown.length === 0 &&
        <DrillMsg text={query.trim() ? 'No matching facts.' : 'No facts stored yet.'} />}
      {shown.map((f, i) => (
        <div key={(f && f.id) || i} style={{ padding: '10px 0', borderBottom: `1px solid ${T.border}` }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
            {f && f.category && <Badge color={drillCatColor(f.category)}>{f.category}</Badge>}
            <div style={{ fontSize: 13, color: T.text, flex: 1, lineHeight: 1.5, overflowWrap: 'anywhere' }}>
              {drillFactText(f)}
            </div>
          </div>
          {f && (f.timestamp || f.relevance_count > 0) && (
            <div className="mono" style={{ fontSize: 11, color: T.textDim, marginTop: 4 }}>
              {f.timestamp ? timeAgo(f.timestamp) : ''}
              {f.relevance_count > 0 ? ` · recalled ${f.relevance_count}×` : ''}
            </div>
          )}
        </div>
      ))}
      {items !== null && items.length > 200 && (
        <div className="mono" style={{ fontSize: 11, color: T.textMuted, paddingTop: 10, textAlign: 'center' }}>
          showing 200 of {total}
        </div>
      )}
    </div>
  );
}

// ── Ledger ─────────────────────────────────────────────────────
function DrillLedger({ project }) {
  const [file, setFile] = useState('');
  const [data, setData] = useState(null);
  const [needsInit, setNeedsInit] = useState(false);
  const [err, setErr] = useState(null);

  const load = async () => {
    setErr(null);
    try {
      const body = { path: project.path, view: 'ledger', limit: 200 };
      if (file.trim()) body.file = file.trim();
      const d = await api.post('/api/projects/inspect', body);
      setData(d);
      setNeedsInit(false);
    } catch (e) {
      if (e.status === 409) setNeedsInit(true);
      else setErr(e.message);
    }
  };
  useEffect(() => {
    const t = setTimeout(load, file.trim() ? 350 : 0);
    return () => clearTimeout(t);
  }, [project.path, file]);

  if (needsInit) return <DrillNeedsInit project={project} onReady={load} />;

  const stats = (data && data.stats) || {};
  const hist = ((data && data.history) || []).slice().reverse();
  return (
    <div className="fade-up">
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        <StatBox label="Total edits" color={T.blue} loading={!data}
          value={stats.total != null ? stats.total : '—'} />
        <StatBox label="Files touched" color={T.purple} loading={!data}
          value={stats.files != null ? stats.files : '—'} />
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '16px 0 6px' }}>
        <I name="fileText" size={13} color={T.textMuted} />
        <input value={file} onChange={e => setFile(e.target.value)}
          placeholder="Filter by file…"
          style={drillFieldStyle({ flex: 1 })} />
      </div>
      {err && <DrillMsg text={'Failed to load ledger: ' + err} color={T.error} />}
      {!err && data && hist.length === 0 && <DrillMsg text="No edits recorded." />}
      {hist.map((e, i) => (
        <div key={(e && e.id) || i} className="mono" style={{
          fontSize: 11, padding: '8px 0', borderBottom: `1px solid ${T.border}`,
          display: 'flex', gap: 8, alignItems: 'baseline',
        }}>
          <span style={{ color: T.textDim, flexShrink: 0, minWidth: 58 }}>{timeAgo(e.timestamp)}</span>
          <span title={e.file} style={{
            color: T.blue, maxWidth: 200, flexShrink: 0,
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}>{e.file || ''}</span>
          <span title={e.summary} style={{
            color: T.textMuted, flex: 1, minWidth: 0,
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}>{e.summary || e.change_type || ''}</span>
          {e.lines_changed != null &&
            <span style={{ color: T.textDim, flexShrink: 0 }}>
              {typeof e.lines_changed === 'number' ? `${e.lines_changed}L` : String(e.lines_changed)}
            </span>}
        </div>
      ))}
    </div>
  );
}

// ── Sessions ───────────────────────────────────────────────────
function DrillSessions({ project }) {
  const [data, setData] = useState(null);
  const [needsInit, setNeedsInit] = useState(false);
  const [err, setErr] = useState(null);

  const load = async () => {
    setErr(null);
    try {
      const d = await api.post('/api/projects/inspect', { path: project.path, view: 'sessions', limit: 100 });
      setData(d);
      setNeedsInit(false);
    } catch (e) {
      if (e.status === 409) setNeedsInit(true);
      else setErr(e.message);
    }
  };
  useEffect(() => { setData(null); load(); }, [project.path]);

  if (needsInit) return <DrillNeedsInit project={project} onReady={load} />;
  if (err) return <DrillMsg text={'Failed to load sessions: ' + err} color={T.error} />;
  if (!data) return <DrillMsg text="Loading sessions…" />;

  const sessions = data.sessions || [];
  if (sessions.length === 0) return <DrillMsg text="No sessions recorded." />;

  return (
    <div className="fade-up">
      {sessions.map((s, i) => {
        const sid = s.id || s.session_id || '';
        const started = s.started_at || s.started || s.start_time || s.timestamp || s.saved_at || '';
        const summary = s.summary || s.description || s.task || s.title || '';
        const known = sid || started || summary;
        return (
          <div key={sid || i} style={{ padding: '10px 0', borderBottom: `1px solid ${T.border}` }}>
            {known ? (
              <React.Fragment>
                <div style={{ fontSize: 13, color: summary ? T.text : T.textDim, lineHeight: 1.5, overflowWrap: 'anywhere' }}>
                  {summary || '(no summary)'}
                </div>
                <div className="mono" style={{ fontSize: 11, color: T.textDim, marginTop: 4 }}>
                  {sid && <span style={{ color: T.purple }}>{sid}</span>}
                  {sid && started ? ' · ' : ''}
                  {started ? `${timeAgo(started)} · ${localDate(started)} ${localTime(started)}` : ''}
                </div>
              </React.Fragment>
            ) : (
              Object.entries(s)
                .filter(([, v]) => v == null || typeof v !== 'object')
                .slice(0, 6)
                .map(([k, v]) => (
                  <div key={k} className="mono" style={{ fontSize: 11, color: T.textMuted }}>
                    <span style={{ color: T.textDim }}>{k}:</span> {String(v)}
                  </div>
                ))
            )}
          </div>
        );
      })}
    </div>
  );
}
