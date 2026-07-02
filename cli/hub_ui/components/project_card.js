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
function CardKebab({ p, isParent, isChild, onChanged, onOpenModal, onOpenDrawer }) {
  const [open, setOpen] = useState(false);
  const [cascadeOpen, setCascadeOpen] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
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
    } catch (e) { notify(`${label}: ${e.message}`, 'err'); }
  };

  const runCascade = async (op) => {
    close();
    const label = op === 'update' ? 'Cascade update'
      : op === 'reindex' ? 'Cascade reindex' : 'Cascade health check';
    let started;
    try { started = await api.post('/api/projects/subprojects/cascade', { parent: p.path, op }); }
    catch (e) { notify('Cascade: ' + e.message, 'err'); return; }
    const total = started.total || 0;
    notifyProgress('cascade', { label, current: 0, total });
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
        done: !!st.done, error: !!st.error,
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
          {isParent && (
            <React.Fragment>
              <CardMenuDivider />
              <CardMenuItem icon="folderOpen" label="Designate sub-project…"
                onClick={() => { close(); onOpenModal('folderPick', p); }} />
              <CardMenuItem icon="refresh" label={cascadeOpen ? 'Cascade ▾' : 'Cascade ▸'}
                onClick={() => setCascadeOpen(o => !o)} />
              {cascadeOpen && (
                <React.Fragment>
                  <CardMenuItem indent label="Update all" onClick={() => runCascade('update')} />
                  <CardMenuItem indent label="Reindex all" onClick={() => runCascade('reindex')} />
                  <CardMenuItem indent label="Health check" onClick={() => runCascade('health')} />
                </React.Fragment>
              )}
            </React.Fragment>
          )}
          {isChild && (
            <React.Fragment>
              <CardMenuDivider />
              <CardMenuItem icon="gitBranch" label="Promote to top-level"
                onClick={() => unlink('Promoted')} />
              <CardMenuItem icon="xCircle" label="Unlink" onClick={() => unlink('Unlinked')} />
            </React.Fragment>
          )}
          <CardMenuDivider />
          <CardMenuItem icon="trash" label="Remove" color={T.error}
            onClick={() => { close(); setConfirmRemove(true); }} />
        </div>
      )}
      {confirmRemove && (
        <ConfirmDialog title="Remove Project"
          message={`Remove "${p.name}" from the hub? Project files are NOT deleted.`}
          confirmLabel="Remove" danger
          onConfirm={doRemove} onCancel={() => setConfirmRemove(false)} />
      )}
    </div>
  );
}

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

  const start = async (e) => {
    e.stopPropagation();
    if (starting) return;
    setStarting(true);
    try {
      const d = await api.post('/api/sessions/start', { path: p.path });
      if (d.launched) {
        notify(`Starting ${p.name}…`);
        setTimeout(onChanged, 1500);
        setTimeout(onChanged, 4000);
      } else {
        notify('Launch failed', 'err');
      }
    } catch (err) { notify('Start: ' + err.message, 'err'); }
    setStarting(false);
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
      {rollup && (
        <span style={{ display: 'inline-flex', gap: 6, flexShrink: 0 }}>
          <Badge color={T.textMuted}>{rollup.count} sub-project{rollup.count === 1 ? '' : 's'}</Badge>
          <Badge color={rollup.active > 0 ? T.accent : T.textMuted}>{rollup.active} active</Badge>
          <Badge color={rollup.alerts > 0 ? T.warn : T.textMuted}>{rollup.alerts} alerts</Badge>
        </span>
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
      <CardKebab p={p} isParent={isParent} isChild={isChild}
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
                display: 'flex', alignItems: 'center', gap: 8, padding: '3px 2px',
                cursor: 'pointer', minWidth: 0,
              }}>
                <GlowDot color={c.active ? T.accent : T.textDim} size={5} />
                <span style={{
                  fontSize: 12, color: T.textMuted, flex: 1,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{c.name}</span>
                {(c.notification_count || 0) > 0 && <Badge color={T.warn}>🔔 {c.notification_count}</Badge>}
                <span className="mono" style={{ fontSize: 10, color: T.textDim, flexShrink: 0 }}>
                  {c.last_session ? timeAgo(c.last_session) : ''}
                </span>
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
