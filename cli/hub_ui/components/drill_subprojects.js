// ─── Drill panel: Sub-projects tab (parent management) ─────────
// Rendered by DrillPanel's 'subprojects' tab — visible for parents
// and for top-level projects eligible to become one (never for
// children; nesting depth is 1). Data comes from
// GET /api/projects/subprojects?parent=…; mutations hit the
// /api/projects/subprojects/{validate,remove,reconcile,cascade,
// cascade/cancel} endpoints. The Designate flow reuses the shared
// FolderPickerModal via onOpenModal('folderPick', project).

function subStatusMeta(status) {
  switch (status) {
    case 'ok': return { label: 'Linked', color: T.accent };
    case 'backlink_broken': return { label: 'Broken back-link', color: T.warn };
    case 'unregistered': return { label: 'Unregistered', color: T.warn };
    case 'missing_folder': return { label: 'Missing folder', color: T.error };
    case 'missing_c3': return { label: 'Missing .c3', color: T.error };
    default: return { label: status || '?', color: T.textMuted };
  }
}

// What Repair (fix=true) does per non-ok status — shown before firing.
// missing_c3 is deliberately NOT repairable by reconcile.
const SUB_REPAIR_ACTIONS = {
  backlink_broken: 'back-link rewritten from the parent config',
  unregistered: 're-registered in the hub registry',
  missing_folder: 'entry pruned (folder is gone)',
  missing_c3: 'not repairable here — re-designate the folder or run Cascade update',
};

const SUB_CASCADE_OPS = [
  ['update', 'Update (c3 init --force)'],
  ['reindex', 'Reindex (code + docs)'],
  ['health', 'Health check'],
];

function subCascadeOpLabel(op) {
  const hit = SUB_CASCADE_OPS.find(([id]) => id === op);
  return hit ? hit[1] : op;
}

// ── One child row: identity, status badge, Validate / Promote ──
function SubChildRow({ parent, parentName, child, onMutated }) {
  const [validation, setValidation] = useState(null);
  const [validating, setValidating] = useState(false);
  const [confirmPromote, setConfirmPromote] = useState(false);
  const [promoting, setPromoting] = useState(false);
  const meta = subStatusMeta(child.status);
  const yn = (v) => (v ? 'yes' : 'no');

  const validate = async () => {
    if (validating) return;
    if (validation) { setValidation(null); return; }  // second click hides the result
    setValidating(true);
    try {
      setValidation(await api.post('/api/projects/subprojects/validate',
        { parent, folder: child.path }));
    } catch (e) {
      setValidation({ ok: false, warnings: [e.message] });
    }
    setValidating(false);
  };

  const promote = async () => {
    setConfirmPromote(false);
    setPromoting(true);
    try {
      const d = await api.post('/api/projects/subprojects/remove',
        { parent, ref: child.path, mode: 'unlink' });
      notify(d.success ? `Promoted ${child.name} to top-level` : 'Promote failed',
        d.success ? 'ok' : 'err');
      if (d.success) onMutated();
    } catch (e) { notify('Promote: ' + apiErr(e), 'err'); }
    setPromoting(false);
  };

  return (
    <div style={{ borderBottom: `1px solid ${T.border}` }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 0', minWidth: 0 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: T.text }}>{child.name || child.rel_path}</div>
          <div className="mono" title={child.path} style={{
            fontSize: 11, color: T.textDim,
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}>{child.rel_path || child.path}</div>
        </div>
        {child.facts_count != null &&
          <span className="mono" style={{ fontSize: 11, color: T.textMuted, flexShrink: 0 }}>
            {child.facts_count} facts
          </span>}
        {child.notification_count > 0 && <Badge color={T.warn}>{child.notification_count} alerts</Badge>}
        <Badge color={meta.color}>{meta.label}</Badge>
        <Btn variant="ghost" onClick={validate} disabled={validating}
          style={{ padding: '4px 10px', fontSize: 11, flexShrink: 0 }}>
          {validating ? 'Validating…' : 'Validate'}
        </Btn>
        <Btn variant="ghost" onClick={() => setConfirmPromote(true)} disabled={promoting}
          style={{ padding: '4px 10px', fontSize: 11, flexShrink: 0 }}>
          {promoting ? 'Promoting…' : 'Promote'}
        </Btn>
      </div>
      {validation && (
        <div style={{
          margin: '0 0 10px', padding: '8px 10px', fontSize: 11,
          border: `1px solid ${T.border}`, borderRadius: 6, background: T.surfaceAlt,
        }}>
          <div className="mono" style={{ color: T.textMuted }}>
            .c3: {yn(validation.has_c3)} · registered: {yn(validation.registered)}
            {' '}· linked here: {yn(validation.already_linked)}
          </div>
          {(validation.warnings || []).map((w, i) => (
            <div key={i} style={{ display: 'flex', gap: 6, alignItems: 'flex-start', color: T.warn, marginTop: 4 }}>
              <I name="alertTriangle" size={11} color={T.warn} />
              <span>{w}</span>
            </div>
          ))}
          {validation.ok && (
            <div style={{ display: 'flex', gap: 6, alignItems: 'center', color: T.accent, marginTop: 4 }}>
              <I name="check" size={11} color={T.accent} />
              <span>Folder passes designation checks{validation.has_c3 ? ' (existing .c3 would be adopted)' : ''}.</span>
            </div>
          )}
        </div>
      )}
      {confirmPromote && (
        <ConfirmDialog title="Promote to Top-level"
          message={`Unlink "${child.name}" from "${parentName}"? It keeps its .c3 workspace and memory and becomes a top-level hub project. No files are deleted.`}
          confirmLabel="Promote"
          onConfirm={promote} onCancel={() => setConfirmPromote(false)} />
      )}
    </div>
  );
}

// ── Inline reconcile results (dry-run → explicit Repair) ───────
function SubReconcilePanel({ rec, busy, onRepair, onDismiss }) {
  const d = rec.result;
  const children = d.children || [];
  const issues = children.filter(c => c.status !== 'ok');
  const orphans = d.orphans || [];
  const prune = issues.some(c => c.status === 'missing_folder');
  const repairable = issues.some(c => c.status !== 'missing_c3') || orphans.length > 0;

  return (
    <div className="fade-up" style={{
      marginTop: 14, border: `1px solid ${T.border}`, borderRadius: 8,
      padding: 14, background: T.surfaceAlt,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <I name="wrench" size={13} color={T.textMuted} />
        <span style={{
          flex: 1, fontSize: 11, fontWeight: 700, textTransform: 'uppercase',
          letterSpacing: 1, color: T.textMuted,
        }}>Reconcile — {rec.phase === 'fixed' ? 'repair result' : 'dry-run'}</span>
        <button onClick={onDismiss} title="Dismiss" style={{
          background: 'none', border: 'none', cursor: 'pointer', padding: 4, display: 'flex',
        }}>
          <I name="xSmall" size={12} color={T.textMuted} />
        </button>
      </div>

      {d.ok && rec.phase !== 'fixed' && (
        <div style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12, color: T.accent }}>
          <I name="check" size={12} color={T.accent} />
          All {children.length} sub-project link{children.length === 1 ? '' : 's'} consistent · no registry orphans.
        </div>
      )}

      {issues.map(c => (
        <div key={c.path || c.rel_path} style={{
          display: 'flex', alignItems: 'center', gap: 8, padding: '5px 0', fontSize: 12,
        }}>
          <Badge color={subStatusMeta(c.status).color}>{subStatusMeta(c.status).label}</Badge>
          <span style={{ color: T.text, fontWeight: 600 }}>{c.name || c.rel_path}</span>
          <span style={{ color: T.textMuted, fontSize: 11 }}>
            {rec.phase === 'fixed' ? 'still ' + subStatusMeta(c.status).label.toLowerCase()
              : SUB_REPAIR_ACTIONS[c.status] || ''}
          </span>
        </div>
      ))}
      {orphans.map(p => (
        <div key={p} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 0', fontSize: 12 }}>
          <Badge color={T.warn}>Registry orphan</Badge>
          <span className="mono" title={p} style={{
            color: T.textMuted, fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>{p}</span>
          <span style={{ color: T.textMuted, fontSize: 11, flexShrink: 0 }}>parent link cleared on repair</span>
        </div>
      ))}

      {rec.phase === 'fixed' && (
        <div style={{ fontSize: 12, color: d.ok ? T.accent : T.warn, marginTop: 6 }}>
          {(d.fixed || []).length} repaired
          {(d.pruned || []).length ? ` · ${(d.pruned || []).length} pruned` : ''}
          {d.ok ? ' — all links consistent now.' : ' — issues remain (see above).'}
        </div>
      )}

      {rec.phase !== 'fixed' && !d.ok && (
        <div style={{ marginTop: 10 }}>
          <div style={{ fontSize: 11, color: T.textMuted, lineHeight: 1.5 }}>
            Repair treats the parent config as source of truth: broken back-links are rewritten,
            unregistered children re-registered, registry orphans get their parent link cleared
            {prune ? ', and entries whose folder is gone are pruned' : ''}.
            {issues.some(c => c.status === 'missing_c3') &&
              ' Missing .c3 is not repairable by reconcile — re-designate the folder or run Cascade update.'}
          </div>
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 8 }}>
            <Btn onClick={onRepair} disabled={busy || !repairable} style={{ padding: '6px 14px' }}>
              {busy ? 'Repairing…' : (prune ? 'Repair + prune' : 'Repair')}
            </Btn>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Sub-projects tab ───────────────────────────────────────────
function DrillSubprojects({ project, onChanged, onOpenModal }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [rec, setRec] = useState(null);            // {result, phase: 'dry'|'fixed'}
  const [recBusy, setRecBusy] = useState(false);
  const [cascOpen, setCascOpen] = useState(false); // launcher visible
  const [cascOp, setCascOp] = useState('update');
  const [includeParent, setIncludeParent] = useState(false);
  const [cascBusy, setCascBusy] = useState(false);
  const [cascStatus, setCascStatus] = useState(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  const load = useCallback(async () => {
    setErr(null);
    try {
      setData(await api.get('/api/projects/subprojects?parent=' + encodeURIComponent(project.path)));
    } catch (e) { setErr(e.message); }
  }, [project.path]);

  useEffect(() => { setData(null); setRec(null); load(); }, [load]);
  usePoll(load, 10000);  // picks up designations made via the modal + external changes

  const mutated = () => { load(); if (onChanged) onChanged(); };

  // ── Reconcile: dry-run inline, explicit Repair after ─────────
  const runReconcile = async () => {
    if (recBusy) return;
    setRecBusy(true);
    try {
      const d = await api.post('/api/projects/subprojects/reconcile', { parent: project.path });
      setRec({ result: d, phase: 'dry' });
    } catch (e) { notify('Reconcile: ' + e.message, 'err'); }
    setRecBusy(false);
  };

  const repair = async () => {
    if (recBusy || !rec) return;
    const prune = (rec.result.children || []).some(c => c.status === 'missing_folder');
    setRecBusy(true);
    try {
      const d = await api.post('/api/projects/subprojects/reconcile',
        { parent: project.path, fix: true, prune });
      setRec({ result: d, phase: 'fixed' });
      const fixed = (d.fixed || []).length;
      const pruned = (d.pruned || []).length;
      notify(`Reconcile: ${fixed} repaired${pruned ? `, ${pruned} pruned` : ''}${d.ok ? '' : ' — issues remain'}`,
        d.ok ? 'ok' : 'warn');
      mutated();
    } catch (e) { notify('Reconcile: ' + e.message, 'err'); }
    setRecBusy(false);
  };

  // ── Cascade: launch + inline status poll (hub-global singleton) ──
  const pollCascade = async (tries) => {
    if (!aliveRef.current) return;
    let st;
    try { st = await api.get('/api/projects/subprojects/cascade/status'); }
    catch (e) {
      if (aliveRef.current) { setCascStatus(null); notify('Cascade: ' + e.message, 'err'); }
      return;
    }
    if (!aliveRef.current) return;
    setCascStatus(st);
    if (st.done) {
      const results = st.results || [];
      const ok = results.filter(r => r.success).length;
      notify(`Cascade ${st.op || ''}: ${ok}/${results.length} succeeded`,
        ok === results.length ? 'ok' : 'warn');
      mutated();
      return;
    }
    if (!st.running) { setCascStatus(null); return; }  // idle state — nothing to watch
    if (tries > 600) { notify('Cascade: timed out waiting for status', 'err'); return; }
    setTimeout(() => pollCascade(tries + 1), 1500);
  };

  // A cascade may already be running (started from a card or another tab).
  useEffect(() => {
    api.get('/api/projects/subprojects/cascade/status')
      .then(st => {
        if (st.running && aliveRef.current) {
          setCascStatus(st);
          setTimeout(() => pollCascade(0), 1500);
        }
      })
      .catch(() => {});
  }, [project.path]);

  const startCascade = async (affectedTotal) => {
    if (cascBusy) return;
    setCascBusy(true);
    try {
      const resp = await api.post('/api/projects/subprojects/cascade',
        { parent: project.path, op: cascOp, include_parent: includeParent });
      setCascOpen(false);
      setCascStatus({
        running: true, op: cascOp, parent: project.path, results: [],
        total: (resp && resp.total) || affectedTotal,
      });
      setTimeout(() => pollCascade(0), 800);
    } catch (e) {
      if (e.status === 409) {
        notify('Another cascade is already running (one at a time, hub-wide) — showing its progress', 'warn');
        setCascOpen(false);
        setTimeout(() => pollCascade(0), 200);
      } else {
        notify('Cascade: ' + e.message, 'err');
      }
    }
    setCascBusy(false);
  };

  const cancelCascade = async () => {
    try {
      const d = await api.post('/api/projects/subprojects/cascade/cancel', {});
      notify(d.cancelled ? 'Cascade cancelled — current child finishes first'
        : (d.message || 'No cascade in progress'), d.cancelled ? 'warn' : 'ok');
    } catch (e) { notify('Cancel: ' + e.message, 'err'); }
  };

  // ── Render ───────────────────────────────────────────────────
  if (project.parent_path) {
    return <DrillMsg text="This project is itself a sub-project — nesting is depth-1." />;
  }
  if (err) return <DrillMsg text={'Failed to load sub-projects: ' + err} color={T.error} />;
  if (!data) return <DrillMsg text="Loading sub-projects…" />;

  const children = data.children || [];
  const parentName = (data.parent && data.parent.name) || project.name || project.path;
  const rollup = data.rollup;
  const designate = () => onOpenModal('folderPick', project);

  if (children.length === 0) {
    return (
      <DrillCenter>
        <I name="gitBranch" size={22} color={T.textDim} />
        <div style={{ fontSize: 13, fontWeight: 700, color: T.text }}>No sub-projects yet</div>
        <div style={{ fontSize: 12, color: T.textMuted, maxWidth: 380, lineHeight: 1.5 }}>
          A sub-project is a folder inside this project designated as a linked child
          workspace: it gets its own <span className="mono">.c3</span> index and memory,
          is excluded from the parent's index, and rolls its alerts and status up here.
          Links are depth-1 — children cannot have children of their own.
        </div>
        <Btn onClick={designate}>
          <I name="folderOpen" size={13} color={T.bg} /> Designate sub-project…
        </Btn>
      </DrillCenter>
    );
  }

  const cascActive = cascStatus && !cascStatus.done;
  const cascResults = (cascStatus && cascStatus.results) || [];
  const cascForeign = cascStatus && cascStatus.parent && project.path
    && String(cascStatus.parent).toLowerCase() !== String(project.path).toLowerCase();
  const affected = children.filter(c =>
    c.status !== 'missing_folder' && (cascOp === 'update' || c.status !== 'missing_c3'));
  const skipped = children.length - affected.length;
  const affectedTotal = affected.length + (includeParent ? 1 : 0);

  return (
    <div className="fade-up">
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <Btn onClick={designate} style={{ padding: '7px 14px' }}>
          <I name="folderOpen" size={13} color={T.bg} /> Designate sub-project…
        </Btn>
        <Btn variant="ghost" onClick={runReconcile} disabled={recBusy} style={{ padding: '7px 14px' }}>
          <I name="wrench" size={13} color={T.textMuted} /> {recBusy && !rec ? 'Checking…' : 'Reconcile'}
        </Btn>
        <Btn variant="ghost" onClick={() => setCascOpen(o => !o)} disabled={!!cascActive}
          style={{ padding: '7px 14px' }}>
          <I name="refresh" size={13} color={T.textMuted} /> Cascade…
        </Btn>
      </div>

      {cascOpen && !cascActive && (
        <div className="fade-up" style={{
          marginTop: 14, border: `1px solid ${T.border}`, borderRadius: 8,
          padding: 14, background: T.surfaceAlt,
        }}>
          <div style={{
            fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 1,
            color: T.textMuted, marginBottom: 10,
          }}>Cascade across sub-projects</div>
          <select value={cascOp} onChange={e => setCascOp(e.target.value)}
            style={drillFieldStyle({ cursor: 'pointer', width: '100%' })}>
            {SUB_CASCADE_OPS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
          {renderBoolToggle('Include parent', includeParent, () => setIncludeParent(v => !v),
            'Run the same operation on this parent project after the children')}
          <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 6 }}>
            Affects {affectedTotal} project{affectedTotal === 1 ? '' : 's'}:
          </div>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {affected.map(c => <Badge key={c.path} color={T.blue}>{c.name}</Badge>)}
            {includeParent && <Badge color={T.purple}>{parentName} (parent)</Badge>}
          </div>
          {skipped > 0 && (
            <div style={{ fontSize: 11, color: T.warn, marginTop: 8 }}>
              {skipped} child{skipped === 1 ? '' : 'ren'} skipped (missing folder
              {cascOp !== 'update' ? ' or missing .c3' : ''}).
            </div>
          )}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 12 }}>
            <Btn variant="ghost" onClick={() => setCascOpen(false)} style={{ padding: '6px 14px' }}>Close</Btn>
            <Btn onClick={() => startCascade(affectedTotal)} disabled={cascBusy || affectedTotal === 0}
              style={{ padding: '6px 14px' }}>
              {cascBusy ? 'Starting…' : 'Start cascade'}
            </Btn>
          </div>
        </div>
      )}

      {cascStatus && (
        <div className="fade-up" style={{
          marginTop: 14, border: `1px solid ${T.border}`, borderRadius: 8,
          padding: 14, background: T.surfaceAlt,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
            <I name="refresh" size={13} color={cascStatus.done ? T.textMuted : T.accent} />
            <span style={{
              flex: 1, fontSize: 11, fontWeight: 700, textTransform: 'uppercase',
              letterSpacing: 1, color: T.textMuted,
            }}>
              Cascade — {subCascadeOpLabel(cascStatus.op)}
              {cascForeign ? ` (running for ${cascStatus.parent})` : ''}
            </span>
            {!cascStatus.done && (
              <Btn variant="ghost" onClick={cancelCascade} style={{ padding: '4px 10px', fontSize: 11 }}>
                Cancel
              </Btn>
            )}
            {cascStatus.done && (
              <button onClick={() => setCascStatus(null)} title="Dismiss" style={{
                background: 'none', border: 'none', cursor: 'pointer', padding: 4, display: 'flex',
              }}>
                <I name="xSmall" size={12} color={T.textMuted} />
              </button>
            )}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <ProgressBar value={cascResults.length} max={cascStatus.total || 1} />
            <span className="mono" style={{ fontSize: 11, color: T.textMuted, flexShrink: 0 }}>
              {cascResults.length}/{cascStatus.total || '?'}
            </span>
          </div>
          <div style={{ fontSize: 11, color: T.textMuted, marginTop: 6 }}>
            {cascStatus.done
              ? (cascStatus.cancelled ? 'Cancelled.' : 'Done.')
              : cascStatus.current ? `Running ${cascStatus.current}…` : 'Starting…'}
          </div>
          {cascResults.map(r => (
            <div key={r.path} style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '5px 0',
              borderTop: `1px solid ${T.border}40`, fontSize: 12, minWidth: 0,
            }}>
              <I name={r.success ? 'check' : 'xCircle'} size={12} color={r.success ? T.accent : T.error} />
              <span style={{ color: T.text, fontWeight: 600, flexShrink: 0 }}>{r.name || r.path}</span>
              <span className="mono" title={r.output} style={{
                flex: 1, minWidth: 0, color: T.textMuted, fontSize: 11,
                whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
              }}>{String(r.output || '').slice(0, 120)}</span>
              {r.elapsed_ms != null &&
                <span className="mono" style={{ fontSize: 11, color: T.textDim, flexShrink: 0 }}>
                  {(r.elapsed_ms / 1000).toFixed(1)}s
                </span>}
            </div>
          ))}
        </div>
      )}

      {rec && (
        <SubReconcilePanel rec={rec} busy={recBusy}
          onRepair={repair} onDismiss={() => setRec(null)} />
      )}

      <DrillSection label={`Sub-projects (${children.length})`}>
        {children.map(c => (
          <SubChildRow key={c.path || c.rel_path} parent={project.path}
            parentName={parentName} child={c} onMutated={mutated} />
        ))}
        {rollup && (
          <div className="mono" style={{ fontSize: 11, color: T.textMuted, paddingTop: 10 }}>
            {rollup.children} sub-project{rollup.children === 1 ? '' : 's'}
            {' · '}{rollup.notifications} alert{rollup.notifications === 1 ? '' : 's'}
            {' · '}{rollup.issues} issue{rollup.issues === 1 ? '' : 's'}
          </div>
        )}
      </DrillSection>
    </div>
  );
}