// ─── Hub modals ─────────────────────────────────────────────────
// HubModals dispatches App's {name, project, props} to the modal
// components below. Semantics ported from the legacy cli/hub.html;
// AddProjectModal lives in add_project.js, SettingsModal in
// settings_modal.js (both resolve at render time in the bundle).

// Generic centered modal shell: backdrop click closes.
function Modal({ title, width = 480, onClose, children }) {
  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, background: '#00000090', zIndex: 300,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <div onClick={e => e.stopPropagation()} className="fade-up" style={{
        background: T.surface, border: `1px solid ${T.border}`, borderRadius: 10,
        padding: 24, width, maxWidth: '92vw', maxHeight: '86vh', overflowY: 'auto',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: T.text }}>{title}</div>
          <button onClick={onClose} style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 4, display: 'flex' }}>
            <I name="xSmall" size={14} color={T.textMuted} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

// Shared field helpers (functions/components so theme switches apply live).
const mdlInputStyle = () => ({
  width: '100%', boxSizing: 'border-box', padding: '9px 12px', borderRadius: 8,
  border: `1px solid ${T.border}`, background: T.surfaceAlt, color: T.text,
  fontSize: 13, outline: 'none', fontFamily: 'inherit',
});
const MdlLabel = ({ children }) => (
  <div style={{
    fontSize: 11, fontWeight: 600, color: T.textMuted, textTransform: 'uppercase',
    letterSpacing: 1, margin: '12px 0 6px',
  }}>{children}</div>
);
const MdlPath = ({ children }) => (
  <div className="mono" style={{ fontSize: 11, color: T.textDim, wordBreak: 'break-all', marginBottom: 4 }}>{children}</div>
);
const MdlFooter = ({ children }) => (
  <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 20 }}>{children}</div>
);

// ── Dispatcher ─────────────────────────────────────────────────
function HubModals({ modal, projects, onClose, onChanged }) {
  if (!modal || !modal.name) return null;
  const { name, project } = modal;
  const props = modal.props || {};
  switch (name) {
    case 'add': return <AddProjectModal onClose={onClose} onChanged={onChanged} {...props} />;
    case 'edit': return <EditMetaModal project={project} onClose={onClose} onChanged={onChanged} {...props} />;
    case 'transfer': return <TransferModal project={project} onClose={onClose} onChanged={onChanged} {...props} />;
    case 'merge': return <MergeModal project={project} projects={projects} onClose={onClose} onChanged={onChanged} {...props} />;
    case 'ide': return <IdePickerModal project={project} onClose={onClose} onChanged={onChanged} {...props} />;
    case 'folderPick': return <FolderPickerModal project={project} onClose={onClose} onChanged={onChanged} {...props} />;
    case 'linkPath': return <LinkProjectModal project={project} onClose={onClose} onChanged={onChanged} {...props} />;
    case 'makeSub': return <MakeSubprojectModal project={project} projects={projects} onClose={onClose} onChanged={onChanged} {...props} />;
    case 'reparent': return <ReparentModal project={project} projects={projects} onClose={onClose} onChanged={onChanged} {...props} />;
    case 'batch': return <BatchUpdateModal projects={projects} onClose={onClose} onChanged={onChanged} {...props} />;
    case 'settings': return <SettingsModal onClose={onClose} onChanged={onChanged} {...props} />;
    default: return null;
  }
}

// ── Make sub-project of… ───────────────────────────────────────
// Reverse designate: pick a registered parent and link this project
// under it via the same validate/add endpoints. Since 2.96 the parent
// need not contain this project on disk, and need not be top-level --
// so any registered project except this one and its own descendants is
// a candidate. validate() has the last word on cycles.
function MakeSubprojectModal({ project, projects, onClose, onChanged }) {
  const norm = (s) => (s || '').replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
  const self = norm(project.path);
  // Exclude descendants: linking this project under one of its own children
  // would close a loop. Walk up from each candidate to see if we are above it.
  const byPath = {};
  (projects || []).forEach(c => { byPath[norm(c.path)] = c; });
  const isDescendant = (c) => {
    const seen = new Set();
    let cursor = c;
    while (cursor && cursor.parent_path) {
      const key = norm(cursor.parent_path);
      if (key === self) return true;
      if (seen.has(key)) break;
      seen.add(key);
      cursor = byPath[key];
    }
    return false;
  };
  const candidates = (projects || []).filter(c => {
    const cp = norm(c.path);
    return cp && cp !== self && !isDescendant(c);
  });
  const [selected, setSelected] = useState(candidates.length === 1 ? candidates[0].path : '');
  const [validation, setValidation] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!selected) { setValidation(null); return; }
    let live = true;
    setValidation({ pending: true });
    api.post('/api/projects/subprojects/validate', { parent: selected, folder: project.path })
      .then(v => { if (live) setValidation(v); })
      .catch(e => { if (live) setValidation({ ok: false, warnings: [e.message] }); });
    return () => { live = false; };
  }, [selected]);

  const designate = async () => {
    setBusy(true);
    try {
      const d = await api.post('/api/projects/subprojects/add',
        { parent: selected, folder: project.path, name: project.name });
      const res = d.result || d;
      const parentName = (candidates.find(c => c.path === selected) || {}).name || selected;
      notify(`${res.adopted ? 'Adopted' : 'Linked'} ${project.name} under ${parentName}`);
      onChanged(); onClose();
    } catch (e) { notify('Designate: ' + e.message, 'err'); }
    setBusy(false);
  };

  return (
    <Modal title={`Make sub-project of — ${project.name || ''}`} width={520} onClose={onClose}>
      <MdlPath>{project.path}</MdlPath>
      {candidates.length === 0 ? (
        <div style={{ fontSize: 12.5, color: T.textMuted, lineHeight: 1.6, margin: '10px 0' }}>
          No registered project contains this folder. A sub-project must live
          physically inside its parent — move the folder under the intended
          parent (then designate from the parent's card), or register the
          containing folder as a project first.
        </div>
      ) : (
        <React.Fragment>
          <MdlLabel>Parent project</MdlLabel>
          <select value={selected} onChange={e => setSelected(e.target.value)} style={mdlInputStyle()}>
            <option value="">— choose —</option>
            {candidates.map(c => <option key={c.path} value={c.path}>{c.name} — {c.path}</option>)}
          </select>
          {validation && !validation.pending && (
            <div style={{ marginTop: 10, fontSize: 12, lineHeight: 1.5 }}>
              {(validation.warnings || []).map((w, i) => (
                <div key={i} style={{ color: T.warn }}>⚠ {w}</div>
              ))}
              {validation.ok && (
                <div style={{ color: T.accent }}>✓ Valid — existing .c3 and hub registration are kept and re-linked.</div>
              )}
            </div>
          )}
        </React.Fragment>
      )}
      <MdlFooter>
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
        <Btn disabled={!selected || busy || !(validation && validation.ok)} onClick={designate}>
          {busy ? 'Linking…' : 'Make sub-project'}
        </Btn>
      </MdlFooter>
    </Modal>
  );
}

// ── Change parent (re-parent) ──────────────────────────────────
// Staged wizard. A sub-project's folder MUST physically live inside
// its parent (services/subprojects.py validate() enforces strict
// containment), so re-parenting can never be one click. Order is
// unlink → [manual move] → verify → link → cleanup, with the unlink
// FIRST while the folder is still at its registered location: `sub
// remove` clears the child's .c3 back-link only when the folder
// exists there, and a stale back-link makes the new-parent add fail
// with "already a sub-project of <old>" — which no hub endpoint can
// repair (config PUT refuses the `parent` section). The intermediate
// state is valid and reversible: top-level, still registered, .c3
// intact; Undo re-links under the old parent until the folder moves.
// Unlink:  POST /api/projects/subprojects/remove {parent, ref, mode:'unlink'}
// Verify:  POST /api/projects/subprojects/validate {parent, folder}
// Link:    POST /api/projects/subprojects/add {parent, folder, name}
// Cleanup: POST /api/projects/remove {path} — after a physical move,
// add_project appends a NEW registry row for the new path (rows are
// keyed by exact path, never migrated), so the old row must go.
function ReparentModal({ project, projects, onClose, onChanged }) {
  const norm = (s) => String(s || '').replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
  const self = norm(project.path);
  const oldParentPath = project.parent_path || '';
  const oldParentName = ((projects || []).find(c => norm(c.path) === norm(oldParentPath)) || {}).name || oldParentPath;
  const folderName = (project.path || '').split(/[\\/]/).filter(Boolean).pop() || project.name;

  // Candidate new parents: registered, not this project, not the current
  // parent, and not one of this project's own descendants (that would close
  // a loop). A candidate may itself be a child -- depth is no longer capped
  // at one -- and it need not contain this project on disk.
  //
  // Since 2.96 `ready` is always true: a child can be addressed by absolute
  // path, so re-parenting is a config write and no folder ever moves. The
  // wizard's move and cleanup stages fall away on their own.
  const byPath = {};
  (projects || []).forEach(c => { byPath[norm(c.path)] = c; });
  const isDescendant = (c) => {
    const seen = new Set();
    let cursor = c;
    while (cursor && cursor.parent_path) {
      const key = norm(cursor.parent_path);
      if (key === self) return true;
      if (seen.has(key)) break;
      seen.add(key);
      cursor = byPath[key];
    }
    return false;
  };
  const candidates = (projects || []).filter(c => {
    const cp = norm(c.path);
    return cp && cp !== self && cp !== norm(oldParentPath)
      && !cp.startsWith(self + '\\') && !isDescendant(c);
  }).map(c => ({ ...c, ready: true, destination: project.path }));

  const [target, setTarget] = useState(null);      // chosen candidate (+ ready/destination)
  const [phase, setPhase] = useState('choose');    // choose | run
  const [steps, setSteps] = useState([]);          // [{id, label, status, detail}]
  const [outcome, setOutcome] = useState(null);    // {kind: done|warn|stopped, message}
  const [busy, setBusy] = useState(false);         // a request is in flight
  const mutatedRef = useRef(false);

  const setStep = (id, patch) => setSteps(s => s.map(st => (st.id === id ? { ...st, ...patch } : st)));
  const stepById = (id) => steps.find(st => st.id === id);
  // apiErr (cli/ui/api.js) digs result.error out of 500 bodies.
  const finish = (kind, message) => {
    setOutcome({ kind, message });
    if (mutatedRef.current) onChanged();
  };

  const doUnlink = async () => {
    setBusy(true);
    setStep('unlink', { status: 'running', detail: '' });
    try {
      const d = await api.post('/api/projects/subprojects/remove',
        { parent: oldParentPath, ref: project.path, mode: 'unlink' });
      const res = (d && d.result) || {};
      if (!d.success || !res.removed) throw new Error(res.error || 'unlink failed');
      mutatedRef.current = true;
      setStep('unlink', { status: 'ok', detail: 'Now top-level: still registered, .c3 intact.' });
      setBusy(false);
      return true;
    } catch (e) {
      setStep('unlink', { status: 'fail', detail: apiErr(e) });
      finish('stopped', `Stopped at unlink. If nothing was removed, ${project.name} is still linked under ` +
        `${oldParentName} exactly as before. If the failure looks partial, run "Reconcile links" on ` +
        `${oldParentName}'s card to check and repair.`);
      setBusy(false);
      return false;
    }
  };

  const doLink = async (folder) => {
    setBusy(true);
    setStep('link', { status: 'running', detail: 'Adopting the existing .c3 (no re-init)…' });
    try {
      const d = await api.post('/api/projects/subprojects/add',
        { parent: target.path, folder, name: project.name });
      const res = (d && d.result) || {};
      if (!d.success || !res.added) throw new Error(res.error || 'link failed');
      mutatedRef.current = true;
      setStep('link', { status: 'ok', detail: `${res.adopted ? 'Adopted' : 'Linked'} under ${target.name}.` });
      setBusy(false);
      return true;
    } catch (e) {
      setStep('link', { status: 'fail', detail: apiErr(e) });
      finish('stopped', target.ready
        ? `Current state: ${project.name} is a TOP-LEVEL project — still registered, .c3 intact, not linked ` +
          `under any parent. Nothing is lost. Retry the link below, or Undo to re-link under ${oldParentName}.`
        : `Current state: the folder lives at ${target.destination} with its .c3 intact, but is NOT linked ` +
          `under ${target.name}, and the hub registry still has the old-path row. Nothing is lost — retry the ` +
          `link below, or finish later via "Designate sub-project…" on ${target.name}'s card.`);
      setBusy(false);
      return false;
    }
  };

  // Old registry row: add_project never migrates it, so remove explicitly.
  const doCleanup = async () => {
    setBusy(true);
    setStep('cleanup', { status: 'running', detail: '' });
    let ok = true;
    try {
      const d = await api.post('/api/projects/remove', { path: project.path });
      setStep('cleanup', { status: 'ok', detail: d.removed
        ? `Removed the stale registry row for ${project.path}.`
        : 'No stale row found — registry already clean.' });
    } catch (e) {
      ok = false;
      setStep('cleanup', { status: 'fail', detail: apiErr(e) });
    }
    setBusy(false);
    return ok;
  };

  const linkAndCleanup = async () => {
    setOutcome(null);
    if (!await doLink(target.destination)) return;
    if (await doCleanup()) {
      finish('done', `${project.name} is now a sub-project of ${target.name} at its new location.`);
    } else {
      finish('warn', `Re-parent complete, but the stale registry row for the old path (${project.path}) ` +
        `could not be removed — remove that card from the hub manually.`);
    }
  };

  const runReadyLink = async () => {
    setOutcome(null);
    if (await doLink(project.path)) {
      finish('done', `${project.name} is now a sub-project of ${target.name}. No files moved.`);
    }
  };

  // Verify the manual move — read-only until it passes; nothing further
  // is touched while the folder is not confirmed at the destination.
  const recheck = async () => {
    setBusy(true);
    setStep('move', { status: 'waiting', detail: 'Checking…' });
    try {
      const v = await api.post('/api/projects/subprojects/validate',
        { parent: target.path, folder: target.destination });
      if (v.ok) {
        setStep('move', { status: 'ok', detail: `Verified — folder present and linkable at ${target.destination}.` });
        setBusy(false);
        await linkAndCleanup();
        return;
      }
      if (!v.is_dir) {
        setStep('move', { status: 'waiting', detail: 'Not there yet — no folder at the destination. Move it, then Re-check.' });
      } else if (v.already_child_of) {
        setStep('move', { status: 'waiting', detail:
          `The folder at the destination still back-links ${v.already_child_of} in its .c3/config.json ` +
          `(it was moved before the unlink step could clear the link). Delete the "parent" key from that ` +
          `file by hand, then Re-check. Nothing has been changed at the destination.` });
      } else {
        setStep('move', { status: 'waiting', detail:
          'Validation failed: ' + ((v.warnings || []).join('; ') || 'unknown reason') + '. Fix and Re-check.' });
      }
    } catch (e) {
      setStep('move', { status: 'waiting', detail: 'Re-check failed: ' + apiErr(e) + '. Try again.' });
    }
    setBusy(false);
  };

  // Undo — valid only while the folder is still at its old path.
  const undoRelink = async () => {
    setBusy(true);
    try {
      const d = await api.post('/api/projects/subprojects/add',
        { parent: oldParentPath, folder: project.path, name: project.name });
      const res = (d && d.result) || {};
      if (!d.success || !res.added) throw new Error(res.error || 'relink failed');
      if (stepById('move')) setStep('move', { status: 'skip', detail: 'Cancelled — the folder was not moved.' });
      setStep('link', { status: 'skip', detail: '' });
      finish('stopped', `Undone: ${project.name} is linked under ${oldParentName} again, exactly as before.`);
    } catch (e) {
      notify('Undo failed: ' + apiErr(e), 'err');
    }
    setBusy(false);
  };

  const start = async () => {
    if (!target) return;
    mutatedRef.current = false;
    setOutcome(null);
    setSteps([
      { id: 'unlink', label: `Unlink from ${oldParentName}`, status: 'pending', detail: '' },
      ...(target.ready ? [] : [{ id: 'move', label: 'Move the folder (manual)', status: 'pending', detail: '' }]),
      { id: 'link', label: `Link under ${target.name}`, status: 'pending', detail: '' },
      ...(target.ready ? [] : [{ id: 'cleanup', label: 'Remove stale registry row (old path)', status: 'pending', detail: '' }]),
    ]);
    setPhase('run');
    if (!await doUnlink()) return;
    if (target.ready) {
      await runReadyLink();
    } else {
      onChanged();   // hub now truthfully shows it as top-level while we wait
      setStep('move', { status: 'waiting', detail: '' });
    }
  };

  const safeClose = () => { if (!busy) onClose(); };
  const linkFailed = (stepById('link') || {}).status === 'fail';
  const waitingMove = (stepById('move') || {}).status === 'waiting';
  const stStatusColor = (st) =>
    st === 'ok' ? T.accent : st === 'fail' ? T.error : st === 'running' || st === 'waiting' ? T.warn : T.textDim;

  return (
    <Modal title={`Change parent — ${project.name || ''}`} width={560} onClose={safeClose}>
      <MdlPath>{project.path}</MdlPath>
      <div style={{ fontSize: 12, color: T.textMuted, marginBottom: 4 }}>
        Current parent: <span style={{ color: T.text, fontWeight: 600 }}>{oldParentName || '—'}</span>
      </div>

      {!oldParentPath && (
        <div style={{ fontSize: 12.5, color: T.textMuted, lineHeight: 1.6, margin: '10px 0' }}>
          This project has no parent — use "Make sub-project of…" to link it under one.
        </div>
      )}

      {oldParentPath && phase === 'choose' && (candidates.length === 0 ? (
        <div style={{ fontSize: 12.5, color: T.textMuted, lineHeight: 1.6, margin: '10px 0' }}>
          No eligible new parent. Candidates must be registered, top-level (not themselves
          sub-projects), and not this project or its current parent.
        </div>
      ) : (
        <React.Fragment>
          <MdlLabel>New parent</MdlLabel>
          <div style={{ border: `1px solid ${T.border}`, borderRadius: 8, overflowY: 'auto', maxHeight: 220, background: T.surfaceAlt }}>
            {candidates.map(c => {
              const sel = target && target.path === c.path;
              return (
                <div key={c.path} onClick={() => setTarget(c)} style={{
                  padding: '8px 10px', cursor: 'pointer', borderBottom: `1px solid ${T.border}40`,
                  background: sel ? T.accentDim : 'transparent',
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <I name="gitBranch" size={13} color={sel ? T.accent : T.textMuted} />
                    <span style={{ fontSize: 13, color: sel ? T.accent : T.text, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {c.name}
                    </span>
                    <Badge color={c.ready ? T.accent : T.warn}>{c.ready ? 'ready now' : 'move needed'}</Badge>
                  </div>
                  <div className="mono" style={{ fontSize: 11, color: T.textDim, marginTop: 2, paddingLeft: 21, wordBreak: 'break-all' }}>
                    {c.path}
                  </div>
                </div>
              );
            })}
          </div>
          {target && (
            <div style={{ marginTop: 12, fontSize: 12, color: T.textMuted, lineHeight: 1.6 }}>
              <div style={{ color: T.text, fontWeight: 600 }}>Plan</div>
              {target.ready ? (
                <React.Fragment>
                  <div>1. Unlink from {oldParentName} &nbsp; 2. Link under {target.name}</div>
                  <div>Re-parenting is a config change — no files move.</div>
                </React.Fragment>
              ) : (
                <React.Fragment>
                  <div>1. Unlink from {oldParentName} (runs immediately)</div>
                  <div>2. You move the folder to the required path — the hub never moves files</div>
                  <div>3. Re-check verifies the destination, then links under {target.name}</div>
                  <div>4. Remove the stale registry row for the old path</div>
                  <div style={{ color: T.warn, marginTop: 6 }}>
                    The unlink must run before the move: clearing the child's back-link is only possible
                    while the folder is at its current path (a stale back-link would block the new link).
                    Between steps, {project.name} is simply a top-level registered project — nothing
                    breaks, and Undo can re-link it under {oldParentName} until the folder is moved.
                  </div>
                </React.Fragment>
              )}
            </div>
          )}
        </React.Fragment>
      ))}

      {phase === 'run' && (
        <div style={{ border: `1px solid ${T.border}`, borderRadius: 8, background: T.surfaceAlt, marginTop: 4 }}>
          {steps.map(st => (
            <div key={st.id} style={{ padding: '8px 10px', borderBottom: `1px solid ${T.border}40` }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                {st.status === 'ok' && <I name="check" size={13} color={T.accent} />}
                {st.status === 'fail' && <I name="xCircle" size={13} color={T.error} />}
                {st.status === 'waiting' && <I name="alertTriangle" size={13} color={T.warn} />}
                {st.status === 'running' && <GlowDot color={T.warn} />}
                {(st.status === 'pending' || st.status === 'skip') && <GlowDot color={T.textDim} />}
                <span style={{ fontSize: 13, color: T.text, flex: 1 }}>{st.label}</span>
                <span className="mono" style={{ fontSize: 11, color: stStatusColor(st.status) }}>
                  {st.status === 'running' ? 'running…' : st.status === 'waiting' ? 'action needed' : st.status === 'skip' ? 'skipped' : st.status}
                </span>
              </div>
              {st.detail && (
                <div style={{ fontSize: 11, color: st.status === 'fail' ? T.error : T.textMuted, marginTop: 3, paddingLeft: 21, lineHeight: 1.5, wordBreak: 'break-word' }}>
                  {st.detail}
                </div>
              )}
              {st.id === 'move' && st.status === 'waiting' && (
                <div style={{ paddingLeft: 21, marginTop: 6 }}>
                  <div className="mono" style={{ fontSize: 11, color: T.text, wordBreak: 'break-all' }}>{project.path}</div>
                  <div className="mono" style={{ fontSize: 11, color: T.textDim, wordBreak: 'break-all' }}>→ {target.destination}</div>
                  <div style={{ fontSize: 11, color: T.textMuted, marginTop: 4, lineHeight: 1.5 }}>
                    Move the folder yourself (File Explorer / terminal — the hub does not move files),
                    then click Re-check. Until the folder is verified at the destination, nothing further
                    is touched — {project.name} stays a valid top-level project. You can also close this
                    wizard and finish later via "Designate sub-project…" on {target.name}'s card.
                  </div>
                  <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                    <Btn onClick={recheck} disabled={busy}>{busy ? 'Checking…' : 'Re-check'}</Btn>
                    <Btn variant="ghost" onClick={undoRelink} disabled={busy}>Undo — re-link under {oldParentName}</Btn>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {outcome && (
        <div style={{
          marginTop: 12, padding: '10px 12px', borderRadius: 8, fontSize: 12, lineHeight: 1.6,
          border: `1px solid ${outcome.kind === 'done' ? T.accent : outcome.kind === 'warn' ? T.warn : T.error}50`,
          background: outcome.kind === 'done' ? T.accentDim : outcome.kind === 'warn' ? T.warnDim : T.errorDim,
          color: outcome.kind === 'done' ? T.accent : outcome.kind === 'warn' ? T.warn : T.error,
        }}>
          {outcome.message}
        </div>
      )}

      <MdlFooter>
        {phase === 'choose' && <Btn variant="ghost" onClick={onClose}>Cancel</Btn>}
        {phase === 'choose' && (
          <Btn onClick={start} disabled={!target || !oldParentPath}>Start re-parent</Btn>
        )}
        {phase === 'run' && linkFailed && !waitingMove && (
          <Btn onClick={target && target.ready ? runReadyLink : linkAndCleanup} disabled={busy}>Retry link</Btn>
        )}
        {phase === 'run' && linkFailed && target && target.ready && (
          <Btn variant="ghost" onClick={undoRelink} disabled={busy}>Undo — re-link under {oldParentName}</Btn>
        )}
        {phase === 'run' && <Btn variant="ghost" onClick={safeClose} disabled={busy}>Close</Btn>}
      </MdlFooter>
    </Modal>
  );
}

// ── IDE picker ─────────────────────────────────────────────────
// POST /api/projects/launch-ide {path, ide, custom_cmd?}
function IdePickerModal({ project, onClose, onChanged }) {
  const known = IDE_OPTIONS.some(o => o.id === (project && project.ide));
  const [selected, setSelected] = useState(known ? project.ide : 'claude-code');
  const [customCmd, setCustomCmd] = useState('');
  const [busy, setBusy] = useState(false);

  const launch = async () => {
    if (selected === 'custom' && !customCmd.trim()) {
      notify('Enter a custom command first.', 'err');
      return;
    }
    setBusy(true);
    try {
      await api.post('/api/projects/launch-ide', {
        path: project.path, ide: selected,
        custom_cmd: selected === 'custom' ? customCmd.trim() : '',
      });
      notify(`Launched ${ideLabel(selected)} in ${project.name || project.path}`);
      onClose();
    } catch (e) {
      notify(`Launch failed: ${e.message}`, 'err');
    }
    setBusy(false);
  };

  return (
    <Modal title="Open in IDE" width={520} onClose={onClose}>
      <MdlPath>{project.path}</MdlPath>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 8, marginTop: 10 }}>
        {IDE_OPTIONS.map(opt => {
          const sel = selected === opt.id;
          return (
            <div key={opt.id} onClick={() => setSelected(opt.id)} style={{
              display: 'flex', alignItems: 'center', gap: 10, padding: '10px 12px',
              borderRadius: 8, cursor: 'pointer',
              border: `1px solid ${sel ? T.accent : T.border}`,
              background: sel ? T.accentDim : T.surfaceAlt,
            }}>
              <span style={{ fontSize: 16 }}>{opt.icon}</span>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: sel ? T.accent : T.text }}>{opt.name}</div>
                <div className="mono" style={{ fontSize: 11, color: T.textMuted }}>{opt.cmd}</div>
              </div>
            </div>
          );
        })}
      </div>
      {selected === 'custom' && (
        <div>
          <MdlLabel>Custom command</MdlLabel>
          <input value={customCmd} onChange={e => setCustomCmd(e.target.value)}
            placeholder="e.g. nvim ." className="mono" style={mdlInputStyle()} />
        </div>
      )}
      <MdlFooter>
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
        <Btn onClick={launch} disabled={busy}>
          <I name="play" size={12} color={T.bg} />{busy ? 'Launching…' : 'Open in IDE'}
        </Btn>
      </MdlFooter>
    </Modal>
  );
}

// ── Edit metadata ──────────────────────────────────────────────
// POST /api/projects/update {path, name, tags, notes} → {updated}
function EditMetaModal({ project, onClose, onChanged }) {
  const [name, setName] = useState((project && project.name) || '');
  const [tags, setTags] = useState(((project && project.tags) || []).join(', '));
  const [notes, setNotes] = useState((project && project.notes) || '');
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    try {
      const d = await api.post('/api/projects/update', {
        path: project.path, name: name.trim(), tags, notes,
      });
      if (!d.updated) throw new Error('Project not found');
      notify('Project updated');
      onChanged();
      onClose();
    } catch (e) {
      notify(`Update failed: ${e.message}`, 'err');
    }
    setBusy(false);
  };

  return (
    <Modal title="Edit project" width={480} onClose={onClose}>
      <MdlPath>{project.path}</MdlPath>
      <MdlLabel>Name</MdlLabel>
      <input value={name} onChange={e => setName(e.target.value)} style={mdlInputStyle()} />
      <MdlLabel>Tags (comma-separated)</MdlLabel>
      <input value={tags} onChange={e => setTags(e.target.value)}
        placeholder="e.g. work, python, agents/tools" style={mdlInputStyle()} />
      <MdlLabel>Notes</MdlLabel>
      <textarea value={notes} onChange={e => setNotes(e.target.value)} rows={4}
        style={{ ...mdlInputStyle(), resize: 'vertical' }} />
      <MdlFooter>
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
        <Btn onClick={save} disabled={busy}>{busy ? 'Saving…' : 'Save'}</Btn>
      </MdlFooter>
    </Modal>
  );
}

// ── Transfer registration ──────────────────────────────────────
// POST /api/projects/transfer {old_path, new_path} → {transferred}
function TransferModal({ project, onClose, onChanged }) {
  const [newPath, setNewPath] = useState('');
  const [busy, setBusy] = useState(false);

  const save = async () => {
    const np = newPath.trim();
    if (!np) { notify('New path is required', 'err'); return; }
    setBusy(true);
    try {
      const d = await api.post('/api/projects/transfer', {
        old_path: project.path, new_path: np,
      });
      if (d.transferred === false) throw new Error(d.error || 'Transfer failed');
      notify('Project registration transferred');
      onChanged();
      onClose();
    } catch (e) {
      notify(`Transfer failed: ${e.message}`, 'err');
    }
    setBusy(false);
  };

  return (
    <Modal title="Transfer project" width={480} onClose={onClose}>
      <MdlLabel>Current path</MdlLabel>
      <MdlPath>{project.path}</MdlPath>
      <MdlLabel>New path</MdlLabel>
      <input value={newPath} onChange={e => setNewPath(e.target.value)}
        placeholder="Absolute path to the project's new location" className="mono" style={mdlInputStyle()} />
      <div style={{ fontSize: 11, color: T.textMuted, marginTop: 10, lineHeight: 1.5 }}>
        Only the hub registration moves — files are not copied. Use this after
        relocating the project folder yourself (the new path must already contain it).
      </div>
      <MdlFooter>
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
        <Btn onClick={save} disabled={busy}>{busy ? 'Transferring…' : 'Transfer'}</Btn>
      </MdlFooter>
    </Modal>
  );
}

// ── Merge into another project ─────────────────────────────────
// POST /api/projects/merge {source_path, target_path, cleanup} →
// {merged, stats:{facts,sessions,ledger_entries}, warnings}
function MergeModal({ project, projects, onClose, onChanged }) {
  const candidates = (projects || []).filter(p => p.path !== project.path && !p.active);
  const [target, setTarget] = useState(candidates.length ? candidates[0].path : '');
  const [cleanup, setCleanup] = useState('keep');
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const doMerge = async () => {
    setConfirming(false);
    setBusy(true);
    try {
      const d = await api.post('/api/projects/merge', {
        source_path: project.path, target_path: target, cleanup,
      });
      if (!d.merged) throw new Error(d.error || 'Merge did not complete');
      const s = d.stats || {};
      notify(`Merged ${s.facts || 0} facts, ${s.sessions || 0} sessions, ` +
        `${s.ledger_entries || 0} ledger entries` +
        (cleanup === 'clear' ? ' — source cleared' : ''));
      onChanged();
      onClose();
    } catch (e) {
      notify(`Merge failed: ${e.message}`, 'err');
    }
    setBusy(false);
  };

  const start = () => {
    if (!target) { notify('Pick a target project', 'err'); return; }
    if (cleanup === 'clear') setConfirming(true);
    else doMerge();
  };

  const radio = (val, label, desc) => (
    <label key={val} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', padding: '6px 0', cursor: 'pointer' }}>
      <input type="radio" name="merge-cleanup" value={val} checked={cleanup === val}
        onChange={() => setCleanup(val)} style={{ marginTop: 2, accentColor: T.accent }} />
      <span>
        <span style={{ fontSize: 12, color: T.text, fontWeight: 600 }}>{label}</span>
        <span style={{ display: 'block', fontSize: 11, color: T.textMuted }}>{desc}</span>
      </span>
    </label>
  );

  return (
    <Modal title="Merge project" width={520} onClose={onClose}>
      <MdlLabel>Source</MdlLabel>
      <div style={{ fontSize: 13, color: T.text, fontWeight: 600 }}>{project.name}</div>
      <MdlPath>{project.path}</MdlPath>
      {project.active && (
        <div style={{
          display: 'flex', gap: 8, alignItems: 'center', padding: '8px 12px', marginTop: 8,
          borderRadius: 8, border: `1px solid ${T.warn}50`, background: T.warnDim,
          fontSize: 12, color: T.warn,
        }}>
          <I name="alertTriangle" size={13} color={T.warn} />
          Source project has an active session — close it before merging.
        </div>
      )}
      <MdlLabel>Merge into</MdlLabel>
      {candidates.length ? (
        <select value={target} onChange={e => setTarget(e.target.value)} style={mdlInputStyle()}>
          {candidates.map(p => (
            <option key={p.path} value={p.path}>{p.name}  ({p.path})</option>
          ))}
        </select>
      ) : (
        <div style={{ fontSize: 12, color: T.textMuted }}>— No eligible target projects —</div>
      )}
      <MdlLabel>After merging</MdlLabel>
      {radio('keep', 'Keep source', 'Source project stays registered and untouched; its data is copied into the target.')}
      {radio('clear', 'Clear source', 'Delete .c3/, MCP configs and instruction docs from the source after merging.')}
      <div style={{ fontSize: 11, color: T.textMuted, marginTop: 10, lineHeight: 1.5 }}>
        Merging copies memory facts, session history and edit-ledger entries from the
        source into the target project.
      </div>
      <MdlFooter>
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
        <Btn onClick={start} disabled={busy || !candidates.length}>{busy ? 'Merging…' : 'Merge'}</Btn>
      </MdlFooter>
      {confirming && (
        <ConfirmDialog
          title="Clear source after merge?"
          message={`This will permanently delete .c3/, MCP configs and instruction docs from "${project.name}". The merged data will live on inside the target. Continue?`}
          confirmLabel="Merge & clear" danger
          onConfirm={doMerge} onCancel={() => setConfirming(false)} />
      )}
    </Modal>
  );
}

// ── Folder picker (designate a sub-project) ────────────────────
// Browse: POST /api/projects/browse {path}
// Validate: POST /api/projects/subprojects/validate {parent, folder}
// Designate: POST /api/projects/subprojects/add {parent, folder, name?, ide?}
// Navigation is fenced to the parent subtree (crumbs above it are inert).
const _pathCrumbs = (full) => {
  if (!full) return [];
  const sep = full.includes('\\') ? '\\' : '/';
  const parts = full.split(/[\\/]/).filter(s => s !== '');
  return parts.map((label, i) => {
    let p = parts.slice(0, i + 1).join(sep);
    if (full.startsWith('/')) p = '/' + p;
    if (i === 0 && /^[A-Za-z]:$/.test(label)) p += sep;  // drive root ("U:\")
    return { label, path: p };
  });
};

// Legal `c3 sub add --ide` values (cli/commands/parser.py); 'auto' = CLI default.
const _SUB_IDE_OPTIONS = [
  { id: 'auto', label: 'Auto-detect' },
  { id: 'claude', label: 'Claude Code' },
  { id: 'vscode', label: 'VS Code' },
  { id: 'cursor', label: 'Cursor' },
  { id: 'codex', label: 'Codex' },
  { id: 'antigravity', label: 'Antigravity' },
];

function FolderPickerModal({ project, onClose, onChanged }) {
  const parentPath = (project && project.path) || '';
  const [listing, setListing] = useState(null);
  const [browsing, setBrowsing] = useState(false);
  const [selected, setSelected] = useState(null);
  const [validation, setValidation] = useState(null);
  const [validating, setValidating] = useState(false);
  const [name, setName] = useState('');
  const [ide, setIde] = useState('auto');
  const [busy, setBusy] = useState(false);

  // Fence: only the parent project's subtree is browsable.
  const _norm = (p) => String(p || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
  const inParent = (p) => {
    const a = _norm(p), b = _norm(parentPath);
    return !!b && (a === b || a.startsWith(b + '/'));
  };

  const loadDir = async (path) => {
    if (!inParent(path)) path = parentPath;   // clamp any escape to the parent root
    setBrowsing(true);
    setSelected(null); setValidation(null); setName('');
    try {
      setListing(await api.post('/api/projects/browse', { path }));
    } catch (e) {
      notify(`Browse failed: ${e.message}`, 'err');
    }
    setBrowsing(false);
  };

  useEffect(() => { if (parentPath) loadDir(parentPath); }, [parentPath]);

  // Selecting a folder auto-runs validation.
  const select = async (dir) => {
    setSelected(dir); setValidation(null); setName(''); setValidating(true);
    try {
      setValidation(await api.post('/api/projects/subprojects/validate', {
        parent: parentPath, folder: dir.path,
      }));
    } catch (e) {
      setValidation({ ok: false, warnings: [e.message] });
    }
    setValidating(false);
  };

  const designate = async () => {
    if (!selected || !validation || !validation.ok || busy) return;
    setBusy(true);
    notifyProgress('sub-designate', { label: 'Designating…', current: 0, total: 1 });
    try {
      const body = { parent: parentPath, folder: selected.path };
      const nm = name.trim();
      if (nm) body.name = nm;
      if (ide && ide !== 'auto') body.ide = ide;
      const d = await api.post('/api/projects/subprojects/add', body);   // long-running (full init)
      const res = (d && d.result) || {};
      if (!d.success) throw new Error(res.error || 'Designation failed');
      notifyProgress('sub-designate', { label: 'Designating…', current: 1, total: 1, done: true });
      notify(`${res.adopted ? 'Adopted' : 'Initialized'} ${res.name || selected.name} as a sub-project of ${project.name}`);
      onChanged();
      onClose();
    } catch (e) {
      notifyProgress('sub-designate', { label: 'Designating…', current: 0, total: 1, done: true, error: true });
      notify(`Designate failed: ${e.message}`, 'err');
    }
    setBusy(false);
  };

  const crumbs = _pathCrumbs(listing && listing.path);

  return (
    <Modal title={`Designate sub-project — ${project.name || ''}`} width={560} onClose={onClose}>
      {/* Breadcrumb */}
      <div className="mono" style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 2, fontSize: 11, marginBottom: 8 }}>
        {crumbs.map((c, i) => (
          <span key={c.path} style={{ display: 'inline-flex', alignItems: 'center', gap: 2 }}>
            {i > 0 && <span style={{ color: T.textDim }}>/</span>}
            {inParent(c.path) ? (
              <span onClick={() => loadDir(c.path)} style={{
                color: i === crumbs.length - 1 ? T.text : T.textMuted,
                cursor: 'pointer', padding: '1px 3px', borderRadius: 4,
              }}>{c.label}</span>
            ) : (
              <span style={{ color: T.textDim, padding: '1px 3px' }}>{c.label}</span>
            )}
          </span>
        ))}
      </div>

      {/* Directory rows */}
      <div style={{
        border: `1px solid ${T.border}`, borderRadius: 8, overflowY: 'auto',
        maxHeight: 260, minHeight: 80, background: T.surfaceAlt,
      }}>
        {browsing && (
          <div style={{ padding: 14, fontSize: 12, color: T.textMuted, animation: 'pulse 1s infinite' }}>Loading…</div>
        )}
        {!browsing && listing && !(listing.dirs || []).length && (
          <div style={{ padding: 14, fontSize: 12, color: T.textDim }}>No sub-folders here.</div>
        )}
        {!browsing && listing && (listing.dirs || []).map(dir => {
          const sel = selected && selected.path === dir.path;
          return (
            <div key={dir.path} onClick={() => select(dir)} onDoubleClick={() => loadDir(dir.path)} style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px',
              cursor: 'pointer', borderBottom: `1px solid ${T.border}40`,
              background: sel ? T.accentDim : 'transparent',
            }}>
              <I name="folder" size={13} color={sel ? T.accent : T.textMuted} />
              <span style={{ fontSize: 13, color: sel ? T.accent : T.text, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {dir.name}
              </span>
              {dir.has_c3 && <Badge color={T.accent}>c3</Badge>}
              {dir.registered && <Badge color={T.blue}>registered</Badge>}
              <button onClick={e => { e.stopPropagation(); loadDir(dir.path); }} title="Open folder"
                style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 2, display: 'flex' }}>
                <I name="chevron" size={12} color={T.textDim} />
              </button>
            </div>
          );
        })}
      </div>

      {/* Selection + validation */}
      {selected && (
        <div style={{ marginTop: 12 }}>
          <MdlPath>{selected.path}</MdlPath>
          {validating && (
            <div style={{ fontSize: 11, color: T.textMuted, animation: 'pulse 1s infinite' }}>Validating…</div>
          )}
          {!validating && validation && (validation.warnings || [])
            .filter(w => !(validation.ok && validation.has_c3 && w.includes('adopted')))   // superseded by the explicit adopt line below
            .map((w, i) => (
            <div key={i} style={{ display: 'flex', gap: 6, alignItems: 'flex-start', fontSize: 11, color: T.warn, marginTop: 3 }}>
              <I name="alertTriangle" size={11} color={T.warn} style={{ marginTop: 1, flexShrink: 0 }} />{w}
            </div>
          ))}
          {!validating && validation && validation.ok && validation.has_c3 && (
            <div style={{ display: 'flex', gap: 6, alignItems: 'flex-start', fontSize: 11, color: T.accent, marginTop: 3 }}>
              <I name="check" size={11} color={T.accent} style={{ marginTop: 1, flexShrink: 0 }} />
              Existing .c3 will be adopted as-is (no re-init).
            </div>
          )}
          {!validating && validation && validation.ok && validation.registered && (
            <div style={{ display: 'flex', gap: 6, alignItems: 'flex-start', fontSize: 11, color: T.blue, marginTop: 3 }}>
              <I name="check" size={11} color={T.blue} style={{ marginTop: 1, flexShrink: 0 }} />
              Already registered in the hub — will be re-linked under this parent.
            </div>
          )}
          {!validating && validation && validation.ok && (
            <div>
              <MdlLabel>Name (optional)</MdlLabel>
              <input value={name} onChange={e => setName(e.target.value)}
                placeholder={selected.name} style={mdlInputStyle()} />
              <MdlLabel>Instruction docs / IDE</MdlLabel>
              <select value={ide} onChange={e => setIde(e.target.value)} style={mdlInputStyle()}>
                {_SUB_IDE_OPTIONS.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}
              </select>
            </div>
          )}
        </div>
      )}

      <MdlFooter>
        {!validating && validation && validation.ok && (
          <span style={{ marginRight: 'auto', alignSelf: 'center', fontSize: 11, color: T.textMuted }}>
            {validation.has_c3 ? 'Will adopt existing .c3' : 'Will initialize a new .c3'}
          </span>
        )}
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
        <Btn onClick={designate} disabled={busy || validating || !(validation && validation.ok)}>
          {busy ? 'Designating…' : 'Designate'}
        </Btn>
      </MdlFooter>
    </Modal>
  );
}

// ── Link a project by path ─────────────────────────────────────
// POST /api/projects/subprojects/inspect {path}   -> read-only report
// POST /api/projects/subprojects/validate {parent, folder}
// POST /api/projects/subprojects/link {parent, folder, name?, ide?, init?}
//
// The sibling of FolderPickerModal, and the difference is the fence: this one
// browses the whole filesystem, because the projects worth linking are the
// ones that do NOT live inside the parent. Selecting a folder inspects it
// first, so you see what you are about to claim -- what is there, who already
// claims it, and what it claims -- before the button does anything.
function LinkProjectModal({ project, onClose, onChanged }) {
  const parentPath = (project && project.path) || '';
  const [listing, setListing] = useState(null);
  const [browsing, setBrowsing] = useState(false);
  const [manual, setManual] = useState('');
  const [selected, setSelected] = useState(null);
  const [report, setReport] = useState(null);
  const [validation, setValidation] = useState(null);
  const [probing, setProbing] = useState(false);
  const [name, setName] = useState('');
  const [ide, setIde] = useState('auto');
  const [busy, setBusy] = useState(false);

  const loadDir = async (path) => {
    setBrowsing(true);
    setSelected(null); setReport(null); setValidation(null); setName('');
    try {
      setListing(await api.post('/api/projects/browse', { path }));
    } catch (e) {
      notify(`Browse failed: ${apiErr(e)}`, 'err');
    }
    setBrowsing(false);
  };

  // Start one level above the parent: a sibling is the common case.
  useEffect(() => {
    if (!parentPath) return;
    const up = parentPath.replace(/[\\/][^\\/]+[\\/]?$/, '') || parentPath;
    loadDir(up);
  }, [parentPath]);

  const probe = async (path, label) => {
    setSelected({ path, name: label || (path.split(/[\\/]/).filter(Boolean).pop() || path) });
    setReport(null); setValidation(null); setName('');
    setProbing(true);
    try {
      const [rep, val] = await Promise.all([
        api.post('/api/projects/subprojects/inspect', { path }),
        api.post('/api/projects/subprojects/validate', { parent: parentPath, folder: path }),
      ]);
      setReport(rep);
      setValidation(val);
    } catch (e) {
      setReport(null);
      setValidation({ ok: false, warnings: [apiErr(e)] });
    }
    setProbing(false);
  };

  const link = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      await api.post('/api/projects/subprojects/link', {
        parent: parentPath,
        folder: selected.path,
        name: name.trim() || undefined,
        ide: ide === 'auto' ? undefined : ide,
        init: !!(report && !report.has_c3),
      });
      notify(`Linked ${name.trim() || selected.name}`, 'ok');
      onChanged && onChanged();
      onClose();
    } catch (e) {
      notify(`Link failed: ${apiErr(e)}`, 'err');
    }
    setBusy(false);
  };

  const crumbs = _pathCrumbs((listing && listing.path) || '');
  const needsInit = !!(report && !report.has_c3);
  const canLink = !!(validation && validation.ok);
  const proj = report && report.project;

  return (
    <Modal title="Link project by path" width={620} onClose={onClose}>
      <div style={{ fontSize: 12, color: T.textMuted, marginBottom: 10 }}>
        Link any C3 project as a sub-project of <b style={{ color: T.text }}>{project.name}</b> —
        it does not have to live inside it.
      </div>

      <MdlLabel>Paste a path</MdlLabel>
      <div style={{ display: 'flex', gap: 6 }}>
        <input value={manual} onChange={e => setManual(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && manual.trim()) probe(manual.trim()); }}
          placeholder="U:\Projects\my-service" style={{ ...mdlInputStyle(), flex: 1 }} />
        <Btn variant="ghost" onClick={() => manual.trim() && probe(manual.trim())}>Inspect</Btn>
        <Btn variant="ghost" onClick={() => manual.trim() && loadDir(manual.trim())}>Browse</Btn>
      </div>

      {crumbs.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3, alignItems: 'center', margin: '12px 0 6px' }}>
          {listing && listing.parent && (
            <button onClick={() => loadDir(listing.parent)} title="Up one level"
              style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 2, color: T.textDim, fontSize: 12 }}>↑</button>
          )}
          {crumbs.map((c, i) => (
            <button key={c.path} onClick={() => loadDir(c.path)} style={{
              background: 'transparent', border: 'none', cursor: 'pointer', padding: '1px 3px',
              fontSize: 11, color: i === crumbs.length - 1 ? T.text : T.textDim,
            }}>{c.label}{i < crumbs.length - 1 ? ' ›' : ''}</button>
          ))}
        </div>
      )}

      <div style={{
        border: `1px solid ${T.border}`, borderRadius: 8, overflowY: 'auto',
        maxHeight: 220, minHeight: 70, background: T.surfaceAlt,
      }}>
        {browsing && (
          <div style={{ padding: 14, fontSize: 12, color: T.textMuted, animation: 'pulse 1s infinite' }}>Loading…</div>
        )}
        {!browsing && listing && !(listing.dirs || []).length && (
          <div style={{ padding: 14, fontSize: 12, color: T.textDim }}>No sub-folders here.</div>
        )}
        {!browsing && listing && (listing.dirs || []).map(dir => {
          const sel = selected && selected.path === dir.path;
          return (
            <div key={dir.path} onClick={() => probe(dir.path, dir.name)} onDoubleClick={() => loadDir(dir.path)} style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px',
              cursor: 'pointer', borderBottom: `1px solid ${T.border}40`,
              background: sel ? T.accentDim : 'transparent',
            }}>
              <I name="folder" size={13} color={sel ? T.accent : T.textMuted} />
              <span style={{ fontSize: 13, color: sel ? T.accent : T.text, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {dir.name}
              </span>
              {dir.has_c3 && <Badge color={T.accent}>c3</Badge>}
              {dir.registered && <Badge color={T.blue}>registered</Badge>}
              <button onClick={e => { e.stopPropagation(); loadDir(dir.path); }} title="Open folder"
                style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 2, display: 'flex' }}>
                <I name="chevron" size={12} color={T.textDim} />
              </button>
            </div>
          );
        })}
      </div>

      {selected && (
        <div style={{ marginTop: 12 }}>
          <MdlPath>{selected.path}</MdlPath>
          {probing && (
            <div style={{ fontSize: 11, color: T.textMuted, animation: 'pulse 1s infinite' }}>Inspecting…</div>
          )}

          {!probing && proj && (
            <div style={{
              border: `1px solid ${T.border}`, borderRadius: 8, padding: 10,
              background: T.surfaceAlt, marginTop: 6,
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <span style={{ fontSize: 13, color: T.text, fontWeight: 500 }}>{proj.name}</span>
                {proj.c3_version && <Badge color={T.textMuted}>v{proj.c3_version}</Badge>}
                <Badge color={report.registered ? T.blue : T.warn}>
                  {report.registered ? 'registered' : 'not registered'}
                </Badge>
              </div>
              <div className="mono" style={{ fontSize: 11, color: T.textMuted }}>
                {proj.facts_count} facts · {proj.sessions} sessions · {proj.edit_ledger_entries} ledger · {proj.notification_count} alerts
              </div>
              {(report.ancestors || []).length > 0 && (
                <div style={{ fontSize: 11, color: T.warn, marginTop: 5 }}>
                  ↳ already under {report.ancestors.map(a => a.name).join(' ‹ ')}
                </div>
              )}
              {(report.children || []).length > 0 && (
                <div style={{ fontSize: 11, color: T.textMuted, marginTop: 5 }}>
                  Brings {report.children.length} sub-project{report.children.length === 1 ? '' : 's'} with it:
                  {' '}{report.children.map(c => c.name).join(', ')}
                </div>
              )}
            </div>
          )}

          {!probing && report && !report.has_c3 && (
            <div style={{ display: 'flex', gap: 6, alignItems: 'flex-start', fontSize: 11, color: T.textMuted, marginTop: 5 }}>
              <I name="folder" size={11} color={T.textMuted} style={{ marginTop: 1, flexShrink: 0 }} />
              Not a C3 project yet — linking will initialize one here.
            </div>
          )}

          {!probing && (report ? (report.detected || []) : []).length > 0 && (
            <div style={{ marginTop: 8 }}>
              <div style={{ fontSize: 11, color: T.textDim, marginBottom: 3 }}>
                Unlinked projects detected inside — suggestions, nothing is linked automatically:
              </div>
              {report.detected.map(d => (
                <div key={d.path} onClick={() => probe(d.path, d.name)} style={{
                  display: 'flex', alignItems: 'center', gap: 6, padding: '3px 4px',
                  cursor: 'pointer', fontSize: 11, color: T.textMuted,
                }}>
                  <I name="folder" size={11} color={T.textDim} />
                  <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{d.name}</span>
                  <Badge color={T.textDim}>select</Badge>
                </div>
              ))}
            </div>
          )}

          {!probing && validation && (validation.warnings || []).map((w, i) => (
            <div key={i} style={{
              display: 'flex', gap: 6, alignItems: 'flex-start', fontSize: 11,
              color: validation.ok ? T.textMuted : T.warn, marginTop: 3,
            }}>
              <I name="alertTriangle" size={11} color={validation.ok ? T.textMuted : T.warn}
                style={{ marginTop: 1, flexShrink: 0 }} />{w}
            </div>
          ))}

          {!probing && canLink && (
            <div style={{ display: 'flex', gap: 6, alignItems: 'flex-start', fontSize: 11, color: T.accent, marginTop: 4 }}>
              <I name="check" size={11} color={T.accent} style={{ marginTop: 1, flexShrink: 0 }} />
              Will link as a {validation.link_kind} child at depth {validation.depth}.
            </div>
          )}

          {!probing && canLink && (
            <div>
              <MdlLabel>Name (optional)</MdlLabel>
              <input value={name} onChange={e => setName(e.target.value)}
                placeholder={(proj && proj.name) || selected.name} style={mdlInputStyle()} />
              {needsInit && (
                <div>
                  <MdlLabel>Instruction docs / IDE</MdlLabel>
                  <select value={ide} onChange={e => setIde(e.target.value)} style={mdlInputStyle()}>
                    {_SUB_IDE_OPTIONS.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}
                  </select>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      <MdlFooter>
        {!probing && canLink && (
          <span style={{ marginRight: 'auto', alignSelf: 'center', fontSize: 11, color: T.textMuted }}>
            {needsInit ? 'Will initialize a new .c3'
              : (report && !report.registered ? 'Will register and link' : 'Will link')}
          </span>
        )}
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
        <Btn onClick={link} disabled={busy || probing || !canLink}>
          {busy ? 'Linking…' : (report && !report.registered && !needsInit ? 'Register + link' : 'Link')}
        </Btn>
      </MdlFooter>
    </Modal>
  );
}

// ── Batch update ───────────────────────────────────────────────
// POST /api/projects/run-init/batch {projects:[{path,name,ide}]}
// GET  /api/projects/run-init/batch/status (poll every 1.5s)
// POST /api/projects/run-init/batch/cancel
function BatchUpdateModal({ projects, onClose, onChanged }) {
  const [hubVersion, setHubVersion] = useState('');
  const [targets, setTargets] = useState(null);   // null until version resolves
  const [phase, setPhase] = useState('idle');     // idle | running | done
  const [state, setState] = useState(null);       // last /status payload
  const doneRef = useRef(false);

  useEffect(() => {
    (async () => {
      let v = '';
      try { const d = await api.get('/api/version'); v = d.c3_version || ''; } catch { }
      setHubVersion(v);
      const outdated = (projects || []).filter(p => p.c3_version && v && p.c3_version !== v);
      setTargets(outdated.length ? outdated : (projects || []));
    })();
  }, []);

  // Poll status while running.
  useEffect(() => {
    if (phase !== 'running') return;
    const iv = setInterval(async () => {
      try {
        const s = await api.get('/api/projects/run-init/batch/status');
        setState(s);
        if (s.done || (!s.running && (s.results || []).length > 0)) {
          setPhase('done');
          if (!doneRef.current) { doneRef.current = true; onChanged(); }
        }
      } catch { /* network hiccup — keep polling */ }
    }, 1500);
    return () => clearInterval(iv);
  }, [phase]);

  const start = async () => {
    if (!targets || !targets.length) return;
    try {
      await api.post('/api/projects/run-init/batch', {
        projects: targets.map(p => ({ path: p.path, name: p.name, ide: p.ide })),
      });
      setPhase('running');
    } catch (e) {
      if (e.status === 409) {
        notify('A batch update is already running — attached to it.', 'warn');
        setPhase('running');
      } else {
        notify(`Batch start failed: ${e.message}`, 'err');
      }
    }
  };

  const cancel = async () => {
    try {
      await api.post('/api/projects/run-init/batch/cancel', {});
      notify('Cancellation requested…', 'warn');
    } catch { }
  };

  const results = (state && state.results) || [];
  const total = (state && state.total) || (targets ? targets.length : 0) || 1;
  const findResult = (p) => results.find(r => (r.path || '').toLowerCase() === (p.path || '').toLowerCase());
  const okCount = results.filter(r => r.success).length;
  const failCount = results.filter(r => !r.success).length;

  const rowStatus = (p, idx) => {
    const r = findResult(p);
    if (r) return r.success ? 'done' : 'failed';
    if (phase === 'running' && state && state.running && idx === (state.current_index || 0)) return 'running';
    return 'pending';
  };

  return (
    <Modal title="Update all projects" width={520} onClose={onClose}>
      <div style={{ fontSize: 12, color: T.textMuted, lineHeight: 1.5 }}>
        Runs <span className="mono" style={{ color: T.text }}>c3 init --force</span> for each
        project below{hubVersion ? <span>, updating it to <b style={{ color: T.text }}>v{hubVersion}</b></span> : null}.
      </div>

      {targets === null && (
        <div style={{ padding: '18px 0', fontSize: 12, color: T.textMuted, animation: 'pulse 1s infinite' }}>Checking versions…</div>
      )}

      {targets !== null && (
        <div style={{
          border: `1px solid ${T.border}`, borderRadius: 8, overflowY: 'auto',
          maxHeight: 280, marginTop: 12, background: T.surfaceAlt,
        }}>
          {!targets.length && (
            <div style={{ padding: 14, fontSize: 12, color: T.textDim }}>No projects registered.</div>
          )}
          {targets.map((p, idx) => {
            const st = rowStatus(p, idx);
            const r = findResult(p);
            return (
              <div key={p.path} style={{ padding: '7px 10px', borderBottom: `1px solid ${T.border}40` }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  {st === 'done' && <I name="check" size={13} color={T.accent} />}
                  {st === 'failed' && <I name="xCircle" size={13} color={T.error} />}
                  {st === 'running' && <GlowDot color={T.warn} />}
                  {st === 'pending' && <GlowDot color={T.textDim} />}
                  <span style={{ fontSize: 13, color: T.text, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {p.name || p.path}
                  </span>
                  {p.c3_version && <span className="mono" style={{ fontSize: 11, color: T.textDim }}>v{p.c3_version}</span>}
                  <span className="mono" style={{
                    fontSize: 11,
                    color: st === 'done' ? T.accent : st === 'failed' ? T.error : st === 'running' ? T.warn : T.textDim,
                  }}>{st === 'running' ? 'running…' : st}</span>
                </div>
                {st === 'failed' && r && r.output && (
                  <div className="mono" style={{ fontSize: 11, color: T.error, marginTop: 3, paddingLeft: 21, whiteSpace: 'pre-wrap' }}>
                    {r.output.split('\n').filter(l => l.trim()).slice(-2).join('\n')}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {(phase === 'running' || phase === 'done') && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 12 }}>
          <ProgressBar value={results.length} max={total} />
          <span className="mono" style={{ fontSize: 11, color: T.textMuted }}>{results.length}/{total}</span>
        </div>
      )}

      {phase === 'done' && (
        <div style={{ fontSize: 12, marginTop: 10, color: failCount ? T.error : T.accent }}>
          Batch {state && state.cancelled ? 'cancelled' : 'finished'} — {okCount} succeeded, {failCount} failed
          {state && state.cancelled ? ` (${Math.max(0, total - results.length)} skipped)` : ''}.
        </div>
      )}

      <MdlFooter>
        {phase === 'idle' && <Btn variant="ghost" onClick={onClose}>Close</Btn>}
        {phase === 'idle' && (
          <Btn onClick={start} disabled={!targets || !targets.length}>
            <I name="play" size={12} color={T.bg} />Start update
          </Btn>
        )}
        {phase === 'running' && <Btn variant="ghost" color={T.error} onClick={cancel}>Cancel batch</Btn>}
        {phase === 'done' && <Btn onClick={onClose}>Close</Btn>}
      </MdlFooter>
    </Modal>
  );
}
