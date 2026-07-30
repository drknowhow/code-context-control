// ─── Agent Locks (cross-project) ──────────────────────────────────────────
// Who holds which file, across every registered project, and the one human
// override: force-release. That bumps the fencing counter so a holder which
// comes back is stale by construction — a decision for a person, which is why
// the agent-facing c3_locks tool deliberately has no force action.
//
// Honesty rules this view must keep (docs/agent-locks.md §4, §9):
//   - never render "protected" for a repo we cannot actually protect;
//   - a project we could not READ is not a project with zero leases;
//   - the coverage caveat stays visible, because a lease gates C3 tool
//     surfaces only — a raw shell redirect is not covered.

function LockBadge({ row }) {
  const pill = (label, fg, bg, title) => (
    <span title={title} className="mono" style={{
      fontSize: 10, padding: '2px 7px', borderRadius: 4,
      color: fg, background: bg, whiteSpace: 'nowrap',
    }}>{label}</span>
  );
  if (row.error) {
    return pill('UNREADABLE', T.error, T.errorDim,
      `Lock state could not be read: ${row.error}. This is not the same as "no leases".`);
  }
  if (!row.initialized) {
    return pill('NO .c3', T.textDim, T.surfaceAlt,
      'C3 is not initialized here, so nothing coordinates edits in this repo.');
  }
  if (!row.enabled) {
    return pill('DISABLED', T.warn, T.warnDim,
      'locks.enabled=false in .c3/config.json — leases are not taken or honoured here.');
  }
  return pill(String(row.mode || 'advisory').toUpperCase(), T.accent, T.accentDim,
    'Advisory: enforced on C3 tool surfaces. A raw shell redirect or a '
    + 'non-Claude agent is not covered.');
}

function LeaseRow({ row, lease, onForce }) {
  // Draining bar: TTL is the real release mechanism, so how much is left is
  // the most useful thing on the row.
  const total = 900;
  const left = Math.max(0, lease.expires_in_s || 0);
  const pct = Math.max(2, Math.min(100, (left / total) * 100));
  const mins = Math.floor(left / 60), secs = Math.floor(left % 60);
  const low = left < 120;

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'minmax(0,2fr) minmax(0,1.2fr) minmax(0,1.6fr) 96px 30px',
      gap: 10, alignItems: 'center', padding: '7px 10px',
      borderTop: `1px solid ${T.border}`, fontSize: 12,
    }}>
      <span className="mono" title={lease.relpath} style={{
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: T.text,
      }}>{lease.relpath}</span>

      <span className="mono" title={`session ${lease.session_id || 'unknown'}`} style={{
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        color: T.blue, fontSize: 11,
      }}>{lease.agent_id || 'unknown'}</span>

      <span title={lease.intent || 'no intent declared'} style={{
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        color: lease.intent ? T.textMuted : T.textDim,
        fontStyle: lease.intent ? 'normal' : 'italic',
      }}>{lease.intent || 'no intent'}</span>

      <div title={`lease expires in ${mins}m${String(secs).padStart(2, '0')}s`}>
        <div className="mono" style={{
          fontSize: 10, color: low ? T.warn : T.textMuted, marginBottom: 3,
        }}>{mins}m{String(secs).padStart(2, '0')}s</div>
        <div style={{ height: 3, background: T.surfaceAlt, borderRadius: 2 }}>
          <div style={{
            width: `${pct}%`, height: '100%', borderRadius: 2,
            background: low ? T.warn : T.accent,
          }} />
        </div>
      </div>

      <div title="Force-release: breaks the lease and bumps the fencing token, so the current holder goes stale even if it thinks it still holds it."
        onClick={() => onForce(row, lease)}
        style={{
          cursor: 'pointer', display: 'flex', justifyContent: 'center',
          padding: 4, borderRadius: 4,
        }}>
        <I name="xCircle" size={13} color={T.error} />
      </div>
    </div>
  );
}

function HubLocks({ projects, onOpenDrill }) {
  const { useState, useCallback } = React;
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');
  const [confirm, setConfirm] = useState(null);

  const load = useCallback(async () => {
    try {
      setData(await api.get('/api/hub/locks/overview'));
      setErr('');
    } catch (e) {
      // Keep the last good snapshot rather than blanking to an empty page,
      // which would read as "nothing is locked".
      setErr(String(e && e.message ? e.message : e));
    }
  }, []);

  React.useEffect(() => { load(); }, [load]);
  usePoll(load, 5000);

  const doForce = async (row, lease) => {
    try {
      await api.post('/api/projects/locks/force-release', {
        path: row.path, relpath: lease.relpath, note: 'released from the Hub',
      });
    } catch (e) {
      setErr(String(e && e.message ? e.message : e));
    }
    setConfirm(null);
    load();
  };

  const rows = (data && data.projects) || [];
  const withLeases = rows.filter(r => (r.count || 0) > 0);
  const problems = rows.filter(r => r.error || (r.initialized && !r.enabled));

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <I name="lock" size={15} color={T.accent} />
        <span style={{ fontSize: 14, color: T.text }}>Agent Locks</span>
        <span className="mono" style={{ fontSize: 11, color: T.textMuted }}>
          {data ? `${data.total} active lease(s) across ${rows.length} project(s)` : 'loading…'}
        </span>
        <div style={{ flex: 1 }} />
        <div onClick={load} title="Refresh"
          style={{ cursor: 'pointer', padding: 4 }}>
          <I name="refresh" size={13} color={T.textMuted} />
        </div>
      </div>

      {/* Never let the page imply more protection than exists. */}
      <div style={{
        fontSize: 11, color: T.textMuted, background: T.surface,
        border: `1px solid ${T.border}`, borderRadius: 6, padding: '8px 10px',
        lineHeight: 1.5,
      }}>
        {(data && data.coverage_note)
          || 'Leases gate C3 tool surfaces only.'}
        {' '}A lease is cooperative coordination, not containment.
      </div>

      {err && (
        <div style={{
          fontSize: 11, color: T.error, background: T.errorDim,
          border: `1px solid ${T.error}`, borderRadius: 6, padding: '8px 10px',
        }}>
          Could not refresh: {err} — showing the last snapshot, which may be stale.
        </div>
      )}

      {data && withLeases.length === 0 && (
        <div style={{
          fontSize: 12, color: T.textMuted, padding: '18px 10px', textAlign: 'center',
          border: `1px dashed ${T.border}`, borderRadius: 6,
        }}>
          No active leases. Agents take one automatically when they edit a file.
        </div>
      )}

      {withLeases.map(row => (
        <div key={row.path} style={{
          background: T.surface, border: `1px solid ${T.border}`, borderRadius: 6,
        }}>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px',
          }}>
            <I name="folder" size={13} color={T.textMuted} />
            <span onClick={() => onOpenDrill && onOpenDrill(
              projects.find(p => p.path === row.path) || { path: row.path, name: row.name })}
              style={{ fontSize: 12, color: T.text, cursor: onOpenDrill ? 'pointer' : 'default' }}>
              {row.name || row.path}
            </span>
            <LockBadge row={row} />
            <span className="mono" style={{ fontSize: 11, color: T.textMuted }}>
              {row.count} lease{row.count === 1 ? '' : 's'}
            </span>
            <div style={{ flex: 1 }} />
            <span className="mono" title="Monotonic fencing counter — force-release bumps it."
              style={{ fontSize: 10, color: T.textDim }}>fencing {row.fencing}</span>
          </div>
          <div style={{
            display: 'grid',
            gridTemplateColumns: 'minmax(0,2fr) minmax(0,1.2fr) minmax(0,1.6fr) 96px 30px',
            gap: 10, padding: '0 10px 5px', fontSize: 10, color: T.textDim,
          }}>
            <span>FILE</span><span>HOLDER</span><span>INTENT</span>
            <span>LEASE LEFT</span><span />
          </div>
          {row.locks.map(lease => (
            <LeaseRow key={lease.relpath} row={row} lease={lease}
              onForce={(r, l) => setConfirm({ row: r, lease: l })} />
          ))}
        </div>
      ))}

      {/* Projects that cannot coordinate are listed explicitly — silence here
          would read as "all clear". */}
      {problems.length > 0 && (
        <div style={{
          background: T.surface, border: `1px solid ${T.border}`,
          borderRadius: 6, padding: '8px 10px',
        }}>
          <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 6 }}>
            Not coordinating
          </div>
          {problems.map(row => (
            <div key={row.path} style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '3px 0', fontSize: 11,
            }}>
              <I name="alertTriangle" size={12} color={row.error ? T.error : T.warn} />
              <span className="mono" style={{ color: T.textMuted }}>{row.name || row.path}</span>
              <LockBadge row={row} />
              {row.error && <span style={{ color: T.textDim }}>{row.error}</span>}
            </div>
          ))}
        </div>
      )}

      {confirm && (
        <ConfirmDialog
          title="Break this lease?"
          danger
          confirmLabel="Force-release"
          message={
            `${confirm.lease.relpath} is held by ${confirm.lease.agent_id}`
            + (confirm.lease.intent ? ` ("${confirm.lease.intent}")` : '')
            + '. Force-releasing bumps the fencing token, so that agent goes '
            + 'stale even if it still believes it holds the file — it may be '
            + 'mid-edit. Prefer waiting for the lease to expire.'
          }
          onConfirm={() => doForce(confirm.row, confirm.lease)}
          onCancel={() => setConfirm(null)} />
      )}
    </div>
  );
}
