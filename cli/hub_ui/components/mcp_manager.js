// ─── MCP manager: servers list + add/update form + C3 setup ────
// Ports the old hub.html MCP modal into a drill tab. Sub-tabs:
// "Servers" (list/add/edit/remove) and "Setup" (C3 install-mcp,
// remove, and the full c3 init runner).

function mcpParseArgs(raw) {
  const text = (raw || '').trim();
  if (!text) return [];
  if (text[0] === '[') {
    const parsed = JSON.parse(text);
    if (!Array.isArray(parsed)) throw new Error('Args JSON must be an array.');
    return parsed.map(item => String(item));
  }
  return text.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
}

function mcpParseEnv(raw) {
  const text = (raw || '').trim();
  if (!text) return {};
  const parsed = JSON.parse(text);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Env must be a JSON object.');
  }
  return parsed;
}

const MCP_EMPTY_FORM = { name: '', command: '', args: '', env: '', enabled: true };

function McpManager({ project, onChanged }) {
  const [sub, setSub] = useState('servers');
  const [caps, setCaps] = useState(null);
  const [details, setDetails] = useState(null);
  const [err, setErr] = useState(null);
  const [ide, setIde] = useState('');
  const [mode, setMode] = useState('');
  const [output, setOutput] = useState('');
  const [outKind, setOutKind] = useState('');
  const [busy, setBusy] = useState('');
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState(MCP_EMPTY_FORM);
  const [confirmRemove, setConfirmRemove] = useState(null);
  const [initMode, setInitMode] = useState('force');
  const [git, setGit] = useState(false);

  const setOut = (text, kind) => { setOutput(text || ''); setOutKind(kind || ''); };

  const loadDetails = async () => {
    setErr(null);
    try {
      const d = await api.post('/api/projects/details', { path: project.path, ide: ide || null });
      setDetails(d);
    } catch (e) {
      setErr(e.message);
    }
  };
  useEffect(() => {
    api.get('/api/projects/mcp-capabilities').then(setCaps).catch(() => {});
  }, []);
  useEffect(() => { setDetails(null); loadDetails(); }, [project.path, ide]);

  const effIde = ide || (details && details.ide) || 'claude-code';
  const servers = ((details && details.mcp_servers) || []).slice().sort((a, b) => {
    if (a.name === 'c3') return -1;
    if (b.name === 'c3') return 1;
    return a.name.localeCompare(b.name);
  });
  const c3Installed = servers.some(s => s.name === 'c3');

  const refreshAfterChange = async () => {
    await loadDetails();
    if (onChanged) onChanged();
  };

  const prefill = (server) => {
    const env = {};
    (server.env_keys || []).forEach(k => { env[k] = ''; });
    setForm({
      name: server.name || '',
      command: server.command || '',
      args: (server.args || []).join('\n'),
      env: Object.keys(env).length ? JSON.stringify(env, null, 2) : '',
      enabled: server.enabled !== false,
    });
    setFormOpen(true);
  };

  const saveServer = async () => {
    try {
      const name = form.name.trim();
      const command = form.command.trim();
      if (!name || !command) throw new Error('Server name and command are required.');
      const payload = {
        path: project.path, ide: ide || null, name, command,
        args: mcpParseArgs(form.args), env: mcpParseEnv(form.env), enabled: !!form.enabled,
      };
      setBusy('save');
      const d = await api.post('/api/projects/mcp-server-add', payload);
      setOut(`Saved MCP server "${name}" to ${d.config_path || 'config'}.`, 'ok');
      notify('MCP server saved', 'ok');
      setFormOpen(false);
      setForm(MCP_EMPTY_FORM);
      await refreshAfterChange();
    } catch (e) {
      setOut('Error: ' + e.message, 'err');
      notify('Error: ' + e.message, 'err');
    }
    setBusy('');
  };

  const removeServer = async (name) => {
    setBusy('remove:' + name);
    setOut(`Removing MCP server "${name}"…`);
    try {
      const d = await api.post('/api/projects/run-mcp-remove', { path: project.path, name, ide: ide || null });
      const ok = !!(d && d.success);
      setOut(d.output || (ok ? `Removed "${name}".` : 'Remove failed.'), ok ? 'ok' : 'err');
      notify(ok ? `Removed MCP server "${name}"` : 'MCP remove failed', ok ? 'ok' : 'err');
      if (ok) await refreshAfterChange();
    } catch (e) {
      setOut('Error: ' + e.message, 'err');
      notify('Error: ' + e.message, 'err');
    }
    setBusy('');
  };

  const installC3 = async () => {
    setBusy('install');
    setOut('Running install-mcp…');
    try {
      const d = await api.post('/api/projects/run-mcp', {
        path: project.path, ide: ide || null, mcp_mode: mode || null,
      });
      setOut(d.output || '(no output)', d.success ? 'ok' : 'err');
      notify(d.success ? 'C3 MCP updated' : 'C3 MCP update failed', d.success ? 'ok' : 'err');
      if (d.success) await refreshAfterChange();
    } catch (e) {
      setOut('Error: ' + e.message, 'err');
      notify('Error: ' + e.message, 'err');
    }
    setBusy('');
  };

  const runInit = async () => {
    setBusy('init');
    setOut('Running c3 init… this can take a while.');
    try {
      const payload = { path: project.path, ide: ide || null, init_mode: initMode };
      if (initMode === 'force') {
        payload.git = git;
        if (mode) payload.mcp_mode = mode;
      }
      const d = await api.post('/api/projects/run-init', payload);
      setOut(d.output || '(no output)', d.success ? 'ok' : 'err');
      notify(d.success ? 'Project initialized' : 'Init finished with errors', d.success ? 'ok' : 'err');
      if (d.success) await refreshAfterChange();
    } catch (e) {
      setOut('Error: ' + e.message, 'err');
      notify('Error: ' + e.message, 'err');
    }
    setBusy('');
  };

  const selectStyle = drillFieldStyle({ flex: 1 });
  const ideOptions = (caps && caps.ides) || [];
  const modeOptions = (caps && caps.modes) || ['direct', 'proxy'];

  const outputBlock = output ? (
    <pre className="mono" style={{
      margin: '16px 0 0', padding: 12, background: T.bg,
      border: `1px solid ${outKind === 'err' ? T.error + '55' : T.border}`, borderRadius: 8,
      fontSize: 11, color: outKind === 'err' ? T.error : T.textMuted,
      whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 220, overflowY: 'auto',
    }}>{output}</pre>
  ) : null;

  const selectorRow = (
    <div style={{ display: 'flex', gap: 10, marginBottom: 16 }}>
      <label style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 4 }}>
        <span style={{ fontSize: 11, color: T.textMuted, textTransform: 'uppercase', letterSpacing: 1, fontWeight: 600 }}>IDE profile</span>
        <select value={ide} onChange={e => setIde(e.target.value)} style={selectStyle}>
          <option value="">Auto ({ideLabel((details && details.ide) || 'unknown')})</option>
          {ideOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </label>
      <label style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 4 }}>
        <span style={{ fontSize: 11, color: T.textMuted, textTransform: 'uppercase', letterSpacing: 1, fontWeight: 600 }}>MCP mode</span>
        <select value={mode} onChange={e => setMode(e.target.value)} style={selectStyle}>
          <option value="">Auto ({(details && details.mcp_mode) || 'direct'})</option>
          {modeOptions.map(m => <option key={m} value={m}>{m}</option>)}
        </select>
      </label>
    </div>
  );

  return (
    <div className="fade-up">
      <div style={{ display: 'flex', gap: 6, marginBottom: 16 }}>
        {[['servers', 'Servers'], ['setup', 'Setup']].map(([id, label]) => (
          <button key={id} onClick={() => setSub(id)} className="mono" style={{
            padding: '5px 14px', borderRadius: 999, fontSize: 11, fontWeight: 700, cursor: 'pointer',
            border: `1px solid ${sub === id ? T.accent : T.border}`,
            background: sub === id ? T.accentDim : 'transparent',
            color: sub === id ? T.accent : T.textMuted,
          }}>{label}</button>
        ))}
      </div>

      {err && <DrillMsg text={'Failed to load MCP status: ' + err} color={T.error} />}
      {!err && !details && <DrillMsg text="Loading MCP status…" />}

      {!err && details && sub === 'servers' && (
        <React.Fragment>
          <div className="mono" style={{
            display: 'flex', gap: 16, flexWrap: 'wrap', fontSize: 11, color: T.textMuted,
            padding: '10px 12px', background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8,
          }}>
            <span><span style={{ color: T.textDim }}>IDE</span> {ideLabel(details.ide)}</span>
            <span><span style={{ color: T.textDim }}>Mode</span> {details.mcp_mode || 'direct'}</span>
            <span><span style={{ color: T.textDim }}>Servers</span> {servers.length}</span>
            <span><span style={{ color: T.textDim }}>C3</span> v{details.hub_c3_version || '?'}</span>
          </div>
          {details.mcp_config_path && (
            <div className="mono" title={details.mcp_config_path} style={{
              fontSize: 11, color: T.textDim, marginTop: 8,
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
            }}>{details.mcp_config_path}</div>
          )}

          {!details.mcp_installed && (
            <DrillCenter>
              <I name="wrench" size={20} color={T.textDim} />
              <div style={{ fontSize: 13, fontWeight: 700, color: T.text }}>MCP is not installed for this project yet</div>
              <Btn onClick={() => setSub('setup')}>Set up C3 MCP</Btn>
            </DrillCenter>
          )}

          {details.mcp_installed && servers.length === 0 && (
            <DrillMsg text="No servers defined in the active config." />
          )}

          {servers.map(s => {
            const isC3 = s.name === 'c3';
            const args = (s.args || []).join(' ');
            const envKeys = (s.env_keys || []).join(', ');
            return (
              <div key={s.name} style={{
                marginTop: 12, padding: '12px 14px', borderRadius: 8,
                border: `1px solid ${isC3 ? T.accent + '40' : T.border}`,
                background: isC3 ? T.accentDim : 'transparent',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontSize: 13, fontWeight: 700, color: T.text, flex: 1 }}>
                    {s.name} {isC3 && <Badge color={T.accent}>C3</Badge>}
                  </span>
                  <Btn variant="ghost" onClick={() => prefill(s)} style={{ padding: '4px 10px', fontSize: 11 }}>Edit</Btn>
                  <Btn variant="ghost" color={T.error} onClick={() => setConfirmRemove(s.name)}
                    disabled={busy === 'remove:' + s.name}
                    style={{ padding: '4px 10px', fontSize: 11, color: T.error }}>
                    {busy === 'remove:' + s.name ? '…' : 'Remove'}
                  </Btn>
                </div>
                <div className="mono" style={{ fontSize: 11, color: T.textMuted, marginTop: 6, overflowWrap: 'anywhere' }}>
                  {s.command || '—'}{args ? ' ' + args : ''}
                </div>
                {envKeys && (
                  <div className="mono" style={{ fontSize: 11, color: T.textDim, marginTop: 4 }}>env: {envKeys}</div>
                )}
              </div>
            );
          })}

          {details.mcp_installed && !formOpen && (
            <div style={{ marginTop: 14 }}>
              <Btn variant="ghost" onClick={() => { setForm(MCP_EMPTY_FORM); setFormOpen(true); }}>
                <I name="plus" size={12} color={T.textMuted} /> Add server
              </Btn>
            </div>
          )}

          {formOpen && (
            <div style={{ marginTop: 14, padding: 14, border: `1px solid ${T.border}`, borderRadius: 8 }}>
              <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 1, color: T.textMuted, marginBottom: 10 }}>
                Add / update server
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                <input placeholder="Server name (e.g. c3, github)" value={form.name}
                  onChange={e => setForm(f => Object.assign({}, f, { name: e.target.value }))}
                  className="mono" style={drillFieldStyle({ fontSize: 11 })} />
                <input placeholder="Command (e.g. python, npx)" value={form.command}
                  onChange={e => setForm(f => Object.assign({}, f, { command: e.target.value }))}
                  className="mono" style={drillFieldStyle({ fontSize: 11 })} />
                <textarea placeholder={'Args — one per line, or a JSON array'} rows={3} spellCheck={false}
                  value={form.args}
                  onChange={e => setForm(f => Object.assign({}, f, { args: e.target.value }))}
                  className="mono" style={drillFieldStyle({ fontSize: 11, resize: 'vertical', fontFamily: "'JetBrains Mono', monospace" })} />
                <textarea placeholder={'Env — JSON object, e.g. {"API_KEY": "…"}'} rows={2} spellCheck={false}
                  value={form.env}
                  onChange={e => setForm(f => Object.assign({}, f, { env: e.target.value }))}
                  className="mono" style={drillFieldStyle({ fontSize: 11, resize: 'vertical', fontFamily: "'JetBrains Mono', monospace" })} />
                {effIde === 'codex' &&
                  renderBoolToggle('Enabled', form.enabled,
                    () => setForm(f => Object.assign({}, f, { enabled: !f.enabled })),
                    'Codex profiles support disabling a server without removing it.')}
              </div>
              <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
                <Btn onClick={saveServer} disabled={busy === 'save'}>
                  {busy === 'save' ? 'Saving…' : 'Save server'}
                </Btn>
                <Btn variant="ghost" onClick={() => { setFormOpen(false); setForm(MCP_EMPTY_FORM); }}>Cancel</Btn>
              </div>
            </div>
          )}
          {outputBlock}
        </React.Fragment>
      )}

      {!err && details && sub === 'setup' && (
        <React.Fragment>
          {selectorRow}

          <DrillSection label="C3 MCP server" style={{ marginTop: 4 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
              <Badge color={c3Installed ? T.accent : T.warn}>{c3Installed ? 'installed' : 'not installed'}</Badge>
              {details.mcp_config_path && (
                <span className="mono" title={details.mcp_config_path} style={{
                  fontSize: 11, color: T.textDim, minWidth: 0,
                  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                }}>{details.mcp_config_path}</span>
              )}
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <Btn onClick={installC3} disabled={busy === 'install'}>
                <I name="download" size={12} color={T.bg} />
                {busy === 'install' ? 'Installing…' : (c3Installed ? 'Update C3' : 'Install C3')}
              </Btn>
              {c3Installed && (
                <Btn variant="ghost" onClick={() => setConfirmRemove('c3')}
                  disabled={busy === 'remove:c3'} style={{ color: T.error }}>
                  {busy === 'remove:c3' ? 'Removing…' : 'Remove C3'}
                </Btn>
              )}
            </div>
          </DrillSection>

          <DrillSection label="Init runner">
            <div style={{ display: 'flex', gap: 10, marginBottom: 6 }}>
              <label style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 4 }}>
                <span style={{ fontSize: 11, color: T.textMuted, textTransform: 'uppercase', letterSpacing: 1, fontWeight: 600 }}>Init mode</span>
                <select value={initMode} onChange={e => setInitMode(e.target.value)} style={selectStyle}>
                  <option value="force">Update — re-run init, keep memory</option>
                  <option value="clear">Clear — wipe .c3 and start fresh</option>
                </select>
              </label>
            </div>
            {initMode === 'force' &&
              renderBoolToggle('Install git hooks', git, () => setGit(v => !v),
                'Adds C3 post-commit enrichment hooks during init.')}
            <div style={{ marginTop: 10 }}>
              <Btn onClick={runInit} disabled={busy === 'init'}>
                <I name="play" size={12} color={T.bg} />
                {busy === 'init' ? 'Running init…' : 'Run c3 init'}
              </Btn>
            </div>
          </DrillSection>
          {outputBlock}
        </React.Fragment>
      )}

      {confirmRemove && (
        <ConfirmDialog
          title="Remove MCP server"
          message={`Remove "${confirmRemove}" from the ${ideLabel(effIde)} MCP config? The server entry is deleted from the IDE config file.`}
          confirmLabel="Remove" danger
          onConfirm={() => { const n = confirmRemove; setConfirmRemove(null); removeServer(n); }}
          onCancel={() => setConfirmRemove(null)} />
      )}
    </div>
  );
}
