// ─── Drill panel: Tasks tab (project management) ───────────────
// Rendered by DrillPanel's 'tasks' tab. Data comes from
// GET /api/projects/pm?path=… (+&include_children=1 for parents);
// a 409 renders the shared DrillNeedsInit CTA. All mutations hit
// the hub PM endpoints with {path}; children rollup rows pass the
// child's own path instead. Presentational primitives (PriorityDot,
// DueBadge, MilestoneChip, TaskLinkIcons, PM_COLUMNS, PM_PRIORITIES,
// statusColor) live in ui/pm_shared.js.

function DrillTaskRow({ task, path, milestones, onMutated }) {
  const ms = task.milestone_id && milestones ? milestones[task.milestone_id] : null;

  const move = async (status) => {
    try {
      await api.put('/api/projects/pm/task', { path, id: task.id, move: { status } });
      notify('Task moved to ' + String(status).replace('_', ' '), 'ok');
      onMutated();
    } catch (e) { notify('Move failed: ' + e.message, 'err'); }
  };
  const archive = async () => {
    try {
      await api.del('/api/projects/pm/task', { path, id: task.id });
      notify('Task archived', 'ok');
      onMutated();
    } catch (e) { notify('Archive failed: ' + e.message, 'err'); }
  };

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 8, padding: '7px 0',
      borderBottom: `1px solid ${T.border}`, minWidth: 0,
    }}>
      <PriorityDot priority={task.priority} />
      <span title={task.description || task.title} style={{
        fontSize: 13, color: T.text, flex: 1, minWidth: 0,
        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
        textDecoration: task.archived ? 'line-through' : 'none',
      }}>{task.title}</span>
      <MilestoneChip milestone={ms} />
      <DueBadge task={task} />
      {(task.tags || []).map(t => <Badge key={t} color={T.blue}>{t}</Badge>)}
      <TaskLinkIcons task={task} />
      <select value={task.status} onChange={e => move(e.target.value)}
        style={drillFieldStyle({ padding: '3px 6px', fontSize: 11, cursor: 'pointer', flexShrink: 0 })}>
        {PM_COLUMNS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
      </select>
      <button onClick={archive} title="Archive task" style={{
        background: 'none', border: 'none', cursor: 'pointer', padding: 4,
        display: 'flex', alignItems: 'center', flexShrink: 0,
      }}>
        <I name="trash" size={12} color={T.textMuted} />
      </button>
    </div>
  );
}

function DrillTasks({ project, onChanged }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [needsInit, setNeedsInit] = useState(false);
  const [showAllDone, setShowAllDone] = useState(false);
  const [title, setTitle] = useState('');
  const [priority, setPriority] = useState('p2');
  const [msName, setMsName] = useState('');
  const [msTarget, setMsTarget] = useState('');
  const [noteText, setNoteText] = useState('');
  const [noteKind, setNoteKind] = useState('note');

  const load = async () => {
    setErr(null);
    try {
      const d = await api.get('/api/projects/pm?path=' + encodeURIComponent(project.path)
        + (project.is_parent ? '&include_children=1' : ''));
      setData(d);
      setNeedsInit(false);
    } catch (e) {
      if (e.status === 409) setNeedsInit(true);
      else setErr(e.message);
    }
  };
  useEffect(() => {
    setData(null); setNeedsInit(false); setShowAllDone(false);
    load();
  }, [project.path, project.is_parent]);

  const mutated = () => { load(); if (onChanged) onChanged(); };

  const addTask = async () => {
    const t = title.trim();
    if (!t) return;
    try {
      await api.post('/api/projects/pm/task', { path: project.path, title: t, priority });
      setTitle('');
      notify('Task added', 'ok');
      mutated();
    } catch (e) { notify('Add task failed: ' + e.message, 'err'); }
  };

  const addMilestone = async () => {
    const n = msName.trim();
    if (!n) return;
    try {
      const body = { path: project.path, name: n };
      if (msTarget.trim()) body.target_date = msTarget.trim();
      await api.post('/api/projects/pm/milestone', body);
      setMsName(''); setMsTarget('');
      notify('Milestone added', 'ok');
      mutated();
    } catch (e) { notify('Add milestone failed: ' + e.message, 'err'); }
  };

  const archiveMilestone = async (id) => {
    try {
      const d = await api.del('/api/projects/pm/milestone', { path: project.path, id });
      const detached = (d && d.milestone && d.milestone.detached_tasks) || 0;
      notify(`Milestone archived · detached ${detached} tasks`, 'ok');
      mutated();
    } catch (e) { notify('Archive milestone failed: ' + e.message, 'err'); }
  };

  const addNote = async () => {
    const t = noteText.trim();
    if (!t) return;
    try {
      await api.post('/api/projects/pm/note', { path: project.path, text: t, kind: noteKind });
      setNoteText('');
      notify('Note added', 'ok');
      mutated();
    } catch (e) { notify('Add note failed: ' + e.message, 'err'); }
  };

  const archiveNote = async (id) => {
    try {
      await api.del('/api/projects/pm/note', { path: project.path, id });
      notify('Note archived', 'ok');
      mutated();
    } catch (e) { notify('Archive note failed: ' + e.message, 'err'); }
  };

  if (needsInit) {
    return <DrillNeedsInit project={project} onReady={() => { load(); if (onChanged) onChanged(); }} />;
  }
  if (err) return <DrillMsg text={'Failed to load tasks: ' + err} color={T.error} />;
  if (!data) return <DrillMsg text="Loading tasks…" />;

  const board = data.board || {};
  const columns = board.columns || {};
  const stats = board.stats || {};
  const milestones = board.milestones || [];
  const msById = {};
  milestones.forEach(m => { msById[m.id] = m; });
  const notes = (data.notes || []).slice()
    .sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
    .slice(0, 10);
  const children = data.children || [];
  const sectionOrder = ['in_progress', 'blocked', 'backlog', 'done'];
  const colLabels = {};
  PM_COLUMNS.forEach(([id, label]) => { colLabels[id] = label; });

  return (
    <div className="fade-up">
      {/* Inline add row */}
      <div style={{ display: 'flex', gap: 8 }}>
        <input value={title} onChange={e => setTitle(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') addTask(); }}
          placeholder="New task title…" style={drillFieldStyle({ flex: 1 })} />
        <select value={priority} onChange={e => setPriority(e.target.value)}
          style={drillFieldStyle({ cursor: 'pointer' })}>
          {PM_PRIORITIES.map(p => <option key={p} value={p}>{p.toUpperCase()}</option>)}
        </select>
        <Btn onClick={addTask} disabled={!title.trim()}>Add</Btn>
      </div>

      {/* Stats line */}
      <div className="mono" style={{ fontSize: 11, color: T.textMuted, marginTop: 10 }}>
        {stats.open != null ? stats.open : 0} open · {stats.overdue != null ? stats.overdue : 0} overdue · {stats.done_total != null ? stats.done_total : 0} done
      </div>

      {/* Status sections */}
      {sectionOrder.map(st => {
        const all = columns[st] || [];
        const total = all.length;
        const capped = st === 'done' && !showAllDone && total > 20;
        const rows = capped ? all.slice(0, 20) : all;
        return (
          <div key={st} style={{ marginTop: 26 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
              <GlowDot color={statusColor(st)} />
              <span style={{
                fontSize: 11, fontWeight: 700, textTransform: 'uppercase',
                letterSpacing: 1, color: statusColor(st),
              }}>{colLabels[st]}</span>
              <span className="mono" style={{ fontSize: 11, color: T.textDim }}>{total}</span>
            </div>
            {total === 0
              ? <div style={{ fontSize: 12, color: T.textDim, padding: '2px 0 4px' }}>&mdash;</div>
              : rows.map(t => (
                  <DrillTaskRow key={t.id} task={t} path={project.path}
                    milestones={msById} onMutated={mutated} />
                ))}
            {st === 'done' && total > 20 && (
              <button onClick={() => setShowAllDone(!showAllDone)} className="mono" style={{
                background: 'none', border: 'none', cursor: 'pointer',
                padding: '6px 0', fontSize: 11, color: T.accent,
              }}>{showAllDone ? 'show fewer' : `show all ${total}`}</button>
            )}
          </div>
        );
      })}

      {/* Milestones */}
      <DrillSection label="Milestones">
        {milestones.length === 0 &&
          <div style={{ fontSize: 12, color: T.textDim, marginBottom: 6 }}>No milestones.</div>}
        {milestones.map(m => {
          const prog = m.progress || {};
          return (
            <div key={m.id} style={{
              display: 'flex', alignItems: 'center', gap: 10, padding: '8px 0',
              borderBottom: `1px solid ${T.border}`, minWidth: 0,
            }}>
              <span style={{
                fontSize: 13, fontWeight: 600, color: T.text, flex: 1, minWidth: 0,
                whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
              }}>{m.name}</span>
              <div style={{ width: 120, display: 'flex', flexShrink: 0 }}>
                <ProgressBar value={prog.done || 0} max={prog.total || 1} color={T.accent} />
              </div>
              <span className="mono" style={{ fontSize: 11, color: T.textMuted, flexShrink: 0 }}>
                {prog.pct != null ? prog.pct : 0}% · target {m.target_date || '—'}
              </span>
              <button onClick={() => archiveMilestone(m.id)} title="Archive milestone" style={{
                background: 'none', border: 'none', cursor: 'pointer', padding: 4,
                display: 'flex', alignItems: 'center', flexShrink: 0,
              }}>
                <I name="trash" size={12} color={T.textMuted} />
              </button>
            </div>
          );
        })}
        <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
          <input value={msName} onChange={e => setMsName(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') addMilestone(); }}
            placeholder="New milestone…" style={drillFieldStyle({ flex: 1 })} />
          <input value={msTarget} onChange={e => setMsTarget(e.target.value)}
            placeholder="YYYY-MM-DD" className="mono" style={drillFieldStyle({ width: 100, fontSize: 11 })} />
          <Btn variant="ghost" onClick={addMilestone} disabled={!msName.trim()}>Add</Btn>
        </div>
      </DrillSection>

      {/* Notes */}
      <DrillSection label="Notes">
        {notes.length === 0 &&
          <div style={{ fontSize: 12, color: T.textDim, marginBottom: 6 }}>No notes yet.</div>}
        {notes.map(n => (
          <div key={n.id} style={{
            display: 'flex', alignItems: 'flex-start', gap: 8, padding: '8px 0',
            borderBottom: `1px solid ${T.border}`, minWidth: 0,
          }}>
            <Badge color={n.kind === 'decision' ? T.purple : T.textMuted}>{n.kind || 'note'}</Badge>
            <span style={{
              fontSize: 12, color: T.text, flex: 1, minWidth: 0,
              lineHeight: 1.5, overflowWrap: 'anywhere',
            }}>{n.text}</span>
            <span className="mono" style={{ fontSize: 11, color: T.textDim, flexShrink: 0, paddingTop: 2 }}>
              {String(n.created_at || '').slice(0, 10)}
            </span>
            <button onClick={() => archiveNote(n.id)} title="Archive note" style={{
              background: 'none', border: 'none', cursor: 'pointer', padding: 4,
              display: 'flex', alignItems: 'center', flexShrink: 0,
            }}>
              <I name="trash" size={12} color={T.textMuted} />
            </button>
          </div>
        ))}
        <div style={{ marginTop: 10 }}>
          <textarea rows={2} value={noteText} onChange={e => setNoteText(e.target.value)}
            placeholder="Add a note or decision…"
            style={drillFieldStyle({
              width: '100%', boxSizing: 'border-box', resize: 'vertical', fontFamily: 'inherit',
            })} />
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
            {['note', 'decision'].map(k => {
              const kColor = k === 'decision' ? T.purple : T.accent;
              const on = noteKind === k;
              return (
                <button key={k} onClick={() => setNoteKind(k)} className="mono" style={{
                  padding: '3px 10px', borderRadius: 999, fontSize: 11, fontWeight: 700,
                  cursor: 'pointer', border: `1px solid ${on ? kColor : T.border}`,
                  background: on ? kColor + '22' : 'transparent',
                  color: on ? kColor : T.textMuted,
                }}>{k}</button>
              );
            })}
            <div style={{ flex: 1 }} />
            <Btn variant="ghost" onClick={addNote} disabled={!noteText.trim()}>Add note</Btn>
          </div>
        </div>
      </DrillSection>

      {/* Children rollup */}
      {children.length > 0 && (
        <DrillSection label="Sub-project tasks">
          {children.map(c => {
            const open = (c.tasks || []).filter(t => t.status !== 'done');
            return (
              <div key={c.path} style={{ marginBottom: 18 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                  <Badge color={T.purple}>{`[sub:${c.name}]`}</Badge>
                  <span className="mono" style={{ fontSize: 11, color: T.textDim }}>{open.length} open</span>
                </div>
                {open.length === 0
                  ? <div style={{ fontSize: 12, color: T.textDim }}>No open tasks.</div>
                  : open.map(t => (
                      <DrillTaskRow key={t.id} task={t} path={c.path}
                        milestones={msById} onMutated={mutated} />
                    ))}
              </div>
            );
          })}
        </DrillSection>
      )}
    </div>
  );
}
