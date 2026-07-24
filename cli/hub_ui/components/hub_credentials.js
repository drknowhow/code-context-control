// ─── Hub credentials: global vault + cross-project manager ─────
// CredsManager is the shared manager (also used by the drill Credentials tab);
// HubCredentials is the top-level mainView='creds' page.
// Write-only wire: values are submitted inbound-only and never returned by
// any hub route — rows show length + fingerprint, never the secret itself.

const HUB_CREDS_EMPTY_FORM = {
  name: '', value: '', scope: 'project', type: 'token',
  description: '', env_var: '', agent_readable: false, inject: false,
};

// path=null → the global vault (~/.c3): scope locked to 'global'.
// path=string → that project's merged view (global entries + project shadows).
function CredsManager({ path, projectName, onChanged }) {
  const isGlobal = !path;
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState(null);        // null = closed; {…} = create/edit
  const [editing, setEditing] = useState(false);
  const [checks, setChecks] = useState({});      // name -> {resolvable, fingerprint}
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState('');
  const [importScope, setImportScope] = useState(isGlobal ? 'global' : 'project');

  const withPath = (obj) => (path ? Object.assign({ path }, obj) : obj);
  const pathQuery = path ? '&path=' + encodeURIComponent(path) : '';

  const load = useCallback(async () => {
    try {
      if (path) {
        const data = await api.get('/api/projects/credentials?path=' + encodeURIComponent(path));
        setEntries((data && data.entries) || []);
      } else {
        const data = await api.get('/api/hub/credentials/overview');
        setEntries((((data || {}).global) || {}).entries || []);
      }
      setError('');
    } catch (e) { setError(String(e)); }
    setLoading(false);
  }, [path]);

  useEffect(() => { setLoading(true); setChecks({}); load(); }, [load]);

  const done = (msg) => { notify(msg); load(); if (onChanged) onChanged(); };

  const saveForm = async () => {
    if (!form || !form.name.trim()) return;
    setBusy(true);
    try {
      const payload = withPath({
        name: form.name.trim(), scope: isGlobal ? 'global' : form.scope,
        type: form.type, description: form.description, env_var: form.env_var,
        agent_readable: !!form.agent_readable, inject: !!form.inject,
      });
      if (form.value) payload.value = form.value; // blank on edit = keep stored value
      const resp = await api.post('/api/projects/credentials', payload);
      if (resp && resp.error) { setError(resp.error); }
      else {
        setForm(null);
        done(`Saved '${payload.name}' (${payload.scope})`);
      }
    } catch (e) { setError(String(e)); }
    setBusy(false);
  };

  const removeEntry = async (entry) => {
    if (!window.confirm(`Delete credential '${entry.name}' (${entry.scope})? The stored value is destroyed.`)) return;
    setBusy(true);
    try {
      await api.del(`/api/projects/credentials/${encodeURIComponent(entry.name)}?scope=${entry.scope}${pathQuery}`);
      done(`Deleted '${entry.name}'`);
    } catch (e) { setError(String(e)); }
    setBusy(false);
  };

  const checkEntry = async (entry) => {
    try {
      const data = await api.post(
        `/api/projects/credentials/${encodeURIComponent(entry.name)}/check`, withPath({}));
      setChecks(prev => Object.assign({}, prev, { [entry.name]: data }));
    } catch (e) { setError(String(e)); }
  };

  const toggleFlag = async (entry, field) => {
    if (field === 'agent_readable' && !entry.agent_readable) {
      if (!window.confirm(
        `Enable agent_readable for '${entry.name}'?\n\nThe agent will be able to read this value into its context ` +
        'and conversation transcripts. Keep it off to allow injection-only use.'
      )) return;
    }
    try {
      const resp = await api.post('/api/projects/credentials', withPath({
        name: entry.name, scope: entry.scope, [field]: !entry[field],
      }));
      if (resp && resp.error) setError(resp.error); else { load(); if (onChanged) onChanged(); }
    } catch (e) { setError(String(e)); }
  };

  const runImport = async () => {
    if (!importText.trim()) return;
    setBusy(true);
    try {
      const resp = await api.post('/api/projects/credentials/import',
        withPath({ text: importText, scope: isGlobal ? 'global' : importScope }));
      if (resp && resp.error) setError(resp.error);
      else {
        setImportText(''); setImportOpen(false);
        done(`Imported ${(resp.created || []).length}, skipped ${(resp.skipped || []).length}`);
      }
    } catch (e) { setError(String(e)); }
    setBusy(false);
  };

  const inputStyle = drillFieldStyle({ width: '100%', boxSizing: 'border-box' });
  const labelStyle = { fontSize: 11, color: T.textMuted, marginBottom: 4, display: 'block' };

  const openCreate = () => {
    setForm(Object.assign({}, HUB_CREDS_EMPTY_FORM, isGlobal ? { scope: 'global' } : {}));
    setEditing(false);
  };
  const openEdit = (entry) => {
    setForm({
      name: entry.name, value: '', scope: entry.scope, type: entry.type || 'token',
      description: entry.description || '', env_var: entry.env_var || '',
      agent_readable: !!entry.agent_readable, inject: !!entry.inject,
    });
    setEditing(true);
  };

  const fmtWhen = (iso) => iso ? String(iso).replace('T', ' ').replace(/\+.*$/, '') : '—';

  return (
    <div className="fade-up">
      {/* Toolbar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <span style={{ fontSize: 12, color: T.textMuted }}>
          {isGlobal ? 'Shared vault — entries visible in every C3 project.'
            : `Merged view for ${projectName || 'this project'} — project entries shadow same-named globals.`}
        </span>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => setImportOpen(!importOpen)} style={{
          background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
          padding: '5px 11px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
        }}>Import .env</button>
        <button className="btn" onClick={openCreate} style={{
          background: T.accent, color: '#fff', border: 'none',
          padding: '5px 11px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
        }}>+ Add credential</button>
      </div>

      {error && (
        <div style={{
          padding: '8px 12px', borderRadius: 6, marginBottom: 10, fontSize: 12,
          background: `${T.error}22`, color: T.error, border: `1px solid ${T.error}55`,
        }}>{error}</div>
      )}

      {/* .env import */}
      {importOpen && (
        <div style={{
          border: `1px solid ${T.border}`, borderRadius: 8, padding: 12,
          marginBottom: 12, background: T.surface,
        }}>
          <span style={labelStyle}>Paste KEY=VALUE lines (comments and `export` prefixes are tolerated)</span>
          <textarea rows={5} value={importText} onChange={e => setImportText(e.target.value)}
            style={Object.assign({}, inputStyle, { fontFamily: 'monospace', resize: 'vertical' })}
            autoComplete="off" spellCheck={false} />
          <div style={{ display: 'flex', gap: 10, marginTop: 8, alignItems: 'center' }}>
            {!isGlobal && (
              <select value={importScope} onChange={e => setImportScope(e.target.value)}
                style={Object.assign({}, inputStyle, { width: 150 })}>
                <option value="project">project scope</option>
                <option value="global">global scope</option>
              </select>
            )}
            <button className="btn" disabled={busy} onClick={runImport} style={{
              background: T.accent, color: '#fff', border: 'none',
              padding: '5px 13px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
            }}>Import</button>
          </div>
        </div>
      )}

      {/* Create / edit form */}
      {form && (
        <div style={{
          border: `1px solid ${T.accent}55`, borderRadius: 8, padding: 12,
          marginBottom: 12, background: T.surface,
        }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: T.text, marginBottom: 10 }}>
            {editing ? `Edit '${form.name}'` : (isGlobal ? 'New global credential' : 'New credential')}
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <div>
              <span style={labelStyle}>Name (env-var safe)</span>
              <input value={form.name} disabled={editing}
                onChange={e => setForm(Object.assign({}, form, { name: e.target.value }))}
                style={inputStyle} autoComplete="off" spellCheck={false} />
            </div>
            <div>
              <span style={labelStyle}>Scope</span>
              {isGlobal ? (
                <div style={Object.assign({}, inputStyle, { color: T.textMuted })}>global (all C3 projects)</div>
              ) : (
                <select value={form.scope} disabled={editing}
                  onChange={e => setForm(Object.assign({}, form, { scope: e.target.value }))}
                  style={inputStyle}>
                  <option value="project">project (this project only)</option>
                  <option value="global">global (all C3 projects)</option>
                </select>
              )}
            </div>
            <div style={{ gridColumn: '1 / -1' }}>
              <span style={labelStyle}>
                Value{editing ? ' (leave blank to keep the stored value)' : ''}
              </span>
              {form.type === 'multiline' ? (
                <textarea rows={4} value={form.value}
                  onChange={e => setForm(Object.assign({}, form, { value: e.target.value }))}
                  style={Object.assign({}, inputStyle, { fontFamily: 'monospace', resize: 'vertical' })}
                  autoComplete="new-password" spellCheck={false} />
              ) : (
                <input type="password" value={form.value}
                  onChange={e => setForm(Object.assign({}, form, { value: e.target.value }))}
                  style={inputStyle} autoComplete="new-password" />
              )}
            </div>
            <div>
              <span style={labelStyle}>Type</span>
              <select value={form.type}
                onChange={e => setForm(Object.assign({}, form, { type: e.target.value }))}
                style={inputStyle}>
                <option value="token">token — single secret</option>
                <option value="env">env — env-style value</option>
                <option value="multiline">multiline — .env blob / PEM</option>
              </select>
            </div>
            <div>
              <span style={labelStyle}>Env var at injection (default: name)</span>
              <input value={form.env_var}
                onChange={e => setForm(Object.assign({}, form, { env_var: e.target.value }))}
                style={inputStyle} autoComplete="off" spellCheck={false} />
            </div>
            <div style={{ gridColumn: '1 / -1' }}>
              <span style={labelStyle}>Description</span>
              <input value={form.description}
                onChange={e => setForm(Object.assign({}, form, { description: e.target.value }))}
                style={inputStyle} autoComplete="off" />
            </div>
          </div>
          <div style={{ display: 'flex', gap: 18, marginTop: 10, fontSize: 12, color: T.text }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
              <input type="checkbox" checked={!!form.inject}
                onChange={e => setForm(Object.assign({}, form, { inject: e.target.checked }))} />
              auto-inject into every c3_shell run
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
              <input type="checkbox" checked={!!form.agent_readable}
                onChange={e => setForm(Object.assign({}, form, { agent_readable: e.target.checked }))} />
              agent_readable
            </label>
          </div>
          {form.agent_readable && (
            <div style={{
              marginTop: 8, padding: '6px 10px', borderRadius: 6, fontSize: 11,
              background: `${T.warn}22`, color: T.warn, border: `1px solid ${T.warn}55`,
            }}>
              ⚠ The agent will be able to reveal this value into its context and
              conversation transcripts. Leave off for injection-only use.
            </div>
          )}
          <div style={{ display: 'flex', gap: 10, marginTop: 12 }}>
            <button className="btn" disabled={busy || !form.name.trim() || (!editing && !form.value)}
              onClick={saveForm} style={{
                background: T.accent, color: '#fff', border: 'none',
                padding: '5px 15px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
                opacity: (busy || !form.name.trim() || (!editing && !form.value)) ? 0.5 : 1,
              }}>Save</button>
            <button className="btn" onClick={() => setForm(null)} style={{
              background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
              padding: '5px 15px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
            }}>Cancel</button>
          </div>
        </div>
      )}

      {/* Entry list */}
      {loading ? (
        <div style={{ color: T.textMuted, fontSize: 13 }}>Loading…</div>
      ) : entries.length === 0 ? (
        <div style={{
          border: `1px dashed ${T.border}`, borderRadius: 8, padding: 26,
          textAlign: 'center', color: T.textMuted, fontSize: 13,
        }}>
          {isGlobal
            ? <span>No global credentials yet. Add one here or run{' '}
              <span className="mono">c3 creds set NAME --global</span>.</span>
            : <span>No credentials registered for this project.</span>}
        </div>
      ) : (
        <div style={{ border: `1px solid ${T.border}`, borderRadius: 8, overflow: 'hidden' }}>
          {entries.map((entry, i) => {
            const chk = checks[entry.name];
            const shadowedIn = entry.shadowed_in || [];
            return (
              <div key={`${entry.scope}|${entry.name}`} style={{
                display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8,
                padding: '8px 12px', borderTop: i === 0 ? 'none' : `1px solid ${T.border}`,
                background: i % 2 ? T.surfaceAlt : T.surface, fontSize: 12,
              }}>
                <I name="lock" size={13} color={T.textMuted} />
                <span className="mono" style={{ fontWeight: 600, color: T.text, minWidth: 120 }}>
                  {entry.name}
                </span>
                <Badge color={entry.scope === 'global' ? T.accent : T.ok}>{entry.scope}</Badge>
                {!!entry.shadows_global && (
                  <span title="This project's value wins over the global entry of the same name">
                    <Badge color={T.warn}>shadows global</Badge>
                  </span>
                )}
                {shadowedIn.length > 0 && (
                  <span title={'Shadowed by a project-scoped entry in: '
                    + shadowedIn.map(s => s.name || s.path).join(', ')}>
                    <Badge color={T.warn}>shadowed in {shadowedIn.length}</Badge>
                  </span>
                )}
                <Badge color={T.textMuted}>{entry.type || 'token'}</Badge>
                <span className="mono" style={{ color: T.textMuted }}>
                  •••• len={entry.value_len}
                </span>
                {entry.env_var && (
                  <span className="mono" style={{ color: T.textMuted }}>→ ${entry.env_var}</span>
                )}
                {!!entry.inject && <Badge color={T.warn}>inject</Badge>}
                {!!entry.agent_readable && <Badge color={T.error}>agent_readable</Badge>}
                {chk && (
                  <span className="mono" style={{
                    color: chk.resolvable ? T.ok : T.error, fontSize: 11,
                  }}>
                    {chk.resolvable ? `✓ ${chk.fingerprint}` : '✗ unresolvable'}
                  </span>
                )}
                <div style={{ flex: 1 }} />
                <span style={{ color: T.textMuted, fontSize: 11 }}>
                  used {entry.use_count || 0}× · {fmtWhen(entry.last_used)}
                </span>
                <span title="Verify the value resolves" onClick={() => checkEntry(entry)}
                  style={{ cursor: 'pointer', color: T.textMuted }}>
                  <I name="refresh" size={13} />
                </span>
                <span title={entry.inject ? 'Disable auto-inject' : 'Auto-inject into every c3_shell run'}
                  onClick={() => toggleFlag(entry, 'inject')}
                  style={{ cursor: 'pointer', color: entry.inject ? T.warn : T.textMuted }}>
                  <I name="zap" size={13} />
                </span>
                <span title={entry.agent_readable
                  ? 'Revoke agent reveal access'
                  : 'Allow the agent to reveal this value (into its context!)'}
                  onClick={() => toggleFlag(entry, 'agent_readable')}
                  style={{ cursor: 'pointer', color: entry.agent_readable ? T.error : T.textMuted }}>
                  <I name="eye" size={13} />
                </span>
                <span title="Edit metadata / replace value" onClick={() => openEdit(entry)}
                  style={{ cursor: 'pointer', color: T.textMuted }}>
                  <I name="edit" size={13} />
                </span>
                <span title="Delete" onClick={() => removeEntry(entry)}
                  style={{ cursor: 'pointer', color: T.error }}>
                  <I name="trash" size={13} />
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// Top-level mainView='creds' page: Global vault | Projects sub-tabs.
function HubCredentials({ projects, onOpenDrill }) {
  const [sub, setSub] = useState('global');
  const [ov, setOv] = useState(null);
  const [ovErr, setOvErr] = useState(null);
  const [expanded, setExpanded] = useState({}); // path -> bool

  const loadOverview = async () => {
    setOvErr(null);
    try { setOv(await api.get('/api/hub/credentials/overview')); }
    catch (e) { setOvErr(e.message); }
  };
  useEffect(() => { if (sub === 'projects' && !ov) loadOverview(); }, [sub, ov]);

  const norm = (s) => String(s || '').replace(/\\/g, '/').toLowerCase();
  const findProject = (row) => (projects || []).find(p => norm(p.path) === norm(row.path));

  const subBtn = (id, label) => (
    <button key={id} onClick={() => setSub(id)} style={{
      display: 'inline-flex', alignItems: 'center', gap: 6, height: 28,
      padding: '0 12px', border: 'none', cursor: 'pointer', fontSize: 12,
      background: sub === id ? T.accentDim : 'transparent',
      color: sub === id ? T.accent : T.textMuted,
      fontWeight: sub === id ? 700 : 400,
    }}>{label}</button>
  );

  return (
    <div className="fade-up" style={{ maxWidth: 1100 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
        <I name="lock" size={18} color={T.accent} />
        <span style={{ fontSize: 15, fontWeight: 600, color: T.text }}>Credentials</span>
        <div style={{
          display: 'inline-flex', marginLeft: 10, border: `1px solid ${T.border}`,
          borderRadius: 6, overflow: 'hidden',
        }}>
          {subBtn('global', 'Global vault')}
          {subBtn('projects', 'Projects')}
        </div>
        <div style={{ flex: 1 }} />
        {sub === 'projects' && (
          <button className="btn" onClick={loadOverview} title="Refresh" style={{
            background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
            padding: '5px 11px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
          }}>Refresh</button>
        )}
      </div>
      <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 14, lineHeight: 1.5 }}>
        Values live in the OS keyring (large values in an encrypted sidecar), are
        submitted inbound-only and are <b>never</b> returned by any hub route. Agents
        use them by name via <span className="mono">c3_shell env_creds</span> or{' '}
        <span className="mono">{'{{cred:NAME}}'}</span> — decoded only at the subprocess
        boundary. <b>Global</b> entries are visible in every C3 project;{' '}
        <b>project</b> entries shadow same-named globals in their project.
      </div>

      {sub === 'global' ? (
        <CredsManager path={null} onChanged={() => setOv(null)} />
      ) : ovErr ? (
        <div style={{
          padding: '8px 12px', borderRadius: 6, fontSize: 12,
          background: `${T.error}22`, color: T.error, border: `1px solid ${T.error}55`,
        }}>Failed to load overview: {ovErr}</div>
      ) : !ov ? (
        <div style={{ color: T.textMuted, fontSize: 13 }}>Loading…</div>
      ) : (ov.projects || []).length === 0 ? (
        <div style={{
          border: `1px dashed ${T.border}`, borderRadius: 8, padding: 26,
          textAlign: 'center', color: T.textMuted, fontSize: 13,
        }}>No projects registered in the hub.</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {(ov.projects || []).map(row => {
            const open = !!expanded[row.path];
            const proj = findProject(row);
            const muted = !row.initialized || row.error;
            return (
              <div key={row.path} style={{
                border: `1px solid ${T.border}`, borderRadius: 8,
                background: T.surface, overflow: 'hidden',
              }}>
                <div onClick={() => !muted && setExpanded(prev =>
                  Object.assign({}, prev, { [row.path]: !open }))}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 10, padding: '9px 14px',
                    cursor: muted ? 'default' : 'pointer', opacity: muted ? 0.55 : 1,
                    fontSize: 12,
                  }}>
                  <span style={{ color: T.textMuted, fontSize: 10, width: 10 }}>
                    {muted ? '' : (open ? '▾' : '▸')}
                  </span>
                  <span style={{ fontWeight: 600, color: T.text }}>{row.name || row.path}</span>
                  <span className="mono" style={{
                    color: T.textDim, fontSize: 11, overflow: 'hidden',
                    textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 340,
                  }}>{row.path}</span>
                  <div style={{ flex: 1 }} />
                  {row.error ? (
                    <Badge color={T.error}>error</Badge>
                  ) : !row.initialized ? (
                    <Badge color={T.textMuted}>not initialized</Badge>
                  ) : (
                    <React.Fragment>
                      <span style={{ color: T.textMuted, fontSize: 11 }}>
                        {row.entries.length} project {row.entries.length === 1 ? 'entry' : 'entries'}
                        {row.entries.filter(e => e.shadows_global).length > 0 &&
                          ` · ${row.entries.filter(e => e.shadows_global).length} shadowing global`}
                      </span>
                      {proj && onOpenDrill && (
                        <button className="btn" onClick={(e) => { e.stopPropagation(); onOpenDrill(proj, 'creds'); }}
                          style={{
                            background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
                            padding: '3px 9px', borderRadius: 6, fontSize: 11, cursor: 'pointer',
                          }}>Open drill</button>
                      )}
                    </React.Fragment>
                  )}
                </div>
                {row.error && (
                  <div style={{ padding: '0 14px 9px 34px', fontSize: 11, color: T.error }}>{row.error}</div>
                )}
                {open && !muted && (
                  <div style={{ padding: '4px 14px 14px', borderTop: `1px solid ${T.border}` }}>
                    <CredsManager path={row.path} projectName={row.name}
                      onChanged={loadOverview} />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
