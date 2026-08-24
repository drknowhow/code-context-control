// ─── Credential audit: one timeline of every change and every use ──────
// Backed by services/cred_audit, which merges two logs that were never
// joined before: .c3/cred_usage.jsonl (uses) and the `cred_action` rows in
// .c3/activity_log.jsonl (changes). Answering "who changed this key, and
// where has it been used since" used to mean reading both by eye.
//
// path falsy + globalOnly → the shared vault alone (?scope=global).
// path falsy          → the cross-project roll-up (/api/hub/credentials/audit).
// path set   → that project's timeline, global scope merged in, because a
//              global credential used from a project records into ~/.c3.
//
// Nothing here can leak a value: neither log stores one. `cmd` is the RAW
// TEMPLATE the user typed ({{cred:X}} / $NAME), never the substitution, so
// the command is shown in full rather than masked — masking it would imply
// there was something behind the mask.

const CRED_AUDIT_KINDS = [
  ['', 'All'],
  ['use', 'Uses'],
  ['change', 'Changes'],
];

// The two actions that put a plaintext value somewhere a person or a model
// can read it. Everything else hands the value to a subprocess and never
// surfaces it, which is a different risk and reads differently in a list.
const CRED_AUDIT_EXPOSING = ['reveal', 'cli_show'];

const credAuditIsExposing = (a) => CRED_AUDIT_EXPOSING.indexOf(a) !== -1;

const CRED_AUDIT_ACTION_COLOR = (ev) => {
  if (credAuditIsExposing(ev.action)) return T.error;
  if (ev.action === 'delete') return T.error;
  if (ev.kind === 'change') return T.warn;
  return T.accent;
};

const credAuditWhen = (iso) => (iso
  ? String(iso).replace('T', ' ').replace(/\.\d+/, '').replace(/\+.*$/, '').replace(/Z$/, '')
  : '—');

// Absolute project paths are long and mostly identical; the tail is what
// tells two rows apart.
const credAuditWhere = (ev) => {
  if (ev.project_name) return ev.project_name;
  const p = String(ev.project || '');
  if (!p) return '';
  const parts = p.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || p;
};

function CredAuditRow({ ev, striped, onFilterName }) {
  const [open, setOpen] = useState(false);
  const color = CRED_AUDIT_ACTION_COLOR(ev);
  const name = ev.name + (ev.field ? `.${ev.field}` : '');
  return (
    <div style={{
      borderTop: `1px solid ${T.border}`,
      background: striped ? `${T.surfaceAlt}70` : T.surface,
      padding: '8px 12px',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <span className="mono" style={{ fontSize: 11, color: T.textMuted, minWidth: 138 }}>
          {credAuditWhen(ev.ts)}
        </span>
        <Badge color={ev.kind === 'change' ? T.warn : T.blue}>{ev.kind}</Badge>
        <span className="mono" style={{ fontSize: 11.5, color, minWidth: 92 }}>{ev.action}</span>
        <span className="mono" title="Filter to this credential"
          onClick={() => onFilterName && onFilterName(ev.name)}
          style={{
            fontSize: 12, color: T.text, fontWeight: 600,
            cursor: onFilterName ? 'pointer' : 'default',
          }}>{name}</span>
        {credAuditIsExposing(ev.action) && (
          <span title="This put a plaintext value where a person or a model could read it">
            <Badge color={T.error}>exposing</Badge>
          </span>
        )}
        {ev.scope && <Badge color={ev.scope === 'global' ? T.accent : T.blue}>{ev.scope}</Badge>}
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 11, color: T.textDim }}>{credAuditWhere(ev)}</span>
        <span className="mono" style={{ fontSize: 11, color: T.textMuted }}>via {ev.surface || '?'}</span>
        {typeof ev.exit === 'number' && (
          <Badge color={ev.exit === 0 ? T.accent : T.error}>exit {ev.exit}</Badge>
        )}
        {ev.cmd && (
          <button className="btn" onClick={() => setOpen(!open)} style={{
            background: 'transparent', border: `1px solid ${T.border}`,
            borderRadius: 5, color: T.textMuted, fontSize: 10.5,
            padding: '1px 7px', cursor: 'pointer',
          }}>{open ? 'hide cmd' : 'cmd'}</button>
        )}
      </div>
      {open && ev.cmd && (
        <div className="mono" style={{
          marginTop: 6, padding: '6px 9px', borderRadius: 5, fontSize: 11,
          background: T.surfaceAlt, color: T.textDim, whiteSpace: 'pre-wrap',
          wordBreak: 'break-all', border: `1px solid ${T.border}`,
        }}>{ev.cmd}</div>
      )}
    </div>
  );
}

// globalOnly: `path` is falsy for BOTH the shared vault and the
// cross-project roll-up, so the caller has to say which it means.
function CredAuditView({ path, projectName, initialName, onOpenDrill, globalOnly }) {
  const [kind, setKind] = useState('');
  const [name, setName] = useState(initialName || '');
  const [q, setQ] = useState('');
  const [exposingOnly, setExposingOnly] = useState(false);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => { setName(initialName || ''); }, [initialName]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const qs = [`limit=400`];
      if (kind) qs.push('kind=' + encodeURIComponent(kind));
      if (name) qs.push('name=' + encodeURIComponent(name));
      if (q) qs.push('q=' + encodeURIComponent(q));
      if (!path && globalOnly) qs.push('scope=global');
      const url = path
        ? `/api/projects/credentials/audit?path=${encodeURIComponent(path)}&${qs.join('&')}`
        : `/api/hub/credentials/audit?${qs.join('&')}`;
      setData(await api.get(url));
      setError('');
    } catch (e) { setError(apiErr(e)); }
    setLoading(false);
  }, [path, globalOnly, kind, name, q]);

  useEffect(() => { load(); }, [load]);

  const all = (data && data.events) || [];
  const events = exposingOnly ? all.filter(e => credAuditIsExposing(e.action)) : all;
  const counts = (data && data.counts) || {};
  const narrowed = !!(kind || name || q || exposingOnly);

  return (
    <div className="fade-up">
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 10 }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 6, flex: '1 1 220px', minWidth: 180,
          border: `1px solid ${T.border}`, borderRadius: 6, padding: '0 9px',
          background: T.surfaceAlt,
        }}>
          <I name="search" size={12} color={T.textMuted} />
          <input value={q} onChange={e => setQ(e.target.value)}
            placeholder="Search name, command or project"
            style={{
              flex: 1, background: 'transparent', border: 'none', outline: 'none',
              color: T.text, fontSize: 12, padding: '6px 0',
            }} autoComplete="off" spellCheck={false} />
          {q && (
            <button onClick={() => setQ('')} aria-label="Clear search"
              style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 2, display: 'flex' }}>
              <I name="xSmall" size={11} color={T.textMuted} />
            </button>
          )}
        </div>
        {CRED_AUDIT_KINDS.map(([id, label]) => (
          <button key={id || 'all'} className="btn" onClick={() => setKind(id)} style={{
            background: kind === id ? `${T.accent}22` : 'transparent',
            color: kind === id ? T.accent : T.textDim,
            border: `1px solid ${kind === id ? T.accent + '88' : T.border}`,
            borderRadius: 999, padding: '3px 12px', fontSize: 11.5, cursor: 'pointer',
          }}>{label}{id && counts[id] ? ` ${counts[id]}` : ''}</button>
        ))}
        <button className="btn" onClick={() => setExposingOnly(!exposingOnly)}
          title="reveal / cli_show — a plaintext value reached a person or a model"
          style={{
            background: exposingOnly ? `${T.error}22` : 'transparent',
            color: exposingOnly ? T.error : T.textDim,
            border: `1px solid ${exposingOnly ? T.error + '88' : T.border}`,
            borderRadius: 999, padding: '3px 12px', fontSize: 11.5, cursor: 'pointer',
          }}>exposing{counts.exposing ? ` ${counts.exposing}` : ''}</button>
        <button className="btn" onClick={load} disabled={loading} style={{
          background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
          padding: '4px 11px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
        }}>{loading ? 'Loading…' : 'Refresh'}</button>
      </div>

      {name && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8,
          fontSize: 11.5, color: T.textDim,
        }}>
          Filtered to <span className="mono" style={{ color: T.text }}>{name}</span>
          <button className="btn" onClick={() => setName('')} style={{
            background: 'transparent', border: `1px solid ${T.border}`,
            borderRadius: 5, color: T.accent, fontSize: 11, padding: '1px 8px', cursor: 'pointer',
          }}>clear</button>
        </div>
      )}

      {error && (
        <div style={{
          padding: '8px 12px', borderRadius: 6, marginBottom: 10, fontSize: 12,
          background: `${T.error}22`, color: T.error, border: `1px solid ${T.error}55`,
        }}>{error}</div>
      )}

      <div style={{ fontSize: 11, color: T.textDim, marginBottom: 6 }}>
        {path
          ? `${projectName || 'This project'} — project entries plus the global vault, which is where a shared credential records its use.`
          : globalOnly
            ? 'The shared vault (~/.c3) only — its own changes, and every use of a global credential from any project.'
            : 'Every registered project plus the global vault. Each project contributes its own entries; the shared vault is counted once.'}
        {data && <span> · showing {events.length} of {data.matched}</span>}
        {data && data.truncated && <span> · older events not loaded</span>}
      </div>

      {loading && !data ? (
        <div style={{ color: T.textMuted, fontSize: 13 }}>Loading…</div>
      ) : !events.length ? (
        <div style={{
          border: `1px dashed ${T.border}`, borderRadius: 8, padding: 24,
          textAlign: 'center', color: T.textMuted, fontSize: 12.5,
        }}>
          {narrowed
            ? <span>Nothing matches these filters.</span>
            : <span>No credential activity recorded yet. Events appear here the
                first time a credential is created, changed, or used by{' '}
                <span className="mono">c3_shell</span>.</span>}
        </div>
      ) : (
        <div style={{ border: `1px solid ${T.border}`, borderRadius: 8, overflow: 'hidden' }}>
          {events.map((ev, i) => (
            <CredAuditRow key={`${ev.ts}-${ev.name}-${ev.action}-${i}`} ev={ev}
              striped={!!(i % 2)} onFilterName={setName} />
          ))}
        </div>
      )}

      {data && data.errors && data.errors.length > 0 && (
        <div style={{
          marginTop: 10, padding: '8px 12px', borderRadius: 6, fontSize: 11.5,
          background: `${T.warn}18`, color: T.text, border: `1px solid ${T.warn}55`,
        }}>
          {data.errors.length} project{data.errors.length === 1 ? '' : 's'} could not
          be read — their events are missing from this timeline rather than
          absent: {data.errors.map(e => e.name).join(', ')}.
        </div>
      )}

      {data && (
        <div style={{ fontSize: 10.5, color: T.textMuted, marginTop: 12, lineHeight: 1.5 }}>
          {data.note}
        </div>
      )}
    </div>
  );
}
