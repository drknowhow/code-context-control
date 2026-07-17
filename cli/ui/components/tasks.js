// ─── Tasks tab (per-project PM, v2.45.0) ───────────────────────
// Full-width task manager against /api/pm* (no `path` in bodies).
// List view: status sections + milestones + notes. Shared PM primitives
// (PM_COLUMNS, PM_PRIORITIES, priorityMeta, statusColor, PriorityDot,
// DueBadge, MilestoneChip, TaskLinkIcons) come from ui/pm_shared.js.
// This bundle has no notify/usePoll — errors render inline, polling is
// a local setInterval.

// Quick-add syntax: "p1 fix auth due:2026-08-01 #infra @Phase 4"
function parseQuickAdd(raw, milestones) {
  const out = { title: '', priority: '', due_date: '', tags: [], milestone_id: '' };
  let text = raw;
  const ms = (milestones || []).slice()
    .sort((a, b) => (b.name || '').length - (a.name || '').length)
    .find(m => m.name && text.toLowerCase().indexOf('@' + m.name.toLowerCase()) !== -1);
  if (ms) {
    const i = text.toLowerCase().indexOf('@' + ms.name.toLowerCase());
    text = text.slice(0, i) + text.slice(i + ms.name.length + 1);
    out.milestone_id = ms.id;
  }
  text = text.replace(/(^|\s)(p[0-3])(?=\s|$)/i,
    (m, pre, p) => { out.priority = p.toLowerCase(); return pre; });
  text = text.replace(/(^|\s)due:(\d{4}-\d{2}-\d{2})(?=\s|$)/,
    (m, pre, d) => { out.due_date = d; return pre; });
  text = text.replace(/(^|\s)#([\w-]+)/g,
    (m, pre, tag) => { out.tags.push(tag); return pre; });
  out.title = text.replace(/\s+/g, ' ').trim();
  return out;
}

// Compact one-line description of a PM history event for the Activity list.
function fmtEventDetail(ev, byId) {
  const name = (id) => (((byId || {})[id]) || {}).title || (id || '').slice(0, 8);
  if (ev.op === 'create') return (ev.data || {}).title || name(ev.id);
  const d = ev.data || {};
  if (ev.op === 'block' || ev.op === 'unblock') {
    return `${name(ev.id)} ${ev.op === 'block' ? 'blocked by' : 'no longer blocked by'} ${d.blocker_title || name(d.blocker)}`;
  }
  if (ev.op === 'unblocked') return `${name(ev.id)} released by ${name(d.released_by)}`;
  const parts = Object.entries(ev.patch || {}).slice(0, 3).map(([k, v]) =>
    (Array.isArray(v) && v.length === 2)
      ? `${k}: ${v[0] == null ? '—' : v[0]} → ${v[1] == null ? '—' : v[1]}`
      : k);
  return parts.length ? `${name(ev.id)} — ${parts.join(', ')}` : name(ev.id);
}

function TasksTab() {
  const [data, setData] = useState(null);          // {board, notes}
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');
  const [newTitle, setNewTitle] = useState('');
  const [newPriority, setNewPriority] = useState('p2');
  const [showAllDone, setShowAllDone] = useState(false);
  const [msName, setMsName] = useState('');
  const [msDate, setMsDate] = useState('');
  const [noteText, setNoteText] = useState('');
  const [noteKind, setNoteKind] = useState('note');
  const [report, setReport] = useState(null);
  const [events, setEvents] = useState([]);
  const [depsFor, setDepsFor] = useState('');   // task id with dep editor open
  const [depPick, setDepPick] = useState('');
  const [showActivity, setShowActivity] = useState(false);
  const [histFor, setHistFor] = useState('');   // task id filtering Activity
  const [archived, setArchived] = useState(null);
  const [showArchived, setShowArchived] = useState(false);
  const [timeData, setTimeData] = useState(null);
  const [tMin, setTMin] = useState('');
  const [tNote, setTNote] = useState('');
  const [tDate, setTDate] = useState('');
  const [editEntry, setEditEntry] = useState('');  // time entry id being edited
  const [editMin, setEditMin] = useState('');
  const [editNote, setEditNote] = useState('');

  const load = useCallback(async () => {
    try {
      const d = await api.get('/api/pm');
      setData(d);
      setErr('');
    } catch (e) { setErr(e.message || 'Failed to load tasks'); }
    try {
      const r = await api.get('/api/pm/report');
      setReport(r);
      const q = histFor ? `&id=${encodeURIComponent(histFor)}` : '';
      const ev = await api.get(`/api/pm/events?limit=${histFor ? 50 : 20}${q}`);
      setEvents((ev && ev.events) || []);
    } catch (e) { /* report/history are best-effort extras */ }
    try {
      setTimeData(await api.get('/api/time'));
    } catch (e) { /* time is a best-effort extra */ }
    setLoading(false);
  }, [histFor]);

  useEffect(() => {
    load();
    const iv = setInterval(load, 10000);
    return () => clearInterval(iv);
  }, [load]);

  // Run a mutation, surface failures inline, then refresh. A 409 means a
  // concurrent writer bumped the doc rev — refresh instead of clobbering.
  const run = async (fn) => {
    try { await fn(); await load(); }
    catch (e) {
      if (e && e.status === 409) {
        setErr('Board changed elsewhere — refreshed. Retry your change.');
        await load();
      } else {
        setErr(e.message || 'Request failed');
      }
    }
  };

  const addTask = () => {
    if (!newTitle.trim()) return;
    const q = parseQuickAdd(newTitle, milestones);
    if (!q.title) return;
    run(async () => {
      const body = { title: q.title, priority: q.priority || newPriority };
      if (q.due_date) body.due_date = q.due_date;
      if (q.tags.length) body.tags = q.tags;
      if (q.milestone_id) body.milestone_id = q.milestone_id;
      await api.post('/api/pm/task', body);
      setNewTitle('');
    });
  };
  const setStatus = (t, status) =>
    run(() => api.put('/api/pm/task',
      { id: t.id, move: { status }, expected_rev: board.rev }));
  const delTask = (t) => run(() => api.del('/api/pm/task', { id: t.id }));
  const addMilestone = () => {
    const name = msName.trim();
    if (!name) return;
    run(async () => {
      await api.post('/api/pm/milestone', { name, target_date: msDate.trim() || null });
      setMsName(''); setMsDate('');
    });
  };
  const delMilestone = (m) => run(() => api.del('/api/pm/milestone', { id: m.id }));
  const addNote = () => {
    const text = noteText.trim();
    if (!text) return;
    run(async () => {
      await api.post('/api/pm/note', { text, kind: noteKind });
      setNoteText('');
    });
  };
  const delNote = (n) => run(() => api.del('/api/pm/note', { id: n.id }));
  const addDep = (t, blocker) => {
    if (!blocker) return;
    run(async () => {
      await api.post('/api/pm/deps', { id: t.id, blocker, op: 'add' });
      setDepPick('');
    });
  };
  const removeDep = (t, blocker) =>
    run(() => api.post('/api/pm/deps', { id: t.id, blocker, op: 'remove' }));
  const loadArchived = async () => {
    try {
      const d = await api.get('/api/pm?include_archived=1');
      const cols = ((d || {}).board || {}).columns || {};
      setArchived(Object.values(cols).flat()
        .filter(t => t.lifecycle === 'archived'));
    } catch (e) { setErr(e.message || 'Failed to load archived'); }
  };
  const restoreTask = (t) => run(async () => {
    await api.put('/api/pm/task', { id: t.id, restore: true });
    await loadArchived();
  });
  const addTime = () => {
    const m = parseInt(tMin, 10);
    if (!m) return;
    run(async () => {
      const body = { minutes: m };
      if (tNote.trim()) body.note = tNote.trim();
      if (tDate.trim()) body.date = tDate.trim();
      await api.post('/api/time/entry', body);
      setTMin(''); setTNote(''); setTDate('');
    });
  };
  const saveTimeEdit = (e) => {
    const fields = { note: editNote };
    const m = parseInt(editMin, 10);
    if (m) fields.minutes = m;
    run(async () => {
      await api.put('/api/time/entry', { id: e.id, fields });
      setEditEntry('');
    });
  };
  const delTime = (e) => run(() => api.del('/api/time/entry', { id: e.id }));

  // ── Derived ───────────────────────────────────────────────────
  const board = (data || {}).board || {};
  const columns = board.columns || {};
  const milestones = board.milestones || [];
  const stats = board.stats || {};
  const notes = (((data || {}).notes) || []).slice()
    .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
    .slice(0, 15);
  const msById = {};
  milestones.forEach(m => { msById[m.id] = m; });
  const allTasks = Object.values(columns).flat();
  const taskById = {};
  allTasks.forEach(t => { taskById[t.id] = t; });
  const childrenByParent = {};
  allTasks.forEach(t => {
    if (t.parent_id) {
      (childrenByParent[t.parent_id] = childrenByParent[t.parent_id] || []).push(t);
    }
  });
  // Order a section's rows so subtasks sit indented under their parent when
  // both share the column; children whose parent lives elsewhere stay flat.
  const orderWithSubtasks = (rows) => {
    const inRows = {};
    rows.forEach(t => { inRows[t.id] = true; });
    const out = [];
    rows.forEach(t => {
      if (t.parent_id && inRows[t.parent_id]) return;
      out.push([t, 0]);
      (childrenByParent[t.id] || []).forEach(c => {
        if (inRows[c.id]) out.push([c, 1]);
      });
    });
    return out;
  };
  const doneTasks = (columns.done || []).filter(t => t.completed_at);
  const sparkDays = [];
  for (let i = 13; i >= 0; i--) {
    sparkDays.push(new Date(Date.now() - i * 86400000).toISOString().slice(0, 10));
  }
  const donePerDay = sparkDays.map(d =>
    doneTasks.filter(t => (t.completed_at || '').slice(0, 10) === d).length);
  const cycleSpark = doneTasks.slice()
    .sort((a, b) => (a.completed_at || '').localeCompare(b.completed_at || ''))
    .slice(-10)
    .map(t => Math.max(0, Math.round(
      (new Date(t.completed_at) - new Date(t.created_at || t.completed_at)) / 86400000)));
  const msTimeline = milestones.filter(m => m.target_date);
  const atRiskIds = {};
  (((report || {}).milestones) || []).forEach(m => {
    if (m.at_risk) atRiskIds[m.id] = true;
  });
  const sectionOrder = ['in_progress', 'blocked', 'backlog', 'done'];
  const labelOf = (k) => (PM_COLUMNS.find(c => c[0] === k) || [k, k])[1];

  const inputStyle = {
    background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 6,
    padding: '6px 8px', fontSize: 12, color: T.text, outline: 'none',
  };
  const trashBtn = (title, onClick) => (
    <button onClick={onClick} title={title} style={{
      background: 'transparent', border: 'none', cursor: 'pointer',
      padding: 2, display: 'flex', flexShrink: 0,
    }}>
      <I name="trash" size={12} color={T.textDim} />
    </button>
  );
  const sectionHeader = (dotColor, label, count) => (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <GlowDot color={dotColor} size={6} />
      <span style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 1, color: T.textMuted }}>
        {label}
      </span>
      <span className="mono" style={{ fontSize: 11, color: T.textDim }}>{count}</span>
    </div>
  );

  const depChainBtn = (t) => (
    <button onClick={() => { setDepsFor(depsFor === t.id ? '' : t.id); setDepPick(''); }}
      title="Dependencies" className="mono" style={{
        background: depsFor === t.id ? T.accentDim : 'transparent',
        border: `1px solid ${depsFor === t.id ? `${T.accent}50` : T.border}`,
        borderRadius: 4, cursor: 'pointer', padding: '1px 5px', fontSize: 10,
        color: (t.blocked_by || []).length ? T.warn : T.textDim, flexShrink: 0,
      }}>⛓{(t.blocked_by || []).length || ''}</button>
  );

  const depsEditor = (t) => {
    const deps = t.blocked_by || [];
    const candidates = allTasks.filter(x =>
      x.id !== t.id && x.status !== 'done' && deps.indexOf(x.id) === -1);
    return (
      <div style={{
        display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap',
        padding: '6px 10px 7px 26px', background: T.surfaceAlt,
        border: `1px solid ${T.border}`, borderTop: 'none',
        borderRadius: '0 0 6px 6px',
      }}>
        <span className="mono" style={{ fontSize: 10, color: T.textDim, textTransform: 'uppercase', letterSpacing: 1 }}>
          blocked by
        </span>
        {deps.length === 0 && (
          <span style={{ fontSize: 11, color: T.textDim, fontStyle: 'italic' }}>nothing</span>
        )}
        {deps.map(id => {
          const b = taskById[id];
          const done = b && b.status === 'done';
          return (
            <span key={id} className="mono" style={{
              display: 'inline-flex', alignItems: 'center', gap: 4,
              fontSize: 11, padding: '2px 6px', borderRadius: 999,
              border: `1px solid ${done ? T.border : `${T.warn}50`}`,
              color: done ? T.textDim : T.warn,
              textDecoration: done ? 'line-through' : 'none',
            }}>
              {(b && b.title) || id.slice(0, 8)}
              <button onClick={() => removeDep(t, id)} title="Remove dependency" style={{
                background: 'transparent', border: 'none', cursor: 'pointer',
                color: 'inherit', fontSize: 11, padding: 0, lineHeight: 1,
              }}>×</button>
            </span>
          );
        })}
        <select value={depPick} onChange={e => setDepPick(e.target.value)}
          className="mono" style={{
            background: T.surface, color: T.textMuted, border: `1px solid ${T.border}`,
            borderRadius: 6, fontSize: 11, padding: '2px 6px', cursor: 'pointer', maxWidth: 220,
          }}>
          <option value="">+ add blocker…</option>
          {candidates.map(c => (
            <option key={c.id} value={c.id}>{(c.title || '').slice(0, 60)}</option>
          ))}
        </select>
        {depPick && (
          <Btn variant="ghost" onClick={() => addDep(t, depPick)}
            style={{ padding: '2px 10px', fontSize: 11 }}>Add</Btn>
        )}
      </div>
    );
  };

  const histBtn = (t) => (
    <button onClick={() => {
      setHistFor(histFor === t.id ? '' : t.id);
      setShowActivity(true);
    }} title="Task history" className="mono" style={{
      background: histFor === t.id ? T.accentDim : 'transparent',
      border: `1px solid ${histFor === t.id ? `${T.accent}50` : T.border}`,
      borderRadius: 4, cursor: 'pointer', padding: '1px 5px',
      fontSize: 10, color: T.textDim, flexShrink: 0,
    }}>🕘</button>
  );

  const taskRow = (t, depth = 0) => (
    <div key={t.id} style={{
      display: 'flex', flexDirection: 'column',
      marginLeft: depth ? 18 : 0,
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px',
        background: T.surface, border: `1px solid ${T.border}`,
        borderRadius: depsFor === t.id ? '6px 6px 0 0' : 6,
      }}>
        {depth > 0 && (
          <span className="mono" style={{ fontSize: 11, color: T.textDim, flexShrink: 0 }}>↳</span>
        )}
        <PriorityDot priority={t.priority} />
        <span style={{
          fontSize: 13, flex: 1, minWidth: 0, overflowWrap: 'anywhere', lineHeight: 1.35,
          color: t.status === 'done' ? T.textMuted : T.text,
          textDecoration: t.status === 'done' ? 'line-through' : 'none',
        }}>{t.title}</span>
        {depth === 0 && t.parent_id && taskById[t.parent_id] && (
          <span className="mono" title={taskById[t.parent_id].title} style={{
            fontSize: 10, color: T.textDim, flexShrink: 0,
          }}>↳ {(taskById[t.parent_id].title || '').slice(0, 20)}</span>
        )}
        {(childrenByParent[t.id] || []).length > 0 && (
          <Badge color={T.textMuted}>
            {(childrenByParent[t.id] || []).filter(c => c.status === 'done').length}
            /{(childrenByParent[t.id] || []).length} sub
          </Badge>
        )}
        {t.milestone_id && msById[t.milestone_id] &&
          <MilestoneChip milestone={msById[t.milestone_id]} />}
        <DepsBadge task={t} byId={taskById} />
        {isTaskReady(t, taskById) && (
          <Btn variant="ghost" onClick={() => setStatus(t, 'backlog')}
            style={{ padding: '2px 8px', fontSize: 10 }}>→ backlog</Btn>
        )}
        <DueBadge task={t} />
        {(t.tags || []).map(tag => <Badge key={tag} color={T.blue}>{tag}</Badge>)}
        <TaskLinkIcons task={t} />
        {depChainBtn(t)}
        {histBtn(t)}
        <select value={t.status} onChange={e => setStatus(t, e.target.value)}
          className="mono" style={{
            background: T.surfaceAlt, color: T.textMuted, border: `1px solid ${T.border}`,
            borderRadius: 6, fontSize: 11, padding: '3px 6px', cursor: 'pointer', flexShrink: 0,
          }}>
          {PM_COLUMNS.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
        </select>
        {trashBtn('Archive task', () => delTask(t))}
      </div>
      {depsFor === t.id && depsEditor(t)}
    </div>
  );

  if (loading) {
    return <div style={{ padding: 20, fontSize: 12, color: T.textMuted }}>Loading tasks…</div>;
  }

  return (
    <div style={{ height: '100%', overflowY: 'auto', padding: 20, display: 'flex', flexDirection: 'column', gap: 18 }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <span style={{ fontSize: 15, fontWeight: 700, color: T.text }}>Tasks</span>
        <span className="mono" style={{ fontSize: 11, color: T.textMuted }}>
          {stats.open || 0} open · {stats.overdue || 0} overdue · {stats.done_total || 0} done
        </span>
        <span style={{ flex: 1 }} />
        <Btn variant="ghost" onClick={load} style={{ padding: '6px 12px' }}>
          <I name="refresh" size={12} color={T.textMuted} />
          Refresh
        </Btn>
      </div>
      {err && <div style={{ fontSize: 12, color: T.error }}>{err}</div>}
      <RecoveryBanner recovery={board.recovery} />

      {/* Health strip (only when something needs attention) */}
      {report && ((report.overdue || []).length > 0 || (report.blocked || []).length > 0 ||
        (report.ready || []).length > 0 ||
        (report.milestones || []).some(m => m.at_risk)) && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          {(report.overdue || []).length > 0 &&
            <Badge color={T.error}>{report.overdue.length} overdue</Badge>}
          {(report.blocked || []).length > 0 &&
            <Badge color={T.warn}>{report.blocked.length} blocked</Badge>}
          {(report.ready || []).length > 0 &&
            <Badge color={T.accent}>{report.ready.length} ready to unblock</Badge>}
          {(report.milestones || []).filter(m => m.at_risk).map(m => (
            <Badge key={m.id} color={T.error}>at risk: {m.name}</Badge>
          ))}
          {report.throughput && report.throughput.done_last_7d > 0 && (
            <span className="mono" style={{ fontSize: 11, color: T.textDim }}>
              {report.throughput.done_last_7d} done this week
              {report.throughput.avg_cycle_days != null
                ? ` · ${report.throughput.avg_cycle_days}d avg cycle` : ''}
            </span>
          )}
        </div>
      )}

      {/* Inline add */}
      <div style={{ display: 'flex', gap: 8 }}>
        <input value={newTitle}
          onChange={e => setNewTitle(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') addTask(); }}
          placeholder="New task…  (p1 · due:2026-08-01 · #tag · @milestone)"
          style={{ ...inputStyle, flex: 1, minWidth: 0 }} />
        <select value={newPriority} onChange={e => setNewPriority(e.target.value)}
          className="mono" style={{ ...inputStyle, fontSize: 11, cursor: 'pointer' }}>
          {PM_PRIORITIES.map(p => (
            <option key={p} value={p}>{(priorityMeta(p) || {}).label || p}</option>
          ))}
        </select>
        <Btn onClick={addTask} disabled={!newTitle.trim()} style={{ padding: '6px 16px' }}>Add</Btn>
      </div>

      {/* Status sections */}
      {sectionOrder.map(key => {
        const all = columns[key] || [];
        const rows = (key === 'done' && !showAllDone) ? all.slice(0, 20) : all;
        return (
          <div key={key} style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {sectionHeader(statusColor(key), labelOf(key), all.length)}
            {rows.length === 0 && (
              <div style={{ fontSize: 11, color: T.textDim, fontStyle: 'italic', paddingLeft: 14 }}>none</div>
            )}
            {orderWithSubtasks(rows).map(pair => taskRow(pair[0], pair[1]))}
            {key === 'done' && all.length > 20 && (
              <button onClick={() => setShowAllDone(!showAllDone)} className="mono" style={{
                alignSelf: 'flex-start', background: 'transparent', border: 'none',
                color: T.accent, fontSize: 11, cursor: 'pointer', padding: '2px 0 2px 14px',
              }}>
                {showAllDone ? 'show fewer' : `show all ${all.length}`}
              </button>
            )}
          </div>
        );
      })}

      {/* Insights */}
      {(doneTasks.length > 0 || msTimeline.length > 0) && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {sectionHeader(T.accent, 'Insights', '')}
          <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap', alignItems: 'flex-start', paddingLeft: 14 }}>
            {doneTasks.length > 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                <span className="mono" style={{ fontSize: 10, color: T.textDim, textTransform: 'uppercase', letterSpacing: 1 }}>
                  completions · 14d
                </span>
                <svg width={140} height={26}>
                  {donePerDay.map((n, i) => {
                    const max = Math.max.apply(null, donePerDay) || 1;
                    const h = n ? Math.max(3, Math.round((n / max) * 24)) : 1;
                    return <rect key={i} x={i * 10} y={26 - h} width={7} height={h}
                      fill={n ? T.accent : T.border} rx={1} />;
                  })}
                </svg>
              </div>
            )}
            {cycleSpark.length > 1 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                <span className="mono" style={{ fontSize: 10, color: T.textDim, textTransform: 'uppercase', letterSpacing: 1 }}>
                  cycle days · last {cycleSpark.length}
                </span>
                <svg width={cycleSpark.length * 12} height={26}>
                  {cycleSpark.map((n, i) => {
                    const max = Math.max.apply(null, cycleSpark) || 1;
                    const h = Math.max(2, Math.round((n / max) * 24));
                    return <rect key={i} x={i * 12} y={26 - h} width={8} height={h}
                      fill={T.blue} rx={1} />;
                  })}
                </svg>
              </div>
            )}
            {msTimeline.length > 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 3, flex: 1, minWidth: 240, maxWidth: 460 }}>
                <span className="mono" style={{ fontSize: 10, color: T.textDim, textTransform: 'uppercase', letterSpacing: 1 }}>
                  milestone timeline
                </span>
                <div style={{ position: 'relative', height: 34, borderBottom: `1px solid ${T.border}`, margin: '0 20px' }}>
                  {(() => {
                    const today = new Date().toISOString().slice(0, 10);
                    const stamps = msTimeline.map(m => m.target_date).concat([today]).sort();
                    const lo = new Date(stamps[0]).getTime() - 7 * 86400000;
                    const hi = new Date(stamps[stamps.length - 1]).getTime() + 7 * 86400000;
                    const pct = (d) => ((new Date(d).getTime() - lo) / ((hi - lo) || 1)) * 100;
                    return (
                      <React.Fragment>
                        <div title={`today · ${today}`} style={{
                          position: 'absolute', left: `${pct(today)}%`, top: 0, bottom: 0,
                          width: 1, background: T.textDim,
                        }} />
                        {msTimeline.map(m => (
                          <div key={m.id} title={`${m.name} · ${m.target_date}`} style={{
                            position: 'absolute', left: `${pct(m.target_date)}%`, top: 3,
                            transform: 'translateX(-50%)', display: 'flex',
                            flexDirection: 'column', alignItems: 'center', gap: 2,
                          }}>
                            <span style={{
                              width: 8, height: 8, borderRadius: '50%',
                              background: atRiskIds[m.id] ? T.error : T.purple,
                            }} />
                            <span className="mono" style={{
                              fontSize: 9, whiteSpace: 'nowrap',
                              color: atRiskIds[m.id] ? T.error : T.textDim,
                            }}>{(m.name || '').slice(0, 14)}</span>
                          </div>
                        ))}
                      </React.Fragment>
                    );
                  })()}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Milestones */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {sectionHeader(T.purple, 'Milestones', milestones.length)}
        {milestones.length === 0 && (
          <div style={{ fontSize: 11, color: T.textDim, fontStyle: 'italic', paddingLeft: 14 }}>none</div>
        )}
        {milestones.map(m => {
          const prog = m.progress || {};
          return (
            <div key={m.id} style={{
              display: 'flex', alignItems: 'center', gap: 10, padding: '7px 10px',
              background: T.surface, border: `1px solid ${T.border}`, borderRadius: 6,
            }}>
              <span style={{ fontSize: 13, color: T.text, minWidth: 120, flexShrink: 0, overflowWrap: 'anywhere' }}>
                {m.name}
              </span>
              <ProgressBar value={prog.done || 0} max={prog.total || 1} color={T.purple} height={5} />
              <span className="mono" style={{ fontSize: 11, color: T.textMuted, whiteSpace: 'nowrap', flexShrink: 0 }}>
                {prog.done || 0}/{prog.total || 0} · {prog.pct != null ? prog.pct : 0}%
              </span>
              {m.target_date && (
                <span className="mono" style={{ fontSize: 11, color: T.textDim, whiteSpace: 'nowrap', flexShrink: 0 }}>
                  {m.target_date}
                </span>
              )}
              {trashBtn('Archive milestone', () => delMilestone(m))}
            </div>
          );
        })}
        <div style={{ display: 'flex', gap: 8 }}>
          <input value={msName}
            onChange={e => setMsName(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') addMilestone(); }}
            placeholder="New milestone…"
            style={{ ...inputStyle, flex: 1, minWidth: 0 }} />
          <input value={msDate}
            onChange={e => setMsDate(e.target.value)}
            placeholder="YYYY-MM-DD"
            className="mono"
            style={{ ...inputStyle, width: 110, fontSize: 11 }} />
          <Btn variant="ghost" onClick={addMilestone} disabled={!msName.trim()}
            style={{ padding: '6px 12px' }}>Add</Btn>
        </div>
      </div>

      {/* Time */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {sectionHeader(T.warn, 'Time',
          timeData && timeData.summary
            ? `${fmtMinutes(timeData.summary.last_7d.total_min)} · 7d` : '')}
        {timeData && timeData.summary && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, paddingLeft: 14, flexWrap: 'wrap' }}>
            <span className="mono" style={{ fontSize: 11, color: T.textMuted }}>
              today {fmtMinutes(timeData.summary.today.total_min)}
              {' · '}7d {fmtMinutes(timeData.summary.last_7d.total_min)}
              {' · '}30d {fmtMinutes(timeData.summary.last_30d.total_min)}
            </span>
            <span className="mono" title="auto = tracked from MCP/IDE activity"
              style={{ fontSize: 10, color: T.textDim }}>
              7d: auto {fmtMinutes(timeData.summary.last_7d.auto_min)}
              {' + '}manual {fmtMinutes(timeData.summary.last_7d.manual_min)}
            </span>
            {timeData.summary.by_day.some(d => d.auto_min + d.manual_min > 0) && (
              <svg width={14 * 12} height={26}>
                {timeData.summary.by_day.map((d, i) => {
                  const total = d.auto_min + d.manual_min;
                  const max = Math.max.apply(null,
                    timeData.summary.by_day.map(x => x.auto_min + x.manual_min)) || 1;
                  const h = total ? Math.max(3, Math.round((total / max) * 24)) : 1;
                  return (
                    <rect key={i} x={i * 12} y={26 - h} width={9} height={h}
                      fill={total ? T.warn : T.border} rx={1}>
                      <title>{`${d.date}: ${fmtMinutes(total)}`}</title>
                    </rect>
                  );
                })}
              </svg>
            )}
          </div>
        )}
        {((timeData && timeData.entries) || []).slice(0, 10).map(e => (
          editEntry === e.id ? (
            <div key={e.id} style={{ display: 'flex', gap: 6, alignItems: 'center', padding: '5px 10px' }}>
              <input value={editMin} onChange={ev => setEditMin(ev.target.value)}
                className="mono" placeholder="min"
                style={{ ...inputStyle, width: 70, fontSize: 11 }} />
              <input value={editNote} onChange={ev => setEditNote(ev.target.value)}
                placeholder="note"
                style={{ ...inputStyle, flex: 1, minWidth: 0, fontSize: 11 }} />
              <Btn onClick={() => saveTimeEdit(e)}
                style={{ padding: '3px 10px', fontSize: 11 }}>Save</Btn>
              <Btn variant="ghost" onClick={() => setEditEntry('')}
                style={{ padding: '3px 10px', fontSize: 11 }}>Cancel</Btn>
            </div>
          ) : (
            <div key={e.id} style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '5px 10px',
              background: T.surface, border: `1px solid ${T.border}`, borderRadius: 6,
            }}>
              <span className="mono" style={{ fontSize: 11, color: T.textDim, flexShrink: 0 }}>{e.date}</span>
              <span className="mono" style={{ fontSize: 11, color: T.warn, flexShrink: 0 }}>{fmtMinutes(e.minutes)}</span>
              <span style={{ fontSize: 12, color: T.text, flex: 1, minWidth: 0, overflowWrap: 'anywhere' }}>
                {e.note || <span style={{ color: T.textDim, fontStyle: 'italic' }}>manual entry</span>}
              </span>
              {e.task_id && taskById[e.task_id] && (
                <span className="mono" title={taskById[e.task_id].title}
                  style={{ fontSize: 10, color: T.textDim, flexShrink: 0 }}>
                  ↳ {(taskById[e.task_id].title || '').slice(0, 18)}
                </span>
              )}
              <button onClick={() => {
                setEditEntry(e.id);
                setEditMin(String(e.minutes));
                setEditNote(e.note || '');
              }} title="Edit entry" className="mono" style={{
                background: 'transparent', border: `1px solid ${T.border}`,
                borderRadius: 4, cursor: 'pointer', padding: '1px 6px',
                fontSize: 10, color: T.textDim,
              }}>edit</button>
              {trashBtn('Delete entry', () => delTime(e))}
            </div>
          )
        ))}
        <div style={{ display: 'flex', gap: 8 }}>
          <input value={tMin} onChange={e => setTMin(e.target.value)}
            placeholder="min" className="mono"
            style={{ ...inputStyle, width: 70, fontSize: 11 }} />
          <input value={tNote} onChange={e => setTNote(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') addTime(); }}
            placeholder="What was the time spent on?"
            style={{ ...inputStyle, flex: 1, minWidth: 0 }} />
          <input value={tDate} onChange={e => setTDate(e.target.value)}
            placeholder={new Date().toISOString().slice(0, 10)} className="mono"
            style={{ ...inputStyle, width: 110, fontSize: 11 }} />
          <Btn variant="ghost" onClick={addTime} disabled={!parseInt(tMin, 10)}
            style={{ padding: '6px 12px' }}>Log time</Btn>
        </div>
      </div>

      {/* Notes */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {sectionHeader(T.blue, 'Notes', notes.length)}
        {notes.length === 0 && (
          <div style={{ fontSize: 11, color: T.textDim, fontStyle: 'italic', paddingLeft: 14 }}>none</div>
        )}
        {notes.map(n => (
          <div key={n.id} style={{
            display: 'flex', alignItems: 'flex-start', gap: 8, padding: '7px 10px',
            background: T.surface, border: `1px solid ${T.border}`, borderRadius: 6,
          }}>
            <Badge color={n.kind === 'decision' ? T.purple : T.blue}>{n.kind || 'note'}</Badge>
            <span style={{
              fontSize: 12, color: T.text, flex: 1, minWidth: 0,
              whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', lineHeight: 1.45,
            }}>{n.text}</span>
            <span className="mono" style={{ fontSize: 11, color: T.textDim, whiteSpace: 'nowrap' }}>
              {(n.created_at || '').slice(0, 10)}
            </span>
            {trashBtn('Archive note', () => delNote(n))}
          </div>
        ))}
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
          <textarea value={noteText} rows={2}
            onChange={e => setNoteText(e.target.value)}
            placeholder="Add a note or decision…"
            style={{
              ...inputStyle, flex: 1, minWidth: 0, resize: 'vertical',
              fontFamily: 'inherit', lineHeight: 1.45,
            }} />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, alignItems: 'stretch' }}>
            <div style={{ display: 'flex', gap: 6 }}>
              {['note', 'decision'].map(k => (
                <button key={k} onClick={() => setNoteKind(k)} className="mono" style={{
                  padding: '3px 10px', borderRadius: 999, fontSize: 11, fontWeight: 600,
                  cursor: 'pointer',
                  border: `1px solid ${noteKind === k ? `${T.accent}50` : T.border}`,
                  background: noteKind === k ? T.accentDim : 'transparent',
                  color: noteKind === k ? T.accent : T.textMuted,
                }}>{k}</button>
              ))}
            </div>
            <Btn variant="ghost" onClick={addNote} disabled={!noteText.trim()}
              style={{ padding: '6px 12px', justifyContent: 'center' }}>Add note</Btn>
          </div>
        </div>
      </div>

      {/* Archived (browse + restore) */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <div onClick={() => {
          const next = !showArchived;
          setShowArchived(next);
          if (next && archived === null) loadArchived();
        }} style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
          {sectionHeader(T.textDim, 'Archived', archived === null ? '·' : archived.length)}
          <span className="mono" style={{ fontSize: 10, color: T.textDim }}>
            {showArchived ? 'hide' : 'show'}
          </span>
        </div>
        {showArchived && archived !== null && archived.length === 0 && (
          <div style={{ fontSize: 11, color: T.textDim, fontStyle: 'italic', paddingLeft: 14 }}>none</div>
        )}
        {showArchived && (archived || []).map(t => (
          <div key={t.id} style={{
            display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px',
            background: T.surface, border: `1px dashed ${T.border}`, borderRadius: 6,
          }}>
            <PriorityDot priority={t.priority} />
            <span style={{ fontSize: 12, color: T.textMuted, flex: 1, minWidth: 0, overflowWrap: 'anywhere' }}>
              {t.title}
            </span>
            <span className="mono" style={{ fontSize: 10, color: T.textDim }}>{t.status}</span>
            <Btn variant="ghost" onClick={() => restoreTask(t)}
              style={{ padding: '2px 10px', fontSize: 11 }}>Restore</Btn>
          </div>
        ))}
      </div>

      {/* Activity (recent PM events, collapsed by default) */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        <div onClick={() => setShowActivity(!showActivity)}
          style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
          {sectionHeader(T.textDim, 'Activity', events.length)}
          {histFor && (
            <span className="mono"
              onClick={(e) => { e.stopPropagation(); setHistFor(''); }}
              title="Clear task filter"
              style={{ fontSize: 10, color: T.accent, cursor: 'pointer' }}>
              {((taskById[histFor] || {}).title || histFor.slice(0, 8))} ×
            </span>
          )}
          <span className="mono" style={{ fontSize: 10, color: T.textDim }}>
            {showActivity ? 'hide' : 'show'}
          </span>
        </div>
        {showActivity && events.length === 0 && (
          <div style={{ fontSize: 11, color: T.textDim, fontStyle: 'italic', paddingLeft: 14 }}>
            no recorded events yet
          </div>
        )}
        {showActivity && events.map((ev, i) => (
          <div key={i} className="mono" style={{
            display: 'flex', gap: 8, fontSize: 11, color: T.textMuted,
            padding: '2px 10px', alignItems: 'baseline',
          }}>
            <span style={{ color: T.textDim, flexShrink: 0 }}>
              {(ev.ts || '').slice(5, 16).replace('T', ' ')}
            </span>
            <span style={{ flexShrink: 0 }}>{ev.entity}.{ev.op}</span>
            <span style={{ minWidth: 0, overflowWrap: 'anywhere', flex: 1 }}>
              {fmtEventDetail(ev, taskById)}
            </span>
            {ev.actor && (
              <span style={{ color: T.textDim, flexShrink: 0 }}>by {ev.actor}</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
