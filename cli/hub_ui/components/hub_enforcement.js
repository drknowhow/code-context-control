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

// Fallback only — the server's data.modes[].help is authoritative when loaded.
const ENF_MODE_SHORT = {
  strict: 'Blocks native Edit/Write until a c3_* call runs first.',
  advisory: 'Allows native Edit/Write with a nudge. Ledger still logs.',
  off: 'No nudging. Access Guard + vault guard still enforce.',
};

// Client-side copy of enforcement_policy.GOVERNABLE_TOOLS, grouped by what
// selecting them means. The server validates — this list is display-only.
const ENF_GOVERNABLE = {
  write: ['Edit', 'Write', 'MultiEdit'],
  read: ['Read', 'Grep', 'Glob', 'FindFiles', 'SearchText'],
};
const ENF_DEFAULT_BLOCKED = ['Edit', 'Write', 'MultiEdit'];

const ENF_AGG_GRID = '44px minmax(0,1.3fr) 64px minmax(0,1.8fr) 92px';
const ENF_EVENT_GRID = '56px 62px minmax(0,1.1fr) 56px minmax(0,1.5fr) 60px';

function enfModeHelp(modes, id) {
  const m = (modes || []).find(x => x && x.id === id);
  return (m && m.help) || ENF_MODE_SHORT[id] || '';
}

function enfModeIds(modes) {
  return (modes && modes.length) ? modes.map(m => m.id) : ['strict', 'advisory', 'off'];
}

function enfDrifted(row) {
  return !!(row.tier && row.tier_implies && row.tier_implies !== row.mode
    && row.set_by !== 'user');
}

function enfMatches(row, terms) {
  if (!terms.length) return true;
  const hay = `${row.name || ''} ${row.path || ''} ${row.mode || ''} ${row.tier || ''}`.toLowerCase();
  return terms.every(t => hay.indexOf(t) !== -1);
}

function enfChipPass(row, chip) {
  if (chip === 'all') return true;
  if (chip === 'denials') return (row.denial_total || 0) > 0;
  if (chip === 'attention') {
    return !!(row.error || (row.warnings || []).length || enfDrifted(row));
  }
  return row.mode === chip;
}

function enfSort(rows, mode) {
  const byName = (a, b) => String(a.name || a.path).localeCompare(String(b.name || b.path));
  const out = rows.slice();
  if (mode === 'denials') {
    out.sort((a, b) => (b.denial_total || 0) - (a.denial_total || 0) || byName(a, b));
  } else if (mode === 'mode') {
    const rank = { strict: 0, advisory: 1, off: 2 };
    out.sort((a, b) => {
      const ra = rank[a.mode] != null ? rank[a.mode] : 3;
      const rb = rank[b.mode] != null ? rank[b.mode] : 3;
      return ra - rb || byName(a, b);
    });
  } else {
    out.sort(byName);
  }
  return out;
}

function EnforcementBadge({ row, modes }) {
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
    `${enfModeHelp(modes, row.mode)} (${src})`);
}

function ModePicker({ row, modes, busy, onPick }) {
  return (
    <div style={{ display: 'flex', gap: 4 }}>
      {enfModeIds(modes).map(m => {
        const active = row.mode === m;
        const color = ENF_MODE_COLOR(m);
        return (
          <div key={m} title={enfModeHelp(modes, m)}
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
  const meta = d.last_ts
    ? `${timeAgo(d.last_ts)}${d.sessions ? ` · ${d.sessions} sess` : ''}`
    : '';
  return (
    <div style={{
      display: 'grid', gridTemplateColumns: ENF_AGG_GRID,
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
      <span className="mono"
        title={d.last_ts
          ? `last hit ${localDate(d.last_ts)} ${localTime(d.last_ts)} · seen in ${d.sessions} session(s)`
          : ''}
        style={{ fontSize: 10, color: T.textDim, textAlign: 'right', whiteSpace: 'nowrap' }}>
        {meta}
      </span>
    </div>
  );
}

function EnfAggHeader() {
  return (
    <div style={{
      display: 'grid', gridTemplateColumns: ENF_AGG_GRID,
      gap: 8, padding: '4px 10px 3px', fontSize: 10, color: T.textDim,
      borderTop: `1px solid ${T.border}`,
    }}>
      <span>HITS</span><span>RULE</span><span>TOOL</span><span>HOW TO UNBLOCK</span>
      <span style={{ textAlign: 'right' }}>LAST</span>
    </div>
  );
}

// Signal-TTL chip that flips into a small inline editor. Posts a mode-less
// body so the server routes it through set_fields (mode/set_by untouched).
function EnfTtlEditor({ value, disabled, disabledReason, busy, onSave }) {
  const { useState } = React;
  const [editing, setEditing] = useState(false);
  const [val, setVal] = useState('');
  const bad = !/^\d+$/.test(String(val).trim())
    || parseInt(val, 10) < 30 || parseInt(val, 10) > 86400;
  if (!editing) {
    return (
      <span className="mono"
        title={disabled ? disabledReason
          : 'Signal TTL: how long a c3_* call keeps native tools unlocked. Click to edit (30..86400s).'}
        onClick={() => { if (!disabled && !busy) { setVal(String(value)); setEditing(true); } }}
        style={{
          fontSize: 10, color: T.textDim, padding: '2px 7px', borderRadius: 4,
          background: T.surfaceAlt, whiteSpace: 'nowrap',
          cursor: (disabled || busy) ? 'default' : 'pointer',
          opacity: disabled ? 0.55 : 1,
        }}>ttl {value}s</span>
    );
  }
  const commit = () => {
    if (bad) return;
    setEditing(false);
    onSave(parseInt(val, 10));
  };
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
      <input className="mono" value={val} autoFocus
        onChange={e => setVal(e.target.value)}
        onKeyDown={e => {
          if (e.key === 'Enter') commit();
          if (e.key === 'Escape') setEditing(false);
        }}
        title={bad ? 'Whole seconds, 30..86400' : 'Enter to save, Escape to cancel'}
        style={{
          width: 64, background: T.surfaceAlt, borderRadius: 4,
          border: `1px solid ${bad ? T.error : T.border}`,
          color: T.text, fontSize: 10, padding: '2px 6px', outline: 'none',
        }} />
      <span onClick={commit} className="mono" style={{
        fontSize: 10, color: bad ? T.textDim : T.accent,
        cursor: bad ? 'default' : 'pointer', fontWeight: 700,
      }}>save</span>
      <span onClick={() => setEditing(false)} className="mono"
        style={{ fontSize: 10, color: T.textMuted, cursor: 'pointer' }}>✕</span>
    </span>
  );
}

// blocked_tools editor — inline expander (the denial expander is the house
// pattern; there is no popover precedent). The server re-validates the list.
function EnfToolsEditor({ current, strict, busy, onSave, onCancel }) {
  const { useState } = React;
  const [sel, setSel] = useState(() => (current || []).slice());
  const has = t => sel.indexOf(t) !== -1;
  const toggle = t => setSel(has(t) ? sel.filter(x => x !== t) : sel.concat([t]));
  const noWrite = strict && !ENF_GOVERNABLE.write.some(has);
  const pill = t => (
    <span key={t} onClick={() => { if (!busy) toggle(t); }} className="mono" style={{
      fontSize: 10, padding: '3px 9px', borderRadius: 10, cursor: 'pointer',
      border: `1px solid ${has(t) ? T.accent : T.border}`,
      color: has(t) ? T.accent : T.textMuted,
      background: has(t) ? T.accentDim : 'transparent',
    }}>{t}</span>
  );
  return (
    <div style={{
      borderTop: `1px solid ${T.border}`, padding: '8px 10px',
      display: 'flex', flexDirection: 'column', gap: 8,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 10, color: T.textMuted, width: 82, flexShrink: 0 }}>Write class</span>
        {ENF_GOVERNABLE.write.map(pill)}
        <span style={{ fontSize: 10, color: T.textDim }}>
          blocked in strict by default — c3_edit is the intended path
        </span>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 10, color: T.textMuted, width: 82, flexShrink: 0 }}>Read class</span>
        {ENF_GOVERNABLE.read.map(pill)}
        <span style={{ fontSize: 10, color: T.warn }}>
          heavy hammer — strict would hard-block native reads too
        </span>
      </div>
      {noWrite && (
        <div style={{ fontSize: 10, color: T.warn }}>
          No write-class tool selected — strict will hard-block nothing; it
          becomes advisory in effect.
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <span onClick={() => setSel(ENF_DEFAULT_BLOCKED.slice())} className="mono"
          style={{ fontSize: 10, color: T.textMuted, cursor: 'pointer' }}>
          reset to defaults
        </span>
        <div style={{ flex: 1 }} />
        <span onClick={onCancel} className="mono"
          style={{ fontSize: 10, color: T.textMuted, cursor: 'pointer' }}>cancel</span>
        <span onClick={() => { if (!busy) onSave(sel); }} className="mono"
          style={{ fontSize: 10, color: T.accent, cursor: 'pointer', fontWeight: 700 }}>save</span>
      </div>
    </div>
  );
}

// Raw denial-event search over one project's .c3/denials.jsonl. Idle, it
// renders `fallback` (the aggregate summary); with any filter active it
// queries the server (350ms debounce + a sequence guard so a slow response
// can never overwrite a newer one).
function EnfDenialSearch({ path, fallback }) {
  const { useState, useRef, useEffect } = React;
  const [dq, setDq] = useState('');
  const [dlayer, setDlayer] = useState('all');
  const [dsession, setDsession] = useState('');
  const [browse, setBrowse] = useState(false);
  const [res, setRes] = useState(null);
  const [derr, setDerr] = useState('');
  const seqRef = useRef(0);
  const active = browse || dq.trim() !== '' || dlayer !== 'all' || dsession !== '';

  useEffect(() => {
    if (!active) { setRes(null); setDerr(''); return; }
    const seq = ++seqRef.current;
    const t = setTimeout(async () => {
      try {
        const params = ['path=' + encodeURIComponent(path), 'limit=200'];
        if (dq.trim()) params.push('q=' + encodeURIComponent(dq.trim()));
        if (dlayer !== 'all') params.push('layer=' + encodeURIComponent(dlayer));
        if (dsession) params.push('session=' + encodeURIComponent(dsession));
        const d = await api.get('/api/projects/enforcement/denials/search?' + params.join('&'));
        if (seq !== seqRef.current) return;
        setRes(d); setDerr('');
      } catch (e) {
        if (seq !== seqRef.current) return;
        setDerr(apiErr(e));
      }
    }, 350);
    return () => clearTimeout(t);
  }, [active, dq, dlayer, dsession, path]);

  const lchip = (id, label) => (
    <span key={id} onClick={() => setDlayer(id)} className="mono" style={{
      fontSize: 10, padding: '2px 8px', borderRadius: 10, cursor: 'pointer',
      border: `1px solid ${dlayer === id ? T.accent : T.border}`,
      color: dlayer === id ? T.accent : T.textMuted,
      background: dlayer === id ? T.accentDim : 'transparent',
    }}>{label}</span>
  );

  const reset = () => { setDq(''); setDlayer('all'); setDsession(''); setBrowse(false); };
  const events = (res && res.events) || [];

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px', flexWrap: 'wrap' }}>
        <I name="search" size={11} color={active ? T.accent : T.textDim} />
        <input value={dq} onChange={e => setDq(e.target.value)}
          onKeyDown={e => { if (e.key === 'Escape') { reset(); e.currentTarget.blur(); } }}
          placeholder="search events — path, rule, tool"
          style={{
            flex: '1 1 140px', minWidth: 120, background: 'transparent',
            border: 'none', outline: 'none', color: T.text, fontSize: 11,
            padding: '2px 0',
          }} autoComplete="off" spellCheck={false} />
        {lchip('all', 'all')}
        {lchip('discipline', 'discipline')}
        {lchip('access', 'access')}
        {dsession && (
          <span onClick={() => setDsession('')} className="mono"
            title={`${dsession} — click to drop the session filter`} style={{
              fontSize: 10, padding: '2px 8px', borderRadius: 10, cursor: 'pointer',
              border: `1px solid ${T.accent}`, color: T.accent, background: T.accentDim,
            }}>session {dsession.slice(0, 8)} ✕</span>
        )}
        {active ? (
          <span onClick={reset} className="mono"
            style={{ fontSize: 10, color: T.textMuted, cursor: 'pointer' }}>summary</span>
        ) : (
          <span onClick={() => setBrowse(true)} className="mono"
            title="Browse the raw event log, newest first"
            style={{ fontSize: 10, color: T.textMuted, cursor: 'pointer' }}>all events</span>
        )}
      </div>
      {!active && fallback}
      {active && derr && (
        <div style={{ padding: '4px 10px 8px', fontSize: 10, color: T.error }}>{derr}</div>
      )}
      {active && !derr && !res && (
        <div style={{ padding: '6px 10px 10px', fontSize: 10, color: T.textDim }}>searching…</div>
      )}
      {active && res && events.length === 0 && (
        <div style={{ padding: '8px 10px 12px', fontSize: 11, color: T.textDim }}>
          No events match.
        </div>
      )}
      {active && events.length > 0 && (
        <div>
          <div style={{
            display: 'grid', gridTemplateColumns: ENF_EVENT_GRID,
            gap: 8, padding: '4px 10px 3px', fontSize: 10, color: T.textDim,
            borderTop: `1px solid ${T.border}`,
          }}>
            <span>WHEN</span><span>LAYER</span><span>RULE</span>
            <span>TOOL</span><span>PATH</span><span>SESSION</span>
          </div>
          {events.map((ev, i) => (
            <div key={i} title={ev.fix ? `How to unblock: ${ev.fix}` : ''} style={{
              display: 'grid', gridTemplateColumns: ENF_EVENT_GRID,
              gap: 8, alignItems: 'center', padding: '3px 10px', fontSize: 10.5,
              borderTop: `1px solid ${T.border}`,
            }}>
              <span className="mono" title={`${localDate(ev.ts)} ${localTime(ev.ts)}`}
                style={{ color: T.textMuted, whiteSpace: 'nowrap' }}>{timeAgo(ev.ts)}</span>
              <span className="mono" style={{
                color: ev.layer === 'discipline' ? T.warn : T.error, fontSize: 9.5,
              }}>{ev.layer}</span>
              <span className="mono" title={ev.rule} style={{
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                color: T.text,
              }}>{ev.rule}</span>
              <span className="mono" style={{ color: T.textDim }}>{ev.tool}</span>
              <span className="mono" title={ev.path} style={{
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                color: T.textMuted,
              }}>{ev.path}</span>
              <span className="mono"
                onClick={() => { if (ev.session) setDsession(ev.session); }}
                title={ev.session ? `${ev.session} — click to filter to this session` : ''}
                style={{ color: T.blue, cursor: ev.session ? 'pointer' : 'default' }}>
                {(ev.session || '').slice(0, 8)}
              </span>
            </div>
          ))}
          <div style={{
            padding: '4px 10px 8px', fontSize: 10, color: T.textDim,
            borderTop: `1px solid ${T.border}`,
          }}>
            showing {events.length} of {res.matched} event(s)
            {res.truncated ? ' — truncated; narrow the filter for the rest' : ''}
          </div>
        </div>
      )}
    </div>
  );
}

// The machine-wide (~/.c3) fallback, shown as its own card. Projects with
// their own enforcement section are unaffected by changes here.
function EnfGlobalCard({ gp, modes, busy, onPick, onSaveTtl }) {
  if (!gp) return null;
  const configured = !!gp.configured;
  const mode = gp.mode || '';
  const color = configured ? ENF_MODE_COLOR(mode) : T.textDim;
  const synth = { mode, error: null };
  return (
    <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 6 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px' }}>
        <I name="layers" size={13} color={T.textMuted} />
        <span style={{ fontSize: 12, color: T.text }}>Global default</span>
        <span className="mono" style={{ fontSize: 10, color: T.textDim }}>~/.c3</span>
        <span className="mono"
          title={configured
            ? `Set in the global config${gp.set_by ? `, by ${gp.set_by}` : ''} — projects without their own setting resolve to this.`
            : 'No global enforcement section — projects without their own setting use the built-in strict default.'}
          style={{
            fontSize: 10, padding: '2px 7px', borderRadius: 4,
            color, background: `${color}22`, whiteSpace: 'nowrap',
          }}>{configured ? mode.toUpperCase() : 'NOT SET'}</span>
        <EnfTtlEditor value={gp.signal_ttl_s || 600} busy={busy}
          disabled={!configured}
          disabledReason="Pick a global mode first — non-mode fields need a section with a mode to live in."
          onSave={onSaveTtl} />
        <div style={{ flex: 1 }} />
        <ModePicker row={synth} modes={modes} busy={busy} onPick={(r, m) => onPick(m)} />
      </div>
      {(gp.warnings || []).map((w, i) => (
        <div key={i} style={{
          fontSize: 10, color: T.warn, padding: '0 10px 6px 31px', lineHeight: 1.5,
        }}>⚠ {w}</div>
      ))}
      {gp.error && (
        <div style={{ fontSize: 10, color: T.error, padding: '0 10px 6px 31px' }}>
          Could not read the global config: {gp.error}
        </div>
      )}
      <div style={{ fontSize: 10, color: T.textDim, padding: '0 10px 8px 31px' }}>
        The machine-wide fallback. Projects with their own setting are unaffected.
      </div>
    </div>
  );
}

// One project card. Extracted from HubEnforcement so its local state (tools
// editor, ttl editing, event search) survives the 5s poll re-render.
function EnfCard({
  row, modes, busy, open, onToggle, onOpenDrill, onPick, onClearDenials,
  onSaveTtl, onSaveTools, selectMode, selected, onSelectToggle,
}) {
  const { useState } = React;
  const [toolsOpen, setToolsOpen] = useState(false);
  const top = row.top_denials || [];
  const projectScoped = row.scope === 'project';
  const inheritNote = 'This project inherits its policy from the '
    + (row.scope === 'global' ? 'global config' : 'built-in default')
    + ' — pick a mode on this card first to set project-level fields.';
  const blocked = row.blocked_tools || ENF_DEFAULT_BLOCKED;

  const aggregate = (
    <div>
      {top.length > 0 && <EnfAggHeader />}
      {top.map((d, i) => <DenialRow key={i} d={d} />)}
      <div style={{
        padding: '6px 10px', display: 'flex', alignItems: 'center', gap: 10,
        borderTop: `1px solid ${T.border}`,
      }}>
        <span style={{ fontSize: 10, color: T.textDim }}>
          Counters are diagnostics, not an audit trail — the ledger keeps the
          record.{row.denial_total > top.length
            ? ` Top ${top.length} of ${row.denial_total} — use the search above for the rest.` : ''}
        </span>
        <div style={{ flex: 1 }} />
        <span onClick={() => onClearDenials(row)} className="mono"
          style={{ fontSize: 10, color: T.textMuted, cursor: 'pointer' }}>
          reset counters
        </span>
      </div>
    </div>
  );

  return (
    <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 6 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px' }}>
        {selectMode && (
          <span onClick={() => onSelectToggle(row.path)} title="Select for bulk apply" style={{
            width: 13, height: 13, borderRadius: 3, flexShrink: 0, cursor: 'pointer',
            border: `1px solid ${selected ? T.accent : T.border}`,
            background: selected ? T.accent : 'transparent',
          }} />
        )}
        <I name="folder" size={13} color={T.textMuted} />
        <span onClick={onOpenDrill} title="Open the project drill on its Discipline tab"
          style={{
            fontSize: 12, color: T.text,
            cursor: onOpenDrill ? 'pointer' : 'default',
          }}>{row.name || row.path}</span>
        <EnforcementBadge row={row} modes={modes} />
        {row.tier && (
          <span className="mono"
            title={`Permission tier '${row.tier}' implies '${row.tier_implies}'`}
            style={{ fontSize: 10, color: T.textDim }}>
            tier {row.tier}
          </span>
        )}
        <EnfTtlEditor value={row.signal_ttl_s} busy={busy}
          disabled={!projectScoped} disabledReason={inheritNote}
          onSave={v => onSaveTtl(row, v)} />
        <span className="mono"
          title={projectScoped
            ? `Blocked in strict: ${blocked.join(', ') || 'none'} — click to edit`
            : inheritNote}
          onClick={() => { if (projectScoped && !busy) setToolsOpen(!toolsOpen); }}
          style={{
            fontSize: 10, color: toolsOpen ? T.accent : T.textDim,
            padding: '2px 7px', borderRadius: 4, background: T.surfaceAlt,
            whiteSpace: 'nowrap', cursor: projectScoped ? 'pointer' : 'default',
            opacity: projectScoped ? 1 : 0.55,
          }}>tools: {blocked.length}</span>
        <div style={{ flex: 1 }} />
        {row.denial_total > 0 && (
          <span onClick={onToggle} className="mono"
            title="Denials recorded in this project — click for the breakdown"
            style={{
              fontSize: 10, color: T.warn, cursor: 'pointer',
              padding: '2px 7px', borderRadius: 4, background: T.warnDim,
            }}>
            {row.denial_total} denial{row.denial_total === 1 ? '' : 's'} {open ? '▾' : '▸'}
          </span>
        )}
        <ModePicker row={row} modes={modes} busy={busy} onPick={onPick} />
      </div>

      {(row.warnings || []).map((w, i) => (
        <div key={i} style={{
          fontSize: 10, color: T.warn, padding: '0 10px 6px 31px', lineHeight: 1.5,
        }}>⚠ {w}</div>
      ))}

      {toolsOpen && (
        <EnfToolsEditor current={blocked} strict={row.mode === 'strict'} busy={busy}
          onSave={list => { setToolsOpen(false); onSaveTools(row, list); }}
          onCancel={() => setToolsOpen(false)} />
      )}

      {open && (
        <div style={{ borderTop: `1px solid ${T.border}` }}>
          <EnfDenialSearch path={row.path} fallback={aggregate} />
        </div>
      )}
    </div>
  );
}

function EnfBulkBar({ count, modes, busy, onApply, onCancel }) {
  const { useState } = React;
  const [bmode, setBmode] = useState('advisory');
  return (
    <div style={{
      position: 'sticky', bottom: 10, zIndex: 5, display: 'flex',
      alignItems: 'center', gap: 10, padding: '10px 14px',
      background: T.surfaceAlt, border: `1px solid ${T.accent}`,
      borderRadius: 8, boxShadow: '0 4px 14px #00000050',
    }}>
      <span style={{ fontSize: 12, color: T.text }}>{count} selected</span>
      <div style={{ display: 'flex', gap: 4 }}>
        {enfModeIds(modes).map(m => {
          const color = ENF_MODE_COLOR(m);
          const active = bmode === m;
          return (
            <span key={m} onClick={() => setBmode(m)} className="mono"
              title={enfModeHelp(modes, m)} style={{
                fontSize: 10, padding: '3px 9px', borderRadius: 4, cursor: 'pointer',
                color: active ? '#fff' : color,
                background: active ? color : `${color}18`,
                border: `1px solid ${active ? color : 'transparent'}`,
              }}>{m}</span>
          );
        })}
      </div>
      <div style={{ flex: 1 }} />
      <span onClick={onCancel} className="mono"
        style={{ fontSize: 11, color: T.textMuted, cursor: 'pointer' }}>cancel</span>
      <span onClick={() => { if (count > 0 && !busy) onApply(bmode); }} className="mono"
        style={{
          fontSize: 11, fontWeight: 700,
          color: count > 0 ? T.accent : T.textDim,
          cursor: count > 0 ? 'pointer' : 'default',
        }}>{busy ? 'applying…' : `apply ${bmode}`}</span>
    </div>
  );
}

// Per-project Discipline view for the drill panel. Same components, one
// project, always-visible field editors, full 12-row aggregate.
function DrillDiscipline({ project }) {
  const { useState, useCallback } = React;
  const [d, setD] = useState(null);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmOff, setConfirmOff] = useState(false);
  const [editTools, setEditTools] = useState(false);

  const load = useCallback(async () => {
    try {
      setD(await api.get('/api/projects/enforcement?path=' + encodeURIComponent(project.path)));
      setErr('');
    } catch (e) {
      setErr(apiErr(e));
    }
  }, [project.path]);
  React.useEffect(() => { load(); }, [load]);

  const post = async (body) => {
    setBusy(true);
    try {
      await api.post('/api/projects/enforcement',
        Object.assign({ path: project.path, scope: 'project' }, body));
    } catch (e) {
      notify(apiErr(e), 'err');
    }
    setBusy(false);
    setConfirmOff(false);
    load();
  };
  const pick = (row, m) => { if (m === 'off') setConfirmOff(true); else post({ mode: m }); };

  if (err) return <DrillMsg text={err} color={T.error} />;
  if (!d) return <DrillMsg text="Loading…" />;

  const projectScoped = d.scope === 'project';
  const inheritNote = 'This project inherits its policy from the '
    + (d.scope === 'global' ? 'global config' : 'built-in default')
    + ' — pick a mode above first to set project-level fields.';
  const denials = d.denials || {};
  const rows = denials.rows || [];
  const synth = { mode: d.mode, error: null };

  const aggregate = rows.length ? (
    <div>
      <EnfAggHeader />
      {rows.map((r, i) => <DenialRow key={i} d={r} />)}
      <div style={{
        padding: '6px 10px', display: 'flex', alignItems: 'center', gap: 10,
        borderTop: `1px solid ${T.border}`,
      }}>
        <span style={{ fontSize: 10, color: T.textDim }}>
          Counters are diagnostics, not an audit trail.
        </span>
        <div style={{ flex: 1 }} />
        <span className="mono" style={{ fontSize: 10, color: T.textMuted, cursor: 'pointer' }}
          onClick={async () => {
            try {
              await api.del('/api/projects/enforcement/denials?path=' + encodeURIComponent(project.path));
            } catch (e) { notify(apiErr(e), 'err'); }
            load();
          }}>reset counters</span>
      </div>
    </div>
  ) : (
    <div style={{ padding: '10px', fontSize: 11, color: T.textDim }}>
      No denials recorded.
    </div>
  );

  return (
    <div className="fade-up">
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <I name="shield" size={14} color={T.accent} />
        <span style={{ fontSize: 13, fontWeight: 700, color: T.text }}>Tool Discipline</span>
        <EnforcementBadge row={{
          mode: d.mode, scope: d.scope, set_by: d.set_by,
          initialized: true, error: null,
        }} modes={d.modes} />
        <div style={{ flex: 1 }} />
        <ModePicker row={synth} modes={d.modes} busy={busy} onPick={pick} />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '110px 1fr', gap: '10px 12px', marginTop: 16 }}>
        <DrillKV label="Mode" mono>
          {d.mode} ({d.scope}{d.set_by ? `, set by ${d.set_by}` : ''})
        </DrillKV>
        <DrillKV label="Tier" mono>
          {d.tier ? `${d.tier} → implies ${d.tier_implies}` : ''}
        </DrillKV>
        <DrillKV label="Signal TTL">
          <EnfTtlEditor value={d.signal_ttl_s} busy={busy}
            disabled={!projectScoped} disabledReason={inheritNote}
            onSave={v => post({ signal_ttl_s: v })} />
        </DrillKV>
        <DrillKV label="Blocked tools" mono>
          {(d.blocked_tools || []).join(', ') || 'none'}
          {projectScoped ? (
            <span onClick={() => setEditTools(!editTools)} className="mono"
              style={{ fontSize: 10, color: T.accent, cursor: 'pointer', marginLeft: 8 }}>
              {editTools ? 'close' : 'edit'}
            </span>
          ) : (
            <span title={inheritNote} style={{ fontSize: 10, color: T.textDim, marginLeft: 8 }}>
              (inherited)
            </span>
          )}
        </DrillKV>
      </div>

      {(d.warnings || []).map((w, i) => (
        <div key={i} style={{ fontSize: 11, color: T.warn, marginTop: 8, lineHeight: 1.5 }}>
          ⚠ {w}
        </div>
      ))}

      {editTools && (
        <div style={{ border: `1px solid ${T.border}`, borderRadius: 6, marginTop: 10 }}>
          <EnfToolsEditor current={d.blocked_tools} strict={d.mode === 'strict'} busy={busy}
            onSave={list => { setEditTools(false); post({ blocked_tools: list }); }}
            onCancel={() => setEditTools(false)} />
        </div>
      )}

      <DrillSection label={`Denials (${denials.total || 0})`}>
        <div style={{ border: `1px solid ${T.border}`, borderRadius: 6 }}>
          <EnfDenialSearch path={project.path} fallback={aggregate} />
        </div>
      </DrillSection>

      {confirmOff && (
        <ConfirmDialog
          title="Turn tool discipline off?"
          confirmLabel="Turn off"
          message={
            `${project.name || project.path}: C3 will stop nudging the agent `
            + 'toward c3_* tools entirely. Native Edit and Write run without '
            + 'a hint.\n\n'
            + 'STILL ENFORCED: Access Guard path rules, the credential-vault '
            + 'write guard, and agent locks. This switch cannot reach them.\n\n'
            + 'WHAT YOU LOSE: c3_edit takes a pre-edit snapshot that makes a '
            + 'clean revert possible. The edit ledger still records native '
            + 'writes, but without that snapshot.'
          }
          onConfirm={() => post({ mode: 'off' })}
          onCancel={() => setConfirmOff(false)} />
      )}
    </div>
  );
}

function HubEnforcement({ projects, onOpenDrill }) {
  const { useState, useCallback, useRef } = React;
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState('');
  const [confirm, setConfirm] = useState(null);
  const [expanded, setExpanded] = useState({});
  const [q, setQ] = useState('');
  const [chipF, setChipF] = useState('all');
  const [sort, setSort] = useState('name');
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState({});
  const searchRef = useRef(null);
  const confirmRef = useRef(null);
  const selectRef = useRef(false);
  confirmRef.current = confirm;
  selectRef.current = selectMode;

  const load = useCallback(async () => {
    try {
      setData(await api.get('/api/hub/enforcement/overview'));
      setErr('');
    } catch (e) {
      // Keep the last good snapshot — blanking would read as "nothing set".
      setErr(apiErr(e));
    }
  }, []);

  React.useEffect(() => { load(); }, [load]);

  // 5s poll like HubLocks — but never mid-interaction: a refresh while the
  // user types (or has a confirm open, or is picking a bulk set) would
  // reorder cards and clobber focus.
  const pollTick = useCallback(() => {
    const el = document.activeElement;
    const tag = el ? String(el.tagName || '').toLowerCase() : '';
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
    if (confirmRef.current || selectRef.current) return;
    load();
  }, [load]);
  usePoll(pollTick, 5000);

  // '/' focuses the project filter. Ctrl/Cmd-K stays with GlobalSearch.
  React.useEffect(() => {
    const onKey = (e) => {
      if (e.key !== '/') return;
      const el = document.activeElement;
      const tag = el ? String(el.tagName || '').toLowerCase() : '';
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      if (searchRef.current) { e.preventDefault(); searchRef.current.focus(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const postPolicy = async (body, busyKey) => {
    setBusy(busyKey);
    try {
      await api.post('/api/projects/enforcement', body);
    } catch (e) {
      setErr(apiErr(e));
    }
    setBusy('');
    setConfirm(null);
    load();
  };

  const pick = (row, mode) => {
    // Only 'off' gets a confirm: it is the one choice that stops C3 nudging
    // entirely, and users should know what it does and does not switch off.
    if (mode === 'off') { setConfirm({ kind: 'off', row, mode }); return; }
    postPolicy({ path: row.path, mode, scope: 'project' }, row.path);
  };

  const pickGlobal = (mode) => {
    if (mode === 'off') { setConfirm({ kind: 'off-global', mode }); return; }
    postPolicy({ scope: 'global', mode }, '__global__');
  };

  const saveTtl = (row, ttl) =>
    postPolicy({ path: row.path, scope: 'project', signal_ttl_s: ttl }, row.path);
  const saveGlobalTtl = (ttl) =>
    postPolicy({ scope: 'global', signal_ttl_s: ttl }, '__global__');
  const saveTools = (row, list) =>
    postPolicy({ path: row.path, scope: 'project', blocked_tools: list }, row.path);

  const clearDenials = async (row) => {
    setBusy(row.path);
    try {
      await api.del(`/api/projects/enforcement/denials?path=${encodeURIComponent(row.path)}`);
    } catch (e) {
      setErr(apiErr(e));
    }
    setBusy('');
    load();
  };

  const applyBulk = async (mode) => {
    const paths = Object.keys(selected).filter(p => selected[p]);
    setBusy('__bulk__');
    setConfirm(null);
    let ok = 0;
    const fails = [];
    for (const p of paths) {
      try {
        await api.post('/api/projects/enforcement', { path: p, mode, scope: 'project' });
        ok++;
      } catch (e) {
        const all = (data && data.projects) || [];
        const name = (all.find(r => r.path === p) || {}).name || p;
        fails.push(`${name}: ${apiErr(e)}`);
      }
    }
    setBusy('');
    setSelectMode(false);
    setSelected({});
    if (fails.length) {
      notify(`Applied ${mode} to ${ok} project(s) · ${fails.length} failed — ${fails[0]}`, 'err');
    } else {
      notify(`Applied ${mode} to ${ok} project(s)`, 'ok');
    }
    load();
  };

  const rows = (data && data.projects) || [];
  const modes = (data && data.modes) || null;
  const gp = data && data.global_policy;
  const live = rows.filter(r => r.initialized && !r.error);
  const problemsAll = rows.filter(r => r.error || !r.initialized);
  const totals = (data && data.totals) || {};
  const drifted = live.filter(enfDrifted);
  const warned = live.filter(r => (r.warnings || []).length);

  const terms = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const filtering = terms.length > 0 || chipF !== 'all';
  const shown = enfSort(
    live.filter(r => enfMatches(r, terms) && enfChipPass(r, chipF)), sort);
  // Unreadable projects stay visible under chip filters (honesty rule) but do
  // follow a typed name/path query.
  const problems = terms.length
    ? problemsAll.filter(r => enfMatches(r, terms)) : problemsAll;
  const selCount = Object.keys(selected).filter(p => selected[p]).length;

  const chip = (id, label, title) => (
    <button key={id} onClick={() => setChipF(id)} title={title} style={{
      padding: '3px 10px', borderRadius: 20, fontSize: 11, cursor: 'pointer',
      border: `1px solid ${chipF === id ? T.accent : T.border}`,
      background: chipF === id ? T.accentDim : 'transparent',
      color: chipF === id ? T.accent : T.textMuted,
    }}>{label}</button>
  );

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
        <span onClick={() => { setSelectMode(!selectMode); setSelected({}); }}
          className="mono" title="Select several projects and apply one mode to all of them"
          style={{
            fontSize: 11, cursor: 'pointer',
            color: selectMode ? T.accent : T.textMuted,
          }}>{selectMode ? 'cancel select' : 'select'}</span>
        <div onClick={load} title="Refresh" style={{ cursor: 'pointer', padding: 4 }}>
          <I name="refresh" size={13} color={T.textMuted} />
        </div>
      </div>

      {/* Search + filter chips + sort — the creds-tab visual grammar. */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 7, flex: '1 1 260px', minWidth: 200,
          border: `1px solid ${q ? T.accent : T.border}`, borderRadius: 7,
          padding: '0 10px', background: T.surfaceAlt,
        }}>
          <I name="search" size={13} color={q ? T.accent : T.textMuted} />
          <input ref={searchRef} value={q} onChange={e => setQ(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Escape' && !confirm) { setQ(''); e.currentTarget.blur(); }
            }}
            placeholder="Filter projects — name, path, mode, tier"
            style={{
              flex: 1, background: 'transparent', border: 'none', outline: 'none',
              color: T.text, fontSize: 12.5, padding: '7px 0',
            }} autoComplete="off" spellCheck={false} />
          {q ? (
            <button onClick={() => setQ('')} aria-label="Clear search" style={{
              background: 'transparent', border: 'none', cursor: 'pointer',
              padding: 2, display: 'flex',
            }}>
              <I name="xSmall" size={12} color={T.textMuted} />
            </button>
          ) : (
            <span className="mono" style={{
              fontSize: 10, color: T.textDim, border: `1px solid ${T.border}`,
              borderRadius: 4, padding: '1px 5px',
            }}>/</span>
          )}
        </div>
        {chip('all', 'All')}
        {chip('strict', 'Strict')}
        {chip('advisory', 'Advisory')}
        {chip('off', 'Off')}
        {chip('denials', 'Has denials', 'Projects with recorded denials')}
        {chip('attention', 'Attention', 'Warnings, unreadable policies, or tier drift')}
        <select value={sort} onChange={e => setSort(e.target.value)}
          style={drillFieldStyle({ width: 128, fontSize: 11 })}>
          <option value="name">sort: name</option>
          <option value="denials">sort: denials</option>
          <option value="mode">sort: mode</option>
        </select>
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

      {data && (
        <EnfGlobalCard gp={gp} modes={modes} busy={busy === '__global__'}
          onPick={pickGlobal} onSaveTtl={saveGlobalTtl} />
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

      {filtering && live.length > 0 && (
        <div className="mono" style={{ fontSize: 10, color: T.textDim }}>
          showing {shown.length} of {live.length} project(s)
          {shown.length === 0 ? ' — nothing matches; clear the filter above' : ''}
        </div>
      )}

      {shown.map(row => (
        <EnfCard key={row.path} row={row} modes={modes}
          busy={busy === row.path || busy === '__bulk__'}
          open={!!expanded[row.path]}
          onToggle={() => setExpanded({ ...expanded, [row.path]: !expanded[row.path] })}
          onOpenDrill={onOpenDrill ? () => onOpenDrill(
            projects.find(p => p.path === row.path) || { path: row.path, name: row.name },
            'discipline') : null}
          onPick={pick}
          onClearDenials={clearDenials}
          onSaveTtl={saveTtl}
          onSaveTools={saveTools}
          selectMode={selectMode}
          selected={!!selected[row.path]}
          onSelectToggle={p => setSelected({ ...selected, [p]: !selected[p] })} />
      ))}

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
              <EnforcementBadge row={row} modes={modes} />
              {row.error && <span style={{ color: T.textDim }}>{row.error}</span>}
            </div>
          ))}
        </div>
      )}

      {selectMode && (
        <EnfBulkBar count={selCount} modes={modes} busy={busy === '__bulk__'}
          onApply={mode => setConfirm({ kind: 'bulk', mode })}
          onCancel={() => { setSelectMode(false); setSelected({}); }} />
      )}

      {confirm && confirm.kind === 'off' && (
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
          onConfirm={() => postPolicy(
            { path: confirm.row.path, mode: confirm.mode, scope: 'project' },
            confirm.row.path)}
          onCancel={() => setConfirm(null)} />
      )}

      {confirm && confirm.kind === 'off-global' && (
        <ConfirmDialog
          title="Turn the global default off?"
          confirmLabel="Turn off"
          message={
            'Every project WITHOUT its own enforcement section will stop '
            + 'nudging entirely — this is the machine-wide fallback. Projects '
            + 'with their own setting are unaffected.\n\n'
            + 'STILL ENFORCED everywhere: Access Guard path rules, the '
            + 'credential-vault write guard, and agent locks. This switch '
            + 'cannot reach them.'
          }
          onConfirm={() => postPolicy({ scope: 'global', mode: confirm.mode }, '__global__')}
          onCancel={() => setConfirm(null)} />
      )}

      {confirm && confirm.kind === 'bulk' && (
        <ConfirmDialog
          title={`Apply '${confirm.mode}' to ${selCount} project(s)?`}
          confirmLabel="Apply to all"
          danger={confirm.mode === 'off'}
          message={
            `Every selected project gets mode '${confirm.mode}', written as an `
            + 'explicit user choice (a later permission-tier change will not '
            + 'undo it).'
            + (confirm.mode === 'off'
              ? '\n\nOFF stops all tool-discipline nudging in those projects. '
                + 'STILL ENFORCED: Access Guard path rules, the '
                + 'credential-vault write guard, and agent locks.'
              : '')
          }
          onConfirm={() => applyBulk(confirm.mode)}
          onCancel={() => setConfirm(null)} />
      )}
    </div>
  );
}
