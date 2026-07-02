// ─── Drill panel: Artifacts tab (agent-config tracking) ────────
// Inventory of agent-affecting files (instruction docs, settings/hooks,
// MCP configs, .claude skills/agents/commands) with version history,
// diff viewer and restore. Reads GET /api/projects/artifacts?path=…;
// history via /api/projects/artifacts/history; scan/diff/restore POST
// to the matching hub endpoints. A 409 renders DrillNeedsInit.

const ARTIFACT_CLASS_LABELS = {
  instructions: 'Instructions', settings: 'Settings', mcp: 'MCP config',
  skill: 'Skills', agent: 'Agents', command: 'Commands', plugin: 'Plugins',
};

function artifactClassColor(cls) {
  return ({
    instructions: T.blue, settings: T.warn, mcp: T.purple,
    skill: T.accent, agent: T.blue, command: T.warn, plugin: T.purple,
  })[cls] || T.textMuted;
}

function DrillArtifactHistory({ project, art, onChanged }) {
  const [events, setEvents] = useState(null);
  const [diff, setDiff] = useState(null);        // {label, text}
  const [confirmV, setConfirmV] = useState(null); // version pending confirm
  const [warnings, setWarnings] = useState([]);

  const load = async () => {
    try {
      const d = await api.get('/api/projects/artifacts/history?path='
        + encodeURIComponent(project.path)
        + '&artifact=' + encodeURIComponent(art.id) + '&limit=30');
      setEvents(d.events || []);
    } catch (e) { notify('History failed: ' + e.message, 'err'); }
  };
  useEffect(() => { load(); }, [art.id]);

  const showDiff = async (v, against) => {
    try {
      const body = { path: project.path, artifact: art.id, version: v };
      if (against) body.against = against;
      const d = await api.post('/api/projects/artifacts/diff', body);
      setDiff({ label: `${d.from} → ${d.to}  (+${d.plus} −${d.minus})`, text: d.diff });
    } catch (e) { notify('Diff failed: ' + e.message, 'err'); }
  };

  const restore = async (v) => {
    if (confirmV !== v) { setConfirmV(v); setTimeout(() => setConfirmV(null), 4000); return; }
    setConfirmV(null);
    try {
      const d = await api.post('/api/projects/artifacts/restore',
        { path: project.path, artifact: art.id, version: v });
      notify(`Restored ${d.id} v${v} → live as v${d.new_version}`, 'ok');
      setWarnings(d.warnings || []);
      setDiff(null);
      load();
      if (onChanged) onChanged();
    } catch (e) { notify('Restore failed: ' + e.message, 'err'); }
  };

  if (events === null) return <DrillMsg text="Loading history…" />;
  if (!events.length) return <DrillMsg text="No recorded events yet." />;

  return (
    <div style={{ padding: '6px 0 10px 22px' }}>
      {warnings.map((w, i) => (
        <div key={i} style={{
          fontSize: 11, color: T.warn, padding: '3px 0', lineHeight: 1.4,
        }}>⚠ {w}</div>
      ))}
      {events.map(ev => (
        <div key={ev.id} style={{
          display: 'flex', alignItems: 'center', gap: 8, padding: '5px 0',
          borderBottom: `1px solid ${T.border}`, minWidth: 0,
        }}>
          <span className="mono" style={{ fontSize: 11, color: T.text, flexShrink: 0 }}>
            v{ev.version}
          </span>
          <Badge color={ev.event === 'deleted' ? T.error
            : ev.event === 'restored' ? T.accent : T.blue}>{ev.event}</Badge>
          <Badge color={ev.source === 'scan' ? T.warn : T.textMuted}>{ev.source}</Badge>
          <span title={ev.summary || ''} style={{
            fontSize: 11, color: T.textMuted, flex: 1, minWidth: 0,
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}>{(ev.ts || '').replace('T', ' ')}{ev.summary ? ' — ' + ev.summary : ''}</span>
          {ev.event !== 'deleted' && (
            <React.Fragment>
              <button onClick={() => showDiff(ev.version)} title="Diff this version vs live"
                style={{ background: 'none', border: 'none', cursor: 'pointer',
                         color: T.blue, fontSize: 11, padding: '2px 4px', flexShrink: 0 }}>
                diff
              </button>
              <button onClick={() => restore(ev.version)} title="Restore this version"
                style={{ background: 'none', border: 'none', cursor: 'pointer',
                         color: confirmV === ev.version ? T.error : T.warn,
                         fontSize: 11, fontWeight: confirmV === ev.version ? 700 : 400,
                         padding: '2px 4px', flexShrink: 0 }}>
                {confirmV === ev.version ? 'confirm?' : 'restore'}
              </button>
            </React.Fragment>
          )}
        </div>
      ))}
      {diff && (
        <div style={{ marginTop: 10 }}>
          <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 4 }}>
            {diff.label}
            <button onClick={() => setDiff(null)} style={{
              background: 'none', border: 'none', cursor: 'pointer',
              color: T.textDim, fontSize: 11, marginLeft: 8 }}>× close</button>
          </div>
          <pre className="mono" style={{
            background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 6,
            padding: 10, fontSize: 11, lineHeight: 1.5, overflow: 'auto',
            maxHeight: 320, whiteSpace: 'pre-wrap', color: T.text, margin: 0,
          }}>{diff.text}</pre>
        </div>
      )}
    </div>
  );
}

function DrillArtifacts({ project, onChanged }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [needsInit, setNeedsInit] = useState(false);
  const [openId, setOpenId] = useState(null);
  const [scanning, setScanning] = useState(false);

  const load = async () => {
    setErr(null);
    try {
      const d = await api.get('/api/projects/artifacts?path='
        + encodeURIComponent(project.path));
      setData(d);
      setNeedsInit(false);
    } catch (e) {
      if (e.status === 409) setNeedsInit(true);
      else setErr(e.message);
    }
  };
  useEffect(() => {
    setData(null); setNeedsInit(false); setOpenId(null);
    load();
  }, [project.path]);

  const scan = async () => {
    if (scanning) return;
    setScanning(true);
    try {
      const d = await api.post('/api/projects/artifacts/scan', { path: project.path });
      notify(`Scan: ${d.added.length} added, ${d.modified.length} modified, `
        + `${d.deleted.length} deleted`, 'ok');
      load();
      if (onChanged) onChanged();
    } catch (e) { notify('Scan failed: ' + e.message, 'err'); }
    setScanning(false);
  };

  if (needsInit) return <DrillNeedsInit project={project} onReady={load} />;
  if (err) return <DrillMsg text={err} color={T.error} />;
  if (!data) return <DrillMsg text="Loading artifacts…" />;

  const arts = data.artifacts || [];
  const st = data.status || {};
  const byClass = {};
  arts.forEach(a => { (byClass[a.class] = byClass[a.class] || []).push(a); });

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
        <span style={{ fontSize: 12, color: T.textMuted, flex: 1 }}>
          {st.tracked || 0} tracked · {st.out_of_band_recent || 0} recent out-of-band
          {st.last_scan ? ` · last scan ${String(st.last_scan).replace('T', ' ')}` : ''}
        </span>
        <Btn onClick={scan} disabled={scanning}>{scanning ? 'Scanning…' : 'Scan now'}</Btn>
      </div>
      {!arts.length && (
        <DrillMsg text="Nothing tracked yet — run a scan to build the inventory." />
      )}
      {Object.keys(ARTIFACT_CLASS_LABELS).filter(c => byClass[c]).map(cls => (
        <DrillSection key={cls} label={ARTIFACT_CLASS_LABELS[cls]} style={{ marginTop: 18 }}>
          {byClass[cls].map(a => (
            <div key={a.id}>
              <div onClick={() => setOpenId(openId === a.id ? null : a.id)} style={{
                display: 'flex', alignItems: 'center', gap: 8, padding: '7px 0',
                borderBottom: `1px solid ${T.border}`, cursor: 'pointer', minWidth: 0,
              }}>
                <span style={{
                  width: 7, height: 7, borderRadius: '50%', flexShrink: 0,
                  background: artifactClassColor(a.class),
                  opacity: a.exists ? 1 : 0.35,
                }} />
                <span className="mono" title={a.root} style={{
                  fontSize: 12, color: a.exists ? T.text : T.textDim, flex: 1, minWidth: 0,
                  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                  textDecoration: a.exists ? 'none' : 'line-through',
                }}>{a.id}</span>
                {!a.exists && <Badge color={T.error}>deleted</Badge>}
                {(a.roles || []).map(r => <Badge key={r} color={T.textMuted}>{r}</Badge>)}
                <span className="mono" style={{ fontSize: 11, color: T.textMuted, flexShrink: 0 }}>
                  v{a.version}
                </span>
                <span style={{ fontSize: 11, color: T.textDim, flexShrink: 0 }}>
                  {a.files} file{a.files === 1 ? '' : 's'}
                </span>
                <span className="mono" style={{ fontSize: 10, color: T.textDim, flexShrink: 0 }}>
                  {(a.last_changed || '').slice(0, 10)}
                </span>
              </div>
              {openId === a.id && (
                <DrillArtifactHistory project={project} art={a} onChanged={onChanged} />
              )}
            </div>
          ))}
        </DrillSection>
      ))}
    </div>
  );
}
