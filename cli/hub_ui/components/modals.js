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
    case 'batch': return <BatchUpdateModal projects={projects} onClose={onClose} onChanged={onChanged} {...props} />;
    case 'settings': return <SettingsModal onClose={onClose} onChanged={onChanged} {...props} />;
    default: return null;
  }
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
// Designate: POST /api/projects/subprojects/add {parent, folder, name?}
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

function FolderPickerModal({ project, onClose, onChanged }) {
  const parentPath = (project && project.path) || '';
  const [listing, setListing] = useState(null);
  const [browsing, setBrowsing] = useState(false);
  const [selected, setSelected] = useState(null);
  const [validation, setValidation] = useState(null);
  const [validating, setValidating] = useState(false);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);

  const loadDir = async (path) => {
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
            <span onClick={() => loadDir(c.path)} style={{
              color: i === crumbs.length - 1 ? T.text : T.textMuted,
              cursor: 'pointer', padding: '1px 3px', borderRadius: 4,
            }}>{c.label}</span>
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
          {!validating && validation && (validation.warnings || []).map((w, i) => (
            <div key={i} style={{ display: 'flex', gap: 6, alignItems: 'flex-start', fontSize: 11, color: T.warn, marginTop: 3 }}>
              <I name="alertTriangle" size={11} color={T.warn} style={{ marginTop: 1, flexShrink: 0 }} />{w}
            </div>
          ))}
          {!validating && validation && validation.ok && (
            <div>
              <MdlLabel>Name (optional)</MdlLabel>
              <input value={name} onChange={e => setName(e.target.value)}
                placeholder={selected.name} style={mdlInputStyle()} />
            </div>
          )}
        </div>
      )}

      <MdlFooter>
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
        <Btn onClick={designate} disabled={busy || validating || !(validation && validation.ok)}>
          {busy ? 'Designating…' : 'Designate'}
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
