// ─── Tool Discipline (cross-project) ──────────────────────────────────────
// The Layer C knob, and the evidence for using it.
//
// This tab exists separately from Access Guard on purpose. Access Guard says
// which PATHS the agent may touch — a security boundary. Tool discipline says
// how hard C3 pushes the agent toward c3_* tools — a workflow preference.
// Conflating them is what made "the guard is slowing me down" unfixable
// without weakening something that should have stayed hard.
//
// Honesty rules this view must keep (docs/enforcement.md):
//   - never imply that lowering discipline lowers a security boundary;
//   - a project we could not READ is not a project running 'strict';
//   - show what strict actually buys, so 'off' is an informed choice;
//   - a malformed config section resolves to strict — say so, loudly.

const ENF_MODE_COLOR = (mode) => (
  mode === 'strict' ? T.error : mode === 'advisory' ? T.accent
    : mode === 'off' ? T.textDim : T.warn
);

const ENF_MODE_SHORT = {
  strict: 'Blocks native Edit/Write until a c3_* call runs first.',
  advisory: 'Allows native Edit/Write with a nudge. Ledger still logs.',
  off: 'No nudging. Access Guard + vault guard still enforce.',
};

function EnforcementBadge({ row }) {
  const pill = (label, fg, bg, title) => (
    <span title={title} className="mono" style={{
      fontSize: 10, padding: '2px 7px', borderRadius: 4,
      color: fg, background: bg, whiteSpace: 'nowrap',
    }}>{label}</span>
  );
  if (row.error) {
    return pill('UNREADABLE', T.error, T.errorDim,
      `Policy could not be read: ${row.error}. This is not the same as "running strict".`);
  }
  if (!row.initialized) {
    return pill('NO .c3', T.textDim, T.surfaceAlt,
      'C3 is not initialized here, so no hooks run in this repo.');
  }
  const color = ENF_MODE_COLOR(row.mode);
  const src = row.scope === 'default'
    ? 'never set — defaults to strict, which is the pre-v2.66 behavior'
    : `from the ${row.scope} config${row.set_by ? `, set by ${row.set_by}` : ''}`;
  return pill(String(row.mode || '').toUpperCase(), color, `${color}22`,
    `${ENF_MODE_SHORT[row.mode] || ''} (${src})`);
}

function ModePicker({ row, busy, onPick }) {
  return (
    <div style={{ display: 'flex', gap: 4 }}>
      {['strict', 'advisory', 'off'].map(m => {
        const active = row.mode === m;
        const color = ENF_MODE_COLOR(m);
        return (
          <div key={m} title={ENF_MODE_SHORT[m]}
            onClick={() => { if (!busy && !active && !row.error) onPick(row, m); }}
            className="mono"
            style={{
              fontSize: 10, padding: '3px 9px', borderRadius: 4,
              cursor: (busy || active || row.error) ? 'default' : 'pointer',
              color: active ? '#fff' : color,
              background: active ? color : `${color}18`,
              border: `1px solid ${active ? color : 'transparent'}`,
              opacity: (busy || row.error) ? 0.45 : 1,
            }}>{m}</div>
        );
      })}
    </div>
  );
}

function DenialRow({ d }) {
  const isDiscipline = d.layer === 'discipline';
  const color = isDiscipline ? T.warn : T.error;
  return (
    <div style={{
      display: 'grid', gridTemplateColumns: '44px minmax(0,1.4fr) 74px minmax(0,2fr)',
      gap: 8, alignItems: 'center', padding: '4px 10px', fontSize: 11,
      borderTop: `1px solid ${T.border}`,
    }}>
      <span className="mono" style={{ color, fontWeight: 600 }}>{d.hits}x</span>
      <span className="mono" title={d.example_path || d.rule} style={{
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        color: T.text,
      }}>{d.rule}</span>
      <span className="mono" style={{ fontSize: 10, color: T.textDim }}>{d.tool}</span>
      <span className="mono" title="Run this to clear the denial" style={{
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        fontSize: 10, color: T.textMuted,
      }}>{d.fix}</span>
    </div>
  );
}

function HubEnforcement({ projects, onOpenDrill }) {
  const { useState, useCallback } = React;
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState('');
  const [confirm, setConfirm] = useState(null);
  const [expanded, setExpanded] = useState({});

  const load = useCallback(async () => {
    try {
      setData(await api.get('/api/hub/enforcement/overview'));
      setErr('');
    } catch (e) {
      // Keep the last good snapshot — blanking would read as "nothing set".
      setErr(String(e && e.message ? e.message : e));
    }
  }, []);

  React.useEffect(() => { load(); }, [load]);

  const applyMode = async (row, mode) => {
    setBusy(row.path);
    try {
      await api.post('/api/projects/enforcement', { path: row.path, mode });
    } catch (e) {
      setErr(String(e && e.message ? e.message : e));
    }
    setBusy('');
    setConfirm(null);
    load();
  };

  const pick = (row, mode) => {
    // Only 'off' gets a confirm: it is the one choice that stops C3 nudging
    // entirely, and users should know what it does and does not switch off.
    if (mode === 'off') { setConfirm({ row, mode }); return; }
    applyMode(row, mode);
  };

  const clearDenials = async (row) => {
    setBusy(row.path);
    try {
      await api.del(`/api/projects/enforcement/denials?path=${encodeURIComponent(row.path)}`);
    } catch (e) {
      setErr(String(e && e.message ? e.message : e));
    }
    setBusy('');
    load();
  };

  const rows = (data && data.projects) || [];
  const live = rows.filter(r => r.initialized && !r.error);
  const problems = rows.filter(r => r.error || !r.initialized);
  const totals = (data && data.totals) || {};
  const drifted = live.filter(
    r => r.tier && r.tier_implies && r.tier_implies !== r.mode && r.set_by !== 'user');
  const warned = live.filter(r => (r.warnings || []).length);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <I name="shield" size={15} color={T.accent} />
        <span style={{ fontSize: 14, color: T.text }}>Tool Discipline</span>
        <span className="mono" style={{ fontSize: 11, color: T.textMuted }}>
          {data
            ? `${live.length} project(s) · ${totals.discipline || 0} block(s) · ${totals.access || 0} path denial(s)`
            : 'loading…'}
        </span>
        <div style={{ flex: 1 }} />
        <div onClick={load} title="Refresh" style={{ cursor: 'pointer', padding: 4 }}>
          <I name="refresh" size={13} color={T.textMuted} />
        </div>
      </div>

      {/* The tab must never imply that turning this down turns security down. */}
      <div style={{
        fontSize: 11, color: T.textMuted, background: T.surface,
        border: `1px solid ${T.border}`, borderRadius: 6, padding: '8px 10px',
        lineHeight: 1.5,
      }}>
        {(data && data.coverage_note)
          || 'Tool discipline governs native Edit/Write only.'}
      </div>

      {err && (
        <div style={{
          fontSize: 11, color: T.error, background: T.errorDim,
          border: `1px solid ${T.error}`, borderRadius: 6, padding: '8px 10px',
        }}>
          Could not refresh: {err} — showing the last snapshot, which may be stale.
        </div>
      )}

      {warned.length > 0 && (
        <div style={{
          fontSize: 11, color: T.warn, background: T.warnDim,
          border: `1px solid ${T.warn}`, borderRadius: 6, padding: '8px 10px',
          lineHeight: 1.5,
        }}>
          <b>{warned.length} project(s) have a malformed enforcement section.</b>{' '}
          Those resolve to <span className="mono">strict</span> and will not
          honour the mode shown until the config is fixed:
          {' '}{warned.map(r => r.name || r.path).join(', ')}
        </div>
      )}

      {drifted.length > 0 && (
        <div style={{
          fontSize: 11, color: T.textMuted, background: T.surface,
          border: `1px dashed ${T.border}`, borderRadius: 6, padding: '8px 10px',
          lineHeight: 1.5,
        }}>
          {drifted.length} project(s) run a mode their permission tier does not
          imply. That is fine — it just means the mode predates the tier, or was
          set before v2.66 defaulted it. Re-picking a tier would change them.
        </div>
      )}

      {data && live.length === 0 && (
        <div style={{
          fontSize: 12, color: T.textMuted, padding: '18px 10px', textAlign: 'center',
          border: `1px dashed ${T.border}`, borderRadius: 6,
        }}>
          No initialized projects. Run <span className="mono">c3 init</span> in a
          repo to bring it under C3.
        </div>
      )}

      {live.map(row => {
        const open = !!expanded[row.path];
        const top = row.top_denials || [];
        return (
          <div key={row.path} style={{
            background: T.surface, border: `1px solid ${T.border}`, borderRadius: 6,
          }}>
            <div style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px',
            }}>
              <I name="folder" size={13} color={T.textMuted} />
              <span onClick={() => onOpenDrill && onOpenDrill(
                projects.find(p => p.path === row.path) || { path: row.path, name: row.name })}
                style={{
                  fontSize: 12, color: T.text,
                  cursor: onOpenDrill ? 'pointer' : 'default',
                }}>{row.name || row.path}</span>
              <EnforcementBadge row={row} />
              {row.tier && (
                <span className="mono" title={`Permission tier '${row.tier}' implies '${row.tier_implies}'`}
                  style={{ fontSize: 10, color: T.textDim }}>
                  tier {row.tier}
                </span>
              )}
              <div style={{ flex: 1 }} />
              {row.denial_total > 0 && (
                <span onClick={() => setExpanded({ ...expanded, [row.path]: !open })}
                  className="mono" title="Denials recorded in this project — click for the breakdown"
                  style={{
                    fontSize: 10, color: T.warn, cursor: 'pointer',
                    padding: '2px 7px', borderRadius: 4, background: T.warnDim,
                  }}>
                  {row.denial_total} denial{row.denial_total === 1 ? '' : 's'} {open ? '▾' : '▸'}
                </span>
              )}
              <ModePicker row={row} busy={busy === row.path} onPick={pick} />
            </div>

            {(row.warnings || []).map((w, i) => (
              <div key={i} style={{
                fontSize: 10, color: T.warn, padding: '0 10px 6px 31px', lineHeight: 1.5,
              }}>⚠ {w}</div>
            ))}

            {open && top.length > 0 && (
              <div>
                <div style={{
                  display: 'grid',
                  gridTemplateColumns: '44px minmax(0,1.4fr) 74px minmax(0,2fr)',
                  gap: 8, padding: '4px 10px 3px', fontSize: 10, color: T.textDim,
                  borderTop: `1px solid ${T.border}`,
                }}>
                  <span>HITS</span><span>RULE</span><span>TOOL</span><span>HOW TO UNBLOCK</span>
                </div>
                {top.map((d, i) => <DenialRow key={i} d={d} />)}
                <div style={{
                  padding: '6px 10px', display: 'flex', alignItems: 'center', gap: 10,
                  borderTop: `1px solid ${T.border}`,
                }}>
                  <span style={{ fontSize: 10, color: T.textDim }}>
                    Counters are diagnostics, not an audit trail — the ledger
                    keeps the record.
                  </span>
                  <div style={{ flex: 1 }} />
                  <span onClick={() => clearDenials(row)} className="mono"
                    style={{ fontSize: 10, color: T.textMuted, cursor: 'pointer' }}>
                    reset counters
                  </span>
                </div>
              </div>
            )}
          </div>
        );
      })}

      {/* Projects we cannot read are listed explicitly — silence would read
          as "all fine". */}
      {problems.length > 0 && (
        <div style={{
          background: T.surface, border: `1px solid ${T.border}`,
          borderRadius: 6, padding: '8px 10px',
        }}>
          <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 6 }}>
            Not reporting
          </div>
          {problems.map(row => (
            <div key={row.path} style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '3px 0', fontSize: 11,
            }}>
              <I name="alertTriangle" size={12} color={row.error ? T.error : T.textDim} />
              <span className="mono" style={{ color: T.textMuted }}>{row.name || row.path}</span>
              <EnforcementBadge row={row} />
              {row.error && <span style={{ color: T.textDim }}>{row.error}</span>}
            </div>
          ))}
        </div>
      )}

      {confirm && (
        <ConfirmDialog
          title="Turn tool discipline off?"
          confirmLabel="Turn off"
          message={
            `${confirm.row.name || confirm.row.path}: C3 will stop nudging the `
            + 'agent toward c3_* tools entirely. Native Edit and Write run '
            + 'without a hint.\n\n'
            + 'STILL ENFORCED: Access Guard path rules, the credential-vault '
            + 'write guard, and agent locks. This switch cannot reach them.\n\n'
            + 'WHAT YOU LOSE: c3_edit takes a pre-edit snapshot that makes a '
            + 'clean revert possible. The edit ledger still records native '
            + 'writes, but without that snapshot.'
          }
          onConfirm={() => applyMode(confirm.row, confirm.mode)}
          onCancel={() => setConfirm(null)} />
      )}
    </div>
  );
}
