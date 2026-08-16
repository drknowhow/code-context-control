// CredentialsPanel — credential vault manager (v2.58.0)
// Globals: T, I, api, useState, useEffect, useCallback, Badge
// Write-only wire: the UI submits values but the API never returns one —
// rows show length + fingerprint, never the secret itself.

const CREDS_EMPTY_FORM = {
  name: "", value: "", scope: "project", type: "token",
  description: "", env_var: "", agent_readable: false, inject: false,
  fields: {},
};

// Structured kinds (v2.87.0): per-type field sets, composed into a JSON
// object client-side. `hidden` fields render as password inputs. These
// entries are inject-only — the server refuses agent_readable/inject.
const CREDS_STRUCTURED = {
  card:     { required: ["cardholder", "number", "expiry"],
              optional: ["cvc", "billing_zip"], hidden: ["number", "cvc"] },
  address:  { required: ["street1", "city", "state", "zip"],
              optional: ["recipient", "street2", "country", "phone"], hidden: [] },
  identity: { required: ["full_name"],
              optional: ["dob", "ssn", "phone", "email"], hidden: ["ssn", "dob"] },
};

const credsDisplayText = (entry) => {
  const d = entry.display || {};
  if (entry.type === "card") return `${d.brand || "card"} ••••${d.last4 || "????"}`;
  const vals = Object.values(d).filter(Boolean);
  return vals.length ? vals.join(", ") : "";
};

const CredentialsPanel = () => {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState(null);        // null = closed; {…} = create/edit
  const [editing, setEditing] = useState(false); // true when form edits an existing entry
  const [checks, setChecks] = useState({});      // name -> {resolvable, fingerprint}
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [importScope, setImportScope] = useState("project");

  const load = useCallback(async () => {
    try {
      const data = await api.get("/api/credentials");
      setEntries((data && data.entries) || []);
      setError("");
    } catch (e) { setError(String(e)); }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const flash = (msg) => { setNotice(msg); setTimeout(() => setNotice(""), 4000); };

  const saveForm = async () => {
    if (!form || !form.name.trim()) return;
    setBusy(true);
    try {
      const structured = !!CREDS_STRUCTURED[form.type];
      const payload = {
        name: form.name.trim(), scope: form.scope, type: form.type,
        description: form.description, env_var: form.env_var,
        agent_readable: structured ? false : !!form.agent_readable,
        inject: structured ? false : !!form.inject,
      };
      if (structured) {
        // Only submit fields the user actually typed — the store MERGES a
        // partial payload into the existing entry, so an edit can change
        // one field without retyping the card number.
        const typed = {};
        Object.entries(form.fields || {}).forEach(([k, v]) => {
          if (String(v || "").trim()) typed[k] = String(v).trim();
        });
        if (Object.keys(typed).length) payload.value = typed;
      } else if (form.value) payload.value = form.value; // blank on edit = keep stored value
      const resp = await api.post("/api/credentials", payload);
      if (resp && resp.error) { setError(resp.error); }
      else {
        setForm(null);
        flash(`Saved '${payload.name}' (${payload.scope})`);
        load();
      }
    } catch (e) { setError(String(e)); }
    setBusy(false);
  };

  const removeEntry = async (entry) => {
    if (!window.confirm(`Delete credential '${entry.name}' (${entry.scope})? The stored value is destroyed.`)) return;
    setBusy(true);
    try {
      await api.del(`/api/credentials/${encodeURIComponent(entry.name)}?scope=${entry.scope}`);
      flash(`Deleted '${entry.name}'`);
      load();
    } catch (e) { setError(String(e)); }
    setBusy(false);
  };

  const checkEntry = async (entry) => {
    try {
      const data = await api.post(`/api/credentials/${encodeURIComponent(entry.name)}/check`, {});
      setChecks(prev => ({ ...prev, [entry.name]: data }));
    } catch (e) { setError(String(e)); }
  };

  const toggleFlag = async (entry, field) => {
    if (field === "agent_readable" && !entry.agent_readable) {
      if (!window.confirm(
        `Enable agent_readable for '${entry.name}'?\n\nThe agent will be able to read this value into its context ` +
        "and conversation transcripts. Keep it off to allow injection-only use."
      )) return;
    }
    try {
      const resp = await api.post("/api/credentials", {
        name: entry.name, scope: entry.scope, [field]: !entry[field],
      });
      if (resp && resp.error) setError(resp.error); else load();
    } catch (e) { setError(String(e)); }
  };

  const runImport = async () => {
    if (!importText.trim()) return;
    setBusy(true);
    try {
      const resp = await api.post("/api/credentials/import",
        { text: importText, scope: importScope });
      if (resp && resp.error) setError(resp.error);
      else {
        flash(`Imported ${(resp.created || []).length}, skipped ${(resp.skipped || []).length}`);
        setImportText(""); setImportOpen(false);
        load();
      }
    } catch (e) { setError(String(e)); }
    setBusy(false);
  };

  const inputStyle = {
    width: "100%", padding: "7px 10px", borderRadius: 6, fontSize: 12,
    border: `1px solid ${T.border}`, background: T.surface, color: T.text,
    outline: "none", boxSizing: "border-box",
  };
  const labelStyle = { fontSize: 11, color: T.textMuted, marginBottom: 4, display: "block" };

  const openCreate = () => { setForm({ ...CREDS_EMPTY_FORM }); setEditing(false); };
  const openEdit = (entry) => {
    setForm({
      name: entry.name, value: "", scope: entry.scope, type: entry.type || "token",
      description: entry.description || "", env_var: entry.env_var || "",
      agent_readable: !!entry.agent_readable, inject: !!entry.inject,
      fields: {},
    });
    setEditing(true);
  };

  const fmtWhen = (iso) => iso ? String(iso).replace("T", " ").replace(/\+.*$/, "") : "—";

  return (
    <div style={{ padding: 20, height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
        <I name="lock" size={18} color={T.accent} />
        <span style={{ fontSize: 15, fontWeight: 600, color: T.text }}>Credentials</span>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => setImportOpen(!importOpen)} style={{
          background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
          padding: "6px 12px", borderRadius: 6, fontSize: 12, cursor: "pointer",
        }}>Import .env</button>
        <button className="btn" onClick={openCreate} style={{
          background: T.accent, color: "#fff", border: "none",
          padding: "6px 12px", borderRadius: 6, fontSize: 12, cursor: "pointer",
        }}>+ Add credential</button>
      </div>
      <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 14, lineHeight: 1.5 }}>
        Values live in the OS keyring (large values in an encrypted sidecar) and are
        <b> never</b> sent back to the browser or the agent's context. Agents use them by
        name via <span className="mono">c3_shell env_creds</span> or{" "}
        <span className="mono">{"{{cred:NAME}}"}</span> — decoded only at the subprocess
        boundary. <b>Global</b> entries are visible in every C3 project; <b>project</b>{" "}
        entries shadow same-named globals here.
      </div>

      {error && (
        <div style={{
          padding: "8px 12px", borderRadius: 6, marginBottom: 10, fontSize: 12,
          background: `${T.error}22`, color: T.error, border: `1px solid ${T.error}55`,
        }}>{error}</div>
      )}
      {notice && (
        <div style={{
          padding: "8px 12px", borderRadius: 6, marginBottom: 10, fontSize: 12,
          background: `${T.accent}22`, color: T.accent, border: `1px solid ${T.accent}55`,
        }}>{notice}</div>
      )}

      {/* .env import */}
      {importOpen && (
        <div style={{
          border: `1px solid ${T.border}`, borderRadius: 8, padding: 14,
          marginBottom: 14, background: T.surface,
        }}>
          <span style={labelStyle}>Paste KEY=VALUE lines (comments and `export` prefixes are tolerated)</span>
          <textarea rows={5} value={importText} onChange={e => setImportText(e.target.value)}
            style={{ ...inputStyle, fontFamily: "monospace", resize: "vertical" }}
            autoComplete="off" spellCheck={false} />
          <div style={{ display: "flex", gap: 10, marginTop: 8, alignItems: "center" }}>
            <select value={importScope} onChange={e => setImportScope(e.target.value)}
              style={{ ...inputStyle, width: 140 }}>
              <option value="project">project scope</option>
              <option value="global">global scope</option>
            </select>
            <button className="btn" disabled={busy} onClick={runImport} style={{
              background: T.accent, color: "#fff", border: "none",
              padding: "6px 14px", borderRadius: 6, fontSize: 12, cursor: "pointer",
            }}>Import</button>
          </div>
        </div>
      )}

      {/* Create / edit form */}
      {form && (
        <div style={{
          border: `1px solid ${T.accent}55`, borderRadius: 8, padding: 14,
          marginBottom: 14, background: T.surface,
        }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: T.text, marginBottom: 10 }}>
            {editing ? `Edit '${form.name}'` : "New credential"}
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
            <div>
              <span style={labelStyle}>Name (env-var safe)</span>
              <input value={form.name} disabled={editing}
                onChange={e => setForm({ ...form, name: e.target.value })}
                style={inputStyle} autoComplete="off" spellCheck={false} />
            </div>
            <div>
              <span style={labelStyle}>Scope</span>
              <select value={form.scope} disabled={editing}
                onChange={e => setForm({ ...form, scope: e.target.value })}
                style={inputStyle}>
                <option value="project">project (this project only)</option>
                <option value="global">global (all C3 projects)</option>
              </select>
            </div>
            {CREDS_STRUCTURED[form.type] ? (
              <div style={{ gridColumn: "1 / -1" }}>
                <span style={labelStyle}>
                  Fields{editing ? " (blank fields keep their stored value)" : ""}
                </span>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                  {[...CREDS_STRUCTURED[form.type].required,
                    ...CREDS_STRUCTURED[form.type].optional].map(fname => (
                    <div key={fname}>
                      <span style={{ ...labelStyle, marginBottom: 2 }}>
                        {fname}
                        {CREDS_STRUCTURED[form.type].required.includes(fname) ? "" : " (optional)"}
                      </span>
                      <input
                        type={CREDS_STRUCTURED[form.type].hidden.includes(fname) ? "password" : "text"}
                        value={(form.fields || {})[fname] || ""}
                        onChange={e => setForm({
                          ...form,
                          fields: { ...(form.fields || {}), [fname]: e.target.value },
                        })}
                        style={inputStyle} autoComplete="new-password" spellCheck={false} />
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div style={{ gridColumn: "1 / -1" }}>
                <span style={labelStyle}>
                  Value{editing ? " (leave blank to keep the stored value)" : ""}
                </span>
                {form.type === "multiline" ? (
                  <textarea rows={4} value={form.value}
                    onChange={e => setForm({ ...form, value: e.target.value })}
                    style={{ ...inputStyle, fontFamily: "monospace", resize: "vertical" }}
                    autoComplete="new-password" spellCheck={false} />
                ) : (
                  <input type="password" value={form.value}
                    onChange={e => setForm({ ...form, value: e.target.value })}
                    style={inputStyle} autoComplete="new-password" />
                )}
              </div>
            )}
            <div>
              <span style={labelStyle}>Type</span>
              <select value={form.type}
                disabled={editing && !!CREDS_STRUCTURED[form.type]}
                onChange={e => setForm({ ...form, type: e.target.value })}
                style={inputStyle}>
                <option value="token">token — single secret</option>
                <option value="env">env — env-style value</option>
                <option value="multiline">multiline — .env blob / PEM</option>
                <option value="card">card — credit/debit card (inject-only)</option>
                <option value="address">address — postal address (inject-only)</option>
                <option value="identity">identity — personal info (inject-only)</option>
              </select>
            </div>
            <div>
              <span style={labelStyle}>Env var at injection (default: name)</span>
              <input value={form.env_var}
                onChange={e => setForm({ ...form, env_var: e.target.value })}
                style={inputStyle} autoComplete="off" spellCheck={false} />
            </div>
            <div style={{ gridColumn: "1 / -1" }}>
              <span style={labelStyle}>Description</span>
              <input value={form.description}
                onChange={e => setForm({ ...form, description: e.target.value })}
                style={inputStyle} autoComplete="off" />
            </div>
          </div>
          {CREDS_STRUCTURED[form.type] ? (
            <div style={{
              marginTop: 10, padding: "6px 10px", borderRadius: 6, fontSize: 11,
              background: `${T.accent}15`, color: T.textMuted,
              border: `1px solid ${T.border}`,
            }}>
              🔒 {form.type} entries are inject-only: the agent can use single
              fields (<span className="mono">{"{{cred:NAME.field}}"}</span> /{" "}
              <span className="mono">env_creds='NAME.field'</span>) but can never
              reveal them, and they never auto-inject.
            </div>
          ) : (
          <div style={{ display: "flex", gap: 18, marginTop: 10, fontSize: 12, color: T.text }}>
            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
              <input type="checkbox" checked={!!form.inject}
                onChange={e => setForm({ ...form, inject: e.target.checked })} />
              auto-inject into every c3_shell run
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
              <input type="checkbox" checked={!!form.agent_readable}
                onChange={e => setForm({ ...form, agent_readable: e.target.checked })} />
              agent_readable
            </label>
          </div>
          )}
          {form.agent_readable && (
            <div style={{
              marginTop: 8, padding: "6px 10px", borderRadius: 6, fontSize: 11,
              background: `${T.warn}22`, color: T.warn, border: `1px solid ${T.warn}55`,
            }}>
              ⚠ The agent will be able to reveal this value into its context and
              conversation transcripts. Leave off for injection-only use.
            </div>
          )}
          <div style={{ display: "flex", gap: 10, marginTop: 12 }}>
            {(() => {
              const hasPayload = CREDS_STRUCTURED[form.type]
                ? Object.values(form.fields || {}).some(v => String(v || "").trim())
                : !!form.value;
              const blocked = busy || !form.name.trim() || (!editing && !hasPayload);
              return (
                <button className="btn" disabled={blocked}
                  onClick={saveForm} style={{
                    background: T.accent, color: "#fff", border: "none",
                    padding: "6px 16px", borderRadius: 6, fontSize: 12, cursor: "pointer",
                    opacity: blocked ? 0.5 : 1,
                  }}>Save</button>
              );
            })()}
            <button className="btn" onClick={() => setForm(null)} style={{
              background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
              padding: "6px 16px", borderRadius: 6, fontSize: 12, cursor: "pointer",
            }}>Cancel</button>
          </div>
        </div>
      )}

      {/* Entry table */}
      {loading ? (
        <div style={{ color: T.textMuted, fontSize: 13 }}>Loading…</div>
      ) : entries.length === 0 ? (
        <div style={{
          border: `1px dashed ${T.border}`, borderRadius: 8, padding: 30,
          textAlign: "center", color: T.textMuted, fontSize: 13,
        }}>
          No credentials yet. Add one here or run{" "}
          <span className="mono">c3 creds set NAME</span>{" "}
          (<span className="mono">--global</span> for all projects).
        </div>
      ) : (
        <div style={{ border: `1px solid ${T.border}`, borderRadius: 8, overflow: "hidden" }}>
          {entries.map((entry, i) => {
            const chk = checks[entry.name];
            return (
              <div key={`${entry.scope}|${entry.name}`} style={{
                display: "flex", alignItems: "center", gap: 10, padding: "9px 14px",
                borderTop: i === 0 ? "none" : `1px solid ${T.border}`,
                background: i % 2 ? T.surfaceAlt : T.surface, fontSize: 12,
              }}>
                <I name="lock" size={13} color={T.textMuted} />
                <span className="mono" style={{ fontWeight: 600, color: T.text, minWidth: 160 }}>
                  {entry.name}
                </span>
                <Badge color={entry.scope === "global" ? T.accent : T.blue}>{entry.scope}</Badge>
                <Badge color={T.textMuted}>{entry.type || "token"}</Badge>
                {CREDS_STRUCTURED[entry.type] ? (
                  <span className="mono" style={{ color: T.text }}>
                    {credsDisplayText(entry)}
                  </span>
                ) : (
                <span className="mono" style={{ color: T.textMuted }}>
                  •••• len={entry.value_len}
                </span>
                )}
                {entry.env_var && (
                  <span className="mono" style={{ color: T.textMuted }}>→ ${entry.env_var}</span>
                )}
                {!!entry.inject && <Badge color={T.warn}>inject</Badge>}
                {!!entry.agent_readable && <Badge color={T.error}>agent_readable</Badge>}
                {chk && (
                  <span className="mono" style={{
                    color: chk.resolvable ? T.accent : T.error, fontSize: 11,
                  }}>
                    {chk.resolvable ? `✓ ${chk.fingerprint}` : "✗ unresolvable"}
                  </span>
                )}
                <div style={{ flex: 1 }} />
                <span style={{ color: T.textMuted, fontSize: 11 }}>
                  used {entry.use_count || 0}× · {fmtWhen(entry.last_used)}
                </span>
                <span title="Verify the value resolves" onClick={() => checkEntry(entry)}
                  style={{ cursor: "pointer", color: T.textMuted }}>
                  <I name="refresh" size={13} />
                </span>
                {CREDS_STRUCTURED[entry.type] ? (
                  <span title="Inject-only: fields are usable via {{cred:NAME.field}}, never revealable"
                    style={{ color: T.textMuted, fontSize: 11 }}>🔒</span>
                ) : (<>
                <span title={entry.inject ? "Disable auto-inject" : "Auto-inject into every c3_shell run"}
                  onClick={() => toggleFlag(entry, "inject")}
                  style={{ cursor: "pointer", color: entry.inject ? T.warn : T.textMuted }}>
                  <I name="zap" size={13} />
                </span>
                <span title={entry.agent_readable
                  ? "Revoke agent reveal access"
                  : "Allow the agent to reveal this value (into its context!)"}
                  onClick={() => toggleFlag(entry, "agent_readable")}
                  style={{ cursor: "pointer", color: entry.agent_readable ? T.error : T.textMuted }}>
                  <I name="eye" size={13} />
                </span>
                </>)}
                <span title="Edit metadata / replace value" onClick={() => openEdit(entry)}
                  style={{ cursor: "pointer", color: T.textMuted }}>
                  <I name="edit" size={13} />
                </span>
                <span title="Delete" onClick={() => removeEntry(entry)}
                  style={{ cursor: "pointer", color: T.error }}>
                  <I name="trash" size={13} />
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};
