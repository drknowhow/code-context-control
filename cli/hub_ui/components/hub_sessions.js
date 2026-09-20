// ─── Sessions (past agent sessions, cross-project) ─────────────────────────
// Find an old session, see what it was about, and get back into it: a
// terminal running `claude --resume <id>`, the exact command to copy, or the
// claude.ai/code link when the session was bridged. Stale is a flag an agent
// (c3_session action='stale') or a person sets, with a reason; the grey
// "hints" are heuristics and never hide anything on their own.
//
// Data: /api/hub/sessions/{overview,detail,mark,resume} and /api/hub/sessions
// (services/session_catalog.py). Not /api/sessions — that manages UI servers.

const SESS_FILTERS = [
  ['hide', 'Active', 'Everything not marked stale'],
  ['likely', 'Likely stale', 'Not marked, but idle for weeks, ended by /clear, branch deleted or very short'],
  ['only', 'Stale', 'Marked stale by an agent or by you'],
  ['all', 'All', 'Everything'],
];

function sessPill(label, fg, bg, title) {
  return (
    <span title={title} className="mono" style={{
      fontSize: 10, padding: '1px 7px', borderRadius: 4, color: fg, background: bg,
      whiteSpace: 'nowrap', maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis',
    }}>{label}</span>
  );
}

function sessCopy(text) {
  const done = () => notify('Copied: ' + text);
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, () => notify('Copy failed', 'err'));
    return;
  }
  // http://127.0.0.1 is a secure context in current browsers; this is the
  // fallback for anything that is not.
  const ta = document.createElement('textarea');
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); done(); } catch { notify('Copy failed', 'err'); }
  document.body.removeChild(ta);
}

function SessIconBtn({ icon, label, title, onClick, disabled, color, href }) {
  const style = {
    display: 'inline-flex', alignItems: 'center', gap: 5, height: 24, padding: '0 8px',
    border: `1px solid ${T.border}`, borderRadius: 5, background: 'transparent',
    color: disabled ? T.textDim : (color || T.textMuted), fontSize: 11,
    cursor: disabled ? 'default' : 'pointer', textDecoration: 'none', whiteSpace: 'nowrap',
    opacity: disabled ? 0.6 : 1,
  };
  const inner = (
    <React.Fragment>
      <I name={icon} size={11} color={disabled ? T.textDim : (color || T.textMuted)} />
      {label}
    </React.Fragment>
  );
  if (href && !disabled) {
    return <a href={href} target="_blank" rel="noopener" title={title} style={style}
      onClick={e => e.stopPropagation()}>{inner}</a>;
  }
  return (
    <button title={title} style={style} disabled={disabled}
      onClick={e => { e.stopPropagation(); if (!disabled && onClick) onClick(); }}>{inner}</button>
  );
}

function SessDetail({ row, detail, onJump }) {
  const label = (t) => (
    <div style={{ fontSize: 10, color: T.textDim, letterSpacing: 1, textTransform: 'uppercase',
      margin: '10px 0 4px' }}>{t}</div>
  );
  const text = (t, dim) => (
    <div style={{ fontSize: 12, color: dim ? T.textMuted : T.text, whiteSpace: 'pre-wrap',
      wordBreak: 'break-word', lineHeight: 1.5 }}>{t}</div>
  );
  if (!detail) {
    return <div style={{ padding: '10px 14px', fontSize: 11, color: T.textMuted }}>loading…</div>;
  }
  if (detail.error) {
    return <div style={{ padding: '10px 14px', fontSize: 11, color: T.error }}>{detail.error}</div>;
  }
  const jump = (id) => (
    <span className="mono" onClick={() => onJump(id)} title="Show that session"
      style={{ color: T.blue, cursor: 'pointer' }}>{id.slice(0, 8)}</span>
  );
  const note = detail.note;
  return (
    <div style={{ padding: '4px 14px 12px 32px', borderTop: `1px solid ${T.border}` }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)', gap: 16 }}>
        <div>
          {label('First prompt')}
          {text(detail.first_prompt || '—', !detail.first_prompt)}
          {label('Last prompt')}
          {text(detail.last_prompt || '—', !detail.last_prompt)}
          {note && (
            <React.Fragment>
              {label(note.source === 'snapshot' ? 'From the last snapshot' : 'Agent note')}
              {note.summary && text(note.summary)}
              {note.next_steps && text('Next: ' + note.next_steps, true)}
            </React.Fragment>
          )}
          {detail.stale && (
            <React.Fragment>
              {label(`Stale — marked by ${detail.stale.by || '?'} ${timeAgo(detail.stale.at)}`)}
              {text(detail.stale.reason || 'no reason given', !detail.stale.reason)}
            </React.Fragment>
          )}
        </div>
        <div>
          {(detail.links.predecessor || detail.links.successor) && (
            <React.Fragment>
              {label('Chain')}
              <div style={{ fontSize: 12, color: T.textMuted, display: 'flex', gap: 14 }}>
                {detail.links.predecessor && <span>continues {jump(detail.links.predecessor)}</span>}
                {detail.links.successor && <span>continued in {jump(detail.links.successor)}</span>}
              </div>
            </React.Fragment>
          )}
          {detail.tasks && detail.tasks.length > 0 && (
            <React.Fragment>
              {label(`Tasks (${detail.tasks.length})`)}
              {detail.tasks.map(t => (
                <div key={t.id} style={{ fontSize: 12, color: T.text, display: 'flex', gap: 8 }}>
                  <span className="mono" style={{ color: t.status === 'done' ? T.accent : T.textMuted,
                    fontSize: 10, minWidth: 70 }}>{t.status}</span>
                  <span>{t.title}</span>
                </div>
              ))}
            </React.Fragment>
          )}
          {detail.decisions && detail.decisions.length > 0 && (
            <React.Fragment>
              {label(`Decisions (${detail.links.decisions})`)}
              {detail.decisions.slice(-5).map((d, i) => (
                <div key={i} style={{ fontSize: 12, color: T.text, marginBottom: 3 }}>• {d}</div>
              ))}
            </React.Fragment>
          )}
          {detail.snapshot && (
            <React.Fragment>
              {label(`Snapshot ${timeAgo(detail.snapshot.at)}`)}
              {text(detail.snapshot.task || '—')}
            </React.Fragment>
          )}
          {label('Where')}
          <div className="mono" style={{ fontSize: 11, color: T.textMuted, wordBreak: 'break-all' }}>
            {detail.id}<br />{detail.resume.cwd}
          </div>
        </div>
      </div>
      {detail.preview && detail.preview.length > 0 && (
        <React.Fragment>
          {label('Recent turns')}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 320,
            overflowY: 'auto', background: T.bg, border: `1px solid ${T.border}`,
            borderRadius: 6, padding: 10 }}>
            {detail.preview.map((p, i) => (
              <div key={i} style={{ display: 'flex', gap: 10 }}>
                <span className="mono" style={{ fontSize: 10, minWidth: 64, paddingTop: 2,
                  color: p.role === 'user' ? T.blue : T.purple }}>{p.role === 'user' ? 'you' : 'agent'}</span>
                <span style={{ fontSize: 12, color: T.text, whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word', lineHeight: 1.45 }}>{p.text}</span>
              </div>
            ))}
          </div>
        </React.Fragment>
      )}
    </div>
  );
}

function SessRow({ row, showProject, open, detail, onToggle, onResume, onMark, onUnmark, onJump }) {
  const r = row.resume;
  const summary = (row.note && row.note.summary) || (row.last_prompt ? 'Last: ' + row.last_prompt : '');
  return (
    <div style={{ background: T.surface, border: `1px solid ${open ? T.borderHover : T.border}`,
      borderRadius: 6, opacity: row.stale ? 0.75 : 1 }}>
      <div onClick={onToggle} style={{ display: 'flex', gap: 10, padding: '9px 12px',
        cursor: 'pointer', alignItems: 'flex-start' }}>
        <div style={{ paddingTop: 5 }}>
          <GlowDot color={row.live ? T.accent : T.textDim} size={7} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: T.text, overflow: 'hidden',
              textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={row.title}>{row.title}</span>
            {row.live && sessPill('LIVE', T.accent, T.accentDim, 'Open in an agent right now')}
            {showProject && sessPill(row.project.name, T.blue, T.blueDim, row.project.path)}
            {row.branch && <span className="mono" style={{ fontSize: 10, color: T.textMuted,
              whiteSpace: 'nowrap' }}>⎇ {row.branch}</span>}
            {row.provider !== 'claude' && sessPill(row.provider, T.purple, T.purpleDim, 'Agent host')}
            <div style={{ flex: 1 }} />
            <span className="mono" title={row.last_active} style={{ fontSize: 11, color: T.textMuted,
              whiteSpace: 'nowrap' }}>{timeAgo(row.last_active)}</span>
          </div>
          {summary && (
            <div style={{ fontSize: 12, color: T.textMuted, marginTop: 3, overflow: 'hidden',
              textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={summary}>{summary}</div>
          )}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 6, alignItems: 'center' }}>
            {row.stale && sessPill('stale: ' + (row.stale.reason || 'no reason'), T.warn, T.warnDim,
              `Marked by ${row.stale.by || '?'} ${timeAgo(row.stale.at)}`)}
            {row.hints.map(h => sessPill(h, T.textMuted, T.surfaceAlt,
              'A hint only — not marked stale'))}
            {row.links.tasks > 0 && sessPill(`${row.links.tasks} task${row.links.tasks === 1 ? '' : 's'}`,
              T.textMuted, T.surfaceAlt, 'C3 tasks created in or linked to this session')}
            {row.links.decisions > 0 && sessPill(`${row.links.decisions} decision${row.links.decisions === 1 ? '' : 's'}`,
              T.textMuted, T.surfaceAlt, 'Decisions logged with c3_session')}
            {row.links.snapshots > 0 && sessPill('snapshot', T.textMuted, T.surfaceAlt,
              'A C3 snapshot was taken in this session')}
            {row.links.successor && sessPill('→ ' + row.links.successor.slice(0, 8), T.blue, T.blueDim,
              'Continued in a later session')}
            <div style={{ flex: 1 }} />
            <SessIconBtn icon="play" label="Resume" color={T.accent} disabled={!r.can_launch}
              title={r.can_launch ? `Open a terminal: ${r.command}` : r.why_not}
              onClick={() => onResume(row)} />
            {r.command && <SessIconBtn icon="copy" label="Copy" title={r.command}
              onClick={() => sessCopy(r.command)} />}
            {r.remote_url && <SessIconBtn icon="external" label="Remote" href={r.remote_url}
              title="Open on claude.ai/code" />}
            {row.stale
              ? <SessIconBtn icon="refresh" label="Unmark" title="Clear the stale flag"
                  onClick={() => onUnmark(row)} />
              : <SessIconBtn icon="bookmark" label="Mark stale" title="Hide this session from the active list"
                  onClick={() => onMark(row)} />}
          </div>
        </div>
      </div>
      {open && <SessDetail row={row} detail={detail} onJump={onJump} />}
    </div>
  );
}

function SessMarkDialog({ row, onCancel, onConfirm }) {
  const [reason, setReason] = React.useState('');
  return (
    <div onClick={onCancel} style={{ position: 'fixed', inset: 0, background: '#0008', zIndex: 300,
      display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div onClick={e => e.stopPropagation()} style={{ width: 460, background: T.surface,
        border: `1px solid ${T.border}`, borderRadius: 8, padding: 18 }}>
        <div style={{ fontSize: 14, color: T.text, marginBottom: 6 }}>Mark this session stale?</div>
        <div style={{ fontSize: 12, color: T.textMuted, marginBottom: 10 }}>{row.title}</div>
        <textarea autoFocus value={reason} onChange={e => setReason(e.target.value)} rows={3}
          placeholder="Why (optional): superseded, finished, abandoned…" style={{
            width: '100%', boxSizing: 'border-box', background: T.bg, color: T.text,
            border: `1px solid ${T.border}`, borderRadius: 6, padding: 8, fontSize: 12,
            resize: 'vertical', outline: 'none',
          }} />
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 12 }}>
          <Btn variant="ghost" onClick={onCancel}>Cancel</Btn>
          <Btn color={T.warn} onClick={() => onConfirm(reason)}>Mark stale</Btn>
        </div>
      </div>
    </div>
  );
}

function HubSessions({ projects }) {
  const { useState, useEffect, useCallback, useRef } = React;
  const [overview, setOverview] = useState(null);
  const [sel, setSel] = useState('');           // '' = all projects
  const [filter, setFilter] = useState('hide');
  const [q, setQ] = useState('');
  const [qLive, setQLive] = useState('');
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(null);       // expanded row key
  const [details, setDetails] = useState({});
  const [marking, setMarking] = useState(null);
  const reqSeq = useRef(0);

  useEffect(() => {
    const t = setTimeout(() => setQ(qLive.trim()), 250);
    return () => clearTimeout(t);
  }, [qLive]);

  const loadOverview = useCallback(async () => {
    try { setOverview(await api.get('/api/hub/sessions/overview')); } catch { }
  }, []);

  const load = useCallback(async () => {
    const seq = ++reqSeq.current;
    setLoading(true);
    const qs = new URLSearchParams({ stale: filter, limit: '100' });
    if (sel) qs.set('path', sel);
    if (q) qs.set('q', q);
    try {
      const res = await api.get('/api/hub/sessions?' + qs.toString());
      if (seq === reqSeq.current) { setData(res); setErr(''); }
    } catch (e) {
      // Keep the last good list: an empty page would read as "no sessions".
      if (seq === reqSeq.current) setErr(apiErr(e));
    }
    if (seq === reqSeq.current) setLoading(false);
  }, [sel, filter, q]);

  useEffect(() => { loadOverview(); }, [loadOverview]);
  useEffect(() => { load(); }, [load]);
  usePoll(load, 15000);
  usePoll(loadOverview, 30000);

  const keyOf = (row) => row.project.path + '|' + row.id;

  const loadDetail = async (row) => {
    const k = keyOf(row);
    try {
      const d = await api.get('/api/hub/sessions/detail?' + new URLSearchParams(
        { path: row.project.path, id: row.id }).toString());
      setDetails(m => Object.assign({}, m, { [k]: d }));
    } catch (e) {
      setDetails(m => Object.assign({}, m, { [k]: { error: apiErr(e) } }));
    }
  };

  const toggle = (row) => {
    const k = keyOf(row);
    if (open === k) { setOpen(null); return; }
    setOpen(k);
    loadDetail(row);
  };

  const resume = async (row) => {
    try {
      const res = await api.post('/api/hub/sessions/resume', { path: row.project.path, id: row.id });
      notify('Opening a terminal: ' + res.command);
    } catch (e) {
      notify(apiErr(e), 'err');
    }
  };

  const doMark = async (row, op, reason) => {
    try {
      await api.post('/api/hub/sessions/mark', { path: row.project.path, id: row.id, op, reason: reason || '' });
      notify(op === 'stale' ? 'Marked stale' : 'Stale flag cleared');
    } catch (e) {
      notify(apiErr(e), 'err');
    }
    setMarking(null);
    load();
    loadOverview();
    if (open === keyOf(row)) loadDetail(row);
  };

  const jump = (id) => { setFilter('all'); setQLive(id); setOpen(null); };

  const rows = (data && data.sessions) || [];
  const projRows = ((overview && overview.projects) || []).filter(p => p.counts && p.counts.total > 0);
  const totals = projRows.reduce((a, p) => ({
    total: a.total + p.counts.total, live: a.live + p.counts.live, stale: a.stale + p.counts.stale,
  }), { total: 0, live: 0, stale: 0 });

  const railItem = (key, label, counts, title) => (
    <div key={key || 'all'} onClick={() => { setSel(key); setOpen(null); }} title={title}
      style={{
        display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px', borderRadius: 5,
        cursor: 'pointer', background: sel === key ? T.accentDim : 'transparent',
        color: sel === key ? T.accent : T.text, fontSize: 12,
      }}>
      <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{label}</span>
      {counts.live > 0 && <GlowDot color={T.accent} size={6} />}
      <span className="mono" style={{ fontSize: 10, color: T.textMuted }}>{counts.total}</span>
    </div>
  );

  return (
    <div style={{ display: 'flex', gap: 14, minHeight: 0 }}>
      <div style={{ width: 220, flexShrink: 0, display: 'flex', flexDirection: 'column', gap: 2,
        background: T.surface, border: `1px solid ${T.border}`, borderRadius: 6, padding: 6,
        alignSelf: 'flex-start', maxHeight: 'calc(100vh - 110px)', overflowY: 'auto' }}>
        {railItem('', 'All projects', totals, 'Sessions from every registered project')}
        <div style={{ height: 1, background: T.border, margin: '4px 0' }} />
        {!overview && <div style={{ fontSize: 11, color: T.textMuted, padding: 8 }}>loading…</div>}
        {projRows.map(p => railItem(p.path, p.name, p.counts,
          `${p.counts.total} sessions · ${p.counts.live} live · ${p.counts.stale} stale · ` +
          `${p.counts.idle} idle — last ${timeAgo(p.last_active)}`))}
      </div>

      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <I name="messageSquare" size={15} color={T.accent} />
          <span style={{ fontSize: 14, color: T.text }}>Sessions</span>
          <span className="mono" style={{ fontSize: 11, color: T.textMuted }}>
            {data ? `${rows.length}${data.next_before ? '+' : ''} shown` : 'loading…'}
            {loading && data ? ' · refreshing' : ''}
          </span>
          <div style={{ flex: 1 }} />
          <input value={qLive} onChange={e => setQLive(e.target.value)}
            placeholder="Search title, prompt, branch, id…" style={{
              width: 260, background: T.surface, border: `1px solid ${T.border}`,
              borderRadius: 6, padding: '6px 10px', fontSize: 12, color: T.text, outline: 'none',
            }} />
          <div style={{ display: 'inline-flex', border: `1px solid ${T.border}`, borderRadius: 6,
            overflow: 'hidden' }}>
            {SESS_FILTERS.map(([id, label, title]) => (
              <button key={id} title={title} onClick={() => setFilter(id)} style={{
                height: 28, padding: '0 10px', border: 'none', cursor: 'pointer', fontSize: 11,
                background: filter === id ? T.accentDim : 'transparent',
                color: filter === id ? T.accent : T.textMuted, fontWeight: filter === id ? 700 : 400,
              }}>{label}</button>
            ))}
          </div>
          <div onClick={() => { load(); loadOverview(); }} title="Refresh" style={{ cursor: 'pointer', padding: 4 }}>
            <I name="refresh" size={13} color={T.textMuted} />
          </div>
        </div>

        {err && (
          <div style={{ fontSize: 11, color: T.error, background: T.errorDim,
            border: `1px solid ${T.error}`, borderRadius: 6, padding: '8px 10px' }}>
            Could not refresh: {err} — showing the last list, which may be out of date.
          </div>
        )}
        {data && data.errors && data.errors.length > 0 && (
          <div style={{ fontSize: 11, color: T.warn, background: T.warnDim, borderRadius: 6,
            padding: '8px 10px' }}>
            {data.errors.length} project(s) could not be read: {data.errors.map(e => e.path).join(', ')}
          </div>
        )}
        {data && rows.length === 0 && (
          <div style={{ fontSize: 12, color: T.textMuted, padding: '18px 10px', textAlign: 'center',
            border: `1px dashed ${T.border}`, borderRadius: 6 }}>
            {q ? 'No session matches this search.'
              : filter === 'only' ? 'Nothing is marked stale.'
              : filter === 'likely' ? 'No unmarked session looks stale.'
              : 'No Claude Code sessions found for this selection.'}
          </div>
        )}
        {rows.map(row => (
          <SessRow key={keyOf(row)} row={row} showProject={!sel}
            open={open === keyOf(row)} detail={details[keyOf(row)]}
            onToggle={() => toggle(row)} onResume={resume}
            onMark={setMarking} onUnmark={(r) => doMark(r, 'unstale')} onJump={jump} />
        ))}
        {data && data.next_before && (
          <div style={{ fontSize: 11, color: T.textMuted, textAlign: 'center', padding: 6 }}>
            Showing the newest {rows.length}. Search or pick a project to narrow down.
          </div>
        )}
      </div>

      {marking && <SessMarkDialog row={marking} onCancel={() => setMarking(null)}
        onConfirm={(reason) => doMark(marking, 'stale', reason)} />}
    </div>
  );
}
