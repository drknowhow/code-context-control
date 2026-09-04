// ─── Access approvals (cross-project) ─────────────────────────────────────
// The desktop half of Override Requests (docs/override-requests.md P5,
// docs/confirm-guard.md): pending confirmation cards with approve/deny, and
// a read-only view of each project's effective path policy.
//
// This tab is about Access Guard — which PATHS the agent may touch, a
// security boundary. Tool discipline (how hard C3 pushes toward c3_* tools)
// is a different layer with its own tab; conflating them is what makes "the
// guard is slowing me down" unfixable without weakening something that
// should have stayed hard.
//
// Honesty rules this view must keep:
//   - the justification is agent-written untrusted text: always quoted,
//     always labeled, never rendered as markup;
//   - approving never changes the rule — the rule survives every grant, and
//     the card must not imply otherwise;
//   - deny is always the cheaper gesture than approve (one click; approving
//     an access_deny/access_builtin request costs the rule glob typed by
//     hand — the server enforces that regardless of what this UI believes);
//   - a request that lapsed while the page showed it refreshes to its real
//     status (the decide route answers 409), never silently mints a grant;
//   - rule MUTATION stays on the per-project server and `c3 access` — this
//     tab approves and reads, it does not edit policy.

const ACC_KIND_COLOR = (kind) => (
  kind === 'deny' ? T.error : kind === 'mask' ? T.blue
    : kind === 'confirm' ? T.accent : T.warn
);

const ACC_STATUS_COLOR = (s) => (
  s === 'pending' ? T.warn : s === 'approved' ? T.accent
    : s === 'denied' ? T.error : T.textDim
);

function accExpiresIn(iso) {
  if (!iso) return '';
  const ms = new Date(iso).getTime() - Date.now();
  if (isNaN(ms)) return '';
  if (ms <= 0) return 'expired';
  const m = Math.floor(ms / 60000), s = Math.floor((ms % 60000) / 1000);
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

// The typed-approval spec, rendered by CredConfirm (hub_credentials.js).
// Only access_deny / access_builtin requests need it; the challenge is the
// rule glob itself, and the server re-checks it in decide().
function accApproveConfirm(row, onConfirm) {
  return {
    title: 'Override a deny rule once?', tone: 'error',
    confirmLabel: 'Approve once', requireText: row.confirm_with,
    intro: <span>Approving lets <b>this session</b> retry{' '}
      <span className="mono">{row.tool} {row.op}</span> on{' '}
      <span className="mono">{row.path}</span> exactly once, soon.</span>,
    bullets: [
      `The rule ${row.rule} stays in force — this is a single-use grant, not a policy change.`,
      'The grant is bound to this session, this tool, this operation, and this exact path.',
      `Project: ${row.project_path}`,
    ],
    onConfirm,
  };
}

// The rule-scoped challenge. Deliberately harsher copy than the one above:
// this is the only approval in the product that authorises files the user is
// not looking at, so the dialog has to say which files those are BEFORE the
// glob is retyped. requireText is always the rule — on every layer, not just
// the two that already demand it — and decide() re-checks it server-side.
function accRuleGrantConfirm(row, onConfirm) {
  return {
    title: 'Stop asking about this rule, this session?', tone: 'error',
    confirmLabel: 'Approve for this rule', requireText: row.rule,
    intro: <span>This is bigger than the file on screen. Approving lets{' '}
      <b>this session</b> do <span className="mono">{row.op}</span> on{' '}
      <b>every path</b> that <span className="mono">{row.rule}</span> matches
      — not just <span className="mono">{row.path}</span> — without asking
      again.</span>,
    bullets: [
      `The rule ${row.rule} stays in force for everything else; this grant is a standing exception inside it.`,
      'Any tool in the same op class can use it — an Edit approval also covers Write.',
      'It expires on its own, and sooner if the session stops using it. Revoke it any time under Active grants.',
      'The vault, .c3 policy files and Tier-0 denies stay unreachable — no grant can cover them.',
      `Project: ${row.project_path}`,
    ],
    onConfirm,
  };
}

function AccJustification({ text }) {
  if (!text) return null;
  return (
    <div style={{
      marginTop: 8, padding: '7px 10px', borderLeft: `3px solid ${T.border}`,
      background: T.surfaceAlt, borderRadius: 4,
    }}>
      <div style={{ fontSize: 10, color: T.textDim, marginBottom: 3 }}>
        The agent wrote this. It may be repeating text it read from a file.
      </div>
      <div style={{ fontSize: 12, color: T.textMuted, whiteSpace: 'pre-wrap' }}>{text}</div>
    </div>
  );
}

function AccRequestCard({ row, busy, onDecide, onTypedApprove, onRuleApprove }) {
  const pending = row.status === 'pending';
  const leaf = String(row.path || '').split(/[\\/]/).filter(Boolean).pop() || row.path;
  const expires = pending ? accExpiresIn(row.expires_at) : '';
  return (
    <div style={{
      background: T.surface, border: `1px solid ${T.border}`, borderRadius: 10,
      padding: '12px 16px',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span className="mono" title={row.project_path} style={{
          fontSize: 10.5, padding: '2px 7px', borderRadius: 4,
          background: T.surfaceAlt, color: T.textMuted,
        }}>{row.project_name || row.project_path}</span>
        <span style={{ fontSize: 12.5, fontWeight: 700, color: T.text }}>
          {row.tool} {row.op}
        </span>
        <span className="mono" title={row.path} style={{ fontSize: 12, color: T.text }}>{leaf}</span>
        <span className="mono" style={{
          fontSize: 10.5, padding: '2px 7px', borderRadius: 4,
          color: ACC_STATUS_COLOR(row.status),
          background: `${ACC_STATUS_COLOR(row.status)}22`,
        }}>{String(row.status || '').toUpperCase()}</span>
        <div style={{ flex: 1 }} />
        {expires && (
          <span className="mono" title={`expires ${row.expires_at}`}
            style={{ fontSize: 10.5, color: expires === 'expired' ? T.error : T.textDim }}>
            {expires === 'expired' ? 'expired' : `expires in ${expires}`}
          </span>
        )}
      </div>
      <div style={{ marginTop: 6, fontSize: 11.5, color: T.textMuted }}>
        blocked by <span className="mono" style={{ color: T.text }}>{row.rule}</span>{' '}
        <span className="mono" style={{ color: T.textDim }}>({row.rule_class})</span>
        {row.session_id && (
          <span className="mono" style={{ color: T.textDim }}> · session {row.session_id}</span>
        )}
        {row.grant_id && (
          <span className="mono" style={{ color: T.accent }}> · grant {row.grant_id}</span>
        )}
      </div>
      <AccJustification text={row.justification} />
      {pending && !row.escalatable && (
        <div style={{ marginTop: 8, fontSize: 11, color: T.warn }}>
          This layer is no longer escalatable for the project — approving would
          be refused. Fix it in the project's override policy, or deny.
        </div>
      )}
      {pending && (
        <div style={{ display: 'flex', gap: 8, marginTop: 10, alignItems: 'center' }}>
          <Btn color={T.accent} disabled={busy}
            onClick={() => (row.needs_typed_confirm
              ? onTypedApprove(row) : onDecide(row, 'approve', {}))}>
            {row.needs_typed_confirm ? 'Approve…' : 'Approve once'}
          </Btn>
          {row.allow_rule_grants && row.escalatable && (
            <Btn variant="ghost" disabled={busy}
              onClick={() => onRuleApprove(row)}
              title={`Stop asking about ${row.rule} for the rest of this session`}>
              Approve for this rule…
            </Btn>
          )}
          <Btn variant="ghost" disabled={busy}
            onClick={() => onDecide(row, 'deny', {})}>Deny</Btn>
          <Btn variant="ghost" disabled={busy}
            onClick={() => onDecide(row, 'deny', { mute: true })}
            title="Deny, and stop this session asking the same thing again">
            Deny + mute
          </Btn>
          {row.needs_typed_confirm && (
            <span style={{ fontSize: 10.5, color: T.textDim }}>
              approval requires the rule glob typed by hand
            </span>
          )}
        </div>
      )}
      {!pending && row.decision_note && (
        <div style={{ marginTop: 6, fontSize: 11, color: T.textDim }}>
          note: {row.decision_note}
        </div>
      )}
    </div>
  );
}

// ── Read-only per-project policy matrix ────────────────────────────────────
function AccRulesPanel({ projects }) {
  const [path, setPath] = useState('');
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');

  const load = useCallback(async (p) => {
    if (!p) { setData(null); setErr(''); return; }
    try {
      setData(await api.get(`/api/hub/access?path=${encodeURIComponent(p)}`));
      setErr('');
    } catch (e) { setData(null); setErr(apiErr(e)); }
  }, []);

  const scopes = (data && data.rules) || {};
  const layerRows = data && data.policy ? Object.entries(data.policy.layers || {}) : [];
  return (
    <div style={{
      background: T.surface, border: `1px solid ${T.border}`, borderRadius: 10,
      padding: '12px 16px',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <div style={{ fontSize: 12.5, fontWeight: 700, color: T.text }}>Effective path policy</div>
        <select value={path}
          onChange={e => { setPath(e.target.value); load(e.target.value); }}
          style={{
            background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
            borderRadius: 6, fontSize: 12, padding: '4px 8px', maxWidth: 340,
          }}>
          <option value="">— pick a project —</option>
          {(projects || []).map(p => (
            <option key={p.path} value={p.path}>{p.name || p.path}</option>
          ))}
        </select>
        <span style={{ fontSize: 10.5, color: T.textDim }}>
          read-only — edit rules in the project's Access tab or `c3 access`
        </span>
      </div>
      {err && <div style={{ marginTop: 8, fontSize: 11.5, color: T.error }}>{err}</div>}
      {data && (
        <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
          {['builtin', 'global', 'project'].map(scope => {
            const sec = scopes[scope] || {};
            const rows = [];
            ['deny', 'read_only', 'confirm'].forEach(kind =>
              (sec[kind] || []).forEach(g => rows.push([kind, g])));
            (sec.mask || []).forEach(e => rows.push(['mask', `${e.glob} → ${e.preset}`]));
            return (
              <div key={scope}>
                <div style={{
                  fontSize: 10.5, fontWeight: 700, letterSpacing: 1,
                  textTransform: 'uppercase', color: T.textDim, marginBottom: 4,
                }}>
                  {scope}
                  {sec.corrupt && (
                    <span style={{ color: T.error, marginLeft: 8, textTransform: 'none', letterSpacing: 0 }}>
                      config invalid — scope fails closed (deny-all)
                    </span>
                  )}
                </div>
                {rows.length === 0 ? (
                  <div style={{ fontSize: 11.5, color: T.textDim }}>no rules</div>
                ) : (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                    {rows.map(([kind, glob], i) => (
                      <span key={i} className="mono" style={{
                        fontSize: 11, padding: '2px 8px', borderRadius: 4,
                        color: ACC_KIND_COLOR(kind),
                        background: `${ACC_KIND_COLOR(kind)}18`,
                        border: `1px solid ${ACC_KIND_COLOR(kind)}44`,
                      }} title={kind}>{kind}: {glob}</span>
                    ))}
                  </div>
                )}
                {scope === 'builtin' && (sec.disabled || []).length > 0 && (
                  <div style={{ marginTop: 4, fontSize: 11, color: T.warn }}>
                    disabled by you: {(sec.disabled || []).join(', ')}
                  </div>
                )}
              </div>
            );
          })}
          {layerRows.length > 0 && (
            <div>
              <div style={{
                fontSize: 10.5, fontWeight: 700, letterSpacing: 1,
                textTransform: 'uppercase', color: T.textDim, marginBottom: 4,
              }}>override layers (escalatable on request)</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                <span className="mono" style={{
                  fontSize: 11, padding: '2px 8px', borderRadius: 4,
                  color: data.policy.enabled ? T.accent : T.textDim,
                  background: T.surfaceAlt,
                }}>enabled: {String(data.policy.enabled)}</span>
                {layerRows.map(([k, v]) => (
                  <span key={k} className="mono" style={{
                    fontSize: 11, padding: '2px 8px', borderRadius: 4,
                    color: v ? T.accent : T.textDim, background: T.surfaceAlt,
                  }}>{k}: {String(v)}</span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Active grants ──────────────────────────────────────────────────────────
// A rule grant can authorise many calls over hours. The only other place it
// shows up is a line in the agent's own transcript, which is the wrong place
// to police it from — so the screen that mints one also lists and kills it.
function AccGrantsPanel({ projects }) {
  const [path, setPath] = useState('');
  const [grants, setGrants] = useState([]);
  const [busyId, setBusyId] = useState('');
  const known = projects || [];

  useEffect(() => {
    if (!path && known.length) setPath(known[0].path);
  }, [known, path]);

  const load = useCallback(async () => {
    if (!path) { setGrants([]); return; }
    try {
      const data = await api.get(`/api/hub/grants?path=${encodeURIComponent(path)}`);
      setGrants(data.grants || []);
    } catch { /* keep last good list */ }
  }, [path]);

  useEffect(() => { load(); }, [load]);
  usePoll(load, 5000);

  const revoke = async (g) => {
    setBusyId(g.id);
    try {
      await api.del(`/api/hub/grants/${g.id}?path=${encodeURIComponent(path)}`);
      notify(`Revoked ${g.id} — the rule ${g.rule} blocks again`, 'ok');
    } catch (e) { notify(apiErr(e), 'err'); }
    setBusyId('');
    load();
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 4 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 13, fontWeight: 700, color: T.text }}>Active grants</span>
        <span style={{ fontSize: 11.5, color: T.textMuted }}>
          live exceptions the agent can still use — revoking one takes effect
          on its next call
        </span>
        <div style={{ flex: 1 }} />
        <select value={path} onChange={e => setPath(e.target.value)} style={{
          height: 26, borderRadius: 6, fontSize: 11.5, padding: '0 8px',
          background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
        }}>
          {known.map(p => <option key={p.path} value={p.path}>{p.name || p.path}</option>)}
        </select>
      </div>
      {grants.length === 0 ? (
        <div style={{ fontSize: 11.5, color: T.textDim }}>
          No live grants in this project.
        </div>
      ) : grants.map(g => {
        const wide = g.scope === 'rule';
        const left = g.uses_remaining === null || g.uses_remaining === undefined
          ? 'unlimited' : `${g.uses_remaining} left`;
        return (
          <div key={g.id} style={{
            background: T.surface, border: `1px solid ${wide ? T.warn : T.border}`,
            borderRadius: 10, padding: '10px 14px',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <span className="mono" style={{
                fontSize: 10.5, padding: '2px 7px', borderRadius: 4,
                color: wide ? T.warn : T.textMuted,
                background: wide ? `${T.warn}22` : T.surfaceAlt,
              }}>{wide ? 'RULE' : 'ONE CALL'}</span>
              <span className="mono" style={{ fontSize: 12, color: T.text }}>{g.rule}</span>
              <span style={{ fontSize: 11.5, color: T.textMuted }}>
                {g.op}{wide ? ' · any path this rule matches' : ` · ${g.tool}`}
              </span>
              <div style={{ flex: 1 }} />
              <span className="mono" style={{ fontSize: 10.5, color: T.textDim }}>
                {left} · expires in {accExpiresIn(g.expires_at) || '—'}
                {g.idle_s ? ` · idle cap ${Math.round(g.idle_s / 60)}m` : ''}
              </span>
              <Btn variant="ghost" disabled={busyId === g.id}
                onClick={() => revoke(g)}>Revoke</Btn>
            </div>
            <div style={{ marginTop: 5, fontSize: 10.5, color: T.textDim }}>
              <span className="mono">{g.id}</span> · session{' '}
              <span className="mono">{g.session_id}</span>
              {wide && <> · minted from <span className="mono">{g.path_key}</span></>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── The tab ────────────────────────────────────────────────────────────────
function HubAccess({ projects }) {
  const [rows, setRows] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [chip, setChip] = useState('pending');
  const [busyId, setBusyId] = useState('');
  const [confirmSpec, setConfirmSpec] = useState(null);

  const load = useCallback(async () => {
    try {
      const status = chip === 'pending' ? 'pending' : '';
      const data = await api.get(`/api/hub/overrides?status=${status}&limit=100`);
      setRows(data.requests || []);
    } catch { /* keep last good list */ }
    setLoaded(true);
  }, [chip]);

  useEffect(() => { load(); }, [load]);
  usePoll(load, 5000);

  const decide = async (row, decision, opts) => {
    setBusyId(row.id);
    try {
      await api.post(`/api/hub/overrides/${row.id}`, { decision, ...opts });
      notify(decision !== 'approve'
        ? `Denied${opts.mute ? ' and muted' : ''}`
        : opts.mode === 'rule'
          ? `Approved every path ${row.rule} matches, this session — revoke it under Active grants`
          : `Approved ${row.tool} ${row.op} once — the rule stays in force`,
        decision === 'approve' ? 'ok' : 'warn');
    } catch (e) {
      notify(apiErr(e), 'err');
    }
    setBusyId('');
    load();
  };

  const typedApprove = (row) => {
    // CredConfirm validates the typed glob client-side; decide() re-checks
    // it server-side — two places compute, one enforces.
    setConfirmSpec(accApproveConfirm(row,
      () => decide(row, 'approve', { confirm: row.confirm_with })));
  };

  const ruleApprove = (row) => {
    setConfirmSpec(accRuleGrantConfirm(row,
      () => decide(row, 'approve', { mode: 'rule', confirm: row.rule })));
  };

  const pending = rows.filter(r => r.status === 'pending').length;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <I name="eye" size={16} color={T.accent} />
        <span style={{ fontSize: 14, fontWeight: 700, color: T.text }}>Access approvals</span>
        <span style={{ fontSize: 11.5, color: T.textMuted }}>
          {pending ? `${pending} pending` : 'nothing waiting'} — an approval is a
          single-use grant; the rule survives it
        </span>
        <div style={{ flex: 1 }} />
        {['pending', 'all'].map(c => (
          <button key={c} onClick={() => setChip(c)} style={{
            height: 26, padding: '0 12px', borderRadius: 6, fontSize: 11.5,
            cursor: 'pointer', border: `1px solid ${T.border}`,
            background: chip === c ? T.accentDim : 'transparent',
            color: chip === c ? T.accent : T.textMuted,
            fontWeight: chip === c ? 700 : 400,
          }}>{c === 'pending' ? 'Pending' : 'All recent'}</button>
        ))}
      </div>

      {!loaded ? (
        <div style={{ fontSize: 12, color: T.textDim }}>Loading…</div>
      ) : rows.length === 0 ? (
        <div style={{
          background: T.surface, border: `1px dashed ${T.border}`, borderRadius: 10,
          padding: '22px 16px', fontSize: 12, color: T.textDim, textAlign: 'center',
        }}>
          No {chip === 'pending' ? 'pending ' : ''}override requests. Agents ask
          here when a write hits a confirm rule or an escalatable block —
          set one with <span className="mono">c3 access add --kind confirm "&lt;glob&gt;"</span>.
        </div>
      ) : (
        rows.map(row => (
          <AccRequestCard key={row.id} row={row} busy={busyId === row.id}
            onDecide={decide} onTypedApprove={typedApprove}
            onRuleApprove={ruleApprove} />
        ))
      )}

      <AccGrantsPanel projects={projects} />
      <AccRulesPanel projects={projects} />
      {confirmSpec && (
        <CredConfirm spec={confirmSpec} onClose={() => setConfirmSpec(null)} />
      )}
    </div>
  );
}
