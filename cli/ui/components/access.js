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

// Mask Guard (v2.63.0, docs/mask-guard.md). Presets are deterministic and
// versioned; the defaults here mirror services/access_guard.MASK_PRESETS.
const MASK_EMPTY_FORM = {
  glob: "", preset: "redact_secrets", scope: "project",
  count: 20, strategy: "first", columns: "",
};
const MASK_PRESET_HELP = {
  redact_secrets: "Replace detected secrets with inert placeholders. Keeps the code readable.",
  redact_columns: "Replace named CSV/TSV columns with stable pseudonyms — joins and cardinality survive.",
  sample_rows: "Keep the header plus N data rows. The rest is withheld, and the agent is told so.",
  signatures_only: "Structure only: signatures and declarations, no bodies.",
};
const MASK_PRESET_ORDER = ["redact_secrets", "redact_columns", "sample_rows",
                           "signatures_only"];

const maskParamsFor = (form) => {
  if (form.preset === "sample_rows") {
    return { count: Number(form.count) || 1, strategy: form.strategy };
  }
  if (form.preset === "redact_columns") {
    return {
      columns: (form.columns || "").split(",")
        .map(c => c.trim()).filter(Boolean),
    };
  }
  return {};
};

const maskParamSummary = (entry) => {
  const p = entry.params || {};
  const parts = Object.keys(p).sort().map(
    k => `${k}=${Array.isArray(p[k]) ? p[k].join("|") : p[k]}`);
  return parts.length ? `(${parts.join(", ")})` : "";
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
  const [maskForm, setMaskForm] = useState(null);   // null = closed
  const [maskState, setMaskState] = useState(null); // activation status
  const [maskSummary, setMaskSummary] = useState("");
  const [preview, setPreview] = useState(null);     // last /preview response
  const [previewBusy, setPreviewBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await api.get("/api/access");
      setScopes((data && data.scopes) || {});
      setCoverage((data && data.coverage) || "");
      setCorrupt((data && data.corrupt) || []);
      setMaskState((data && data.mask) || null);
      setMaskSummary((data && data.mask_summary) || "");
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
    setPreview(null);
    try {
      const data = await api.get(
        `/api/access/check?path=${encodeURIComponent(p)}&op=${encodeURIComponent(probeOp)}`);
      setProbe(data);
      // A masked path reports as denied to /check (that endpoint is the
      // fail-closed evaluator the agent sees). Pull the human preview so the
      // user can see the actual transformed view.
      if (data && data.verdict !== "allowed") runPreview(p);
    } catch (e) { setError(String(e)); }
    setProbeBusy(false);
  };

  const runPreview = async (path) => {
    const p = (path || probePath).trim();
    if (!p) return;
    setPreviewBusy(true);
    try {
      const data = await api.post("/api/access/preview", { path: p });
      setPreview(data);
    } catch (e) { setError(String(e)); }
    setPreviewBusy(false);
  };

  const addMaskRule = async () => {
    const glob = (maskForm.glob || "").trim();
    if (!glob) { setError("Glob required — e.g. data/** or *.csv"); return; }
    setBusy(true);
    try {
      const resp = await api.post("/api/access/mask", {
        glob, preset: maskForm.preset, params: maskParamsFor(maskForm),
        scope: maskForm.scope,
      });
      if (resp && resp.error) setError(resp.error);
      else {
        setMaskForm(null);
        flash(`${resp.rule.replaced ? "Replaced" : "Added"} mask rule '${glob}' → ${maskForm.preset}. Activate to purge pre-mask artifacts.`);
        load();
      }
    } catch (e) { setError(String(e)); }
    setBusy(false);
  };

  const removeMaskRule = async (scope, glob) => {
    if (!window.confirm(
      `Remove mask rule '${glob}' (${scope} scope)?\n\nThe agent will see the REAL contents of matching files again.`)) return;
    setBusy(true);
    try {
      const resp = await api.del(
        `/api/access/mask?glob=${encodeURIComponent(glob)}&scope=${encodeURIComponent(scope)}`);
      if (resp && resp.error) setError(resp.error);
      else flash(`Removed mask rule '${glob}'`);
      load();
    } catch (e) { setError(String(e)); }
    setBusy(false);
  };

  const activateMasks = async () => {
    setBusy(true);
    try {
      const resp = await api.post("/api/access/mask/activate", {});
      const r = (resp && resp.report) || {};
      const failed = (r.failures || []).length;
      if (failed) {
        setError(`Activation INCOMPLETE — ${failed} path(s) could not be rendered and now refuse reads: ` +
          (r.failures || []).slice(0, 3).map(f => `${f.path} (${f.reason})`).join("; "));
      } else {
        flash(`Activated: ${r.views_built} view(s) built, ${r.cache_entries_removed} cache entr(ies) wiped, ` +
          `${(r.facts || {}).purged || 0} fact(s) purged` +
          ((r.facts || {}).unknown_purged ? ` (+${r.facts.unknown_purged} unknown-provenance)` : ""));
      }
      load();
    } catch (e) { setError(String(e)); }
    setBusy(false);
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
    (sec.mask || []).forEach(e => rows.push({
      kind: "mask", glob: e.glob, preset: e.preset, params: e.params,
    }));
    return rows;
  };

  const verdictColor = (v) =>
    v === "allowed" ? T.accent : v === "read_only" ? T.warn
      : v === "masked" ? T.blue : T.error;
  const kindColorFull = (kind) =>
    kind === "deny" ? T.error : kind === "mask" ? T.blue : T.warn;

  // Activation banner: masking that has not been through the purge is NOT in
  // effect for derived artifacts, and the UI must never imply otherwise.
  const maskBanner = () => {
    if (!maskState || !maskState.rule_count) return null;
    const stale = maskState.stale || maskState.status === "pending";
    const broken = maskState.status === "incomplete";
    const color = broken ? T.error : stale ? T.warn : T.accent;
    return (
      <div style={{
        padding: "10px 12px", borderRadius: 6, marginBottom: 12, fontSize: 12,
        background: `${color}18`, color, border: `1px solid ${color}55`,
        display: "flex", alignItems: "center", gap: 10,
      }}>
        <I name={(broken || stale) ? "alertTriangle" : "check"} size={14} color={color} />
        <span style={{ flex: 1, lineHeight: 1.5 }}>
          {broken ? (
            <>Masking is <b>INCOMPLETE</b> — {((maskState.last_report || {}).failures || []).length} path(s)
            could not be rendered and now <b>refuse reads</b> rather than leak. Fix the rule or the file, then re-activate.</>
          ) : stale ? (
            <><b>{maskState.rule_count} mask rule(s) configured but not activated.</b> Caches, the
            search index, file memory and auto-memory facts may still hold pre-mask content.
            Activation purges them, then builds and validates every view.</>
          ) : (
            <>{maskState.rule_count} mask rule(s) active — matching paths are served as
            policy-transformed views, and are read-only.</>
          )}
        </span>
        {(stale || broken) && (
          <button className="btn" disabled={busy} onClick={activateMasks} style={{
            background: color, color: "#fff", border: "none", whiteSpace: "nowrap",
            padding: "6px 12px", borderRadius: 6, fontSize: 12, cursor: "pointer",
            opacity: busy ? 0.5 : 1,
          }}>{busy ? "Activating…" : "Activate now"}</button>
        )}
      </div>
    );
  };

  return (
    <div style={{ padding: 20, height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
        <I name="lock" size={18} color={T.accent} />
        <span style={{ fontSize: 15, fontWeight: 600, color: T.text }}>Access Guard</span>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => { setMaskForm(maskForm ? null : { ...MASK_EMPTY_FORM }); setForm(null); }} style={{
          background: T.blue, color: "#fff", border: "none",
          padding: "6px 12px", borderRadius: 6, fontSize: 12, cursor: "pointer",
        }}>+ Add mask</button>
        <button className="btn" onClick={() => { setForm(form ? null : { ...ACCESS_EMPTY_FORM }); setMaskForm(null); }} style={{
          background: T.accent, color: "#fff", border: "none",
          padding: "6px 12px", borderRadius: 6, fontSize: 12, cursor: "pointer",
        }}>+ Add rule</button>
      </div>
      <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 14, lineHeight: 1.5 }}>
        Paths the agent must not read (<b>deny</b>), must not write
        (<b>read_only</b>), or may read only in transformed form (<b>mask</b>),
        enforced across every C3 surface. Rule changes are human-only — this
        tab and <span className="mono">c3 access</span> — and every mutation is
        ledger-logged. <b>deny</b> also hides matching paths from listings and
        search (no existence oracle); <b>mask</b> keeps them discoverable but
        serves a policy-transformed view and refuses every write.
      </div>

      {maskBanner()}

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

      {/* Add-mask form */}
      {maskForm && (
        <div style={{
          border: `1px solid ${T.blue}55`, borderRadius: 8, padding: 14,
          marginBottom: 14, background: T.surface,
        }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: T.text, marginBottom: 4 }}>
            New mask rule
          </div>
          <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 10, lineHeight: 1.5 }}>
            The file stays visible and searchable, but the agent only ever sees the
            transformed view — and cannot write to it. Transforms are deterministic:
            the same file renders identically on every read and through every surface.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "2fr 1.4fr 1fr", gap: 10 }}>
            <div>
              <span style={labelStyle}>Glob (POSIX, ** crosses directories)</span>
              <input value={maskForm.glob}
                onChange={e => setMaskForm({ ...maskForm, glob: e.target.value })}
                placeholder="e.g. data/** or *.csv"
                style={{ ...inputStyle, fontFamily: "monospace" }}
                autoComplete="off" spellCheck={false} />
            </div>
            <div>
              <span style={labelStyle}>Preset</span>
              <select value={maskForm.preset}
                onChange={e => setMaskForm({ ...maskForm, preset: e.target.value })}
                style={inputStyle}>
                {MASK_PRESET_ORDER.map(p => (
                  <option key={p} value={p}>{p}</option>
                ))}
              </select>
            </div>
            <div>
              <span style={labelStyle}>Scope</span>
              <select value={maskForm.scope}
                onChange={e => setMaskForm({ ...maskForm, scope: e.target.value })}
                style={inputStyle}>
                <option value="project">project (this project only)</option>
                <option value="global">global (all C3 projects)</option>
              </select>
            </div>
          </div>

          <div style={{ fontSize: 11, color: T.textDim, marginTop: 8 }}>
            {MASK_PRESET_HELP[maskForm.preset]}
          </div>

          {maskForm.preset === "sample_rows" && (
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 2fr", gap: 10, marginTop: 10 }}>
              <div>
                <span style={labelStyle}>Rows to keep</span>
                <input type="number" min={1} value={maskForm.count}
                  onChange={e => setMaskForm({ ...maskForm, count: e.target.value })}
                  style={inputStyle} />
              </div>
              <div>
                <span style={labelStyle}>From</span>
                <select value={maskForm.strategy}
                  onChange={e => setMaskForm({ ...maskForm, strategy: e.target.value })}
                  style={inputStyle}>
                  <option value="first">first</option>
                  <option value="last">last</option>
                </select>
              </div>
            </div>
          )}
          {maskForm.preset === "redact_columns" && (
            <div style={{ marginTop: 10 }}>
              <span style={labelStyle}>Columns (comma-separated, must exist in the header)</span>
              <input value={maskForm.columns}
                onChange={e => setMaskForm({ ...maskForm, columns: e.target.value })}
                placeholder="email, full_name, ssn"
                style={{ ...inputStyle, fontFamily: "monospace" }}
                autoComplete="off" spellCheck={false} />
              <div style={{ fontSize: 11, color: T.textDim, marginTop: 4 }}>
                A missing column is a hard error, not a silent skip — a typo would
                otherwise leave real values exposed while this tab reported the file masked.
              </div>
            </div>
          )}

          <div style={{ display: "flex", gap: 10, marginTop: 12 }}>
            <button className="btn" disabled={busy || !maskForm.glob.trim()}
              onClick={addMaskRule} style={{
                background: T.blue, color: "#fff", border: "none",
                padding: "6px 16px", borderRadius: 6, fontSize: 12, cursor: "pointer",
                opacity: (busy || !maskForm.glob.trim()) ? 0.5 : 1,
              }}>Add mask</button>
            <button className="btn" onClick={() => setMaskForm(null)} style={{
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

        {previewBusy && (
          <div style={{ marginTop: 10, fontSize: 12, color: T.textMuted }}>
            Rendering preview…
          </div>
        )}

        {/* Before/after — the headline affordance: see exactly what the agent
            sees, on your real file, before trusting the rule. `before` is a
            HUMAN-only surface and never reaches an agent. */}
        {preview && preview.verdict === "masked" && !previewBusy && (
          <div style={{ marginTop: 12 }}>
            <div style={{
              display: "flex", alignItems: "center", gap: 8, marginBottom: 6,
              fontSize: 12, color: T.text, flexWrap: "wrap",
            }}>
              <Badge color={T.blue}>masked</Badge>
              <span className="mono" style={{ color: T.textMuted }}>
                '{preview.rule}' ({preview.scope}) → {preview.preset}
              </span>
              {preview.stats && (
                <span style={{ fontSize: 11, color: T.textDim }}>
                  {Object.keys(preview.stats).sort()
                    .map(k => `${k}: ${JSON.stringify(preview.stats[k])}`).join(" · ")}
                </span>
              )}
            </div>
            {preview.error ? (
              <div style={{
                padding: "8px 10px", borderRadius: 6, fontSize: 12,
                background: `${T.error}22`, color: T.error,
                border: `1px solid ${T.error}55`,
              }}>
                Cannot render ({preview.error_reason}): {preview.error}
                <div style={{ marginTop: 4, fontSize: 11 }}>
                  This path currently <b>refuses reads</b> rather than serving the
                  original — fail-closed is the intended behaviour, but the agent
                  cannot use this file until the rule or the file is fixed.
                </div>
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                <div>
                  <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 4 }}>
                    Real file <span style={{ color: T.textDim }}>(you only)</span>
                  </div>
                  <pre className="mono" style={{
                    margin: 0, padding: 10, borderRadius: 6, fontSize: 11,
                    background: T.surfaceAlt, color: T.text, maxHeight: 300,
                    overflow: "auto", border: `1px solid ${T.border}`,
                    whiteSpace: "pre-wrap", wordBreak: "break-word",
                  }}>{preview.before}</pre>
                </div>
                <div>
                  <div style={{ fontSize: 11, color: T.blue, marginBottom: 4 }}>
                    What the agent sees
                  </div>
                  <pre className="mono" style={{
                    margin: 0, padding: 10, borderRadius: 6, fontSize: 11,
                    background: T.surfaceAlt, color: T.text, maxHeight: 300,
                    overflow: "auto", border: `1px solid ${T.blue}55`,
                    whiteSpace: "pre-wrap", wordBreak: "break-word",
                  }}>{(preview.header ? preview.header + "\n\n" : "") + preview.after}</pre>
                </div>
              </div>
            )}
          </div>
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
                    <Badge color={kindColorFull(r.kind)}>{r.kind}</Badge>
                    {r.kind === "mask" && (
                      <span className="mono" style={{ fontSize: 11, color: T.blue }}>
                        → {r.preset}{maskParamSummary(r)}
                      </span>
                    )}
                    {isBuiltin && (
                      <span style={{ fontSize: 11, color: T.textDim }}>built-in — read-only</span>
                    )}
                    <div style={{ flex: 1 }} />
                    {!isBuiltin && r.kind === "mask" && (
                      <span title="Preview what the agent sees"
                        onClick={() => { setProbePath(r.glob.replace(/\*+/g, "")); }}
                        style={{ cursor: "pointer", color: T.textMuted, marginRight: 4 }}>
                        <I name="eye" size={13} />
                      </span>
                    )}
                    {!isBuiltin && (
                      <span title={r.kind === "deny"
                        ? "Remove (typed confirmation required)"
                        : r.kind === "mask"
                          ? "Remove (agent regains real contents)" : "Remove"}
                        onClick={() => (r.kind === "mask"
                          ? removeMaskRule(scope, r.glob)
                          : removeRule(scope, r.kind, r.glob))}
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