// ─── Shared Components ────────────────────
const GlowDot = ({ color = T.accent, size = 6 }) => (
  <span style={{
    display: "inline-block", width: size, height: size, borderRadius: "50%",
    background: color, boxShadow: `0 0 ${size}px ${color}60`, flexShrink: 0
  }} />
);

const Badge = ({ children, color = T.accent }) => (
  <span className="mono" style={{
    display: "inline-flex", alignItems: "center", gap: 4,
    padding: "2px 8px", borderRadius: 4, fontSize: 11, fontWeight: 600,
    background: `${color}15`, color, border: `1px solid ${color}30`, whiteSpace: "nowrap"
  }}>
    {children}
  </span>
);

const StatBox = ({ label, value, sub, color = T.accent, loading }) => (
  <div style={{
    background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8,
    padding: "16px 18px", flex: 1, minWidth: 140
  }}>
    <div style={{
      color: T.textMuted, fontSize: 11, fontWeight: 600, textTransform: "uppercase",
      letterSpacing: 1, marginBottom: 8
    }}>{label}</div>
    <div className="mono" style={{ fontSize: 26, fontWeight: 700, color, lineHeight: 1.1 }}>
      {loading ? <span style={{ animation: "pulse 1s infinite" }}>&mdash;</span> : value}
    </div>
    {sub && <div style={{ color: T.textMuted, fontSize: 11, marginTop: 4 }}>{sub}</div>}
  </div>
);

const ProgressBar = ({ value, max, color = T.accent, height = 6 }) => {
  const pct = Math.min(100, (value / max) * 100);
  return (
    <div style={{ flex: 1, height, borderRadius: height, background: T.surfaceAlt, overflow: "hidden" }}>
      <div style={{
        height: "100%", borderRadius: height, width: `${pct}%`,
        background: `linear-gradient(90deg, ${color}90, ${color})`,
        boxShadow: `0 0 8px ${color}40`, transition: "width 0.5s ease"
      }} />
    </div>
  );
};

const Btn = ({ children, color = T.accent, variant = "solid", onClick, disabled, style: s = {} }) => {
  const isSolid = variant === "solid";
  return (
    <button onClick={onClick} disabled={disabled} style={{
      padding: "8px 18px", borderRadius: 6, border: isSolid ? "none" : `1px solid ${T.border}`,
      background: isSolid ? `linear-gradient(135deg, ${color}, ${color}cc)` : "transparent",
      color: isSolid ? T.bg : T.textMuted, fontSize: 12, fontWeight: 700, cursor: disabled ? "default" : "pointer",
      fontFamily: "'JetBrains Mono', monospace", display: "flex", alignItems: "center", gap: 6,
      opacity: disabled ? 0.5 : 1, transition: "all 0.15s", ...s,
    }}>{children}</button>
  );
};

const Section = ({ label, icon, color, open, onToggle, badge, children }) => (
  <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8, overflow: "hidden" }}>
    <div onClick={onToggle} style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 16px", cursor: "pointer", userSelect: "none" }}>
      <I name={icon} size={13} color={color || T.textMuted} />
      <span style={{ fontSize: 12, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1, flex: 1 }}>{label}</span>
      {badge}
      <I name="chevron" size={12} color={T.textMuted} style={{ transform: open ? "rotate(90deg)" : "rotate(0deg)", transition: "transform 0.15s" }} />
    </div>
    {open && <div style={{ padding: "0 16px 16px" }}>{children}</div>}
  </div>
);

// ─── Shared Constants ─────────────────────
const typeColors = { tool_call: T.blue, decision: T.purple, file_change: T.accent, fact_stored: T.warn, session_start: "#4ade80", session_save: "#4ade80" };
const toolColors = { search: T.blue, compress: T.purple, read: T.blue, filter: T.purple, validate: T.accent, session: "#4ade80", memory: T.purple, status: T.warn, delegate: "#e879f9", snapshot: "#4ade80", restore: "#4ade80", transcript: T.blue };
const getToolColor = (name) => { const n = (name || "").toLowerCase(); const k = Object.keys(toolColors).find(k => n.includes(k)); return k ? toolColors[k] : T.textMuted; };

const timeAgo = (iso) => {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
};
const localTime = (iso) => { if (!iso) return ""; const d = new Date(iso); return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); };
const localDate = (iso) => { if (!iso) return ""; return new Date(iso).toLocaleDateString(); };
const formatDuration = (seconds) => {
  if (seconds < 0) seconds = 0;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h ? `${h}h ${m}m` : `${m}m ${s}s`;
};
const useLiveDuration = (startedIso, isLive) => {
  const [dur, setDur] = useState("");
  useEffect(() => {
    if (!startedIso || !isLive) { setDur(""); return; }
    const tick = () => setDur(formatDuration(Math.floor((Date.now() - new Date(startedIso).getTime()) / 1000)));
    tick();
    const iv = setInterval(tick, 1000);
    return () => clearInterval(iv);
  }, [startedIso, isLive]);
  return dur;
};

const renderBoolToggle = (label, checked, onChange, description) => (
  <label style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, padding: "8px 0" }}>
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <span style={{ color: T.textMuted, fontSize: 12 }}>{label}</span>
      {description && <span style={{ color: T.textDim, fontSize: 11 }}>{description}</span>}
    </div>
    <button type="button" onClick={onChange} style={{
      padding: "4px 10px", borderRadius: 999, border: `1px solid ${T.border}`,
      background: checked ? T.accent + "22" : T.surfaceAlt,
      color: checked ? T.accent : T.textMuted, cursor: "pointer", fontSize: 11, fontWeight: 700, minWidth: 74,
    }}>{checked ? "ON" : "OFF"}</button>
  </label>
);
