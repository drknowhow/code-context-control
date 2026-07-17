// ─── Hub Task Board (v2.45.0) ──────────────────────────────────
// Cross-project kanban mounted by app.js when the topbar switcher is
// on "Tasks". 'global' scope aggregates open tasks from every
// registered project (/api/pm/global); a project-path scope shows that
// project's full board (/api/projects/pm) with inline add + rank moves.
// Shared PM primitives (PM_COLUMNS, PriorityDot, DueBadge, statusColor,
// ...) come from ui/pm_shared.js, loaded earlier in the bundle.

let _pmDragTask = null;  // active drag payload (module-level; no useRef in this bundle)

function TaskBoardCard({ task, mode, colTasks, index, onMoveStatus, onMoveRank, onOpenProject, onMoveTo, onDropOnCard, byId }) {
  const keys = PM_COLUMNS.map(c => c[0]);
  const ci = keys.indexOf(task.status);
  const navBtn = (label, title, onClick) => (
    <button onClick={onClick} title={title} className="mono" style={{
      width: 20, height: 18, padding: 0, borderRadius: 4, cursor: 'pointer',
      border: `1px solid ${T.border}`, background: 'transparent',
      color: T.textMuted, fontSize: 9, lineHeight: 1,
      display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
    }}>{label}</button>
  );
  return (
    <div className="fade-up" draggable
      onDragStart={() => { _pmDragTask = task; }}
      onDragOver={e => e.preventDefault()}
      onDrop={e => { e.preventDefault(); e.stopPropagation(); onDropOnCard(task); }}
      style={{
        background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8,
        padding: '8px 10px', display: 'flex', flexDirection: 'column', gap: 6,
        cursor: 'grab',
      }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 6 }}>
        <span style={{ marginTop: 3, display: 'inline-flex', flexShrink: 0 }}>
          <PriorityDot priority={task.priority} />
        </span>
        <span style={{ fontSize: 13, color: T.text, lineHeight: 1.35, flex: 1, minWidth: 0, overflowWrap: 'anywhere' }}>
          {task.title}
        </span>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 5, flexWrap: 'wrap' }}>
        {mode === 'global' && task.project && (
          <span onClick={() => onOpenProject(task)} title={task.project.path}
            style={{ cursor: 'pointer', display: 'inline-flex' }}>
            <Badge color={T.purple}>{task.project.name || 'project'}</Badge>
          </span>
        )}
        <DepsBadge task={task} byId={byId} />
        <DueBadge task={task} />
        {(task.tags || []).map(tag => <Badge key={tag} color={T.blue}>{tag}</Badge>)}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        {isTaskReady(task, byId) &&
          navBtn('✓', 'Unblocked — move to Backlog', () => onMoveTo(task, 'backlog'))}
        {ci > 0 && navBtn('◀', `Move to ${PM_COLUMNS[ci - 1][1]}`, () => onMoveStatus(task, -1))}
        {ci >= 0 && ci < keys.length - 1 &&
          navBtn('▶', `Move to ${PM_COLUMNS[ci + 1][1]}`, () => onMoveStatus(task, +1))}
        <span style={{ flex: 1 }} />
        {mode === 'project' && index > 0 &&
          navBtn('▲', 'Move up', () => onMoveRank(task, colTasks, index, -1))}
        {mode === 'project' && index < colTasks.length - 1 &&
          navBtn('▼', 'Move down', () => onMoveRank(task, colTasks, index, +1))}
      </div>
    </div>
  );
}

function TaskBoard({ projects, onOpenDrill }) {
  const [scope, setScope] = useState('global');      // 'global' | project path
  const [globalData, setGlobalData] = useState(null); // /api/pm/global response
  const [boardData, setBoardData] = useState(null);   // /api/projects/pm response
  const [msFilter, setMsFilter] = useState('');       // milestone id ('' = all)
  const [err, setErr] = useState('');
  const [newTitle, setNewTitle] = useState('');
  const [timeData, setTimeData] = useState(null);

  const mode = scope === 'global' ? 'global' : 'project';

  const load = useCallback(async () => {
    try {
      const g = await api.get('/api/pm/global');
      setGlobalData(g);
      if (scope !== 'global') {
        const q = msFilter ? `&milestone=${encodeURIComponent(msFilter)}` : '';
        const b = await api.get(`/api/projects/pm?path=${encodeURIComponent(scope)}${q}`);
        setBoardData(b);
        try {
          setTimeData(await api.get(
            `/api/projects/time?path=${encodeURIComponent(scope)}`));
        } catch (e2) { setTimeData(null); }
      }
      setErr('');
    } catch (e) {
      if (e.status === 409) {
        setErr('This project has no task store yet — run c3 init there first.');
      } else {
        setErr(e.message || 'Failed to load tasks');
      }
      if (scope !== 'global') setBoardData(null);
    }
  }, [scope, msFilter]);

  // Reset per-project state when the scope changes (avoid stale board flash).
  useEffect(() => { setBoardData(null); setMsFilter(''); setNewTitle(''); setErr(''); setTimeData(null); }, [scope]);
  useEffect(() => { load(); }, [load]);
  usePoll(load, 10000);

  // ── Mutations (notify + reload) ───────────────────────────────
  const moveStatus = async (task, dir) => {
    const keys = PM_COLUMNS.map(c => c[0]);
    const ci = keys.indexOf(task.status);
    const next = keys[ci + dir];
    if (ci < 0 || !next) return;
    const path = mode === 'global' ? (task.project && task.project.path) : scope;
    if (!path) return;
    try {
      const body = { path, id: task.id, move: { status: next } };
      if (mode === 'project' && boardData && boardData.board && boardData.board.rev != null) {
        body.expected_rev = boardData.board.rev;
      }
      await api.put('/api/projects/pm/task', body);
      notify(`Moved to ${PM_COLUMNS[ci + dir][1]}`);
      load();
    } catch (e) {
      if (e && e.status === 409) {
        notify('Board changed elsewhere — refreshed', 'err');
        load();
      } else { notify(e.message || 'Move failed', 'err'); }
    }
  };

  const moveRank = async (task, colTasks, index, dir) => {
    const move = dir < 0
      ? { before_id: colTasks[index - 1].id }
      : { after_id: colTasks[index + 1].id };
    try {
      const body = { path: scope, id: task.id, move };
      if (boardData && boardData.board && boardData.board.rev != null) {
        body.expected_rev = boardData.board.rev;
      }
      await api.put('/api/projects/pm/task', body);
      notify(dir < 0 ? 'Moved up' : 'Moved down');
      load();
    } catch (e) {
      if (e && e.status === 409) {
        notify('Board changed elsewhere — refreshed', 'err');
        load();
      } else { notify(e.message || 'Reorder failed', 'err'); }
    }
  };

  const moveTo = async (task, status) => {
    const path = mode === 'global' ? (task.project && task.project.path) : scope;
    if (!path || task.status === status) return;
    try {
      await api.put('/api/projects/pm/task', { path, id: task.id, move: { status } });
      notify(`Moved to ${(PM_COLUMNS.find(c => c[0] === status) || [status, status])[1]}`);
      load();
    } catch (e) { notify(e.message || 'Move failed', 'err'); }
  };

  const dropOnColumn = (key) => {
    const t = _pmDragTask; _pmDragTask = null;
    if (!t) return;
    moveTo(t, key);
  };

  const dropOnCard = (target) => {
    const t = _pmDragTask; _pmDragTask = null;
    if (!t || t.id === target.id) return;
    if (mode !== 'project') { moveTo(t, target.status); return; }
    const move = t.status === target.status
      ? { before_id: target.id }
      : { status: target.status, before_id: target.id };
    api.put('/api/projects/pm/task', { path: scope, id: t.id, move })
      .then(() => { notify('Moved'); load(); })
      .catch(e => notify(e.message || 'Move failed', 'err'));
  };

  const addTask = async () => {
    const title = newTitle.trim();
    if (!title || mode !== 'project') return;
    try {
      const body = { path: scope, title };
      if (msFilter) body.milestone_id = msFilter;
      await api.post('/api/projects/pm/task', body);
      setNewTitle('');
      notify(`Task added: ${title}`);
      load();
    } catch (e) { notify(e.message || 'Add failed', 'err'); }
  };

  const openProject = (task) => {
    if (!task.project) return;
    const p = (projects || []).find(x => x.path === task.project.path) || task.project;
    onOpenDrill(p, 'tasks');
  };

  // ── Derived: cards per column ─────────────────────────────────
  const columns = {};
  PM_COLUMNS.forEach(([k]) => { columns[k] = []; });
  if (mode === 'global') {
    (((globalData || {}).tasks) || []).forEach(t => {
      if (columns[t.status]) columns[t.status].push(t);
    });
  } else {
    const cols = (((boardData || {}).board) || {}).columns || {};
    PM_COLUMNS.forEach(([k]) => { columns[k] = cols[k] || []; });
  }
  const boardById = {};
  Object.values(columns).forEach(list =>
    list.forEach(t => { boardById[t.id] = t; }));
  const milestones = (((boardData || {}).board) || {}).milestones || [];
  const recovery = (((boardData || {}).board) || {}).recovery || null;
  const byProject = (globalData || {}).by_project || {};
  const projChips = Object.entries(byProject)
    .sort((a, b) => (b[1].open || 0) - (a[1].open || 0) ||
      (a[1].name || '').localeCompare(b[1].name || ''));
  const loadingBoard = (mode === 'global' && !globalData) ||
    (mode === 'project' && !boardData && !err);

  const chip = (label, active, onClick, title) => (
    <button onClick={onClick} title={title || ''} className="mono" style={{
      padding: '3px 10px', borderRadius: 999, fontSize: 11, fontWeight: 600,
      cursor: 'pointer', whiteSpace: 'nowrap',
      border: `1px solid ${active ? `${T.accent}50` : T.border}`,
      background: active ? T.accentDim : 'transparent',
      color: active ? T.accent : T.textMuted,
    }}>{label}</button>
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, flex: 1, minHeight: 0 }}>
      {/* Scope selector */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        {chip('All projects', scope === 'global', () => setScope('global'))}
        {projChips.map(([path, info]) => (
          <React.Fragment key={path}>
            {chip(`${info.name || path} · ${info.open || 0}`, scope === path,
              () => setScope(path), path)}
          </React.Fragment>
        ))}
      </div>

      {/* Milestone filter (per-project scope only) */}
      {mode === 'project' && milestones.length > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          <span className="mono" style={{ fontSize: 11, color: T.textDim, textTransform: 'uppercase', letterSpacing: 1 }}>
            milestone
          </span>
          {milestones.map(ms => (
            <React.Fragment key={ms.id}>
              {chip(ms.name, msFilter === ms.id,
                () => setMsFilter(msFilter === ms.id ? '' : ms.id))}
            </React.Fragment>
          ))}
        </div>
      )}

      {mode === 'project' && timeData && timeData.summary && (
        <div className="mono" style={{ fontSize: 11, color: T.textDim }}>
          ⏱ {fmtMinutes(timeData.summary.today.total_min)} today
          {' · '}{fmtMinutes(timeData.summary.last_7d.total_min)} last 7d
          {' · '}{fmtMinutes(timeData.summary.last_30d.total_min)} last 30d
          {' · '}{(timeData.entries || []).length} manual
        </div>
      )}
      {err && <div style={{ fontSize: 12, color: T.error }}>{err}</div>}
      {mode === 'project' && <RecoveryBanner recovery={recovery} />}

      {/* Board */}
      {loadingBoard ? (
        <div style={{ fontSize: 12, color: T.textMuted, padding: '18px 2px' }}>Loading tasks…</div>
      ) : (
        <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start', flex: 1, minHeight: 0 }}>
          {PM_COLUMNS.map(([key, label]) => {
            const colTasks = columns[key] || [];
            const hiddenDone = mode === 'global' && key === 'done';
            return (
              <div key={key} style={{
                flex: 1, minWidth: 220, display: 'flex', flexDirection: 'column', gap: 8,
                borderTop: `3px solid ${statusColor(key)}`, paddingTop: 10,
              }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                  <span style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 1, color: T.textMuted }}>
                    {label}
                  </span>
                  <span className="mono" style={{ fontSize: 11, color: T.textDim }}>
                    {hiddenDone ? '—' : colTasks.length}
                  </span>
                </div>
                <div
                  onDragOver={e => e.preventDefault()}
                  onDrop={e => { e.preventDefault(); dropOnColumn(key); }}
                  style={{
                    display: 'flex', flexDirection: 'column', gap: 8,
                    overflowY: 'auto', maxHeight: 'calc(100vh - 220px)', paddingRight: 2,
                  }}>
                  {mode === 'project' && key === 'backlog' && (
                    <div style={{ display: 'flex', gap: 6 }}>
                      <input value={newTitle}
                        onChange={e => setNewTitle(e.target.value)}
                        onKeyDown={e => { if (e.key === 'Enter') addTask(); }}
                        placeholder="Add a task…"
                        style={{
                          flex: 1, minWidth: 0, background: T.surfaceAlt,
                          border: `1px solid ${T.border}`, borderRadius: 6,
                          padding: '6px 8px', fontSize: 12, color: T.text, outline: 'none',
                        }} />
                      <button onClick={addTask} title="Add task" style={{
                        width: 28, borderRadius: 6, border: `1px solid ${T.accent}40`,
                        background: T.accentDim, cursor: 'pointer',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                      }}>
                        <I name="plus" size={12} color={T.accent} />
                      </button>
                    </div>
                  )}
                  {hiddenDone ? (
                    <div style={{ fontSize: 11, color: T.textDim, fontStyle: 'italic', padding: '4px 2px' }}>
                      hidden in global view
                    </div>
                  ) : (
                    <React.Fragment>
                      {colTasks.map((t, i) => (
                        <TaskBoardCard key={t.id} task={t} mode={mode}
                          colTasks={colTasks} index={i} byId={boardById}
                          onMoveStatus={moveStatus} onMoveRank={moveRank}
                          onOpenProject={openProject}
                          onMoveTo={moveTo} onDropOnCard={dropOnCard} />
                      ))}
                      {colTasks.length === 0 && !(mode === 'project' && key === 'backlog') && (
                        <div style={{ fontSize: 11, color: T.textDim, fontStyle: 'italic', padding: '4px 2px' }}>
                          —
                        </div>
                      )}
                    </React.Fragment>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Footer (global mode only) */}
      {mode === 'global' && globalData && (
        <div className="mono" style={{ fontSize: 11, color: T.textDim }}>
          {globalData.projects_scanned || 0} projects
          {' · '}{(globalData.skipped || []).length} skipped
          {globalData.capped ? ' · capped' : ''}
        </div>
      )}
    </div>
  );
}
