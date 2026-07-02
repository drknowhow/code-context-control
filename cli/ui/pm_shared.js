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
