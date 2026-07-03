// ─── Config editor: typed .c3/config.json section editor ───────
// GET /api/projects/config?path=&section= → {config, defaults};
// PUT {path, section, values} (writable sections only). The
// bitbucket section is shown read-only; server-refused keys hidden.

const CFG_ED_SECTIONS = ['hybrid', 'agents', 'delegate', 'proxy', 'mcp', 'meta', 'memory_llm', 'bitbucket'];
const CFG_ED_READONLY = ['bitbucket'];
const CFG_ED_REFUSED = ['version', 'project_path', 'permission_tier', 'subprojects', 'parent', 'api_key'];

function ConfigEditor({ project }) {
  const [section, setSection] = useState('hybrid');
  const [cfg, setCfg] = useState(null);
  const [defs, setDefs] = useState({});
  const [edits, setEdits] = useState({});
  const [jsonText, setJsonText] = useState({});
  const [jsonBad, setJsonBad] = useState({});
  const [openKeys, setOpenKeys] = useState({});
  const [needsInit, setNeedsInit] = useState(false);
  const [err, setErr] = useState(null);
  const [saving, setSaving] = useState(false);

  const readOnly = CFG_ED_READONLY.indexOf(section) >= 0;

  const load = async () => {
    setErr(null);
    try {
      const d = await api.get(`/api/projects/config?path=${encodeURIComponent(project.path)}&section=${section}`);
      setCfg(d.config || {});
      setDefs(d.defaults || {});
      setEdits({}); setJsonText({}); setJsonBad({}); setOpenKeys({});
      setNeedsInit(false);
    } catch (e) {
      if (e.status === 409) setNeedsInit(true);
      else setErr(e.message);
    }
  };
  useEffect(() => { setCfg(null); load(); }, [project.path, section]);

  const keys = [];
  if (cfg) {
    Object.keys(defs).forEach(k => { if (CFG_ED_REFUSED.indexOf(k) < 0) keys.push(k); });
    Object.keys(cfg).forEach(k => {
      if (CFG_ED_REFUSED.indexOf(k) < 0 && keys.indexOf(k) < 0) keys.push(k);
    });
  }
  const currentVal = (k) => Object.prototype.hasOwnProperty.call(edits, k) ? edits[k]
    : (cfg && cfg[k] !== undefined ? cfg[k] : defs[k]);
  const kindOf = (k) => {
    const base = defs[k] !== undefined ? defs[k] : (cfg ? cfg[k] : undefined);
    if (typeof base === 'boolean') return 'bool';
    if (typeof base === 'number') return 'number';
    if (base !== null && typeof base === 'object') return 'json';
    return 'string';
  };
  const setEdit = (k, v) => setEdits(e => Object.assign({}, e, { [k]: v }));
  const isEdited = (k) => Object.prototype.hasOwnProperty.call(edits, k);

  const save = async () => {
    if (readOnly) return;
    setSaving(true);
    try {
      const values = {};
      Object.keys(edits).forEach(k => {
        let v = edits[k];
        if (kindOf(k) === 'number') {
          v = Number(v);
          if (!isFinite(v)) throw new Error(`'${k}' must be a number`);
        }
        values[k] = v;
      });
      const d = await api.put('/api/projects/config', { path: project.path, section, values });
      setCfg(d.config || {});
      setEdits({}); setJsonText({}); setJsonBad({});
      notify('Saved ' + section, 'ok');
    } catch (e) {
      notify('Save failed: ' + e.message, 'err');
    }
    setSaving(false);
  };

  const resetDefaults = () => {
    const next = {};
    Object.keys(defs).forEach(k => {
      if (CFG_ED_REFUSED.indexOf(k) < 0) next[k] = JSON.parse(JSON.stringify(defs[k] === undefined ? null : defs[k]));
    });
    setEdits(next);
    setJsonText({}); setJsonBad({});
    notify(`Prefilled ${section} defaults — review, then Save section`, 'warn');
  };

  const dirty = Object.keys(edits).length > 0;
  const invalid = Object.keys(jsonBad).some(k => jsonBad[k]);

  const renderField = (k) => {
    const kind = kindOf(k);
    const val = currentVal(k);
    const edited = isEdited(k);
    const dot = edited
      ? <span style={{ width: 6, height: 6, borderRadius: '50%', background: T.accent, display: 'inline-block', flexShrink: 0 }} />
      : null;

    if (readOnly) {
      return (
        <div key={k} style={{ display: 'flex', gap: 12, alignItems: 'flex-start', padding: '8px 0', borderBottom: `1px solid ${T.border}` }}>
          <span className="mono" style={{ fontSize: 11, color: T.textMuted, width: 200, flexShrink: 0, overflowWrap: 'anywhere' }}>{k}</span>
          <span className="mono" style={{ fontSize: 11, color: T.text, flex: 1, overflowWrap: 'anywhere' }}>
            {kind === 'json' ? JSON.stringify(val) : String(val === undefined ? '' : val)}
          </span>
        </div>
      );
    }
    if (kind === 'bool') {
      return (
        <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ flex: 1 }}>
            {renderBoolToggle(k, !!val, () => setEdit(k, !val),
              defs[k] !== undefined ? `default: ${defs[k] ? 'on' : 'off'}` : undefined)}
          </div>
          {dot}
        </div>
      );
    }
    if (kind === 'json') {
      const open = !!openKeys[k];
      const raw = jsonText[k] !== undefined ? jsonText[k] : JSON.stringify(val, null, 2);
      const count = val && typeof val === 'object' ? Object.keys(val).length : 0;
      return (
        <div key={k} style={{ padding: '6px 0' }}>
          <div onClick={() => setOpenKeys(o => Object.assign({}, o, { [k]: !open }))}
            style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', userSelect: 'none' }}>
            <I name="chevron" size={11} color={T.textMuted}
              style={{ transform: open ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform 0.15s' }} />
            <span style={{ fontSize: 12, color: T.textMuted, flex: 1 }}>{k}</span>
            {dot}
            <span className="mono" style={{ fontSize: 11, color: T.textDim }}>
              {Array.isArray(val) ? `[${count}]` : `{${count}}`}
            </span>
          </div>
          {open && (
            <textarea value={raw} spellCheck={false} rows={Math.min(12, Math.max(4, raw.split('\n').length))}
              onChange={ev => {
                const text = ev.target.value;
                setJsonText(t => Object.assign({}, t, { [k]: text }));
                try {
                  const parsed = JSON.parse(text);
                  setEdit(k, parsed);
                  setJsonBad(b => Object.assign({}, b, { [k]: false }));
                } catch (e2) {
                  setJsonBad(b => Object.assign({}, b, { [k]: true }));
                }
              }}
              className="mono"
              style={drillFieldStyle({
                width: '100%', marginTop: 8, fontSize: 11, lineHeight: 1.5, resize: 'vertical',
                fontFamily: "'JetBrains Mono', monospace", boxSizing: 'border-box',
                borderColor: jsonBad[k] ? T.error : T.border,
              })} />
          )}
          {open && jsonBad[k] &&
            <div style={{ fontSize: 11, color: T.error, marginTop: 4 }}>Invalid JSON — fix before saving.</div>}
        </div>
      );
    }
    // number | string
    return (
      <label key={k} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: '8px 0' }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
          <span style={{ color: T.textMuted, fontSize: 12, overflowWrap: 'anywhere' }}>{k}</span>
          {dot}
        </span>
        <input type={kind === 'number' ? 'number' : 'text'}
          value={val == null ? '' : String(val)}
          onChange={ev => setEdit(k, kind === 'number'
            ? (ev.target.value === '' ? '' : Number(ev.target.value))
            : ev.target.value)}
          className="mono"
          style={drillFieldStyle({ width: kind === 'number' ? 110 : 220, textAlign: kind === 'number' ? 'right' : 'left', fontSize: 11 })} />
      </label>
    );
  };

  return (
    <div className="fade-up">
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 16 }}>
        {CFG_ED_SECTIONS.map(s => (
          <button key={s} onClick={() => setSection(s)} className="mono" style={{
            padding: '5px 12px', borderRadius: 999, fontSize: 11, fontWeight: 700, cursor: 'pointer',
            border: `1px solid ${section === s ? T.accent : T.border}`,
            background: section === s ? T.accentDim : 'transparent',
            color: section === s ? T.accent : T.textMuted,
          }}>
            {s}{CFG_ED_READONLY.indexOf(s) >= 0 ? ' (ro)' : ''}
          </button>
        ))}
      </div>

      {needsInit && <DrillNeedsInit project={project} onReady={load} />}
      {!needsInit && err && <DrillMsg text={'Failed to load config: ' + err} color={T.error} />}
      {!needsInit && !err && !cfg && <DrillMsg text="Loading config…" />}

      {!needsInit && !err && cfg && (
        <React.Fragment>
          {readOnly && (
            <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 10 }}>
              This section is read-only in the hub (credentials live in the OS keyring — use <span className="mono">c3 bitbucket login</span>).
            </div>
          )}
          {section === 'memory_llm' && (
            <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 10 }}>
              <span className="mono">cloud_enabled</span> sends session content to Ollama Cloud — leave off for local-only privacy (<span className="mono">local_model</span> is used instead).
              The cloud API key is not editable here: it lives in the OS keyring — set it in the project's Settings tab or via <span className="mono">OLLAMA_API_KEY</span>.
            </div>
          )}
          {keys.length === 0 && <DrillMsg text="No editable keys in this section." />}
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            {keys.map(renderField)}
          </div>
          {!readOnly && (
            <div style={{
              display: 'flex', alignItems: 'center', gap: 10, marginTop: 20,
              paddingTop: 14, borderTop: `1px solid ${T.border}`,
            }}>
              <Btn onClick={save} disabled={saving || !dirty || invalid}>
                <I name="save" size={12} color={T.bg} />
                {saving ? 'Saving…' : 'Save section'}
              </Btn>
              <Btn variant="ghost" onClick={resetDefaults} disabled={saving}>Reset to defaults</Btn>
              {dirty && (
                <span className="mono" style={{ fontSize: 11, color: T.textMuted }}>
                  {Object.keys(edits).length} change{Object.keys(edits).length === 1 ? '' : 's'} pending
                </span>
              )}
            </div>
          )}
        </React.Fragment>
      )}
    </div>
  );
}
