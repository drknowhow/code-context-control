// ─── Scheduled tasks (cross-project) ──────────────────────────────────────
// Skills, prompts and commands the Oracle fires on a cadence, their runs and
// the approvals waiting on a person. Reads and writes /api/hub/schedules*; the
// Oracle's tick does the firing, so a change here shows up within one tick.

const SCHED_PRESETS = ['hourly', 'daily', 'weekdays', 'weekly', 'monthly'];
const SCHED_EFFORTS = ['', 'low', 'medium', 'high', 'xhigh', 'max'];
const SCHED_MODES = ['acceptEdits', 'auto', 'dontAsk', 'manual', 'plan', 'bypassPermissions'];
const SCHED_PROVIDERS = ['claude', 'codex', 'gemini', 'grok'];

function schedWhenLabel(when) {
  if (!when) return '—';
  if (when.type === 'every') return `every ${when.seconds >= 3600 ? `${Math.round(when.seconds / 360) / 10}h` : `${Math.round(when.seconds / 60)}m`}`;
  if (when.type === 'at') return `once at ${String(when.when || '').slice(0, 16)}`;
  if (when.type === 'preset') return `${when.preset} ${when.at || ''}${when.days && when.days.length ? ' ' + when.days.join(',') : ''}`;
  return when.expr || '—';
}

function schedRelative(iso) {
  if (!iso) return '—';
  const diff = (new Date(iso).getTime() - Date.now()) / 1000;
  const abs = Math.abs(diff);
  const text = abs < 90 ? `${Math.round(abs)}s` : abs < 5400 ? `${Math.round(abs / 60)}m`
    : abs < 172800 ? `${Math.round(abs / 360) / 10}h` : `${Math.round(abs / 86400)}d`;
  return diff >= 0 ? `in ${text}` : `${text} ago`;
}

function schedTarget(row) {
  const t = row.target || {};
  if (row.kind === 'skill') return `${t.skill || ''} ${t.args || ''}`.trim();
  if (row.kind === 'prompt') return t.prompt || '';
  return t.cmd || '';
}

const SCHED_STATUS_COLOR = {
  done: 'accent', running: 'blue', dispatched: 'blue', check_running: 'blue', claimed: 'blue',
  awaiting_approval: 'warn', approved: 'blue', failed: 'error', timeout: 'error', lost: 'error',
  denied: 'warn', expired: 'warn', missed: 'warn', skipped_overlap: 'textDim', check_skipped: 'textDim',
  cancelled: 'textDim',
};

function SchedStatusPill({ status }) {
  const key = SCHED_STATUS_COLOR[status] || 'textMuted';
  return (
    <span className="mono" style={{
      fontSize: 10, padding: '2px 7px', borderRadius: 4, whiteSpace: 'nowrap',
      color: T[key] || T.textMuted, background: T.surfaceAlt,
    }}>{status || '—'}</span>
  );
}

function ScheduleModal({ row, projects, onClose, onSaved }) {
  const { useState, useEffect } = React;
  const editing = !!(row && row.id);
  const w = (row && row.when) || {};
  const [f, setF] = useState({
    name: row ? row.name : '',
    project_path: row ? row.project_path : ((projects[0] || {}).path || ''),
    kind: row ? row.kind : 'skill',
    target: row ? schedTarget(row) : '',
    cadence: w.type || 'preset',
    preset: w.preset || 'weekdays', preset_at: w.at || '09:00', days: (w.days || []).join(','),
    cron: w.type === 'cron' ? w.expr : '', every: w.seconds ? Math.round(w.seconds / 60) : 60,
    at: w.type === 'at' ? w.when : '', tz: w.tz || 'local',
    provider: row ? row.provider : 'claude', model: row ? row.model : '', effort: row ? row.effort : '',
    permission_mode: row ? row.permission_mode : 'acceptEdits',
    autonomy: row ? row.autonomy : 'approval',
    max_budget_usd: row ? row.max_budget_usd : 0, timeout_s: row ? row.timeout_s : 1800,
    overlap: row ? row.overlap : 'skip',
    grace_s: row && row.misfire ? row.misfire.grace_s : 900,
    check_command: row ? row.check_command : '',
    allowed_tools: row ? row.allowed_tools : '',
  });
  const [preview, setPreview] = useState([]);
  const [err, setErr] = useState('');
  const [saving, setSaving] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e && e.target ? (e.target.type === 'checkbox' ? e.target.checked : e.target.value) : e });

  const when = () => {
    if (f.cadence === 'cron') return { type: 'cron', expr: f.cron, tz: f.tz };
    if (f.cadence === 'every') return { type: 'every', seconds: Math.max(1, Number(f.every) || 0) * 60 };
    if (f.cadence === 'at') return { type: 'at', when: f.at };
    return { type: 'preset', preset: f.preset, at: f.preset_at, days: f.days.split(',').map(s => s.trim()).filter(Boolean), tz: f.tz };
  };

  useEffect(() => {
    let alive = true;
    api.post('/api/hub/schedules/preview', { when: when(), count: 3 })
      .then(d => { if (alive) { setPreview(d.next || []); setErr(''); } })
      .catch(e => { if (alive) { setPreview([]); setErr(String(e && e.message ? e.message : e)); } });
    return () => { alive = false; };
  }, [f.cadence, f.preset, f.preset_at, f.days, f.cron, f.every, f.at, f.tz]);

  const body = () => {
    const target = f.kind === 'command' ? { cmd: f.target } : f.kind === 'prompt' ? { prompt: f.target }
      : { skill: f.target.split(' ')[0], args: f.target.split(' ').slice(1).join(' ') };
    return {
      name: f.name, project: f.project_path, kind: f.kind, target, when: when(),
      provider: f.provider, model: f.model, effort: f.effort, permission_mode: f.permission_mode,
      autonomy: f.autonomy, max_budget_usd: Number(f.max_budget_usd) || 0, timeout_s: Number(f.timeout_s) || 1800,
      overlap: f.overlap, misfire: { policy: 'grace', grace_s: Number(f.grace_s) || 0 },
      check_command: f.check_command, allowed_tools: f.allowed_tools,
    };
  };

  const save = async () => {
    setSaving(true);
    try {
      if (editing) await api.patch(`/api/hub/schedules/${row.id}`, body());
      else await api.post('/api/hub/schedules', body());
      onSaved();
    } catch (e) {
      setErr(String(e && e.message ? e.message : e));
      setSaving(false);
    }
  };

  const llm = f.kind !== 'command';
  const sel = (k, opts, labels) => (
    <select value={f[k]} onChange={set(k)} style={mdlInputStyle()}>
      {opts.map((o, i) => <option key={o} value={o}>{labels ? labels[i] : (o || 'default')}</option>)}
    </select>
  );
  const two = { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 };

  return (
    <Modal title={editing ? `Edit ${row.name}` : 'New scheduled task'} width={620} onClose={onClose}>
      <div style={two}>
        <div><MdlLabel>Name</MdlLabel><input value={f.name} onChange={set('name')} style={mdlInputStyle()} placeholder="daily report" /></div>
        <div><MdlLabel>Project</MdlLabel>
          <select value={f.project_path} onChange={set('project_path')} style={mdlInputStyle()}>
            {projects.map(p => <option key={p.path} value={p.path}>{p.name || p.path}</option>)}
          </select>
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '150px 1fr', gap: 10 }}>
        <div><MdlLabel>Kind</MdlLabel>{sel('kind', ['skill', 'prompt', 'command'])}</div>
        <div><MdlLabel>{f.kind === 'skill' ? 'Skill and args' : f.kind === 'prompt' ? 'Prompt' : 'Command'}</MdlLabel>
          <input value={f.target} onChange={set('target')} style={mdlInputStyle()}
            placeholder={f.kind === 'skill' ? '/daily_report weekly' : f.kind === 'prompt' ? 'Summarise this week…' : 'git fetch --all'} />
        </div>
      </div>

      <MdlLabel>When</MdlLabel>
      <div style={{ display: 'grid', gridTemplateColumns: '120px 1fr 1fr', gap: 10 }}>
        {sel('cadence', ['preset', 'cron', 'every', 'at'], ['Preset', 'Cron', 'Every N min', 'Once at'])}
        {f.cadence === 'preset' && sel('preset', SCHED_PRESETS)}
        {f.cadence === 'preset' && <input value={f.preset_at} onChange={set('preset_at')} style={mdlInputStyle()} placeholder="09:00" />}
        {f.cadence === 'cron' && <input value={f.cron} onChange={set('cron')} style={{ ...mdlInputStyle(), gridColumn: 'span 2' }} placeholder="0 9 * * 1-5" className="mono" />}
        {f.cadence === 'every' && <input type="number" min="1" value={f.every} onChange={set('every')} style={mdlInputStyle()} />}
        {f.cadence === 'every' && <span style={{ alignSelf: 'center', fontSize: 12, color: T.textMuted }}>minutes</span>}
        {f.cadence === 'at' && <input value={f.at} onChange={set('at')} style={{ ...mdlInputStyle(), gridColumn: 'span 2' }} placeholder="2026-10-14T09:00" className="mono" />}
      </div>
      {f.cadence === 'preset' && f.preset === 'weekly' && (
        <input value={f.days} onChange={set('days')} style={{ ...mdlInputStyle(), marginTop: 8 }} placeholder="mon,fri" />
      )}
      {(f.cadence === 'preset' || f.cadence === 'cron') && (
        <input value={f.tz} onChange={set('tz')} style={{ ...mdlInputStyle(), marginTop: 8 }} placeholder="local or Europe/Berlin" />
      )}
      <div className="mono" style={{ fontSize: 11, color: preview.length ? T.textMuted : T.warn, marginTop: 6 }}>
        {preview.length ? 'next: ' + preview.map(p => p.replace('T', ' ').slice(0, 16)).join('  ·  ') : (err || 'no upcoming fire')}
      </div>

      {llm && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 }}>
          <div><MdlLabel>Provider</MdlLabel>{sel('provider', SCHED_PROVIDERS)}</div>
          <div><MdlLabel>Model</MdlLabel><input value={f.model} onChange={set('model')} style={mdlInputStyle()} placeholder="sonnet" /></div>
          <div><MdlLabel>Effort</MdlLabel>{sel('effort', SCHED_EFFORTS)}</div>
        </div>
      )}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 }}>
        <div><MdlLabel>Autonomy</MdlLabel>{sel('autonomy', ['approval', 'autonomous'], ['Ask me first', 'Autonomous'])}</div>
        {llm && <div><MdlLabel>Permission mode</MdlLabel>{sel('permission_mode', SCHED_MODES)}</div>}
        {llm && <div><MdlLabel>Budget (USD, 0 = none)</MdlLabel><input type="number" min="0" step="0.1" value={f.max_budget_usd} onChange={set('max_budget_usd')} style={mdlInputStyle()} /></div>}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 }}>
        <div><MdlLabel>Timeout (s)</MdlLabel><input type="number" min="30" value={f.timeout_s} onChange={set('timeout_s')} style={mdlInputStyle()} /></div>
        <div><MdlLabel>Overlap</MdlLabel>{sel('overlap', ['skip', 'allow'], ['Skip if still running', 'Allow'])}</div>
        <div><MdlLabel>Misfire grace (s)</MdlLabel><input type="number" min="0" value={f.grace_s} onChange={set('grace_s')} style={mdlInputStyle()} /></div>
      </div>
      <MdlLabel>Check command (optional — exit 1 arms the run, exit 0 skips it)</MdlLabel>
      <input value={f.check_command} onChange={set('check_command')} style={mdlInputStyle()} className="mono" placeholder="test -s inbox.txt" />
      {llm && f.provider === 'claude' && (
        <div>
          <MdlLabel>Allowed tools (optional)</MdlLabel>
          <input value={f.allowed_tools} onChange={set('allowed_tools')} style={mdlInputStyle()} className="mono" placeholder="Read Edit Bash(git *)" />
        </div>
      )}
      {err && preview.length > 0 && <div style={{ fontSize: 11, color: T.error, marginTop: 8 }}>{err}</div>}
      <MdlFooter>
        <button onClick={onClose} style={{ ...mdlInputStyle(), width: 'auto', cursor: 'pointer' }}>Cancel</button>
        <button onClick={save} disabled={saving || !f.name || !f.target} style={{
          ...mdlInputStyle(), width: 'auto', cursor: 'pointer', background: T.accentDim, color: T.accent, fontWeight: 700,
        }}>{saving ? 'Saving…' : editing ? 'Save' : 'Create'}</button>
      </MdlFooter>
    </Modal>
  );
}

function HubSchedules({ projects, onOpenDrill }) {
  const { useState, useCallback } = React;
  const [rows, setRows] = useState(null);
  const [runs, setRuns] = useState([]);
  const [approvals, setApprovals] = useState([]);
  const [status, setStatus] = useState(null);
  const [err, setErr] = useState('');
  const [modal, setModal] = useState(null);
  const [confirm, setConfirm] = useState(null);

  const load = useCallback(async () => {
    try {
      const [s, r, a, st] = await Promise.all([
        api.get('/api/hub/schedules'), api.get('/api/hub/schedules/runs?limit=30'),
        api.get('/api/hub/schedules/approvals?status=pending'), api.get('/api/hub/schedules/status'),
      ]);
      setRows(s.schedules || []); setRuns(r.runs || []); setApprovals(a.approvals || []); setStatus(st);
      setErr('');
    } catch (e) {
      setErr(String(e && e.message ? e.message : e));
    }
  }, []);
  React.useEffect(() => { load(); }, [load]);
  usePoll(load, 5000);

  const act = async (fn) => {
    try { await fn(); } catch (e) { setErr(String(e && e.message ? e.message : e)); }
    load();
  };
  const decide = (id, decision) => act(() => api.post(`/api/hub/schedules/approvals/${id}`, { decision }));
  const toggle = (row) => act(() => api.post(`/api/hub/schedules/${row.id}/${row.enabled ? 'disable' : 'enable'}`, {}));
  const runNow = (row) => act(() => api.post(`/api/hub/schedules/${row.id}/run`, {}));
  const remove = (row) => act(() => api.del(`/api/hub/schedules/${row.id}`));

  const sched = status || {};
  const schedState = !status ? 'loading' : status.reachable === false ? 'oracle unreachable'
    : sched.standby ? 'standby' : sched.running ? 'running' : 'stopped';
  const schedColor = schedState === 'running' ? T.accent : schedState === 'loading' ? T.textMuted : T.warn;
  const grid = 'minmax(0,1.6fr) minmax(0,1fr) minmax(0,1.8fr) minmax(0,1.3fr) minmax(0,1fr) 90px 150px';
  const btn = (label, onClick, color, title) => (
    <span onClick={onClick} title={title || label} style={{ cursor: 'pointer', color: color || T.textMuted, fontSize: 11, padding: '0 4px' }}>{label}</span>
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <I name="clock" size={15} color={T.accent} />
        <span style={{ fontSize: 14, color: T.text }}>Scheduled tasks</span>
        <span className="mono" title={status ? JSON.stringify(status) : ''} style={{
          fontSize: 10, padding: '2px 7px', borderRadius: 4, color: schedColor, background: T.surfaceAlt,
        }}>scheduler {schedState}{sched.next_due ? ` · next ${schedRelative(sched.next_due)}` : ''}</span>
        <span className="mono" style={{ fontSize: 11, color: T.textMuted }}>
          {rows ? `${rows.length} schedule(s)` : 'loading…'}
        </span>
        <div style={{ flex: 1 }} />
        <button onClick={() => setModal({})} style={{
          display: 'inline-flex', alignItems: 'center', gap: 6, height: 28, padding: '0 12px', borderRadius: 6,
          border: `1px solid ${T.border}`, background: T.accentDim, color: T.accent, cursor: 'pointer', fontSize: 12,
        }}><I name="plus" size={12} color={T.accent} /> New</button>
        <div onClick={load} title="Refresh" style={{ cursor: 'pointer', padding: 4 }}>
          <I name="refresh" size={13} color={T.textMuted} />
        </div>
      </div>

      {err && (
        <div style={{ fontSize: 11, color: T.error, background: T.errorDim, border: `1px solid ${T.error}`, borderRadius: 6, padding: '8px 10px' }}>
          {err}
        </div>
      )}

      {approvals.length > 0 && (
        <div style={{ background: T.surface, border: `1px solid ${T.warn}`, borderRadius: 6, padding: '8px 10px' }}>
          <div style={{ fontSize: 11, color: T.warn, marginBottom: 6 }}>Waiting for your approval</div>
          {approvals.map(a => (
            <div key={a.id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '4px 0', fontSize: 12 }}>
              <span style={{ color: T.text }}>{a.schedule_name}</span>
              <span className="mono" style={{ color: T.textMuted, fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{a.summary}</span>
              <span className="mono" style={{ fontSize: 10, color: T.textDim }}>expires {schedRelative(a.expires_at)}</span>
              <button onClick={() => decide(a.id, 'approve')} style={{ cursor: 'pointer', borderRadius: 4, border: 'none', padding: '3px 10px', background: T.accentDim, color: T.accent, fontSize: 11 }}>Approve</button>
              <button onClick={() => decide(a.id, 'deny')} style={{ cursor: 'pointer', borderRadius: 4, border: 'none', padding: '3px 10px', background: T.errorDim, color: T.error, fontSize: 11 }}>Deny</button>
            </div>
          ))}
        </div>
      )}

      {rows && rows.length === 0 && (
        <div style={{ fontSize: 12, color: T.textMuted, padding: '18px 10px', textAlign: 'center', border: `1px dashed ${T.border}`, borderRadius: 6 }}>
          No scheduled tasks yet. Create one here, or from a session with <span className="mono">c3_schedule(action='create', …)</span>.
        </div>
      )}

      {rows && rows.length > 0 && (
        <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 6 }}>
          <div style={{ display: 'grid', gridTemplateColumns: grid, gap: 10, padding: '8px 10px 5px', fontSize: 10, color: T.textDim }}>
            <span>NAME</span><span>PROJECT</span><span>RUNS</span><span>WHEN · NEXT</span><span>MODEL</span><span>LAST</span><span />
          </div>
          {rows.map(row => (
            <div key={row.id} style={{
              display: 'grid', gridTemplateColumns: grid, gap: 10, alignItems: 'center', padding: '7px 10px',
              borderTop: `1px solid ${T.border}`, fontSize: 12, opacity: row.enabled ? 1 : 0.55,
            }}>
              <span title={row.id} style={{ color: T.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {row.name}
                <span className="mono" style={{ fontSize: 10, color: row.autonomy === 'autonomous' ? T.accent : T.warn, marginLeft: 6 }}>
                  {row.autonomy === 'autonomous' ? 'auto' : 'ask'}
                </span>
              </span>
              <span onClick={() => onOpenDrill && onOpenDrill(projects.find(p => p.path === row.project_path) || { path: row.project_path, name: row.project_name })}
                style={{ color: T.textMuted, cursor: onOpenDrill ? 'pointer' : 'default', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {row.project_name || row.project_path}
              </span>
              <span className="mono" title={schedTarget(row)} style={{ color: T.blue, fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {row.kind === 'command' ? '$ ' : ''}{schedTarget(row)}
              </span>
              <span className="mono" title={row.next_run_at || ''} style={{ fontSize: 11, color: T.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {schedWhenLabel(row.when)} · {row.enabled ? schedRelative(row.next_run_at) : 'paused'}
              </span>
              <span className="mono" style={{ fontSize: 11, color: T.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {row.kind === 'command' ? '—' : `${row.provider}${row.model ? '/' + row.model : ''}${row.effort ? ' ' + row.effort : ''}`}
              </span>
              <SchedStatusPill status={row.last_status} />
              <span style={{ display: 'flex', justifyContent: 'flex-end', gap: 2 }}>
                {btn('Run', () => runNow(row), T.accent, 'Queue a run now')}
                {btn(row.enabled ? 'Pause' : 'Resume', () => toggle(row))}
                {btn('Edit', () => setModal(row))}
                {btn('Delete', () => setConfirm(row), T.error)}
              </span>
            </div>
          ))}
        </div>
      )}

      {runs.length > 0 && (
        <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 6 }}>
          <div style={{ fontSize: 11, color: T.textMuted, padding: '8px 10px 4px' }}>Recent runs</div>
          {runs.map(run => (
            <div key={run.run_id} style={{
              display: 'grid', gridTemplateColumns: '130px minmax(0,1.4fr) minmax(0,1fr) 70px 70px minmax(0,2fr)',
              gap: 10, alignItems: 'center', padding: '5px 10px', borderTop: `1px solid ${T.border}`, fontSize: 11,
            }}>
              <SchedStatusPill status={run.status} />
              <span style={{ color: T.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{run.schedule_name}</span>
              <span className="mono" style={{ color: T.textMuted }}>{schedRelative(run.claimed_at)} · {run.trigger}</span>
              <span className="mono" style={{ color: T.textMuted }}>{run.duration_ms != null ? `${(run.duration_ms / 1000).toFixed(1)}s` : ''}</span>
              <span className="mono" style={{ color: T.textMuted }}>{run.cost_usd != null ? `$${Number(run.cost_usd).toFixed(3)}` : ''}</span>
              <span title={run.error || run.result_excerpt || ''} style={{ color: run.error ? T.error : T.textDim, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {run.error || run.result_excerpt || ''}
              </span>
            </div>
          ))}
        </div>
      )}

      {modal && (
        <ScheduleModal row={modal.id ? modal : null} projects={projects} onClose={() => setModal(null)}
          onSaved={() => { setModal(null); load(); }} />
      )}
      {confirm && (
        <ConfirmDialog title="Delete this schedule?" danger confirmLabel="Delete"
          message={`${confirm.name} will no longer fire. Its past runs stay in the history until they are swept.`}
          onConfirm={() => { remove(confirm); setConfirm(null); }} onCancel={() => setConfirm(null)} />
      )}
    </div>
  );
}
