// AccessPanel — Access Guard rule manager (v2.62.0)
// Globals: T, I, api, useState, useEffect, useCallback, Badge
// Human-only mutation surface: ALL rule changes happen here (or `c3 access`)
// and are ledger/activity-logged. Agents get refusal strings, never this tab.

const ACCESS_EMPTY_FORM = { glob: "", kind: "deny", scope: "project" };
const ACCESS_SCOPE_ORDER = ["builtin", "global", "project"];
const ACCESS_SCOPE_NOTES = {
  builtin: "hardcoded, always on, fail-closed — cannot be edited",
  global: "~/.c3/config.json — applies to every C3 project",
  project: ".c3/config.json — this project only",
};

const AccessPanel = () => {
  const [scopes, setScopes] = useState(null);
  const [coverage, setCoverage] = useState("");
  const [corrupt, setCorrupt] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState(null);       // null = closed; {…} = add-rule form
  const [probePath, setProbePath] = useState("");
  const [probeOp, setProbeOp] = useState("read");
  const [probe, setProbe] = useState(null);     // last /check response
  const [probeBusy, setProbeBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await api.get("/api/access");
      setScopes((data && data.scopes) || {});
      setCoverage((data && data.coverage) || "");
      setCorrupt((data && data.corrupt) || []);
      setError("");
    } catch (e) { setError(String(e)); }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const flash = (msg) => { setNotice(msg); setTimeout(() => setNotice(""), 4000); };

  const addRule = async () => {
    const glob = (form.glob || "").trim();
    if (!glob) { setError("Glob required — e.g. secrets/** or *.pem"); return; }
    setBusy(true);
    try {
      const resp = await api.post("/api/access",
        { glob, kind: form.kind, scope: form.scope });
      if (resp && resp.error) { setError(resp.error); }
      else if (resp && resp.rule && !resp.rule.added) {
        setError(`Rule already present: '${resp.rule.glob}' (${form.kind}, ${form.scope})`);
      } else {
        setForm(null);
        flash(`Added ${form.kind} rule '${glob}' (${form.scope})`);
        load();
      }
    } catch (e) { setError(String(e)); }
    setBusy(false);
  };

  const removeRule = async (scope, kind, glob) => {
    if (kind === "deny") {
      // Typed-name confirm: removing a deny rule re-exposes the paths.
      const typed = window.prompt(
        `Removing DENY rule '${glob}' (${scope} scope) re-opens agent access to matching paths.\n\nType the glob exactly to confirm:`);
      if (typed === null) return;
      if (typed.trim() !== glob) {
        setError("Confirmation text did not match — rule kept.");
        return;
      }
    } else if (!window.confirm(`Remove read-only rule '${glob}' (${scope} scope)?`)) {
      return;
    }
    setBusy(true);
    try {
      const resp = await api.del(
        `/api/access?glob=${encodeURIComponent(glob)}&kind=${encodeURIComponent(kind)}&scope=${encodeURIComponent(scope)}`);
      if (resp && resp.error) setError(resp.error);
      else { flash(`Removed ${kind} rule '${glob}'`); }
      load();
    } catch (e) { setError(String(e)); }
    setBusy(false);
  };

  const runProbe = async () => {
    const p = probePath.trim();
    if (!p) return;
    setProbeBusy(true);
    try {
      const data = await api.get(
        `/api/access/check?path=${encodeURIComponent(p)}&op=${encodeURIComponent(probeOp)}`);
      setProbe(data);
    } catch (e) { setError(String(e)); }
    setProbeBusy(false);
  };

  const inputStyle = {
    width: "100%", padding: "7px 10px", borderRadius: 6, fontSize: 12,
    border: `1px solid ${T.border}`, background: T.surface, color: T.text,
    outline: "none", boxSizing: "border-box",
  };
  const labelStyle = { fontSize: 11, color: T.textMuted, marginBottom: 4, display: "block" };
  const kindColor = (kind) => (kind === "deny" ? T.error : T.warn);

  const scopeRules = (scope) => {
    const sec = (scopes && scopes[scope]) || {};
    const rows = [];
    (sec.deny || []).forEach(g => rows.push({ kind: "deny", glob: g }));
    (sec.read_only || []).forEach(g => rows.push({ kind: "read_only", glob: g }));
    return rows;
  };

  const verdictColor = (v) =>
    v === "allowed" ? T.accent : v === "read_only" ? T.warn : T.error;

  return (
    <div style={{ padding: 20, height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
        <I name="lock" size={18} color={T.accent} />
        <span style={{ fontSize: 15, fontWeight: 600, color: T.text }}>Access Guard</span>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => setForm(form ? null : { ...ACCESS_EMPTY_FORM })} style={{
          background: T.accent, color: "#fff", border: "none",
          padding: "6px 12px", borderRadius: 6, fontSize: 12, cursor: "pointer",
        }}>+ Add rule</button>
      </div>
      <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 14, lineHeight: 1.5 }}>
        Paths the agent must not read (<b>deny</b>) or must not write
        (<b>read_only</b>), enforced across every C3 surface. Rule changes are
        human-only — this tab and <span className="mono">c3 access</span> — and
        every mutation is ledger-logged. <b>deny</b> also hides matching paths
        from listings and search (no existence oracle).
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
      {corrupt.length > 0 && (
        <div style={{
          padding: "8px 12px", borderRadius: 6, marginBottom: 10, fontSize: 12,
          background: `${T.warn}22`, color: T.warn, border: `1px solid ${T.warn}55`,
        }}>
          ⚠ Corrupt access section in scope{corrupt.length > 1 ? "s" : ""}:{" "}
          {corrupt.join(", ")} — that scope evaluates <b>deny-all</b> until you
          fix <span className="mono">config.json</span> "access" by hand.
        </div>
      )}

      {/* Add-rule form */}
      {form && (
        <div style={{
          border: `1px solid ${T.accent}55`, borderRadius: 8, padding: 14,
          marginBottom: 14, background: T.surface,
        }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: T.text, marginBottom: 10 }}>
            New rule
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr 1fr", gap: 10 }}>
            <div>
              <span style={labelStyle}>Glob (POSIX, ** crosses directories)</span>
              <input value={form.glob}
                onChange={e => setForm({ ...form, glob: e.target.value })}
                placeholder="e.g. secrets/** or *.pem"
                style={{ ...inputStyle, fontFamily: "monospace" }}
                autoComplete="off" spellCheck={false} />
            </div>
            <div>
              <span style={labelStyle}>Kind</span>
              <select value={form.kind}
                onChange={e => setForm({ ...form, kind: e.target.value })}
                style={inputStyle}>
                <option value="deny">deny — no read, no write, hidden</option>
                <option value="read_only">read_only — no write</option>
              </select>
            </div>
            <div>
              <span style={labelStyle}>Scope</span>
              <select value={form.scope}
                onChange={e => setForm({ ...form, scope: e.target.value })}
                style={inputStyle}>
                <option value="project">project (this project only)</option>
                <option value="global">global (all C3 projects)</option>
              </select>
            </div>
          </div>
          <div style={{ display: "flex", gap: 10, marginTop: 12 }}>
            <button className="btn" disabled={busy || !form.glob.trim()}
              onClick={addRule} style={{
                background: T.accent, color: "#fff", border: "none",
                padding: "6px 16px", borderRadius: 6, fontSize: 12, cursor: "pointer",
                opacity: (busy || !form.glob.trim()) ? 0.5 : 1,
              }}>Add</button>
            <button className="btn" onClick={() => setForm(null)} style={{
              background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
              padding: "6px 16px", borderRadius: 6, fontSize: 12, cursor: "pointer",
            }}>Cancel</button>
          </div>
        </div>
      )}

      {/* Test-path probe */}
      <div style={{
        border: `1px solid ${T.border}`, borderRadius: 8, padding: 14,
        marginBottom: 14, background: T.surface,
      }}>
        <span style={labelStyle}>Test a path against the active rules</span>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <input value={probePath} onChange={e => setProbePath(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") runProbe(); }}
            placeholder="e.g. src/payroll/report.xlsx"
            style={{ ...inputStyle, flex: 1, fontFamily: "monospace" }}
            autoComplete="off" spellCheck={false} />
          <select value={probeOp} onChange={e => setProbeOp(e.target.value)}
            style={{ ...inputStyle, width: 110 }}>
            <option value="read">read</option>
            <option value="write">write</option>
            <option value="create">create</option>
            <option value="delete">delete</option>
          </select>
          <button className="btn" disabled={probeBusy || !probePath.trim()}
            onClick={runProbe} style={{
              background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
              padding: "6px 14px", borderRadius: 6, fontSize: 12, cursor: "pointer",
            }}>Check</button>
        </div>
        {probe && !probe.error && (
          <div style={{ marginTop: 10, fontSize: 12 }}>
            <Badge color={verdictColor(probe.verdict)}>{probe.verdict}</Badge>{" "}
            {probe.verdict === "allowed" ? (
              <span style={{ color: T.textMuted }}>
                {probe.op} permitted for <span className="mono">{probe.path}</span>
              </span>
            ) : (
              <span style={{ color: T.textMuted }}>
                matched rule <span className="mono">'{probe.rule}'</span>{" "}
                ({probe.scope} scope)
              </span>
            )}
            {probe.refusal && (
              <div className="mono" style={{
                marginTop: 8, padding: "8px 10px", borderRadius: 6, fontSize: 11,
                background: T.surfaceAlt, color: T.textMuted,
                border: `1px solid ${T.border}`, whiteSpace: "pre-wrap",
              }}>{probe.refusal}</div>
            )}
          </div>
        )}
        {probe && probe.error && (
          <div style={{ marginTop: 8, fontSize: 12, color: T.error }}>{probe.error}</div>
        )}
      </div>

      {/* Rules grouped by scope */}
      {loading ? (
        <div style={{ color: T.textMuted, fontSize: 13 }}>Loading…</div>
      ) : ACCESS_SCOPE_ORDER.map(scope => {
        const rows = scopeRules(scope);
        const isBuiltin = scope === "builtin";
        return (
          <div key={scope} style={{ marginBottom: 14 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <span style={{ fontSize: 12, fontWeight: 600, color: T.text }}>
                {scope}
              </span>
              <span style={{ fontSize: 11, color: T.textDim }}>
                {ACCESS_SCOPE_NOTES[scope]}
              </span>
            </div>
            {rows.length === 0 ? (
              <div style={{
                border: `1px dashed ${T.border}`, borderRadius: 8, padding: 12,
                color: T.textMuted, fontSize: 12,
              }}>
                No {scope} rules. {isBuiltin ? "" : "Add one above or run "}
                {!isBuiltin && <span className="mono">c3 access add &lt;glob&gt; --kind deny</span>}
              </div>
            ) : (
              <div style={{ border: `1px solid ${T.border}`, borderRadius: 8, overflow: "hidden" }}>
                {rows.map((r, i) => (
                  <div key={`${scope}|${r.kind}|${r.glob}`} style={{
                    display: "flex", alignItems: "center", gap: 10, padding: "8px 14px",
                    borderTop: i === 0 ? "none" : `1px solid ${T.border}`,
                    background: i % 2 ? T.surfaceAlt : T.surface, fontSize: 12,
                  }}>
                    {isBuiltin && <I name="lock" size={13} color={T.textMuted} />}
                    <span className="mono" style={{ fontWeight: 600, color: T.text, minWidth: 220 }}>
                      {r.glob}
                    </span>
                    <Badge color={kindColor(r.kind)}>{r.kind}</Badge>
                    {isBuiltin && (
                      <span style={{ fontSize: 11, color: T.textDim }}>built-in — read-only</span>
                    )}
                    <div style={{ flex: 1 }} />
                    {!isBuiltin && (
                      <span title={r.kind === "deny"
                        ? "Remove (typed confirmation required)" : "Remove"}
                        onClick={() => removeRule(scope, r.kind, r.glob)}
                        style={{ cursor: "pointer", color: T.error }}>
                        <I name="trash" size={13} />
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}

      {/* §5 coverage matrix footer */}
      <div style={{
        marginTop: 6, paddingTop: 10, borderTop: `1px solid ${T.border}`,
        fontSize: 11, color: T.textDim, lineHeight: 1.5,
      }}>
        {coverage || ("Enforced: C3 MCP tools (all agents using C3) · Claude Code " +
          "native tools (hooks) · c3_shell (best-effort scan, advisory). " +
          "NOT enforced: non-Claude agents' raw shell, direct file APIs, editors.")}
      </div>
    </div>
  );
};