// EnforcementPanel — tool discipline for THIS project (v2.66.0)
// Globals: T, I, api, useState, useEffect, useCallback
//
// Layer C: how hard C3 pushes the agent toward c3_* tools. Deliberately a
// separate tab from Access Guard — that one governs which PATHS the agent may
// touch (a security boundary), this one governs a workflow preference.
// Conflating them is what made "the guard is slowing me down" unfixable
// without weakening something that should have stayed hard.
//
// Honesty rules (docs/enforcement.md):
//   - never imply that lowering discipline lowers a security boundary;
//   - show what strict actually buys, so 'off' is an informed choice;
//   - a malformed config section resolves to strict — say so, loudly.

const ENFORCE_MODE_HELP = {
  strict: "Native Edit/Write are blocked until a c3_* call runs first. Maximum ledger fidelity — every change carries c3_edit's pre-edit snapshot.",
  advisory: "Native Edit/Write run, with a one-line nudge. The ledger still records them; what you lose is the pre-edit snapshot.",
  off: "No nudging at all. Access Guard, the credential-vault guard and agent locks are untouched by this.",
};
const ENFORCE_MODE_ORDER = ["strict", "advisory", "off"];
const ENFORCE_MODE_COLOR = (m) => (
  m === "strict" ? T.error : m === "advisory" ? T.accent : T.textDim
);

const EnforcementPanel = () => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmOff, setConfirmOff] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await api.get("/api/enforcement"));
      setError("");
    } catch (e) { setError(String(e && e.message ? e.message : e)); }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const flash = (msg) => { setNotice(msg); setTimeout(() => setNotice(""), 4000); };

  const apply = async (mode) => {
    setBusy(true);
    try {
      const resp = await api.post("/api/enforcement", { mode });
      if (resp && resp.error) setError(resp.error);
      else {
        flash(`Tool discipline set to '${mode}'.`);
        load();
      }
    } catch (e) { setError(String(e && e.message ? e.message : e)); }
    setBusy(false);
    setConfirmOff(false);
  };

  const pick = (mode) => {
    if (mode === (data && data.mode)) return;
    // Only 'off' confirms: it is the one choice that stops C3 nudging
    // entirely, and the user should know what it does and does NOT switch off.
    if (mode === "off") { setConfirmOff(true); return; }
    apply(mode);
  };

  const clearDenials = async () => {
    setBusy(true);
    try {
      await api.del("/api/enforcement/denials");
      flash("Denial counters reset.");
      load();
    } catch (e) { setError(String(e && e.message ? e.message : e)); }
    setBusy(false);
  };

  if (loading) {
    return <div style={{ padding: 20, color: T.textMuted, fontSize: 12 }}>Loading…</div>;
  }

  const d = data || {};
  const denials = d.denials || {};
  const rows = denials.rows || [];
  const byLayer = denials.by_layer || {};
  const sourceNote = d.scope === "default"
    ? "never set — defaults to strict, the pre-v2.66 behavior"
    : `from the ${d.scope} config${d.set_by ? `, set by ${d.set_by}` : ""}`;
  const tierDrift = d.tier && d.tier_implies && d.tier_implies !== d.mode;

  return (
    <div style={{ padding: 20, height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
        <I name="shield" size={18} color={T.accent} />
        <span style={{ fontSize: 15, fontWeight: 600, color: T.text }}>Tool Discipline</span>
        <div style={{ flex: 1 }} />
        <div onClick={load} title="Refresh" style={{ cursor: "pointer", padding: 4 }}>
          <I name="refresh" size={14} color={T.textMuted} />
        </div>
      </div>
      <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 14, lineHeight: 1.5 }}>
        How hard C3 pushes the agent toward <span className="mono">c3_*</span> tools.
        This is <b>not</b> Access Guard: that decides which <b>paths</b> the agent
        may touch and is a security boundary, while this is a workflow
        preference. Turning it down is safe precisely because it cannot reach
        the other one. Same knob as <span className="mono">c3 enforce</span>;
        every change is ledger-logged.
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

      {/* A malformed section silently resolves to strict — never let that be
          a surprise the user discovers by being blocked. */}
      {(d.warnings || []).length > 0 && (
        <div style={{
          padding: "8px 12px", borderRadius: 6, marginBottom: 10, fontSize: 12,
          background: `${T.warn}22`, color: T.warn, border: `1px solid ${T.warn}55`,
          lineHeight: 1.5,
        }}>
          ⚠ The <span className="mono">enforcement</span> section is malformed, so
          it resolves to <b>strict</b> regardless of what it says. Fix{" "}
          <span className="mono">.c3/config.json</span> by hand:
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {d.warnings.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        </div>
      )}

      {/* Mode cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10, marginBottom: 14 }}>
        {ENFORCE_MODE_ORDER.map(mode => {
          const active = d.mode === mode;
          const color = ENFORCE_MODE_COLOR(mode);
          return (
            <div key={mode} onClick={() => { if (!busy) pick(mode); }}
              style={{
                border: `1px solid ${active ? color : T.border}`,
                background: active ? `${color}14` : T.surface,
                borderRadius: 8, padding: 12, cursor: busy ? "default" : "pointer",
                opacity: busy ? 0.6 : 1,
              }}>
              <div style={{ display: "flex", alignItems: "center", gap: 7, marginBottom: 6 }}>
                <div style={{
                  width: 9, height: 9, borderRadius: "50%",
                  background: active ? color : "transparent",
                  border: `1.5px solid ${active ? color : T.border}`,
                }} />
                <span className="mono" style={{
                  fontSize: 12, fontWeight: 600,
                  color: active ? color : T.text,
                }}>{mode}</span>
                {active && (
                  <span className="mono" style={{ fontSize: 9, color: T.textDim }}>
                    ACTIVE
                  </span>
                )}
              </div>
              <div style={{ fontSize: 11, color: T.textMuted, lineHeight: 1.5 }}>
                {ENFORCE_MODE_HELP[mode]}
              </div>
            </div>
          );
        })}
      </div>

      {/* Provenance */}
      <div style={{
        border: `1px solid ${T.border}`, borderRadius: 8, padding: "10px 12px",
        marginBottom: 14, background: T.surface, fontSize: 11, color: T.textMuted,
        lineHeight: 1.6,
      }}>
        <div>
          Active mode <span className="mono" style={{ color: ENFORCE_MODE_COLOR(d.mode) }}>{d.mode}</span>
          {" — "}{sourceNote}.
          {" "}Signal TTL <span className="mono">{d.signal_ttl_s}s</span>
          {d.mode === "strict" && (d.blocked_tools || []).length > 0 && (
            <> · blocks <span className="mono">{(d.blocked_tools || []).join(", ")}</span></>
          )}
        </div>
        {d.tier && (
          <div style={{ marginTop: 3 }}>
            Permission tier <span className="mono">{d.tier}</span> implies{" "}
            <span className="mono">{d.tier_implies}</span>
            {tierDrift && d.set_by === "user" && (
              <> — your explicit choice wins, and a tier change will not undo it.</>
            )}
            {tierDrift && d.set_by !== "user" && (
              <> — differs from the active mode; re-picking a tier would change it.</>
            )}
          </div>
        )}
      </div>

      {/* What this switch cannot reach. */}
      <div style={{
        border: `1px solid ${T.border}`, borderRadius: 8, padding: "10px 12px",
        marginBottom: 14, background: T.surface, fontSize: 11,
        color: T.textMuted, lineHeight: 1.6,
      }}>
        <div style={{ color: T.text, fontWeight: 600, marginBottom: 4, fontSize: 12 }}>
          Enforced at every mode, including <span className="mono">off</span>
        </div>
        <div>• Access Guard path rules — the Access Guard tab</div>
        <div>• Credential vault write guard — <span className="mono">.c3/secrets.enc</span>, <span className="mono">cred_state.json</span></div>
        <div>• Agent locks — file leases between concurrent agents</div>
        <div style={{ marginTop: 5, color: T.textDim }}>{d.coverage_note}</div>
      </div>

      {/* Denials */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: T.text }}>Denials</span>
        <span className="mono" style={{ fontSize: 11, color: T.textMuted }}>
          {denials.total || 0} recorded · {byLayer.discipline || 0} discipline ·{" "}
          {byLayer.access || 0} path policy
        </span>
        <div style={{ flex: 1 }} />
        {(denials.total || 0) > 0 && (
          <span onClick={clearDenials} className="mono"
            style={{ fontSize: 10, color: T.textMuted, cursor: "pointer" }}>
            reset counters
          </span>
        )}
      </div>

      {(denials.total || 0) === 0 ? (
        <div style={{
          fontSize: 12, color: T.textMuted, padding: "16px 10px", textAlign: "center",
          border: `1px dashed ${T.border}`, borderRadius: 6, lineHeight: 1.6,
        }}>
          Nothing recorded. Either nothing was denied, or this project predates
          denial telemetry (v2.66.0) — it logs from the first denial after upgrade.
        </div>
      ) : (
        <div style={{
          border: `1px solid ${T.border}`, borderRadius: 8, background: T.surface,
          overflow: "hidden",
        }}>
          <div style={{
            display: "grid",
            gridTemplateColumns: "50px minmax(0,1.3fr) 84px 86px minmax(0,2fr)",
            gap: 8, padding: "6px 10px", fontSize: 10, color: T.textDim,
            borderBottom: `1px solid ${T.border}`,
          }}>
            <span>HITS</span><span>RULE</span><span>TOOL</span>
            <span>LAYER</span><span>HOW TO UNBLOCK</span>
          </div>
          {rows.map((r, i) => (
            <div key={i} style={{
              display: "grid",
              gridTemplateColumns: "50px minmax(0,1.3fr) 84px 86px minmax(0,2fr)",
              gap: 8, alignItems: "center", padding: "5px 10px", fontSize: 11,
              borderTop: i ? `1px solid ${T.border}` : "none",
            }}>
              <span className="mono" style={{
                color: r.layer === "discipline" ? T.warn : T.error, fontWeight: 600,
              }}>{r.hits}x</span>
              <span className="mono" title={r.example_path || r.rule} style={{
                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                color: T.text,
              }}>{r.rule}</span>
              <span className="mono" style={{ fontSize: 10, color: T.textDim }}>{r.tool}</span>
              <span className="mono" style={{ fontSize: 10, color: T.textDim }}>{r.layer}</span>
              <span className="mono" title={r.fix} style={{
                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                fontSize: 10, color: T.textMuted,
              }}>{r.fix}</span>
            </div>
          ))}
          <div style={{
            padding: "6px 10px", fontSize: 10, color: T.textDim,
            borderTop: `1px solid ${T.border}`,
          }}>
            Counters are diagnostics, not an audit trail — the edit ledger keeps
            the record.
          </div>
        </div>
      )}

      {/* Confirm 'off' */}
      {confirmOff && (
        <div style={{
          position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)",
          display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
        }} onClick={() => setConfirmOff(false)}>
          <div onClick={e => e.stopPropagation()} style={{
            background: T.surface, border: `1px solid ${T.border}`, borderRadius: 10,
            padding: 18, maxWidth: 520, boxShadow: "0 10px 40px rgba(0,0,0,0.4)",
          }}>
            <div style={{ fontSize: 14, fontWeight: 600, color: T.text, marginBottom: 10 }}>
              Turn tool discipline off?
            </div>
            <div style={{ fontSize: 12, color: T.textMuted, lineHeight: 1.6 }}>
              C3 will stop nudging the agent toward <span className="mono">c3_*</span>{" "}
              tools entirely. Native Edit and Write run without a hint.
              <div style={{ marginTop: 8, color: T.text }}>
                <b>Still enforced:</b> Access Guard path rules, the
                credential-vault write guard, and agent locks. This switch
                cannot reach them.
              </div>
              <div style={{ marginTop: 8, color: T.text }}>
                <b>What you lose:</b> <span className="mono">c3_edit</span> takes a
                pre-edit snapshot that makes a clean revert possible. The edit
                ledger still records native writes, but without that snapshot.
              </div>
            </div>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 16 }}>
              <button className="btn" onClick={() => setConfirmOff(false)} style={{
                background: "transparent", color: T.textMuted,
                border: `1px solid ${T.border}`, padding: "6px 14px",
                borderRadius: 6, fontSize: 12, cursor: "pointer",
              }}>Cancel</button>
              <button className="btn" disabled={busy} onClick={() => apply("off")} style={{
                background: T.warn, color: "#fff", border: "none",
                padding: "6px 14px", borderRadius: 6, fontSize: 12,
                cursor: "pointer", opacity: busy ? 0.5 : 1,
              }}>{busy ? "Applying…" : "Turn off"}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
