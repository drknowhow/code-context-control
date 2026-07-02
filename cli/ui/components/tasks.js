// ─── Tasks tab (per-project PM, v2.45.0) ───────────────────────
// Full-width task manager against /api/pm* (no `path` in bodies).
// List view: status sections + milestones + notes. Shared PM primitives
// (PM_COLUMNS, PM_PRIORITIES, priorityMeta, statusColor, PriorityDot,
// DueBadge, MilestoneChip, TaskLinkIcons) come from ui/pm_shared.js.
// This bundle has no notify/usePoll — errors render inline, polling is
// a local setInterval.

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

  const load = useCallback(async () => {
    try {
      const d = await api.get('/api/pm');
      setData(d);
      setErr('');
    } catch (e) { setErr(e.message || 'Failed to load tasks'); }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
    const iv = setInterval(load, 10000);
    return () => clearInterval(iv);
  }, [load]);

  // Run a mutation, surface failures inline, then refresh.
  const run = async (fn) => {
    try { await fn(); await load(); }
    catch (e) { setErr(e.message || 'Request failed'); }
  };

  const addTask = () => {
    const title = newTitle.trim();
    if (!title) return;
    run(async () => {
      await api.post('/api/pm/task', { title, priority: newPriority });
      setNewTitle('');
    });
  };
  const setStatus = (t, status) =>
    run(() => api.put('/api/pm/task', { id: t.id, move: { status } }));
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

  const taskRow = (t) => (
    <div key={t.id} style={{
      display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px',
      background: T.surface, border: `1px solid ${T.border}`, borderRadius: 6,
    }}>
      <PriorityDot priority={t.priority} />
      <span style={{
        fontSize: 13, flex: 1, minWidth: 0, overflowWrap: 'anywhere', lineHeight: 1.35,
        color: t.status === 'done' ? T.textMuted : T.text,
        textDecoration: t.status === 'done' ? 'line-through' : 'none',
      }}>{t.title}</span>
      {t.milestone_id && msById[t.milestone_id] &&
        <MilestoneChip milestone={msById[t.milestone_id]} />}
      <DueBadge task={t} />
      {(t.tags || []).map(tag => <Badge key={tag} color={T.blue}>{tag}</Badge>)}
      <TaskLinkIcons task={t} />
      <select value={t.status} onChange={e => setStatus(t, e.target.value)}
        className="mono" style={{
          background: T.surfaceAlt, color: T.textMuted, border: `1px solid ${T.border}`,
          borderRadius: 6, fontSize: 11, padding: '3px 6px', cursor: 'pointer', flexShrink: 0,
        }}>
        {PM_COLUMNS.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
      </select>
      {trashBtn('Archive task', () => delTask(t))}
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

      {/* Inline add */}
      <div style={{ display: 'flex', gap: 8 }}>
        <input value={newTitle}
          onChange={e => setNewTitle(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') addTask(); }}
          placeholder="New task title…"
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
            {rows.map(taskRow)}
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
    </div>
  );
}
