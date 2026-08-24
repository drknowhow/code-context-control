// ─── Hub tokens: where the context budget actually went ────────
// TokensPanel is the shared per-project view (also the drill Tokens tab);
// HubTokens is the top-level mainView='tokens' page and owns the
// cross-project roll-up.
//
// TWO LOGS, DELIBERATELY NOT MERGED.
//   .c3/tool_telemetry.jsonl  — one row per C3 tool call: what the tool
//                               returned to the model.
//   .c3/session_stats.jsonl   — one row per Stop event: what the WHOLE
//                               conversation cost, read from the transcript.
// Neither is a subset of the other (a session spends tokens on prose, files
// read by native tools, and cache the tool log never sees), so they are shown
// side by side. Adding them together would be a made-up number.
//
// Nothing here is an estimate except the explicitly-labelled "saved vs full
// read" figure, which is a counterfactual and says so.

const TOKEN_WINDOWS = [[7, '7d'], [30, '30d'], [90, '90d'], [0, 'all']];

const TOKEN_VIEWS = [
  ['tool', 'By tool', 'wrench'],
  ['day', 'By day', 'clock'],
  ['target', 'By file', 'file'],
  ['session', 'By session', 'terminal'],
];

const tokFmt = (n) => Number(n || 0).toLocaleString();

// 1.2M / 940K / 512 — a table of 12-digit numbers is unreadable at a glance.
const tokShort = (n) => {
  const v = Number(n || 0);
  if (v >= 1e9) return (v / 1e9).toFixed(1) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
  return String(v);
};

const tokWhen = (iso) => (iso
  ? String(iso).replace('T', ' ').replace(/\.\d+/, '').replace(/\+.*$/, '').replace(/Z$/, '')
  : '—');

// by_tool arrives as an object (keyed lookup); every other breakdown arrives
// as an already-ranked list. Normalize here so the table renders one shape.
function tokRows(tools, view) {
  if (!tools) return [];
  if (view === 'tool') {
    return Object.keys(tools.by_tool || {})
      .map(k => Object.assign({ name: k }, tools.by_tool[k]))
      .sort((a, b) => b.response_tokens - a.response_tokens);
  }
  if (view === 'day') return (tools.by_day || []).slice().reverse();
  if (view === 'target') return tools.by_target || [];
  return tools.by_session || [];
}

// A bar drawn against the biggest row, so the table reads as a shape rather
// than a column of numbers the eye has to compare digit by digit.
function TokBar({ value, max, color }) {
  const pct = max > 0 ? Math.max(1.5, (value / max) * 100) : 0;
  return (
    <div style={{ height: 6, borderRadius: 3, background: T.surfaceAlt, overflow: 'hidden' }}>
      <div style={{
        height: '100%', width: `${pct}%`, borderRadius: 3,
        background: `linear-gradient(90deg, ${color}80, ${color})`,
      }} />
    </div>
  );
}

function TokChip({ on, children, onClick, title }) {
  return (
    <button className="btn" onClick={onClick} title={title} style={{
      background: on ? `${T.accent}22` : 'transparent',
      color: on ? T.accent : T.textDim,
      border: `1px solid ${on ? T.accent + '88' : T.border}`,
      borderRadius: 999, padding: '3px 12px', fontSize: 11.5, cursor: 'pointer',
    }}>{children}</button>
  );
}

// ── The per-project panel (also the drill Tokens tab) ───────────
// `days` from a parent makes the panel FOLLOW that window instead of keeping
// its own. Two window pickers on one screen showing different totals for the
// same project reads as a bug, because you cannot tell which number is true.
function TokensPanel({ path, projectName, embedded, days: fixedDays }) {
  const [ownDays, setOwnDays] = useState(30);
  const controlled = fixedDays !== undefined && fixedDays !== null;
  const days = controlled ? fixedDays : ownDays;
  const setDays = setOwnDays;
  const [view, setView] = useState('tool');
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const q = `?path=${encodeURIComponent(path || '')}&days=${days}`;
      setData(await api.get('/api/projects/tokens' + q));
      setError('');
    } catch (e) { setError(apiErr(e)); }
    setLoading(false);
  }, [path, days]);

  useEffect(() => { load(); }, [load]);

  const tools = (data && data.tools) || null;
  const sess = (data && data.sessions) || null;
  const rows = useMemo(() => tokRows(tools, view), [tools, view]);
  const max = rows.length ? Math.max(...rows.map(r => r.response_tokens || 0)) : 0;

  // Every session row zero means the log predates the Stop-hook fix, NOT that
  // the sessions were free. Saying so is the whole point of showing it.
  const staleSessions = !!(sess && sess.rows_seen && sess.all_zero_rows === sess.rows_seen);

  return (
    <div className="fade-up">
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12 }}>
        {!controlled && TOKEN_WINDOWS.map(([d, label]) => (
          <TokChip key={d} on={days === d} onClick={() => setDays(d)}>{label}</TokChip>
        ))}
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={load} disabled={loading} style={{
          background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
          padding: '4px 11px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
        }}>{loading ? 'Loading…' : 'Refresh'}</button>
      </div>

      {error && (
        <div style={{
          padding: '8px 12px', borderRadius: 6, marginBottom: 10, fontSize: 12,
          background: `${T.error}22`, color: T.error, border: `1px solid ${T.error}55`,
        }}>{error}</div>
      )}

      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        <StatBox label="Tool calls" loading={loading} color={T.blue}
          value={tools ? tokFmt(tools.total_calls) : '—'}
          sub={projectName || 'this project'} />
        <StatBox label="Tool tokens" loading={loading} color={T.accent}
          value={tools ? tokShort(tools.total_response_tokens) : '—'}
          sub="returned by C3 tools" />
        <StatBox label="Session tokens" loading={loading}
          color={staleSessions ? T.textMuted : T.warn}
          value={sess ? tokShort(sess.total_tokens) : '—'}
          sub={sess ? `${sess.session_count} session${sess.session_count === 1 ? '' : 's'} (transcript)` : ''} />
        <StatBox label="Est. saved" loading={loading} color={T.textMuted}
          value={tools ? tokShort(tools.estimated_saved_vs_full_read) : '—'}
          sub="vs full-read baseline" />
      </div>

      {staleSessions && (
        <div style={{
          marginTop: 12, padding: '8px 12px', borderRadius: 6, fontSize: 11.5,
          background: `${T.warn}18`, color: T.text, border: `1px solid ${T.warn}55`,
        }}>
          All {sess.rows_seen} session rows are zero — they predate the Stop-hook
          fix, which read fields the event never sent. This is a gap in the log,
          <b> not </b> a quiet run. Sessions from here on record real numbers.
        </div>
      )}

      <DrillSection label="Breakdown">
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 10 }}>
          {TOKEN_VIEWS.map(([id, label, icon]) => (
            <TokChip key={id} on={view === id} onClick={() => setView(id)}>
              <I name={icon} size={11} color={view === id ? T.accent : T.textDim} /> {label}
            </TokChip>
          ))}
        </div>

        {loading ? (
          <div style={{ color: T.textMuted, fontSize: 13 }}>Loading…</div>
        ) : !rows.length ? (
          <div style={{
            border: `1px dashed ${T.border}`, borderRadius: 8, padding: 22,
            textAlign: 'center', color: T.textMuted, fontSize: 12.5,
          }}>
            {view === 'target'
              ? <span>No file attribution recorded yet. C3 logs which file a
                  call was about from v2.95.0 — earlier rows only know the tool.</span>
              : <span>Nothing recorded in this window.</span>}
          </div>
        ) : (
          <div style={{ border: `1px solid ${T.border}`, borderRadius: 8, overflow: 'hidden' }}>
            <div style={{
              display: 'grid', gridTemplateColumns: '1fr 70px 90px 60px 110px',
              gap: 10, padding: '7px 12px', background: T.surfaceAlt,
              fontSize: 10.5, color: T.textMuted, textTransform: 'uppercase', letterSpacing: 1,
            }}>
              <span>{view === 'target' ? 'file' : view}</span>
              <span style={{ textAlign: 'right' }}>calls</span>
              <span style={{ textAlign: 'right' }}>tokens</span>
              <span style={{ textAlign: 'right' }}>avg</span>
              <span>share</span>
            </div>
            {rows.slice(0, 40).map((r, i) => {
              const calls = r.calls || 0;
              const toks = r.response_tokens || 0;
              return (
                <div key={`${r.name}-${i}`} style={{
                  display: 'grid', gridTemplateColumns: '1fr 70px 90px 60px 110px',
                  gap: 10, alignItems: 'center', padding: '8px 12px',
                  borderTop: `1px solid ${T.border}`,
                  background: i % 2 ? `${T.surfaceAlt}70` : T.surface,
                }}>
                  <span className="mono" title={r.name} style={{
                    fontSize: 12, color: T.text, overflow: 'hidden',
                    textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>{r.name || '—'}</span>
                  <span className="mono" style={{ fontSize: 11.5, color: T.textDim, textAlign: 'right' }}>{tokFmt(calls)}</span>
                  <span className="mono" style={{ fontSize: 11.5, color: T.text, textAlign: 'right' }}>{tokFmt(toks)}</span>
                  <span className="mono" style={{ fontSize: 11.5, color: T.textMuted, textAlign: 'right' }}>{tokFmt(Math.round(toks / Math.max(1, calls)))}</span>
                  <TokBar value={toks} max={max} color={T.accent} />
                </div>
              );
            })}
            {rows.length > 40 && (
              <div style={{
                padding: '6px 12px', borderTop: `1px solid ${T.border}`,
                fontSize: 11, color: T.textMuted,
              }}>… {rows.length - 40} more not shown</div>
            )}
          </div>
        )}
      </DrillSection>

      {sess && sess.sessions && sess.sessions.length > 0 && (
        <DrillSection label="Conversation cost (from the transcript)">
          <div style={{ border: `1px solid ${T.border}`, borderRadius: 8, overflow: 'hidden' }}>
            {sess.sessions.slice(0, 12).map((s, i) => (
              <div key={s.session_id} style={{
                display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap',
                padding: '8px 12px', borderTop: i ? `1px solid ${T.border}` : 'none',
                background: i % 2 ? `${T.surfaceAlt}70` : T.surface, fontSize: 11.5,
              }}>
                <span className="mono" style={{ color: T.text }}>{tokWhen(s.ts)}</span>
                {s.model && <Badge color={T.blue}>{s.model}</Badge>}
                <span style={{ color: T.textDim }}>{tokFmt(s.assistant_messages)} msgs</span>
                <div style={{ flex: 1 }} />
                <span className="mono" style={{ color: T.textMuted }} title="uncached input">in {tokShort(s.input_tokens)}</span>
                <span className="mono" style={{ color: T.text }} title="output">out {tokShort(s.output_tokens)}</span>
                <span className="mono" style={{ color: T.textMuted }} title="cache write">cw {tokShort(s.cache_creation_tokens)}</span>
                <span className="mono" style={{ color: T.textMuted }} title="cache read">cr {tokShort(s.cache_read_tokens)}</span>
              </div>
            ))}
          </div>
          <div style={{ fontSize: 10.5, color: T.textMuted, marginTop: 8, lineHeight: 1.5 }}>
            {sess.cost_note}
          </div>
        </DrillSection>
      )}

      {tools && !embedded && (
        <div style={{ fontSize: 10.5, color: T.textMuted, marginTop: 14, lineHeight: 1.5 }}>
          {tools.baseline_note}
        </div>
      )}
    </div>
  );
}

// ── Cross-project page (mainView='tokens') ─────────────────────
function HubTokens({ projects, onOpenDrill }) {
  const [days, setDays] = useState(30);
  const [ov, setOv] = useState(null);
  const [err, setErr] = useState('');
  const [openPath, setOpenPath] = useState(null);

  const load = useCallback(async () => {
    try {
      setOv(await api.get(`/api/hub/tokens/overview?days=${days}`));
      setErr('');
    } catch (e) { setErr(apiErr(e)); }
  }, [days]);

  useEffect(() => { setOv(null); load(); }, [load]);

  const rows = (ov && ov.projects) || [];
  const totals = (ov && ov.totals) || {};
  const withData = rows.filter(r => r.tool_calls > 0 || r.session_tokens > 0);
  const max = withData.length ? Math.max(...withData.map(r => r.tool_tokens)) : 0;

  return (
    <div className="fade-up">
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
        <h2 style={{ margin: 0, fontSize: 17, color: T.text, display: 'flex', alignItems: 'center', gap: 8 }}>
          <I name="gauge" size={15} color={T.accent} /> Tokens
        </h2>
        {TOKEN_WINDOWS.map(([d, label]) => (
          <TokChip key={d} on={days === d} onClick={() => setDays(d)}>{label}</TokChip>
        ))}
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={load} style={{
          background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
          padding: '4px 11px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
        }}>Refresh</button>
      </div>

      <div style={{ fontSize: 11, color: T.textDim, marginBottom: 12, lineHeight: 1.6 }}>
        Two measurements, kept apart on purpose. <b>Tool tokens</b> are what C3's
        own tools returned to the model, per call, with the file each call was
        about. <b>Session tokens</b> are what the whole conversation cost,
        summed from the Claude Code transcript. A session spends tokens on
        prose and cache that no tool log can see, so the two are never added
        together.
      </div>

      {err && (
        <div style={{
          padding: '8px 12px', borderRadius: 6, marginBottom: 10, fontSize: 12,
          background: `${T.error}22`, color: T.error, border: `1px solid ${T.error}55`,
        }}>{err}</div>
      )}

      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 6 }}>
        <StatBox label="Tool calls" loading={!ov} color={T.blue} value={tokFmt(totals.tool_calls)} sub="all projects" />
        <StatBox label="Tool tokens" loading={!ov} color={T.accent} value={tokShort(totals.tool_tokens)} sub="returned by C3 tools" />
        <StatBox label="Session tokens" loading={!ov} color={T.warn} value={tokShort(totals.session_tokens)} sub="from transcripts" />
        <StatBox label="Est. saved" loading={!ov} color={T.textMuted} value={tokShort(totals.saved)} sub="vs full-read baseline" />
      </div>

      {!ov ? (
        <div style={{ color: T.textMuted, fontSize: 13, marginTop: 14 }}>Loading…</div>
      ) : !rows.length ? (
        <div style={{
          border: `1px dashed ${T.border}`, borderRadius: 8, padding: 26, marginTop: 14,
          textAlign: 'center', color: T.textMuted, fontSize: 13,
        }}>No projects registered.</div>
      ) : (
        <DrillSection label={`Projects (${withData.length} with recorded usage)`}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {rows.map((r) => {
              const open = openPath === r.path;
              const quiet = !r.tool_calls && !r.session_tokens;
              return (
                <div key={r.path} style={{
                  border: `1px solid ${open ? T.accent + '66' : T.border}`,
                  borderRadius: 8, background: T.surface, overflow: 'hidden',
                }}>
                  <div onClick={() => setOpenPath(open ? null : r.path)} style={{
                    display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
                    padding: '10px 12px', cursor: 'pointer',
                  }}>
                    <I name="chevron" size={12} color={T.textMuted}
                      style={{ transform: open ? 'rotate(90deg)' : 'none' }} />
                    <span style={{ fontWeight: 600, color: quiet ? T.textMuted : T.text, fontSize: 13 }}>{r.name}</span>
                    {!r.initialized && <Badge color={T.textMuted}>not initialized</Badge>}
                    {r.error && <Badge color={T.error}>unreadable</Badge>}
                    <div style={{ flex: 1, minWidth: 60 }}>
                      {!quiet && <TokBar value={r.tool_tokens} max={max} color={T.accent} />}
                    </div>
                    <span className="mono" style={{ fontSize: 11.5, color: T.textDim }}>{tokFmt(r.tool_calls)} calls</span>
                    <span className="mono" style={{ fontSize: 11.5, color: T.text, minWidth: 62, textAlign: 'right' }}>{tokShort(r.tool_tokens)} tool</span>
                    <span className="mono" style={{ fontSize: 11.5, color: T.warn, minWidth: 68, textAlign: 'right' }}>{tokShort(r.session_tokens)} session</span>
                    <button className="btn" onClick={(e) => { e.stopPropagation(); onOpenDrill(r, 'tokens'); }}
                      style={{
                        background: 'transparent', color: T.textMuted,
                        border: `1px solid ${T.border}`, borderRadius: 6,
                        padding: '3px 10px', fontSize: 11, cursor: 'pointer',
                      }}>Open drill</button>
                  </div>
                  {open && (
                    <div style={{ borderTop: `1px solid ${T.border}`, padding: '12px 14px' }}>
                      {r.error
                        ? <div style={{ fontSize: 12, color: T.error }}>{r.error}</div>
                        : <TokensPanel path={r.path} projectName={r.name} embedded days={days} />}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </DrillSection>
      )}

      {ov && (
        <div style={{ fontSize: 10.5, color: T.textMuted, marginTop: 16, lineHeight: 1.5 }}>
          {ov.baseline_note}
        </div>
      )}
    </div>
  );
}
