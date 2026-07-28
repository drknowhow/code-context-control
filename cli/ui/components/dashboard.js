// ─── Dashboard ────────────────────────────
// Globals used: T, I, GlowDot, Badge, StatBox, ProgressBar, Btn, Section, api,
//               timeAgo, localTime, localDate, getToolColor, toolColors, useLiveDuration,
//               useState, useEffect, useCallback (React hooks via Babel standalone)

const Dashboard = ({ stats, loading, notifications = [], ackNotification, ackAllNotifications }) => {
  // ── State ──
  const [memFacts, setMemFacts] = useState([]);
  const [mcpStatus, setMcpStatus] = useState(null);
  const [meta, setMeta] = useState({});
  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState({});
  const [saving, setSaving] = useState(false);
  const [showUsage, setShowUsage] = useState(false);
  const [session, setSession] = useState(null);
  const [activity, setActivity] = useState([]);
  const [health, setHealth] = useState(null);
  const [watcher, setWatcher] = useState([]);
  const [actionMsg, setActionMsg] = useState("");
  const [liveTokens, setLiveTokens] = useState(null);  // live Claude transcript tokens

  // ── Data fetch on mount + polling ──
  const loadSession = useCallback(async () => {
    try { setSession(await api.get('/api/sessions/current') || null); } catch {}
  }, []);
  const loadActivity = useCallback(async () => {
    try { const a = await api.get('/api/activity?limit=8'); setActivity(Array.isArray(a) ? a : []); } catch {}
  }, []);
  const loadHealth = useCallback(async () => {
    try { setHealth(await api.get('/api/health')); } catch {}
  }, []);
  const loadWatcher = useCallback(async () => {
    try { const w = await api.get('/api/watcher/changes'); setWatcher(Array.isArray(w) ? w : []); } catch {}
  }, []);

  useEffect(() => {
    api.get('/api/memory/facts').then(setMemFacts).catch(() => {});
    api.get('/api/mcp/status').then(setMcpStatus).catch(() => {});
    api.get('/api/project/meta').then(setMeta).catch(() => {});
    loadSession();
    loadActivity();
    loadHealth();
    loadWatcher();
    api.get('/api/session-stats/live').then(setLiveTokens).catch(() => {});
    const fast = setInterval(() => {
      loadSession(); loadActivity();
      api.get('/api/session-stats/live').then(setLiveTokens).catch(() => {});
    }, 5000);
    const slow = setInterval(() => { loadHealth(); loadWatcher(); }, 20000);
    return () => { clearInterval(fast); clearInterval(slow); };
  }, [loadSession, loadActivity, loadHealth, loadWatcher]);

  // ── Project meta edit helpers ──
  const saveMeta = async () => {
    setSaving(true);
    try {
      const updated = await api.put('/api/project/meta', editForm);
      setMeta(updated);
      setEditing(false);
    } catch (e) {}
    setSaving(false);
  };

  const openEdit = () => {
    setEditForm({
      name: meta.name || projectName,
      tech_stack: meta.tech_stack || stats?.tech_stack || '',
      description: meta.description || '',
    });
    setEditing(true);
  };

  // ── Quick Actions ──
  const rebuildIndex = async () => {
    setActionMsg("Rebuilding index...");
    try { await api.post('/api/index/rebuild', {}); setActionMsg("Index rebuilt."); loadActivity(); }
    catch { setActionMsg("Rebuild failed."); }
    setTimeout(() => setActionMsg(""), 3000);
  };
  const openExplorer = async () => {
    try { await api.post('/api/projects/open', { path: stats?.project_path }); }
    catch {}
  };

  // ── Derived values ──
  const projectName = stats?.project_path ? stats.project_path.split(/[/\\]/).pop() : "\u2014";
  const orig = stats?.total_original_tokens || 0;
  const comp = stats?.total_compressed_tokens || 0;
  const saved = orig - comp;
  const pct = stats?.savings_pct || 0;
  const cost = (saved * 0.003 / 1000).toFixed(2);
  const totalLines = stats?.total_lines || 0;

  // Provider-agnostic token usage from conversation sources
  const convUsage = stats?.conversation_token_usage;
  const sourceUsage = convUsage?.sources || {};
  const sourceEntries = Object.entries(sourceUsage).sort((a, b) => (b[1]?.total_tokens || 0) - (a[1]?.total_tokens || 0));
  const totalSourceTokens = sourceEntries.reduce((sum, [, d]) => sum + (d?.total_tokens || 0), 0);
  const topSourceSummary = sourceEntries.slice(0, 3)
    .map(([name, d]) => `${name}:${(((d?.total_tokens || 0) / 1000) || 0).toFixed(1)}K`)
    .join(" \u00b7 ");

  // Budget
  const b = stats?.context_budget;
  const hasBudget = b && b.call_count > 0;
  const overBudget = b && b.response_tokens >= (b.threshold || 35000);
  const budgetPct = hasBudget
    ? Math.min(100, Math.round((b.response_tokens / (b.threshold || 35000)) * 100))
    : 0;
  const budgetColor = overBudget ? T.error : budgetPct >= 70 ? T.warn : T.accent;

  const fmtK = n => n >= 1000 ? (n / 1000).toFixed(1) + "K" : String(n);

  // Session
  const sessionDuration = useLiveDuration(session?.started, session?.live);

  // File type distribution from stats
  const files = stats?.files || [];
  const extCounts = {};
  files.forEach(f => { extCounts[f.type] = (extCounts[f.type] || 0) + 1; });
  const topExts = Object.entries(extCounts).sort((a, bb) => bb[1] - a[1]).slice(0, 6);
  const extColorMap = {
    tsx: T.blue, ts: T.purple, py: T.accent, jsx: T.blue, js: T.warn,
    yaml: T.warn, json: T.textMuted, md: T.textMuted, css: T.purple,
    html: T.error, r: T.accent, sh: T.warn, go: T.blue, rs: T.error,
  };

  // Health sources
  const healthSources = health?.sources ? Object.entries(health.sources).filter(([k]) => k !== "mcp_mode") : [];

  // ── Local Section component (collapsible header) ──
  const SectionBlock = ({ label, icon, color, open, onToggle, badge, children }) => (
    <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8, overflow: "hidden" }}>
      <div
        onClick={onToggle}
        style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 16px", cursor: "pointer", userSelect: "none" }}
      >
        <I name={icon} size={13} color={color || T.textMuted} />
        <span style={{ fontSize: 12, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1, flex: 1 }}>
          {label}
        </span>
        {badge}
        <I
          name="chevron"
          size={12}
          color={T.textMuted}
          style={{ transform: open ? "rotate(90deg)" : "rotate(0deg)", transition: "transform 0.15s" }}
        />
      </div>
      {open && <div style={{ padding: "0 16px 16px" }}>{children}</div>}
    </div>
  );

  // Event summary helper
  const eventSummary = (e) => {
    if (!e) return "-";
    if (e.type === "tool_call") return (e.tool || "tool") + (e.result_summary ? `: ${e.result_summary}` : "");
    if (e.type === "decision") return e.decision || e.data || "decision";
    if (e.type === "file_change") return e.file || e.data || "file change";
    if (e.type === "session_start") return "session started";
    if (e.type === "session_save") return "session saved";
    return e.type || "event";
  };

  // ── Render ──
  return (
    <div className="fade-up" style={{ display: "flex", flexDirection: "column", gap: 14 }}>

      {/* ── Row 1: Project header ── */}
      {editing ? (
        <div style={{
          background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8,
          padding: "14px 16px", display: "flex", flexDirection: "column", gap: 10,
        }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: T.textDim, textTransform: "uppercase", letterSpacing: 1 }}>
            Edit Project Info
          </div>
          <div style={{ display: "flex", gap: 10 }}>
            <div style={{ flex: 2 }}>
              <div style={{ fontSize: 10, color: T.textDim, marginBottom: 3 }}>Name</div>
              <input
                value={editForm.name || ''}
                onChange={e => setEditForm(p => ({ ...p, name: e.target.value }))}
                onKeyDown={e => { if (e.key === 'Enter') saveMeta(); if (e.key === 'Escape') setEditing(false); }}
                style={{
                  width: "100%", background: T.bg, border: `1px solid ${T.border}`, borderRadius: 4,
                  padding: "5px 8px", fontSize: 13, color: T.text, fontFamily: "'JetBrains Mono', monospace", outline: "none",
                }}
                placeholder={projectName}
                autoFocus
              />
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 10, color: T.textDim, marginBottom: 3 }}>Tech Stack</div>
              <input
                value={editForm.tech_stack || ''}
                onChange={e => setEditForm(p => ({ ...p, tech_stack: e.target.value }))}
                onKeyDown={e => { if (e.key === 'Enter') saveMeta(); if (e.key === 'Escape') setEditing(false); }}
                style={{
                  width: "100%", background: T.bg, border: `1px solid ${T.border}`, borderRadius: 4,
                  padding: "5px 8px", fontSize: 13, color: T.text, fontFamily: "'JetBrains Mono', monospace", outline: "none",
                }}
                placeholder="e.g. Python | React"
              />
            </div>
          </div>
          <div>
            <div style={{ fontSize: 10, color: T.textDim, marginBottom: 3 }}>Description</div>
            <input
              value={editForm.description || ''}
              onChange={e => setEditForm(p => ({ ...p, description: e.target.value }))}
              onKeyDown={e => { if (e.key === 'Enter') saveMeta(); if (e.key === 'Escape') setEditing(false); }}
              style={{
                width: "100%", background: T.bg, border: `1px solid ${T.border}`, borderRadius: 4,
                padding: "5px 8px", fontSize: 13, color: T.text, fontFamily: "'DM Sans', sans-serif", outline: "none",
              }}
              placeholder="Short project description"
            />
          </div>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button
              onClick={() => setEditing(false)}
              style={{
                padding: "5px 12px", borderRadius: 4, border: `1px solid ${T.border}`, background: "transparent",
                color: T.textMuted, fontSize: 12, cursor: "pointer", fontFamily: "'DM Sans', sans-serif",
              }}
            >
              Cancel
            </button>
            <button
              onClick={saveMeta}
              disabled={saving}
              style={{
                padding: "5px 12px", borderRadius: 4, border: "none", background: T.accent, color: T.bg,
                fontSize: 12, fontWeight: 600, cursor: saving ? "default" : "pointer", opacity: saving ? 0.7 : 1,
                display: "flex", alignItems: "center", gap: 5, fontFamily: "'DM Sans', sans-serif",
              }}
            >
              <I name={saving ? "refresh" : "save"} size={11} color={T.bg}
                style={saving ? { animation: "spin 0.6s linear infinite" } : {}} />
              {saving ? "Saving..." : "Save"}
            </button>
          </div>
        </div>
      ) : (
        <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "6px 0" }}>
          <span className="mono" style={{ fontSize: 17, fontWeight: 700, color: T.accent }}>
            {meta.name || projectName}
          </span>
          {(meta.tech_stack || stats?.tech_stack) && (
            <Badge color={T.purple}>{meta.tech_stack || stats?.tech_stack}</Badge>
          )}
          {meta.description && (
            <span style={{ fontSize: 12, color: T.textDim, fontStyle: "italic" }}>{meta.description}</span>
          )}
          <Badge color={mcpStatus?.configured ? T.accent : T.textMuted}>
            {mcpStatus?.configured ? "MCP Active" : "MCP Off"}
          </Badge>
          <span style={{ flex: 1 }} />
          <span className="mono" style={{ fontSize: 11, color: T.textDim }}>{stats?.project_path || ""}</span>
          <button
            onClick={openEdit}
            title="Edit project info"
            style={{
              padding: "3px 6px", borderRadius: 4, border: `1px solid ${T.border}`, background: "transparent",
              cursor: "pointer", display: "flex", alignItems: "center", flexShrink: 0,
            }}
          >
            <I name="edit" size={11} color={T.textDim} />
          </button>
        </div>
      )}

      {/* ── Row 2: Hero stats strip ── */}
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <StatBox
          label="Tokens Saved"
          value={saved >= 1000 ? (saved / 1000).toFixed(1) + "K" : saved}
          sub={`${pct}% rate \u00b7 $${cost} saved`}
          color={T.accent}
          loading={loading}
        />
        <StatBox
          label="Files"
          value={stats?.index?.files_indexed || 0}
          sub={`${stats?.index?.total_chunks || 0} chunks \u00b7 ${((orig / 1000) || 0).toFixed(1)}K tokens`}
          color={T.blue}
          loading={loading}
        />
        <StatBox
          label="Sessions"
          value={stats?.sessions_count || 0}
          sub={`${stats?.total_decisions || 0} decisions \u00b7 ${memFacts.length} facts`}
          color={T.warn}
          loading={loading}
        />
        {sourceEntries.length > 0 && (
          <StatBox
            label="Token Sources"
            value={fmtK(totalSourceTokens)}
            sub={topSourceSummary}
            color={T.purple}
          />
        )}
        {hasBudget && (
          <StatBox
            label="Budget"
            value={`${budgetPct}%`}
            sub={`${fmtK(b.response_tokens)} C3 tokens \u00b7 ${b.call_count} calls`}
            color={budgetColor}
          />
        )}
      </div>

      {/* ── Row 3: Savings + Budget progress bars ── */}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ fontSize: 11, color: T.textMuted, flexShrink: 0 }}>Savings</span>
        <ProgressBar value={pct} max={100} color={T.accent} height={6} />
        <span className="mono" style={{ fontSize: 12, color: T.accent, fontWeight: 600, flexShrink: 0 }}>{pct}%</span>
        {hasBudget && (
          <>
            <span style={{ color: T.border, margin: "0 4px" }}>|</span>
            <span style={{ fontSize: 11, color: T.textMuted, flexShrink: 0 }}>Budget</span>
            <ProgressBar value={b.response_tokens} max={b.threshold || 35000} color={budgetColor} height={6} />
            <span className="mono" style={{ fontSize: 12, color: budgetColor, fontWeight: 600, flexShrink: 0 }}>
              {fmtK(b.response_tokens)}
            </span>
          </>
        )}
      </div>

      {/* ── Row 4: Two-column layout: Session + Health | Codebase + Actions ── */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>

        {/* Left: Current Session */}
        <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8, padding: "14px 16px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
            <I name="clock" size={13} color={T.blue} />
            <span style={{ fontSize: 12, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1 }}>
              Current Session
            </span>
            <span style={{ flex: 1 }} />
            {session?.live && <Badge color={T.accent}>Live</Badge>}
            {session && (
              <Badge color={T.blue}>
                {session.source_system || session.source_ide || "unknown"}
              </Badge>
            )}
          </div>
          {!session ? (
            <div style={{ color: T.textDim, fontSize: 12, padding: "8px 0" }}>No active session.</div>
          ) : (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 8, marginBottom: 10 }}>
                {[
                  { label: "Duration", value: sessionDuration || "-", color: T.accent },
                  { label: "Tools", value: Array.isArray(session.tool_calls) ? session.tool_calls.length : (session.tool_calls || 0), color: T.warn },
                  { label: "Decisions", value: Array.isArray(session.decisions) ? session.decisions.length : (session.decisions || 0), color: T.purple },
                  { label: "Files", value: Array.isArray(session.files_touched) ? session.files_touched.length : (session.files_touched || 0), color: T.blue },
                ].map(s => (
                  <div key={s.label} style={{ background: `${s.color}10`, border: `1px solid ${s.color}25`, borderRadius: 6, padding: "6px 8px" }}>
                    <div className="mono" style={{ fontSize: 9, color: T.textDim, textTransform: "uppercase", letterSpacing: 0.8 }}>{s.label}</div>
                    <div className="mono" style={{ fontSize: 16, fontWeight: 700, color: s.color, marginTop: 2 }}>{s.value}</div>
                  </div>
                ))}
              </div>
              <div className="mono" style={{ fontSize: 10, color: T.textDim }}>
                ID: {(session.id || "-").slice(0, 16)} {"\u00b7"} started {localTime(session.started)}
              </div>

              {/* Live Claude Code token ticker — updates each exchange */}
              {liveTokens && liveTokens.input_tokens > 0 && (
                <div style={{ marginTop: 8, padding: '7px 10px', background: `${T.blue}08`, border: `1px solid ${T.blue}20`, borderRadius: 6 }}>
                  <div style={{ fontSize: 9, color: T.blue, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 0.8, marginBottom: 5 }}>
                    Live Tokens · {liveTokens.turns || 0} turns
                  </div>
                  <div style={{ display: 'flex', gap: 12 }} className="mono">
                    {[['In', liveTokens.input_tokens, T.blue],
                      ['Out', liveTokens.output_tokens, T.accent],
                      ['Cache-rd', liveTokens.cache_read_tokens, T.purple],
                    ].map(([label, val, color]) => (
                      <span key={label} style={{ fontSize: 11 }}>
                        <span style={{ color: T.textDim }}>{label}: </span>
                        <span style={{ color, fontWeight: 600 }}>
                          {val >= 1000 ? (val / 1000).toFixed(1) + 'K' : val}
                        </span>
                      </span>
                    ))}
                  </div>
                </div>
              )}            </>
          )}

          {/* Health sources inline */}
          {healthSources.length > 0 && (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 10, paddingTop: 10, borderTop: `1px solid ${T.border}` }}>
              <span style={{ fontSize: 10, color: T.textDim, alignSelf: "center", marginRight: 2 }}>Services:</span>
              {healthSources.map(([name, ok]) => (
                <span key={name} className="mono" style={{
                  padding: "2px 6px", borderRadius: 4, fontSize: 9,
                  background: ok ? `${T.accent}15` : `${T.error}15`,
                  color: ok ? T.accent : T.error,
                  border: `1px solid ${ok ? T.accent : T.error}30`,
                }}>{name}</span>
              ))}
            </div>
          )}
        </div>

        {/* Right: Codebase + Quick Actions */}
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {/* Codebase Overview */}
          <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8, padding: "14px 16px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
              <I name="layers" size={13} color={T.accent} />
              <span style={{ fontSize: 12, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1 }}>
                Codebase
              </span>
              <span style={{ flex: 1 }} />
              <span className="mono" style={{ fontSize: 10, color: T.textDim }}>
                {fmtK(totalLines)} lines
              </span>
            </div>
            {topExts.length > 0 ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {topExts.map(([ext, count]) => {
                  const total = files.length;
                  const extPct = total > 0 ? Math.round((count / total) * 100) : 0;
                  const color = extColorMap[ext] || T.textMuted;
                  return (
                    <div key={ext} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span className="mono" style={{ fontSize: 10, color, width: 36, textAlign: "right", flexShrink: 0 }}>.{ext}</span>
                      <ProgressBar value={extPct} max={100} color={color} height={5} />
                      <span className="mono" style={{ fontSize: 10, color: T.textDim, width: 50, flexShrink: 0 }}>
                        {count} ({extPct}%)
                      </span>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div style={{ color: T.textDim, fontSize: 12 }}>No files indexed.</div>
            )}
          </div>

          {/* Quick Actions */}
          <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8, padding: "14px 16px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
              <I name="zap" size={13} color={T.accent} />
              <span style={{ fontSize: 12, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1 }}>
                Quick Actions
              </span>
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button onClick={rebuildIndex} style={{
                padding: "6px 14px", borderRadius: 6, border: `1px solid ${T.blue}40`,
                background: `${T.blue}10`, color: T.blue, fontSize: 11, fontWeight: 600,
                cursor: "pointer", fontFamily: "'JetBrains Mono', monospace",
                display: "flex", alignItems: "center", gap: 5,
              }}>
                <I name="refresh" size={11} color={T.blue} /> Rebuild Index
              </button>
              <button onClick={openExplorer} style={{
                padding: "6px 14px", borderRadius: 6, border: `1px solid ${T.accent}40`,
                background: `${T.accent}10`, color: T.accent, fontSize: 11, fontWeight: 600,
                cursor: "pointer", fontFamily: "'JetBrains Mono', monospace",
                display: "flex", alignItems: "center", gap: 5,
              }}>
                <I name="folderOpen" size={11} color={T.accent} /> Open Folder
              </button>
              <a href="/docs" target="_blank" style={{
                padding: "6px 14px", borderRadius: 6, border: `1px solid ${T.purple}40`,
                background: `${T.purple}10`, color: T.purple, fontSize: 11, fontWeight: 600,
                textDecoration: "none", fontFamily: "'JetBrains Mono', monospace",
                display: "flex", alignItems: "center", gap: 5,
              }}>
                <I name="external" size={11} color={T.purple} /> API Docs
              </a>
            </div>
            {actionMsg && (
              <div className="mono" style={{ fontSize: 10, color: actionMsg.includes("fail") ? T.error : T.accent, marginTop: 8 }}>
                {actionMsg}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ── Row 5: Token Usage by Source (provider-agnostic) ── */}
      {sourceEntries.length > 0 && (
        <SectionBlock
          label="Token Usage by Source"
          icon="zap"
          color={T.purple}
          open={showUsage}
          onToggle={() => setShowUsage(!showUsage)}
          badge={<Badge color={T.purple}>{fmtK(totalSourceTokens)} total {"\u00b7"} {sourceEntries.length} sources</Badge>}
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {sourceEntries.map(([name, data]) => {
              const tokens = data?.total_tokens || 0;
              const input = data?.input_tokens || 0;
              const output = data?.output_tokens || 0;
              const calls = data?.call_count || data?.sessions || 0;
              const sourcePct = totalSourceTokens > 0 ? Math.round((tokens / totalSourceTokens) * 100) : 0;
              const colors = [T.accent, T.blue, T.purple, T.warn, T.error];
              const color = colors[sourceEntries.findIndex(([n]) => n === name) % colors.length];

              return (
                <div key={name} style={{
                  background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 6, padding: "10px 12px",
                }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                    <GlowDot color={color} size={6} />
                    <span style={{ fontSize: 12, fontWeight: 600, color: T.text, flex: 1 }}>{name}</span>
                    <span className="mono" style={{ fontSize: 11, color }}>{fmtK(tokens)} tokens</span>
                    {calls > 0 && (
                      <span className="mono" style={{ fontSize: 10, color: T.textDim }}>{calls} calls</span>
                    )}
                    <Badge color={color}>{sourcePct}%</Badge>
                  </div>
                  <ProgressBar value={sourcePct} max={100} color={color} height={5} />
                  {(input > 0 || output > 0) && (
                    <div style={{ display: "flex", gap: 12, marginTop: 6 }}>
                      <span className="mono" style={{ fontSize: 10, color: T.textDim }}>
                        {fmtK(input)} in
                      </span>
                      <span className="mono" style={{ fontSize: 10, color: T.textDim }}>
                        {fmtK(output)} out
                      </span>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </SectionBlock>
      )}

      {/* ── Row 5b: Agent Workflows ── */}
      {(() => {
        const wf = stats?.hybrid_config?.agent_workflows;
        if (!wf) return null;
        const workflows = ["prefetch", "batch_validate", "compound_compress", "delegate_chain"];
        const cfgItems = [
          { label: "Prefetch", value: wf.prefetch_max_files ?? "-" },
          { label: "Batch", value: wf.batch_validate_max_files ?? "-" },
          { label: "Compound", value: wf.compound_max_compress ?? "-" },
        ];
        return (
          <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8, padding: "14px 16px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
              <I name="layers" size={13} color={T.purple} />
              <span style={{ fontSize: 12, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1 }}>
                Agent Workflows
              </span>
              <Badge color={wf.enabled ? T.accent : T.error}>{wf.enabled ? "Enabled" : "Disabled"}</Badge>
              {wf.delegate_in_workflows && <Badge color={T.blue}>Delegate</Badge>}
            </div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 10 }}>
              {workflows.map(w => (
                <span key={w} className="mono" style={{
                  padding: "2px 8px", borderRadius: 4, fontSize: 10,
                  background: wf.enabled ? `${T.purple}15` : `${T.textMuted}15`,
                  color: wf.enabled ? T.purple : T.textDim,
                  border: `1px solid ${wf.enabled ? T.purple : T.textMuted}30`,
                }}>{w.replace(/_/g, " ")}</span>
              ))}
            </div>
            <div style={{ display: "flex", gap: 12 }}>
              {cfgItems.map(c => (
                <span key={c.label} className="mono" style={{ fontSize: 10, color: T.textDim }}>
                  {c.label}: <span style={{ color: T.text, fontWeight: 600 }}>{c.value}</span>
                </span>
              ))}
            </div>
          </div>
        );
      })()}

      {/* ── Row 6: Recent Activity ── */}
      <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8, padding: "14px 16px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
          <I name="terminal" size={13} color={T.blue} />
          <span style={{ fontSize: 12, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1 }}>
            Recent Activity
          </span>
          <Badge color={T.blue}>{activity.length}</Badge>
          <span style={{ flex: 1 }} />
          {watcher.length > 0 && (
            <Badge color={T.warn}>{watcher.length} file changes</Badge>
          )}
        </div>
        {activity.length === 0 ? (
          <div style={{ color: T.textDim, fontSize: 12, padding: "4px 0" }}>No recent activity.</div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {activity.map((e, i) => {
              const typeColor = e.type === "tool_call" ? T.blue
                : e.type === "decision" ? T.purple
                : e.type === "file_change" ? T.accent
                : e.type === "session_start" || e.type === "session_save" ? "#4ade80"
                : T.textMuted;
              return (
                <div key={i} style={{
                  display: "flex", alignItems: "center", gap: 8, padding: "5px 10px",
                  borderRadius: 6, background: T.surfaceAlt,
                }}>
                  <Badge color={typeColor}>{e.type || "event"}</Badge>
                  <span className="mono" style={{
                    fontSize: 11, color: T.text, flex: 1,
                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                  }}>
                    {eventSummary(e)}
                  </span>
                  <span className="mono" style={{ fontSize: 10, color: T.textDim, flexShrink: 0 }}>
                    {timeAgo(e.timestamp)}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* ── Row 7: Notifications (only if present) ── */}
      {notifications.length > 0 && (
        <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8, padding: "12px 16px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <I name="zap" size={13} color={T.warn} />
              <span style={{ fontSize: 12, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1 }}>
                Notifications
              </span>
              <Badge color={T.warn}>{notifications.length}</Badge>
            </div>
            <button
              onClick={ackAllNotifications}
              style={{
                padding: "3px 8px", borderRadius: 4, border: `1px solid ${T.border}`, background: "transparent",
                color: T.textMuted, fontSize: 10, cursor: "pointer", fontFamily: "'JetBrains Mono', monospace",
              }}
            >
              Dismiss all
            </button>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {notifications.slice(0, 5).map((n, i) => {
              const sevColor = n.severity === "critical" ? T.error : n.severity === "warning" ? T.warn : T.blue;
              return (
                <div
                  key={n.id || i}
                  style={{
                    display: "flex", alignItems: "center", gap: 8, padding: "5px 10px",
                    borderRadius: 6, background: T.surfaceAlt, borderLeft: `3px solid ${sevColor}`,
                  }}
                >
                  <Badge color={sevColor}>{n.severity}</Badge>
                  {n.ai_enhanced && <Badge color="#b38aff">AI</Badge>}
                  <span style={{ fontSize: 10, color: T.accent, fontWeight: 600, fontFamily: "'JetBrains Mono', monospace" }}>
                    {n.agent}
                  </span>
                  <span className="mono" style={{ fontSize: 11, color: T.text, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {n.title}{n.message ? ` - ${n.message}` : ""}
                  </span>
                  <button
                    onClick={() => ackNotification(n.id)}
                    style={{
                      width: 20, height: 20, borderRadius: 4, border: "none", background: "transparent",
                      cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
                    }}
                  >
                    <I name="xSmall" size={12} color={T.textMuted} />
                  </button>
                </div>
              );
            })}
            {notifications.length > 5 && (
              <div style={{ fontSize: 11, color: T.textMuted, textAlign: "center", padding: 4 }}>
                +{notifications.length - 5} more
              </div>
            )}
          </div>
        </div>
      )}

    </div>
  );
};
