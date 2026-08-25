// ─── Project card (slim row) ───────────────────────────────────
// Rendered exclusively by ProjectTree (its only consumer). One slim
// row per project: status dot · name · chips · mono meta · primary
// action · IDE launch · kebab menu. Grid view stacks the same groups
// vertically and lists child projects compactly at the card bottom.

const CARD_BTN_SM = { padding: '5px 12px', fontSize: 11 };

function CardIconBtn({ name, title, color, onClick }) {
  const [hover, setHover] = useState(false);
  return (
    <button onClick={onClick} title={title}
      onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      style={{
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        width: 26, height: 26, padding: 0, borderRadius: 6, cursor: 'pointer',
        border: `1px solid ${hover ? T.border : 'transparent'}`,
        background: hover ? T.surfaceAlt : 'transparent',
      }}>
      <I name={name} size={13} color={color || T.textMuted} />
    </button>
  );
}

function CardMenuItem({ icon, label, color, indent, onClick }) {
  const [hover, setHover] = useState(false);
  return (
    <div onClick={onClick}
      onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '7px 12px', paddingLeft: indent ? 30 : 12,
        fontSize: 12, cursor: 'pointer', whiteSpace: 'nowrap',
        color: color || T.text,
        background: hover ? T.surfaceAlt : 'transparent',
      }}>
      {icon && <I name={icon} size={12} color={color || T.textMuted} />}
      <span>{label}</span>
    </div>
  );
}

function CardMenuDivider() {
  return <div style={{ height: 1, background: T.border, margin: '4px 0' }} />;
}

// Kebab menu: absolutely positioned dropdown, closes on outside click.
// Typed-name confirm for the destructive clear-mode removal: unlink
// AND delete the child's .c3 / MCP config / instruction docs.
function DeinitDialog({ p, onConfirm, onCancel }) {
  const [typed, setTyped] = useState('');
  const match = !!p.name && typed.trim() === p.name;
  return (
    <div onClick={onCancel} style={{
      position: 'fixed', inset: 0, background: '#00000090', zIndex: 300,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <div onClick={e => e.stopPropagation()} className="fade-up" style={{
        background: T.surface, border: `1px solid ${T.error}60`, borderRadius: 10,
        padding: 24, width: 440, maxWidth: '90vw',
      }}>
        <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 8, color: T.error }}>De-initialize Sub-project</div>
        <div style={{ fontSize: 12, color: T.textMuted, marginBottom: 14, lineHeight: 1.5 }}>
          Unlinks <b style={{ color: T.text }}>{p.name}</b> from its parent AND deletes its
          <span className="mono"> .c3</span> state, uninstalls its MCP config, removes its
          instruction docs, and deregisters it from the hub. Source code is not touched.
          This cannot be undone.
        </div>
        <input value={typed} onChange={e => setTyped(e.target.value)}
          placeholder={`Type "${p.name}" to confirm`}
          style={{
            width: '100%', boxSizing: 'border-box', padding: '8px 10px', borderRadius: 8,
            border: `1px solid ${T.border}`, background: T.surfaceAlt, color: T.text,
            fontSize: 13, outline: 'none', fontFamily: 'inherit',
          }} />
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
          <Btn variant="ghost" onClick={onCancel}>Cancel</Btn>
          <Btn color={T.error} disabled={!match} onClick={onConfirm}>De-initialize</Btn>
        </div>
      </div>
    </div>
  );
}

function CardKebab({ p, isParent, isChild, rollup, onChanged, onOpenModal, onOpenDrawer }) {
  const [open, setOpen] = useState(false);
  const [cascadeOpen, setCascadeOpen] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [confirmDeinit, setConfirmDeinit] = useState(false);
  const [cascadeParent, setCascadeParent] = useState(false);
  const [reconcileInfo, setReconcileInfo] = useState(null);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e) => {
      if (ref.current && !ref.current.contains(e.target)) {
        setOpen(false); setCascadeOpen(false);
      }
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  const close = () => { setOpen(false); setCascadeOpen(false); };

  const openFolder = async () => {
    close();
    try { await api.post('/api/projects/open', { path: p.path }); notify('Folder opened'); }
    catch (e) { notify('Folder: ' + e.message, 'err'); }
  };

  const toggleAutostart = async () => {
    close();
    try {
      const d = await api.post('/api/sessions/autostart', { path: p.path, enabled: !p.autostart_ui });
      notify(d.autostart_ui ? 'Autostart enabled — UI launches when the hub starts' : 'Autostart disabled');
      onChanged();
    } catch (e) { notify('Autostart: ' + e.message, 'err'); }
  };

  // Promote and Unlink are the same operation server-side (mode: unlink).
  const unlink = async (label) => {
    close();
    try {
      const d = await api.post('/api/projects/subprojects/remove',
        { parent: p.parent_path, ref: p.path, mode: 'unlink' });
      notify(d.success ? `${label}: ${p.name}` : `${label} failed`, d.success ? 'ok' : 'err');
      if (d.success) onChanged();
    } catch (e) { notify(`${label}: ${apiErr(e)}`, 'err'); }
  };

  const runCascade = async (op) => {
    close();
    const label = op === 'update' ? 'Cascade update'
      : op === 'reindex' ? 'Cascade reindex' : 'Cascade health check';
    let started;
    try { started = await api.post('/api/projects/subprojects/cascade', { parent: p.path, op, include_parent: cascadeParent }); }
    catch (e) { notify('Cascade: ' + e.message, 'err'); return; }
    const total = started.total || 0;
    const cancelCascade = async () => {
      try { await api.post('/api/projects/subprojects/cascade/cancel'); notify('Cascade: cancelling…', 'warn'); }
      catch (e) { notify('Cancel: ' + e.message, 'err'); }
    };
    notifyProgress('cascade', { label, current: 0, total, onCancel: cancelCascade });
    const poll = async (tries) => {
      let st;
      try { st = await api.get('/api/projects/subprojects/cascade/status'); }
      catch (e) {
        notifyProgress('cascade', { label, current: 0, total, done: true, error: true });
        notify('Cascade: ' + e.message, 'err');
        return;
      }
      const results = st.results || [];
      notifyProgress('cascade', {
        label: st.current ? `${label} — ${st.current}` : label,
        current: results.length, total: st.total || total,
        done: !!st.done, error: !!st.error, cancelled: !!st.cancelled,
        onCancel: st.done ? undefined : cancelCascade,
      });
      if (st.done) {
        const ok = results.filter(r => r.success).length;
        notify(`${label}: ${ok}/${results.length} succeeded`, ok === results.length ? 'ok' : 'warn');
        onChanged();
        return;
      }
      if (tries > 600) { notify('Cascade: timed out waiting for status', 'err'); return; }
      setTimeout(() => poll(tries + 1), 1500);
    };
    setTimeout(() => poll(0), 800);
  };

  // Reconcile: dry-run check first; repair only after an explicit confirm.
  const runReconcile = async () => {
    close();
    let d;
    try { d = await api.post('/api/projects/subprojects/reconcile', { parent: p.path }); }
    catch (e) { notify('Reconcile: ' + e.message, 'err'); return; }
    const children = d.children || [];
    const issues = children.filter(c => c.status !== 'ok');
    const orphans = d.orphans || [];
    if (d.ok) {
      notify(`Reconcile: all ${children.length} sub-project link${children.length === 1 ? '' : 's'} consistent`);
      return;
    }
    setReconcileInfo({ issues, orphans });
  };

  const reconcileMessage = (info) => {
    const label = {
      backlink_broken: 'broken back-link',
      unregistered: 'unregistered',
      missing_folder: 'missing folder (removed on repair)',
      missing_c3: 'missing .c3 (not repairable here — re-designate or Cascade update)',
    };
    const counts = {};
    info.issues.forEach(c => { counts[c.status] = (counts[c.status] || 0) + 1; });
    const parts = Object.entries(counts).map(([s, n]) => `${n} ${label[s] || s}`);
    if (info.orphans.length) {
      parts.push(`${info.orphans.length} registry orphan${info.orphans.length === 1 ? '' : 's'} (parent link cleared on repair)`);
    }
    return `Found ${parts.join('; ')}. Repair from the parent config (parent wins)?`;
  };

  const doReconcileFix = async () => {
    const info = reconcileInfo;
    setReconcileInfo(null);
    const prune = info.issues.some(c => c.status === 'missing_folder');
    try {
      const d = await api.post('/api/projects/subprojects/reconcile',
        { parent: p.path, fix: true, prune });
      const fixed = (d.fixed || []).length;
      const pruned = (d.pruned || []).length;
      notify(`Reconcile: ${fixed} repaired${pruned ? `, ${pruned} pruned` : ''}${d.ok ? '' : ' — issues remain'}`,
        d.ok ? 'ok' : 'warn');
      onChanged();
    } catch (e) { notify('Reconcile: ' + e.message, 'err'); }
  };

  const doDeinit = async () => {
    setConfirmDeinit(false);
    try {
      const d = await api.post('/api/projects/subprojects/remove',
        { parent: p.parent_path, ref: p.path, mode: 'clear' });
      const warns = ((d.result || {}).warnings) || [];
      if (d.success && warns.length) {
        notify(`Unlinked ${p.name}, but cleanup was partial: ${warns.join('; ')}`, 'warn');
      } else {
        notify(d.success ? `De-initialized ${p.name} — .c3 removed, project deregistered` : 'De-initialize failed',
          d.success ? 'ok' : 'err');
      }
      if (d.success) onChanged();
    } catch (e) { notify('De-initialize: ' + apiErr(e), 'err'); }
  };

  const doRemove = async () => {
    setConfirmRemove(false);
    try {
      const d = await api.post('/api/projects/remove', { path: p.path });
      if (d.removed) {
        const orphans = d.orphaned_children || [];
        if (orphans.length) {
          notify(`Removed ${p.name} — ${orphans.length} sub-project${orphans.length === 1 ? '' : 's'} became top-level`, 'warn');
        } else {
          notify(`Removed ${p.name}`);
        }
        onChanged();
      } else {
        notify('Project not found', 'warn');
      }
    } catch (e) { notify('Remove: ' + e.message, 'err'); }
  };

  return (
    <div ref={ref} onClick={(e) => e.stopPropagation()}
      style={{ position: 'relative', display: 'inline-flex' }}>
      <CardIconBtn name="kebab" title="More actions" onClick={() => setOpen(o => !o)} />
      {open && (
        <div className="fade-up" style={{
          position: 'absolute', right: 0, top: 'calc(100% + 4px)', zIndex: 250,
          background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8,
          minWidth: 210, padding: '4px 0',
        }}>
          <CardMenuItem icon="folder" label="Folder" onClick={openFolder} />
          <CardMenuItem icon="fileText" label="Session Log"
            onClick={() => { close(); onOpenDrawer(p); }} />
          <CardMenuItem icon="zap" label={p.autostart_ui ? 'Autostart: on' : 'Autostart: off'}
            color={p.autostart_ui ? T.accent : null} onClick={toggleAutostart} />
          <CardMenuDivider />
          <CardMenuItem icon="edit" label="Edit…"
            onClick={() => { close(); onOpenModal('edit', p); }} />
          <CardMenuItem icon="external" label="Transfer…"
            onClick={() => { close(); onOpenModal('transfer', p); }} />
          <CardMenuItem icon="layers" label="Merge…"
            onClick={() => { close(); onOpenModal('merge', p); }} />
          {/* Every project can take children -- a sub-project is allowed
              its own sub-projects, so these are no longer gated on !isChild. */}
          <CardMenuDivider />
          <CardMenuItem icon="tree" label="Link project by path…"
            onClick={() => { close(); onOpenModal('linkPath', p); }} />
          <CardMenuItem icon="folderOpen" label="Designate sub-project…"
            onClick={() => { close(); onOpenModal('folderPick', p); }} />
          {!isChild && !isParent && (
            <CardMenuItem icon="gitBranch" label="Make sub-project of…"
              onClick={() => { close(); onOpenModal('makeSub', p); }} />
          )}
          {isParent && (
            <React.Fragment>
              <CardMenuItem icon="refresh" label={cascadeOpen ? 'Cascade ▾' : 'Cascade ▸'}
                onClick={() => setCascadeOpen(o => !o)} />
              {cascadeOpen && (
                <React.Fragment>
                  <CardMenuItem indent label="Update all" onClick={() => runCascade('update')} />
                  <CardMenuItem indent label="Reindex all" onClick={() => runCascade('reindex')} />
                  <CardMenuItem indent label="Health check" onClick={() => runCascade('health')} />
                  <CardMenuItem indent icon={cascadeParent ? 'check' : null}
                    label="Include parent" color={cascadeParent ? T.accent : null}
                    onClick={() => setCascadeParent(o => !o)} />
                </React.Fragment>
              )}
              <CardMenuItem icon="wrench" label="Reconcile links" onClick={runReconcile} />
            </React.Fragment>
          )}
          {isChild && (
            <React.Fragment>
              <CardMenuDivider />
              <CardMenuItem icon="shuffle" label="Change parent…"
                onClick={() => { close(); onOpenModal('reparent', p); }} />
              <CardMenuItem icon="gitBranch" label="Promote to top-level"
                onClick={() => unlink('Promoted')} />
              <CardMenuItem icon="xCircle" label="De-initialize…" color={T.error}
                onClick={() => { close(); setConfirmDeinit(true); }} />
            </React.Fragment>
          )}
          <CardMenuDivider />
          <CardMenuItem icon="trash" label="Remove" color={T.error}
            onClick={() => { close(); setConfirmRemove(true); }} />
        </div>
      )}
      {confirmRemove && (
        <ConfirmDialog title="Remove Project"
          message={`Remove "${p.name}" from the hub? Project files are NOT deleted.` +
            (rollup && rollup.count > 0
              ? ` Its ${rollup.count} sub-project${rollup.count === 1 ? '' : 's'} will become top-level.`
              : '')}
          confirmLabel="Remove" danger
          onConfirm={doRemove} onCancel={() => setConfirmRemove(false)} />
      )}
      {reconcileInfo && (
        <ConfirmDialog title="Reconcile Sub-project Links"
          message={reconcileMessage(reconcileInfo)}
          confirmLabel="Repair"
          onConfirm={doReconcileFix} onCancel={() => setReconcileInfo(null)} />
      )}
      {confirmDeinit && (
        <DeinitDialog p={p} onConfirm={doDeinit} onCancel={() => setConfirmDeinit(false)} />
      )}
    </div>
  );
}

const SUB_LINK_LABEL = {
  backlink_broken: 'broken back-link', unregistered: 'unregistered',
  missing_folder: 'missing folder', missing_c3: 'missing .c3', orphan: 'orphan link',
};

function ProjectCard({ p, isChild, rollup, expanded, onToggleExpand, onChanged, onOpenDrill, onOpenModal, onOpenDrawer, view, hubVersion, childRows }) {
  const [updating, setUpdating] = useState(false);
  const [starting, setStarting] = useState(false);

  const isGrid = view === 'grid';
  const isParent = !!p.is_parent || !!(rollup && rollup.count > 0);
  const accessible = p.accessible !== false;
  const updateAvailable = !!(p.c3_version && hubVersion && p.c3_version !== hubVersion);

  // Version update = re-init in place (POST /api/projects/run-init, init_mode force).
  const runUpdate = async (e) => {
    e.stopPropagation();
    if (updating) return;
    setUpdating(true);
    const id = 'update:' + p.path;
    const label = `Updating ${p.name}`;
    notifyProgress(id, { label, current: 0, total: 1 });
    try {
      const d = await api.post('/api/projects/run-init', { path: p.path, init_mode: 'force' });
      notifyProgress(id, { label, current: 1, total: 1, done: true, error: !d.success });
      notify(d.success ? `${p.name} updated to v${hubVersion}` : `Update failed for ${p.name}`,
        d.success ? 'ok' : 'err');
      if (d.success) onChanged();
    } catch (err) {
      notifyProgress(id, { label, current: 0, total: 1, done: true, error: true });
      notify('Update: ' + err.message, 'err');
    }
    setUpdating(false);
  };

  // Launch the UI server, then open its tab once the port shows up in the
  // registry. launch_session cannot return the port (the detached child
  // picks it), so open a placeholder tab synchronously — still inside the
  // click gesture, so popup blockers allow it. The placeholder carries its
  // own poll/redirect script (about:blank inherits this tab's origin, so
  // its fetches are same-origin) and resolves even if this hub tab is
  // closed, reloaded, or hung mid-launch — it must never depend on the
  // opener surviving the launch window.
  const start = async (e) => {
    e.stopPropagation();
    if (starting) return;
    setStarting(true);
    const win = window.open('', '_blank');
    // armed = the placeholder polls and navigates itself; when false
    // (write failed / popup blocked) the hub-side poll navigates instead.
    let armed = false;
    if (win) {
      try {
        const escHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
        const jsStr = (s) => JSON.stringify(String(s)).replace(/</g, '\\u003c');
        win.document.write(
          `<title>Starting ${escHtml(p.name)}…</title>` +
          '<body style="background:#0d1117;color:#8b949e;font-family:sans-serif;' +
          'display:flex;align-items:center;justify-content:center;height:100vh;margin:0">' +
          '<div style="text-align:center;max-width:560px;padding:0 20px">' +
          '<div id="spin" style="margin:0 auto 14px;width:22px;height:22px;' +
          'border:2px solid #21262d;border-top-color:#58a6ff;border-radius:50%;' +
          'animation:s .8s linear infinite"></div>' +
          '<style>@keyframes s{to{transform:rotate(360deg)}}</style>' +
          `<div id="msg" style="color:#c9d1d9">Starting ${escHtml(p.name)}…</div>` +
          '<div id="sub" style="margin-top:6px;font-size:12px">waiting for the UI server to report a port</div>' +
          '</div><script>' +
          `var ORIGIN=${jsStr(location.origin)},TARGET=${jsStr((p.path || '').toLowerCase())},NAME=${jsStr(p.name)},tries=0,dead=0;` +
          'function fail(why){dead=1;document.getElementById("spin").style.display="none";' +
          'document.getElementById("msg").textContent=NAME+": "+why;' +
          'document.getElementById("sub").textContent=' +
          '"Check the project\'s .c3/ui.log — you can close this tab.";}' +
          'window.__c3fail=fail;' +
          'function poll(){if(dead)return;' +
          'fetch(ORIGIN+"/api/projects").then(function(r){return r.json()})' +
          '.then(function(rows){if(dead)return;' +
          'var row=(Array.isArray(rows)?rows:[]).find(function(r){' +
          'return (r.path||"").toLowerCase()===TARGET});' +
          'if(row&&row.port){location.replace("http://127.0.0.1:"+row.port);return}' +
          'if(++tries>=20){fail("UI server did not report a port after 30s");return}' +
          'setTimeout(poll,1500)}).catch(function(){if(dead)return;' +
          'if(++tries>=20){fail("hub unreachable");return}setTimeout(poll,1500)})}' +
          'setTimeout(poll,1200);<\/script>');
        win.document.close();
        armed = true;
      } catch { }
    }
    const fail = (msg) => {
      if (win) {
        // Surface the error inside the tab the user is looking at; close
        // it only when the placeholder script isn't there to show it.
        let shown = false;
        try { if (armed && win.__c3fail) { win.__c3fail(msg); shown = true; } } catch { }
        if (!shown) { try { win.close(); } catch { } }
      }
      notify(msg, 'err');
      setStarting(false);
    };
    try {
      const d = await api.post('/api/sessions/start', { path: p.path });
      if (!d.launched) { fail('Launch failed'); return; }
      notify(`Starting ${p.name}…`);
      setTimeout(onChanged, 1500);
      // Bookkeeping poll: refresh the card and clear the busy state. Only
      // navigates or closes the tab when the placeholder isn't armed.
      const poll = async (tries) => {
        let rows = [];
        try { rows = await api.get('/api/projects'); } catch { }
        const row = (Array.isArray(rows) ? rows : []).find(r =>
          (r.path || '').toLowerCase() === (p.path || '').toLowerCase());
        if (row && row.port) {
          if (!armed) {
            const url = 'http://127.0.0.1:' + row.port;
            if (win) { win.location = url; } else { window.open(url, '_blank'); }
          }
          onChanged();
          setStarting(false);
          return;
        }
        if (tries >= 20) {
          if (!armed && win) { try { win.close(); } catch { } }
          notify(`${p.name}: UI server did not report a port — check .c3/ui.log`, 'err');
          setStarting(false);
          return;
        }
        setTimeout(() => poll(tries + 1), 1500);
      };
      setTimeout(() => poll(0), 1200);
    } catch (err) { fail('Start: ' + err.message); }
  };

  const stop = async (e) => {
    e.stopPropagation();
    try {
      if (p.port) {
        const d = await api.post('/api/sessions/stop', { port: p.port });
        notify(d.stopped ? `Stopped :${p.port}` : 'Stop failed', d.stopped ? 'ok' : 'err');
      } else {
        const d = await api.post('/api/sessions/end', { path: p.path });
        notify(d.stopped ? 'Session ended' : 'No active session found', d.stopped ? 'ok' : 'warn');
      }
      setTimeout(onChanged, 800);
    } catch (err) { notify('Stop: ' + err.message, 'err'); }
  };

  const launchIde = async (e) => {
    e.stopPropagation();
    if (!p.ide || p.ide === 'unknown') { onOpenModal('edit', p); return; }
    try {
      await api.post('/api/projects/launch-ide', { path: p.path, ide: p.ide, custom_cmd: '' });
      notify(`Launched ${ideLabel(p.ide)}`);
    } catch (err) { notify('IDE: ' + err.message, 'err'); }
  };

  const portLabel = p.port ? `:${p.port}` : (p.session_active ? 'MCP' : '—');
  const metaText = `${ideLabel(p.ide)} · ${portLabel} · ${p.last_session ? timeAgo(p.last_session) : 'never'}`;

  const identity = (
    <React.Fragment>
      {!isGrid && onToggleExpand && (
        <span onClick={(e) => { e.stopPropagation(); onToggleExpand(); }}
          style={{ display: 'inline-flex', cursor: 'pointer', flexShrink: 0 }}>
          <I name="chevron" size={12} color={T.textMuted}
            style={{ transform: expanded ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s' }} />
        </span>
      )}
      <GlowDot color={p.active ? T.accent : T.textDim} />
      <span style={{
        fontSize: 13, fontWeight: 600, color: T.text,
        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
      }}>{p.name}</span>
      {!accessible && <Badge color={T.warn}>offline</Badge>}
      {p.c3_version
        ? <Badge color={T.textMuted}>v{p.c3_version}</Badge>
        : <Badge color={T.textDim}>not initialized</Badge>}
      {updateAvailable && (
        <span onClick={runUpdate} title={`Update to v${hubVersion}`} className="mono" style={{
          fontSize: 10, fontWeight: 700, color: T.accent, cursor: 'pointer',
          border: `1px solid ${T.accent}40`, borderRadius: 4, padding: '1px 6px',
          whiteSpace: 'nowrap', opacity: updating ? 0.5 : 1, flexShrink: 0,
        }}>{updating ? 'updating…' : '↑ update'}</span>
      )}
      {(p.notification_count || 0) > 0 && (
        <span onClick={(e) => { e.stopPropagation(); onOpenDrawer(p); }}
          style={{ cursor: 'pointer', display: 'inline-flex', flexShrink: 0 }}>
          <Badge color={T.warn}>🔔 {p.notification_count}</Badge>
        </span>
      )}
      {(p.open_task_count || 0) > 0 && (
        <span onClick={(e) => { e.stopPropagation(); onOpenDrill(p, 'tasks'); }}
          style={{ cursor: 'pointer', display: 'inline-flex', flexShrink: 0 }}>
          <Badge color={T.blue}>☑ {p.open_task_count}</Badge>
        </span>
      )}
      {rollup && (
        <span style={{ display: 'inline-flex', gap: 6, flexShrink: 0 }}>
          <Badge color={T.textMuted}>{rollup.count} sub-project{rollup.count === 1 ? '' : 's'}</Badge>
          <Badge color={rollup.active > 0 ? T.accent : T.textMuted}>{rollup.active} active</Badge>
          <Badge color={rollup.alerts > 0 ? T.warn : T.textMuted}>{rollup.alerts} alerts</Badge>
        </span>
      )}
      {(p.subproject_issues || 0) > 0 && (
        <span title="Broken parent/child links — kebab menu → Reconcile links"
          style={{ display: 'inline-flex', flexShrink: 0 }}>
          <Badge color={T.error}>{p.subproject_issues} link issue{p.subproject_issues === 1 ? '' : 's'}</Badge>
        </span>
      )}
      {isChild && p.parent_name && (
        <span className="mono" title={p.parent_path}
          style={{ fontSize: 10, color: T.textDim, whiteSpace: 'nowrap', flexShrink: 0 }}>
          ↳ {p.parent_name}
        </span>
      )}
      {isChild && p.link_status && p.link_status !== 'ok' && (
        <Badge color={p.link_status === 'missing_folder' || p.link_status === 'missing_c3' ? T.error : T.warn}>
          {SUB_LINK_LABEL[p.link_status] || p.link_status}
        </Badge>
      )}
    </React.Fragment>
  );

  const actions = (
    <span onClick={(e) => e.stopPropagation()}
      style={{ display: 'inline-flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
      {p.active ? (
        <React.Fragment>
          {p.port ? (
            <Btn variant="ghost" style={CARD_BTN_SM}
              onClick={(e) => { e.stopPropagation(); window.open('http://127.0.0.1:' + p.port, '_blank'); }}>
              Open UI
            </Btn>
          ) : (
            <Btn variant="ghost" style={CARD_BTN_SM} disabled={starting || !accessible} onClick={start}>
              {starting ? 'Starting…' : 'Open UI'}
            </Btn>
          )}
          <CardIconBtn name="stop" title="Stop session" onClick={stop} />
        </React.Fragment>
      ) : (
        <Btn style={CARD_BTN_SM} disabled={starting || !accessible} onClick={start}>
          {starting ? 'Starting…' : 'Start'}
        </Btn>
      )}
      <CardIconBtn name="terminal" title={`Launch ${ideLabel(p.ide)}`} onClick={launchIde} />
      <CardKebab p={p} isParent={isParent} isChild={isChild} rollup={rollup}
        onChanged={onChanged} onOpenModal={onOpenModal} onOpenDrawer={onOpenDrawer} />
    </span>
  );

  if (isGrid) {
    return (
      <div className="fade-up" onClick={() => onOpenDrill(p)} style={{
        background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8,
        padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 8,
        cursor: 'pointer', minWidth: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0, flexWrap: 'wrap' }}>
          {identity}
        </div>
        <div className="mono" style={{
          fontSize: 11, color: T.textMuted,
          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
        }}>{metaText}</div>
        <div style={{ display: 'flex', alignItems: 'center' }}>
          {actions}
        </div>
        {childRows && childRows.length > 0 && (
          <div onClick={(e) => e.stopPropagation()} style={{
            borderTop: `1px solid ${T.border}`, marginTop: 2, paddingTop: 8,
            display: 'flex', flexDirection: 'column', gap: 4,
          }}>
            {childRows.map(c => (
              <div key={c.path} onClick={() => onOpenDrill(c)} style={{
                display: 'flex', alignItems: 'center', gap: 8,
                // Grid view flattens the subtree; indent carries the depth.
                padding: '3px 2px', paddingLeft: 2 + (c._depth || 0) * 14,
                cursor: 'pointer', minWidth: 0,
              }}>
                <GlowDot color={c.active ? T.accent : T.textDim} size={5} />
                <span style={{
                  fontSize: 12, color: T.textMuted, flex: 1,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{c.name}</span>
                {(c.notification_count || 0) > 0 && <Badge color={T.warn}>🔔 {c.notification_count}</Badge>}
                {(c.open_task_count || 0) > 0 && <Badge color={T.blue}>☑ {c.open_task_count}</Badge>}
                {c.link_status && c.link_status !== 'ok' && (
                  <Badge color={c.link_status === 'missing_folder' || c.link_status === 'missing_c3' ? T.error : T.warn}>
                    {SUB_LINK_LABEL[c.link_status] || c.link_status}
                  </Badge>
                )}
                <span className="mono" style={{ fontSize: 10, color: T.textDim, flexShrink: 0 }}>
                  {c.last_session ? timeAgo(c.last_session) : ''}
                </span>
                <CardKebab p={c} isParent={!!c.is_parent} isChild
                  onChanged={onChanged} onOpenModal={onOpenModal} onOpenDrawer={onOpenDrawer} />
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="fade-up" onClick={() => onOpenDrill(p)} style={{
      background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8,
      padding: isChild ? '7px 12px' : '9px 14px',
      display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', minWidth: 0,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0, flex: 1 }}>
        {identity}
      </div>
      <span className="mono" style={{ fontSize: 11, color: T.textMuted, whiteSpace: 'nowrap', flexShrink: 0 }}>
        {metaText}
      </span>
      {actions}
    </div>
  );
}
