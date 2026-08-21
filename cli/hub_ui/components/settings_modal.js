// ─── Hub settings modal ─────────────────────────────────────────
// Config: GET/POST /api/hub/config {port, auto_open_browser, oracle_url,
// runtime_cache_size}. Hub service: GET /api/hub/service + POST
// /api/hub/service/install|uninstall|start|stop. Restart: POST /api/hub/restart.
// Oracle service: GET /api/oracle/service + POST
// /api/oracle/service/install|uninstall|start|stop — runs `c3 oracle serve`
// windowless in the background; install also registers it to start at login.
// Theme is handled by the topbar; bind host is CLI/config-file only (read-only here).

let _oraclePollTimer = null;

function SettingsModal({ onClose, onChanged }) {
  const [cfg, setCfg] = useState(null);
  const [port, setPort] = useState('');
  const [autoBrowser, setAutoBrowser] = useState(true);
  const [oracleUrl, setOracleUrl] = useState('');
  const [cacheSize, setCacheSize] = useState(8);
  const [saving, setSaving] = useState(false);
  const [svc, setSvc] = useState(null);            // hub: {installed, running, method, port, log_path} | {error}
  const [svcBusy, setSvcBusy] = useState('');
  const [osvc, setOsvc] = useState(null);          // oracle: same shape + {url, bind_host, mcp_port}
  const [osvcBusy, setOsvcBusy] = useState('');
  const [confirmAction, setConfirmAction] = useState(null);   // 'stop' | 'uninstall' | 'oracle-stop' | 'oracle-uninstall'

  const loadSvc = async () => {
    try { setSvc(await api.get('/api/hub/service')); }
    catch { setSvc({ error: true }); }
  };
  const loadOsvc = async () => {
    try { setOsvc(await api.get('/api/oracle/service')); }
    catch { setOsvc({ error: true }); }
  };

  // After a start the Oracle needs a few seconds before /api/health answers —
  // poll until it reports running, give up after ~30 s.
  const pollOsvc = (attempt = 0) => {
    clearTimeout(_oraclePollTimer);
    _oraclePollTimer = setTimeout(async () => {
      let d = null;
      try { d = await api.get('/api/oracle/service'); setOsvc(d); }
      catch { setOsvc({ error: true }); }
      if (d && !d.running && attempt < 12) pollOsvc(attempt + 1);
    }, attempt === 0 ? 1500 : 2500);
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
      loadOsvc();
    })();
    return () => clearTimeout(_oraclePollTimer);
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

  const osvcAction = async (action) => {
    setConfirmAction(null);
    setOsvcBusy(action);
    try {
      const d = await api.post(`/api/oracle/service/${action}`, {});
      const ok = d.success !== false;
      notify(d.output || `Oracle ${action} ${ok ? 'succeeded' : 'failed'}`, ok ? 'ok' : 'err');
      if (ok && d.oracle_url_set) {
        // The hub had no oracle_url; the server filled it so the top-bar link appears.
        setOracleUrl(d.oracle_url || '');
        notify(`Oracle URL set to ${d.oracle_url} — the Open Oracle button is now in the top bar`);
        if (onChanged) onChanged();
      }
      if (ok && (action === 'start' || action === 'install')) pollOsvc();
    } catch (e) {
      notify(`Oracle ${action} failed: ${e.message}`, 'err');
    }
    setOsvcBusy('');
    setTimeout(loadOsvc, 800);
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

  // Shared status line for both service blocks.
  const svcSummary = (s) => {
    let label, color;
    if (!s) { label = 'checking…'; color = T.textMuted; }
    else if (s.error) { label = 'unavailable'; color = T.error; }
    else if (!s.installed) {
      label = s.running === true ? 'running (not installed)' : 'not installed';
      color = s.running === true ? T.accent : T.textMuted;
    } else {
      label = s.running === true ? 'running' : s.running === false ? 'stopped' : 'installed';
      color = s.running !== false ? T.accent : T.textMuted;
    }
    return { label, color };
  };

  const serviceStatus = (s) => {
    const { label, color } = svcSummary(s);
    return (
      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <GlowDot color={color} />
          <span className="mono" style={{ fontSize: 12, color }}>{label}</span>
          {s && !s.error && s.method && (
            <span className="mono" style={{ fontSize: 11, color: T.textDim }}>
              {s.method}{s.port ? ` (port ${s.port})` : ''}
            </span>
          )}
        </div>
        {s && !s.error && s.log_path && (
          <div className="mono" style={{ fontSize: 11, color: T.textDim, marginBottom: 8, wordBreak: 'break-all' }}>
            log: {s.log_path}
          </div>
        )}
      </div>
    );
  };

  const busyLine = (what) => (
    <div style={{ fontSize: 11, color: T.textMuted, marginTop: 8, animation: 'pulse 1s infinite' }}>
      {what}…
    </div>
  );

  const svcInstalled = svc && !svc.error && svc.installed;
  const osvcInstalled = osvc && !osvc.error && osvc.installed;
  const osvcRunning = osvc && !osvc.error && osvc.running === true;
  const openOracle = () => {
    const url = oracleUrl.trim() || (osvc && osvc.url) || '';
    if (url) window.open(url, '_blank', 'noopener');
  };

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
          placeholder="http://localhost:3331" className="mono" style={inputStyle(220)} />
      ), 'Optional — link to a running C3 Oracle instance. Filled in automatically when you start the Oracle below.')}
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 10 }}>
        <Btn onClick={save} disabled={saving || !cfg}>{saving ? 'Saving…' : 'Save settings'}</Btn>
      </div>

      {sectionLabel('Startup service')}
      {serviceStatus(svc)}
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
      {svcBusy && busyLine(svcBusy)}
      {cfg && (
        <div style={{ fontSize: 11, color: T.textDim, marginTop: 10 }}>
          {cfg.has_terminal
            ? 'Running with a terminal window attached.'
            : 'Running as a background process (no terminal).'}
        </div>
      )}

      {sectionLabel('Oracle service')}
      {serviceStatus(osvc)}
      {osvc && !osvc.error && (
        <div className="mono" style={{ fontSize: 11, color: T.textDim, marginBottom: 8, wordBreak: 'break-all' }}>
          bind {osvc.bind_host || '127.0.0.1'} · MCP port {osvc.mcp_port || 3332} · {osvc.url}
        </div>
      )}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <Btn variant="ghost" onClick={() => osvcAction('install')} disabled={!!osvcBusy}>
          {osvcInstalled ? 'Reinstall' : 'Install'}
        </Btn>
        {osvcInstalled && (
          <Btn variant="ghost" onClick={() => setConfirmAction('oracle-uninstall')} disabled={!!osvcBusy}>Uninstall</Btn>
        )}
        {!osvcRunning && (
          <Btn variant="ghost" onClick={() => osvcAction('start')} disabled={!!osvcBusy || !osvc}>
            <I name="play" size={12} color={T.textMuted} />Start oracle
          </Btn>
        )}
        {osvcRunning && (
          <Btn variant="ghost" color={T.error} onClick={() => setConfirmAction('oracle-stop')} disabled={!!osvcBusy}>Stop oracle</Btn>
        )}
        {osvcRunning && (
          <Btn variant="ghost" onClick={openOracle}>
            <I name="external" size={12} color={T.textMuted} />Open
          </Btn>
        )}
      </div>
      {osvcBusy && busyLine(`oracle ${osvcBusy}`)}
      <div style={{ fontSize: 11, color: T.textDim, marginTop: 10 }}>
        Runs <span className="mono">c3 oracle serve</span> in the background — no terminal window.
        Install also registers it to start at login, so it survives reboots.
      </div>

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
      {confirmAction === 'oracle-stop' && (
        <ConfirmDialog title="Stop Oracle"
          message="Kills the Oracle process (dashboard, discovery API and MCP endpoint). It will start again at the next login if installed, or via Start oracle."
          confirmLabel="Stop" danger
          onConfirm={() => osvcAction('stop')} onCancel={() => setConfirmAction(null)} />
      )}
      {confirmAction === 'oracle-uninstall' && (
        <ConfirmDialog title="Uninstall Oracle Service"
          message="The Oracle will no longer auto-start on login. A running Oracle keeps running."
          confirmLabel="Uninstall" danger
          onConfirm={() => osvcAction('uninstall')} onCancel={() => setConfirmAction(null)} />
      )}
    </Modal>
  );
}
