// ─── PM shared primitives ──────────────────────────────────────
// Purely presentational helpers for project-management views.
// Loaded in BOTH the hub bundle and the per-project bundle, right
// after ui/shared.js — no fetches, no hub-only globals (notify,
// usePoll, Drill*) may be referenced here. Colors are computed at
// call/render time so theme switches (mutable T) stay live.

const PM_COLUMNS = [
  ['backlog', 'Backlog'],
  ['in_progress', 'In progress'],
  ['blocked', 'Blocked'],
  ['done', 'Done'],
];

const PM_PRIORITIES = ['p0', 'p1', 'p2', 'p3'];

// Function (not a module-level map) so T lookups stay live across theme switches.
function priorityMeta(p) {
  switch (p) {
    case 'p0': return { color: T.error, label: 'P0' };
    case 'p1': return { color: T.warn, label: 'P1' };
    case 'p2': return { color: T.blue, label: 'P2' };
    default: return { color: T.textMuted, label: 'P3' };
  }
}

function statusColor(status) {
  switch (status) {
    case 'in_progress': return T.blue;
    case 'blocked': return T.warn;
    case 'done': return T.accent;
    default: return T.textMuted;
  }
}

const PriorityDot = ({ priority }) => {
  const meta = priorityMeta(priority);
  return (
    <span title={`Priority ${meta.label}`} style={{
      display: 'inline-flex', alignItems: 'center', gap: 4, flexShrink: 0,
    }}>
      <span style={{ width: 6, height: 6, borderRadius: '50%', background: meta.color, flexShrink: 0 }} />
      <span className="mono" style={{ fontSize: 10, fontWeight: 700, color: meta.color }}>{meta.label}</span>
    </span>
  );
};

const DueBadge = ({ task }) => {
  if (!task || !task.due_date) return null;
  const today = new Date().toISOString().slice(0, 10);
  const overdue = task.due_date < today && task.status !== 'done';
  return <Badge color={overdue ? T.error : T.textMuted}>due {task.due_date}</Badge>;
};

const MilestoneChip = ({ milestone }) => {
  if (!milestone) return null;
  const prog = milestone.progress || {};
  const total = prog.total != null ? prog.total : 0;
  const done = prog.done != null ? prog.done : 0;
  const pct = prog.pct != null ? prog.pct : (total ? Math.round((done / total) * 100) : 0);
  return <Badge color={T.purple}>{milestone.name} · {done}/{total} · {pct}%</Badge>;
};

const TaskLinkIcons = ({ task }) => {
  const links = (task && task.links) || [];
  if (!links.length) return null;
  const title = links.map(l => `${(l && l.type) || 'link'}:${(l && l.ref) || ''}`).join('\n');
  return (
    <span className="mono" title={title} style={{
      fontSize: 10, color: T.textMuted, border: `1px solid ${T.border}`,
      borderRadius: 4, padding: '1px 5px', whiteSpace: 'nowrap', flexShrink: 0,
    }}>🔗 {links.length}</span>
  );
};

// Open-blocker count for a task. Prefers server-enriched fields
// (blockers_open from /api/pm/global); otherwise resolves through `byId`,
// counting unresolved ids as open (conservative).
function taskOpenBlockers(task, byId) {
  const deps = (task && task.blocked_by) || [];
  if (!deps.length) return 0;
  if (task.blockers_open != null) return task.blockers_open;
  return deps.filter(id => {
    const b = (byId || {})[id];
    return !b || (b.status !== 'done' && (b.lifecycle || 'active') === 'active');
  }).length;
}

// True when a task still sits in "blocked" but nothing blocks it anymore.
function isTaskReady(task, byId) {
  const deps = (task && task.blocked_by) || [];
  return !!task && task.status === 'blocked' && deps.length > 0 &&
    taskOpenBlockers(task, byId) === 0;
}

// Blocked/ready dependency badge.
const DepsBadge = ({ task, byId }) => {
  const deps = (task && task.blocked_by) || [];
  if (!deps.length) return null;
  const open = taskOpenBlockers(task, byId);
  const title = (task.blocker_titles && task.blocker_titles.join('\n')) ||
    deps.map(id => {
      const b = (byId || {})[id];
      const label = (b && b.title) || id;
      return b && b.status === 'done' ? `${label} (done)` : label;
    }).join('\n');
  if (isTaskReady(task, byId)) {
    return (
      <span title={title} style={{ display: 'inline-flex', flexShrink: 0 }}>
        <Badge color={T.accent}>✓ ready</Badge>
      </span>
    );
  }
  return (
    <span title={title} style={{ display: 'inline-flex', flexShrink: 0 }}>
      <Badge color={open ? T.warn : T.textMuted}>
        {open ? `⛔ ${open}` : '✓ deps'}
      </Badge>
    </span>
  );
};

// Warning strip shown when the store quarantined a corrupt pm.json.
const RecoveryBanner = ({ recovery }) => {
  if (!recovery) return null;
  return (
    <div style={{
      fontSize: 12, color: T.warn, border: `1px solid ${T.warn}50`,
      background: `${T.warn}14`, borderRadius: 6, padding: '6px 10px',
      lineHeight: 1.45,
    }}>
      pm.json was corrupt and {recovery.restored_from_backup
        ? 'has been restored from backup (pm.json.bak)'
        : 'no backup existed — the task store restarted empty'}
      {recovery.quarantined ? ` — the original is kept as ${recovery.quarantined}` : ''}.
    </div>
  );
};
