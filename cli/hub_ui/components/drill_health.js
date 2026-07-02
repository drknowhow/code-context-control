// ─── Drill views: Health / Budget ──────────────────────────────

const DRILL_HEALTH_COMPONENTS = [
  ['index', 'Index', 'database'],
  ['embeddings', 'Embeddings', 'brain'],
  ['doc_index', 'Docs', 'fileText'],
  ['dictionary', 'Dictionary', 'layers'],
  ['instructions', 'Instructions', 'bookmark'],
];

function DrillHealth({ project, onChanged }) {
  const [report, setReport] = useState(null);
  const [checking, setChecking] = useState(false);
  const [busy, setBusy] = useState({});
  const [output, setOutput] = useState('');
  const [perms, setPerms] = useState(null);
  const [permTier, setPermTier] = useState('');
  const [applying, setApplying] = useState(false);

  const runCheck = async () => {
    setChecking(true);
    try {
      const h = await api.post('/api/projects/health', { path: project.path });
      setReport(h);
    } catch (e) {
      notify('Health check failed: ' + e.message, 'err');
    }
    setChecking(false);
  };

  const loadPerms = async () => {
    try {
      const p = await api.post('/api/projects/permissions', { path: project.path });
      setPerms(p);
      setPermTier(p.current_tier || '');
    } catch (e) {
      setPerms({ error: e.message });
    }
  };
  useEffect(() => {
    setReport(null); setOutput(''); setPerms(null); setPermTier('');
    loadPerms();
  }, [project.path]);

  const rebuild = async (comp, label) => {
    setBusy(b => Object.assign({}, b, { [comp]: true }));
    setOutput(`Rebuilding ${label}…`);
    try {
      const d = await api.post('/api/projects/run-component', { path: project.path, component: comp });
      setOutput(d.output || d.error || '(no output)');
      notify(d.success ? `${label} rebuilt` : `${label} rebuild failed`, d.success ? 'ok' : 'err');
      if (d.success && onChanged) onChanged();
    } catch (e) {
      setOutput('Error: ' + e.message);
      notify(`${label} rebuild failed: ` + e.message, 'err');
    }
    setBusy(b => Object.assign({}, b, { [comp]: false }));
  };

  const applyTier = async () => {
    if (!permTier) return;
    setApplying(true);
    try {
      const d = await api.post('/api/projects/permissions/apply', { path: project.path, tier: permTier });
      notify(d.message || `Applied '${permTier}' permissions`, 'ok');
      loadPerms();
    } catch (e) {
      notify('Failed to apply tier: ' + e.message, 'err');
    }
    setApplying(false);
  };

  const issues = (report && report.issues) || [];
  const tierColor = (name) => name === 'read-only' ? T.blue : name === 'standard' ? T.accent : T.warn;

  return (
    <div className="fade-up">
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <Btn onClick={runCheck} disabled={checking}>
          <I name="zap" size={12} color={T.bg} />
          {checking ? 'Checking…' : 'Run health check'}
        </Btn>
        {report && !report.error && (
          <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 700, color: report.healthy ? T.accent : T.warn }}>
            <GlowDot color={report.healthy ? T.accent : T.warn} />
            {report.healthy ? 'Healthy' : `${issues.length} issue${issues.length === 1 ? '' : 's'}`}
          </span>
        )}
      </div>

      {report && report.error && <DrillMsg text={report.error} color={T.error} />}
      {report && !report.error && (
        <React.Fragment>
          {issues.length > 0 && (
            <div style={{ marginTop: 14 }}>
              {issues.map((iss, i) => (
                <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: 8, padding: '6px 0', fontSize: 12, color: T.warn }}>
                  <I name="alertTriangle" size={13} color={T.warn} />
                  <span style={{ lineHeight: 1.5, overflowWrap: 'anywhere' }}>{iss}</span>
                </div>
              ))}
            </div>
          )}
          <DrillSection label="Report" style={{ marginTop: 18 }}>
            <div style={{ display: 'grid', gridTemplateColumns: '110px 1fr', rowGap: 8, columnGap: 14 }}>
              <DrillKV label="Version" mono>{report.config_version}</DrillKV>
              <DrillKV label="Index" mono>
                {report.index_files != null ? `${report.index_files} files, ${report.index_chunks || 0} chunks` : ''}
              </DrillKV>
              <DrillKV label="Embeddings" mono>
                {report.embedded_files > 0 ? `${report.embedded_files} files (semantic ready)` : 'not built'}
              </DrillKV>
              <DrillKV label="Doc index" mono>
                {report.doc_chunks > 0 ? `${report.doc_chunks} chunks` : 'not built'}
              </DrillKV>
              <DrillKV label="Stale files" mono>{report.stale_files != null ? String(report.stale_files) : ''}</DrillKV>
              <DrillKV label="Facts" mono>{report.facts != null ? String(report.facts) : ''}</DrillKV>
              <DrillKV label="Sessions" mono>{report.sessions != null ? String(report.sessions) : ''}</DrillKV>
            </div>
          </DrillSection>
        </React.Fragment>
      )}

      <DrillSection label="Components">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))', gap: 10 }}>
          {DRILL_HEALTH_COMPONENTS.map(([comp, label, icon]) => (
            <div key={comp} style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '10px 12px',
              background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8,
            }}>
              <I name={icon} size={13} color={T.textMuted} />
              <span style={{ fontSize: 12, fontWeight: 600, color: T.text, flex: 1 }}>{label}</span>
              <Btn variant="ghost" disabled={!!busy[comp]} onClick={() => rebuild(comp, label)}
                style={{ padding: '4px 10px', fontSize: 11 }}>
                {busy[comp] ? '…' : 'Rebuild'}
              </Btn>
            </div>
          ))}
        </div>
        {output && (
          <pre className="mono" style={{
            margin: '12px 0 0', padding: 12, background: T.bg, border: `1px solid ${T.border}`,
            borderRadius: 8, fontSize: 11, color: T.textMuted, whiteSpace: 'pre-wrap',
            overflowWrap: 'anywhere', maxHeight: 180, overflowY: 'auto',
          }}>{output}</pre>
        )}
      </DrillSection>

      <DrillSection label="Permissions">
        {!perms && <DrillMsg text="Loading permissions…" />}
        {perms && perms.error && <DrillMsg text={'Failed to load permissions: ' + perms.error} color={T.error} />}
        {perms && !perms.error && perms.supported === false && (
          <DrillMsg text={`Permission tiers are managed for Claude Code projects only (this project: ${ideLabel(perms.ide)}).`} />
        )}
        {perms && !perms.error && perms.supported !== false && (
          <React.Fragment>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
              <span style={{ fontSize: 12, color: T.textMuted }}>Current tier</span>
              <Badge color={perms.current_tier ? tierColor(perms.current_tier) : T.textMuted}>
                {perms.current_tier || 'not set'}
              </Badge>
              {perms.allow_count > 0 && (
                <span className="mono" style={{ fontSize: 11, color: T.textDim }}>
                  {perms.allow_count} allow · {perms.deny_count} deny
                </span>
              )}
            </div>
            {Object.entries(perms.tiers || {}).map(([name, info]) => {
              const selected = permTier === name;
              const color = tierColor(name);
              return (
                <div key={name} onClick={() => setPermTier(name)} style={{
                  display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px',
                  marginBottom: 6, borderRadius: 8, cursor: 'pointer',
                  border: `1px solid ${selected ? color : T.border}`,
                  background: selected ? `${color}12` : 'transparent',
                }}>
                  <span style={{
                    width: 12, height: 12, borderRadius: '50%', flexShrink: 0,
                    border: `2px solid ${selected ? color : T.textDim}`,
                    background: selected ? color : 'transparent',
                  }} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 12, fontWeight: selected ? 700 : 500, color: selected ? color : T.text }}>
                      {name}{perms.current_tier === name ? ' (active)' : ''}
                    </div>
                    <div style={{ fontSize: 11, color: T.textMuted }}>{info.description}</div>
                  </div>
                  <span className="mono" style={{ fontSize: 11, color: T.textDim, whiteSpace: 'nowrap' }}>
                    {info.allow_count} allow
                  </span>
                </div>
              );
            })}
            <div style={{ marginTop: 10 }}>
              <Btn onClick={applyTier} disabled={applying || !permTier || permTier === perms.current_tier}>
                {applying ? 'Applying…' : 'Apply tier'}
              </Btn>
            </div>
          </React.Fragment>
        )}
      </DrillSection>
    </div>
  );
}

// ── Budget ─────────────────────────────────────────────────────
function DrillBudget({ project }) {
  const [threshold, setThreshold] = useState('');
  const [nudges, setNudges] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [err, setErr] = useState(null);
  const [saving, setSaving] = useState(false);

  const load = async () => {
    setErr(null);
    try {
      const d = await api.post('/api/projects/budget', { path: project.path });
      setThreshold(String(d.threshold != null ? d.threshold : 35000));
      setNudges(d.show_context_nudges !== false);
      setLoaded(true);
    } catch (e) {
      if (e.status === 409) { setErr('needs_init'); return; }
      setErr(e.message);
    }
  };
  useEffect(() => { setLoaded(false); load(); }, [project.path]);

  const save = async () => {
    const t = parseInt(threshold, 10);
    if (!t || t < 1000) {
      notify('Threshold must be an integer of at least 1000 tokens', 'err');
      return;
    }
    setSaving(true);
    try {
      const d = await api.put('/api/projects/budget', {
        path: project.path, threshold: t, show_context_nudges: nudges,
      });
      setThreshold(String(d.threshold != null ? d.threshold : t));
      setNudges(d.show_context_nudges !== false);
      notify('Budget settings saved', 'ok');
    } catch (e) {
      notify('Save failed: ' + e.message, 'err');
    }
    setSaving(false);
  };

  if (err === 'needs_init') return <DrillNeedsInit project={project} onReady={load} />;
  if (err) return <DrillMsg text={'Failed to load budget: ' + err} color={T.error} />;

  const live = project.budget && typeof project.budget === 'object' ? project.budget : null;

  return (
    <div className="fade-up">
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        <StatBox label="Context threshold" color={T.accent} loading={!loaded}
          value={loaded ? Number(threshold || 0).toLocaleString() : '—'} sub="tokens per session" />
        <StatBox label="Nudges" color={nudges ? T.blue : T.textMuted} loading={!loaded}
          value={nudges ? 'ON' : 'OFF'} sub="context usage reminders" />
      </div>

      {live && (
        <DrillSection label="Live session budget">
          <div style={{ display: 'grid', gridTemplateColumns: '110px 1fr', rowGap: 8, columnGap: 14 }}>
            {Object.entries(live)
              .filter(([, v]) => v == null || typeof v !== 'object')
              .slice(0, 8)
              .map(([k, v]) => <DrillKV key={k} label={k.replace(/_/g, ' ')} mono>{String(v)}</DrillKV>)}
          </div>
        </DrillSection>
      )}

      <DrillSection label="Settings">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <label style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: '8px 0' }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
              <span style={{ color: T.textMuted, fontSize: 12 }}>Context budget threshold</span>
              <span style={{ color: T.textDim, fontSize: 11 }}>Token estimate that triggers snapshot / clear nudges.</span>
            </div>
            <input type="number" min="1000" step="1000" value={threshold}
              onChange={e => setThreshold(e.target.value)}
              className="mono" style={drillFieldStyle({ width: 110, textAlign: 'right' })} />
          </label>
          {renderBoolToggle('Show context nudges', nudges, () => setNudges(v => !v),
            'Surface budget warnings inside tool responses as usage grows.')}
        </div>
        <div style={{ marginTop: 14 }}>
          <Btn onClick={save} disabled={saving || !loaded}>
            <I name="save" size={12} color={T.bg} />
            {saving ? 'Saving…' : 'Save budget'}
          </Btn>
        </div>
      </DrillSection>
    </div>
  );
}
