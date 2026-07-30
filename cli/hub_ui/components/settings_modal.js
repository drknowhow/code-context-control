// ─── Hub settings modal ─────────────────────────────────────────
// Config: GET/POST /api/hub/config {port, auto_open_browser, oracle_url,
// runtime_cache_size}. Service: GET /api/hub/service + POST
// /api/hub/service/install|uninstall|start|stop. Restart: POST /api/hub/restart.
// Theme is handled by the topbar; bind host is CLI/config-file only (read-only here).

function SettingsModal({ onClose, onChanged }) {
  const [cfg, setCfg] = useState(null);
  const [port, setPort] = useState('');
  const [autoBrowser, setAutoBrowser] = useState(true);
  const [oracleUrl, setOracleUrl] = useState('');
  const [cacheSize, setCacheSize] = useState(8);
  const [saving, setSaving] = useState(false);
  const [svc, setSvc] = useState(null);            // {installed, running, method, port, log_path} | {error}
  const [svcBusy, setSvcBusy] = useState('');
  const [confirmAction, setConfirmAction] = useState(null);   // 'stop' | 'uninstall'

  const loadSvc = async () => {
    try { setSvc(await api.get('/api/hub/service')); }
    catch { setSvc({ error: true }); }
  };

  useEffect(() => {
    (async () => {
      try {
        const d = await api.get('/api/hub/config');
        setCfg(d);
        setPort(d.port || 3330);
        setAutoBrowser(!!d.auto_open_browser);
        setOracleUrl(d.oracle_url || '');
        setCacheSize(d.runtime_cache_size || 8);
      } catch {
        notify('Could not load hub config', 'err');
      }
      loadSvc();
    })();
  }, []);

  const save = async () => {
    const portVal = parseInt(port, 10);
    if (isNaN(portVal) || portVal < 1024 || portVal > 65535) {
      notify('Port must be 1024-65535', 'err');
      return;
    }
    const cache = Math.max(1, parseInt(cacheSize, 10) || 8);
    setSaving(true);
    try {
      await api.post('/api/hub/config', {
        port: portVal, auto_open_browser: autoBrowser,
        oracle_url: oracleUrl.trim(), runtime_cache_size: cache,
      });
      const portChanged = cfg && portVal !== cfg.port;
      notify(portChanged
        ? 'Settings saved — restart the hub to apply the new port'
        : 'Settings saved', portChanged ? 'warn' : 'ok');
      if (onChanged) onChanged();
    } catch (e) {
      notify(`Save failed: ${e.message}`, 'err');
    }
    setSaving(false);
  };

  const svcAction = async (action) => {
    setConfirmAction(null);
    setSvcBusy(action);
    try {
      const d = await api.post(`/api/hub/service/${action}`, {});
      if (action === 'stop') {
        // Server exits after responding — the page goes unreachable.
        notify('Hub stopped. Restart with: c3 hub', 'warn');
        setSvcBusy('');
        return;
      }
      const ok = d.success !== false;
      notify(ok ? `Service ${action} succeeded` : (d.output || `Service ${action} failed`), ok ? 'ok' : 'err');
    } catch (e) {
      if (action === 'stop') notify('Hub stopped. Restart with: c3 hub', 'warn');
      else notify(`Service ${action} failed: ${e.message}`, 'err');
    }
    setSvcBusy('');
    setTimeout(loadSvc, 800);
  };

  const restartHub = async () => {
    try { await api.post('/api/hub/restart', {}); } catch { /* server drops mid-response */ }
    notify('Hub restarting…', 'warn');
  };

  const sectionLabel = (text) => (
    <div style={{
      fontSize: 11, fontWeight: 600, color: T.textMuted, textTransform: 'uppercase',
      letterSpacing: 1, margin: '18px 0 8px',
    }}>{text}</div>
  );

  const fieldRow = (label, control, note) => (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: '6px 0' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 3, flex: 1 }}>
        <span style={{ color: T.textMuted, fontSize: 12 }}>{label}</span>
        {note && <span style={{ color: T.textDim, fontSize: 11 }}>{note}</span>}
      </div>
      {control}
    </div>
  );

  const inputStyle = (w) => ({
    width: w, boxSizing: 'border-box', padding: '7px 10px', borderRadius: 8,
    border: `1px solid ${T.border}`, background: T.surfaceAlt, color: T.text,
    fontSize: 13, outline: 'none', fontFamily: 'inherit',
  });

  const svcInstalled = svc && !svc.error && svc.installed;
  const svcLabel = !svc ? 'checking…'
    : svc.error ? 'unavailable'
      : !svc.installed ? 'not installed'
        : svc.running === true ? 'running'
          : svc.running === false ? 'stopped'
            : 'installed';
  const svcColor = !svc ? T.textMuted
    : svc.error ? T.error
      : (svc.installed && svc.running !== false) ? T.accent : T.textMuted;

  return (
    <Modal title="Hub Settings" width={520} onClose={onClose}>
      {sectionLabel('Server')}
      {fieldRow('Hub port', (
        <input type="number" value={port} onChange={e => setPort(e.target.value)}
          min={1024} max={65535} className="mono" style={inputStyle(110)} />
      ), 'Changing the port requires a hub restart.')}
      {fieldRow('Bind host', (
        <span className="mono" style={{ fontSize: 12, color: T.text }}>{(cfg && cfg.host) || '127.0.0.1'}</span>
      ), 'Read-only — set "host" in ~/.c3/hub_config.json to expose beyond loopback.')}
      {renderBoolToggle('Auto-open browser', autoBrowser, () => setAutoBrowser(v => !v),
        'Open the hub UI in your browser when the server starts.')}
      {fieldRow('Runtime cache size', (
        <input type="number" value={cacheSize} onChange={e => setCacheSize(e.target.value)}
          min={1} className="mono" style={inputStyle(110)} />
      ), 'Max project runtimes kept warm for drill-in and global search.')}
      {fieldRow('Oracle URL', (
        <input value={oracleUrl} onChange={e => setOracleUrl(e.target.value)}
          placeholder="http://localhost:3333" className="mono" style={inputStyle(220)} />
      ), 'Optional — link to a running C3 Oracle instance.')}
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 10 }}>
        <Btn onClick={save} disabled={saving || !cfg}>{saving ? 'Saving…' : 'Save settings'}</Btn>
      </div>

      {sectionLabel('Startup service')}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <GlowDot color={svcColor} />
        <span className="mono" style={{ fontSize: 12, color: svcColor }}>{svcLabel}</span>
        {svc && !svc.error && svc.method && (
          <span className="mono" style={{ fontSize: 11, color: T.textDim }}>
            {svc.method}{svc.port ? ` (port ${svc.port})` : ''}
          </span>
        )}
      </div>
      {svc && !svc.error && svc.log_path && (
        <div className="mono" style={{ fontSize: 11, color: T.textDim, marginBottom: 8, wordBreak: 'break-all' }}>
          log: {svc.log_path}
        </div>
      )}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <Btn variant="ghost" onClick={() => svcAction('install')} disabled={!!svcBusy}>
          {svcInstalled ? 'Reinstall' : 'Install'}
        </Btn>
        {svcInstalled && (
          <Btn variant="ghost" onClick={() => setConfirmAction('uninstall')} disabled={!!svcBusy}>Uninstall</Btn>
        )}
        {svcInstalled && (
          <Btn variant="ghost" onClick={() => svcAction('start')} disabled={!!svcBusy}>Start</Btn>
        )}
        <Btn variant="ghost" color={T.error} onClick={() => setConfirmAction('stop')} disabled={!!svcBusy}>Stop hub</Btn>
        <Btn variant="ghost" onClick={restartHub} disabled={!!svcBusy}>
          <I name="refresh" size={12} color={T.textMuted} />Restart hub
        </Btn>
      </div>
      {svcBusy && (
        <div style={{ fontSize: 11, color: T.textMuted, marginTop: 8, animation: 'pulse 1s infinite' }}>
          {svcBusy}…
        </div>
      )}
      {cfg && (
        <div style={{ fontSize: 11, color: T.textDim, marginTop: 10 }}>
          {cfg.has_terminal
            ? 'Running with a terminal window attached.'
            : 'Running as a background process (no terminal).'}
        </div>
      )}

      {confirmAction === 'stop' && (
        <ConfirmDialog title="Stop Hub"
          message="The page will become unreachable until the hub is restarted (c3 hub)."
          confirmLabel="Stop" danger
          onConfirm={() => svcAction('stop')} onCancel={() => setConfirmAction(null)} />
      )}
      {confirmAction === 'uninstall' && (
        <ConfirmDialog title="Uninstall Service"
          message="The hub will no longer auto-start on login."
          confirmLabel="Uninstall" danger
          onConfirm={() => svcAction('uninstall')} onCancel={() => setConfirmAction(null)} />
      )}
    </Modal>
  );
}
