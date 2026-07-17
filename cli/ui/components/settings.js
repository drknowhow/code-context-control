// ─── SettingsPanel ────────────────────────
// Globals: T, I, GlowDot, Badge, StatBox, Btn, Section, api, timeAgo, renderBoolToggle, useState, useEffect, useCallback

const SettingsPanel = ({ stats }) => {
  // ── Core state ──
  const [msg, setMsg] = useState("");
  const [rebuilding, setRebuilding] = useState(false);
  const [generating, setGenerating] = useState(false);

  // ── Budget ──
  const [budgetCfg, setBudgetCfg] = useState(null);
  const [savingBudget, setSavingBudget] = useState(false);

  // ── Hybrid / Feature Flags ──
  const [hybridCfg, setHybridCfg] = useState(null);

  // ── Agents ──
  const [agentsCfg, setAgentsCfg] = useState(null);
  const [agentsStatus, setAgentsStatus] = useState({});
  const [runningAgent, setRunningAgent] = useState("");

  // ── Delegate ──
  const [delegateCfg, setDelegateCfg] = useState(null);

  // ── Memory LLM (distillation + recall injection) ──
  const [memoryLlmCfg, setMemoryLlmCfg] = useState(null);
  const [memKeyInput, setMemKeyInput] = useState("");

  // ── Proxy ──
  const [proxyCfg, setProxyCfg] = useState(null);

  // ── MCP ──
  const [mcpStatus, setMcpStatus] = useState(null);
  const [mcpIde, setMcpIde] = useState("auto");
  const [installIde, setInstallIde] = useState("auto");
  const [installMcpMode, setInstallMcpMode] = useState("direct");
  const [installing, setInstalling] = useState(false);
  const [showAddMcp, setShowAddMcp] = useState(false);
  const [newMcpName, setNewMcpName] = useState("");
  const [newMcpCmd, setNewMcpCmd] = useState("");
  const [newMcpArgs, setNewMcpArgs] = useState("");

  // ── Permissions ──
  const [permsCfg, setPermsCfg] = useState(null);
  const [savingPerms, setSavingPerms] = useState(false);

  // ── Shared ──
  const [ollamaModels, setOllamaModels] = useState([]);
  const [busy, setBusyState] = useState({ agents: false, delegate: false, proxy: false, memoryLlm: false });

  // ── Project Data ──
  const [dataSummary, setDataSummary] = useState(null);
  const [dataLoading, setDataLoading] = useState(false);
  const [dataWorking, setDataWorking] = useState(null);
  const [dataConfirm, setDataConfirm] = useState(null);
  const [dataMsg, setDataMsg] = useState(null);

  // ── Section open state ──
  const [sections, setSections] = useState({
    project: true,
    budget: false,
    features: false,
    editLedger: false,
    agents: false,
    delegate: false,
    memoryLlm: false,
    codex: false,
    gemini: false,
    workflows: false,
    proxy: false,
    mcp: false,
    permissions: false,
    data: false,
  });

  const toggleSection = (key) => setSections(prev => ({ ...prev, [key]: !prev[key] }));

  // ── Helpers ──
  const flashMsg = (text, delay = 3000) => {
    setMsg(text);
    if (delay) setTimeout(() => setMsg(""), delay);
  };

  const setBusy = (key, value) => setBusyState(prev => ({ ...prev, [key]: value }));

  const inputStyle = {
    width: "100%",
    background: T.surfaceAlt,
    border: `1px solid ${T.border}`,
    borderRadius: 4,
    padding: "5px 8px",
    color: T.text,
    fontFamily: "'JetBrains Mono', monospace",
    fontSize: 12,
    outline: "none",
  };
  const labelStyle = {
    fontSize: 10,
    color: T.textDim,
    textTransform: "uppercase",
    letterSpacing: 0.8,
    marginBottom: 4,
  };

  const renderModelOptions = () => (
    <>
      <option value="">Auto-select</option>
      {ollamaModels.map(m => <option key={m} value={m}>{m}</option>)}
    </>
  );

  // ── Load functions ──
  const loadMcpStatus = useCallback(async (ide = mcpIde) => {
    try {
      const q = ide && ide !== "auto" ? `?ide=${encodeURIComponent(ide)}` : "";
      const s = await api.get(`/api/mcp/status${q}`);
      setMcpStatus(s);
    } catch (e) { }
  }, [mcpIde]);

  const loadAgentStatus = useCallback(async () => {
    try {
      const s = await api.get('/api/agents/status');
      const mapped = {};
      for (const item of (s?.agents || [])) mapped[item.name] = item;
      setAgentsStatus(mapped);
    } catch (e) { }
  }, []);

  const loadDataSummary = useCallback(async () => {
    setDataLoading(true);
    try { const s = await api.get('/api/data/summary'); setDataSummary(s); } catch (e) { }
    setDataLoading(false);
  }, []);

  // ── Mount load ──
  useEffect(() => {
    const init = async () => {
      try {
        const [hybrid, budget, agents, delegate, proxy, models, perms, memoryLlm] = await Promise.all([
          api.get('/api/hybrid/config').catch(() => null),
          api.get('/api/budget/config').catch(() => null),
          api.get('/api/agents/config').catch(() => null),
          api.get('/api/delegate/config').catch(() => null),
          api.get('/api/proxy/config').catch(() => null),
          api.get('/api/ollama/models').catch(() => ({ models: [] })),
          api.get('/api/permissions').catch(() => null),
          api.get('/api/memory-llm/config').catch(() => null),
        ]);
        if (hybrid) setHybridCfg(hybrid);
        if (budget) setBudgetCfg(budget);
        if (agents) setAgentsCfg(agents);
        if (delegate) setDelegateCfg(delegate);
        if (proxy) setProxyCfg(proxy);
        if (memoryLlm) setMemoryLlmCfg(memoryLlm);
        setOllamaModels(models?.models || []);
        if (perms) setPermsCfg(perms);
      } catch (e) { }
    };
    init();
    loadMcpStatus();
    loadAgentStatus();
    loadDataSummary();
  }, []);

  // ── MCP ide watcher ──
  useEffect(() => { loadMcpStatus(mcpIde); }, [mcpIde]);

  // ── Agent status auto-refresh ──
  useEffect(() => {
    const iv = setInterval(loadAgentStatus, 15000);
    return () => clearInterval(iv);
  }, [loadAgentStatus]);

  // ── Hybrid flag toggle ──
  const toggleHybridFlag = async (flag) => {
    if (!hybridCfg) return;
    const next = !hybridCfg[flag];
    try {
      const updated = await api.put('/api/hybrid/config', { [flag]: next });
      setHybridCfg(updated || {});
    } catch (e) { flashMsg(`✗ Toggle ${flag}: ${e.message}`); }
  };

  // ── Budget save ──
  const saveBudget = async () => {
    if (!budgetCfg) return;
    setSavingBudget(true);
    try {
      const updated = await api.put('/api/budget/config', budgetCfg);
      setBudgetCfg(updated || {});
      flashMsg("✓ Saved budget settings");
    } catch (e) { flashMsg(`✗ Save budget: ${e.message}`); }
    setSavingBudget(false);
  };

  // ── Agent field update ──
  const updateAgentField = (agentName, key, value) => {
    setAgentsCfg(prev => ({
      ...(prev || {}),
      [agentName]: { ...(prev?.[agentName] || {}), [key]: value },
    }));
  };

  const saveAgent = async (agentName) => {
    if (!agentsCfg?.[agentName]) return;
    setBusy("agents", true);
    try {
      const updated = await api.put('/api/agents/config', { [agentName]: agentsCfg[agentName] });
      setAgentsCfg(updated || {});
      await loadAgentStatus();
      flashMsg(`✓ Saved ${agentName}`);
    } catch (e) { flashMsg(`✗ Save agent: ${e.message}`); }
    setBusy("agents", false);
  };

  const runAgentNow = async (agentName) => {
    setRunningAgent(agentName);
    try {
      await api.post(`/api/agents/run/${encodeURIComponent(agentName)}`, {});
      await loadAgentStatus();
      flashMsg(`✓ Ran ${agentName}`);
    } catch (e) { flashMsg(`✗ Run agent: ${e.message}`); }
    setRunningAgent("");
  };

  // ── Delegate field update ──
  const updateDelegateField = (key, value) => {
    setDelegateCfg(prev => ({ ...(prev || {}), [key]: value }));
  };

  const saveDelegate = async () => {
    if (!delegateCfg) return;
    setBusy("delegate", true);
    try {
      const updated = await api.put('/api/delegate/config', delegateCfg);
      setDelegateCfg(updated || {});
      flashMsg("✓ Saved delegate settings");
    } catch (e) { flashMsg(`✗ Save delegate: ${e.message}`); }
    setBusy("delegate", false);
  };

  // ── Memory LLM field update ──
  const updateMemoryLlmField = (key, value) => {
    setMemoryLlmCfg(prev => ({ ...(prev || {}), [key]: value }));
  };

  const saveMemoryLlm = async () => {
    if (!memoryLlmCfg) return;
    setBusy("memoryLlm", true);
    try {
      const updated = await api.put('/api/memory-llm/config', memoryLlmCfg);
      const keySet = memoryLlmCfg.api_key_set;
      setMemoryLlmCfg(updated ? { ...updated, api_key_set: keySet } : memoryLlmCfg);
      flashMsg("✓ Saved memory LLM settings");
    } catch (e) { flashMsg(`✗ Save memory LLM: ${e.message}`); }
    setBusy("memoryLlm", false);
  };

  const setMemoryLlmKey = async (key) => {
    setBusy("memoryLlm", true);
    try {
      const res = await api.post('/api/memory-llm/key', { key: key || "" });
      setMemoryLlmCfg(prev => ({ ...(prev || {}), api_key_set: !!res.api_key_set }));
      setMemKeyInput("");
      flashMsg(key ? "✓ API key stored in OS keyring" : "✓ API key cleared");
    } catch (e) { flashMsg(`✗ API key: ${e.message}`); }
    setBusy("memoryLlm", false);
  };

  // ── Proxy field update ──
  const updateProxyField = (key, value) => {
    setProxyCfg(prev => ({ ...(prev || {}), [key]: value }));
  };

  const saveProxy = async () => {
    if (!proxyCfg) return;
    setBusy("proxy", true);
    try {
      const payload = {
        ...proxyCfg,
        always_visible: Array.isArray(proxyCfg.always_visible)
          ? proxyCfg.always_visible
          : String(proxyCfg.always_visible || "").split(",").map(v => v.trim()).filter(Boolean),
      };
      const updated = await api.put('/api/proxy/config', payload);
      setProxyCfg(updated || {});
      flashMsg("✓ Saved proxy settings");
    } catch (e) { flashMsg(`✗ Save proxy: ${e.message}`); }
    setBusy("proxy", false);
  };

  // ── Permissions ──
  const applyPermTier = async (tier) => {
    setSavingPerms(true);
    try {
      const res = await api.put('/api/permissions', { tier });
      // Reload full state after apply
      const updated = await api.get('/api/permissions');
      setPermsCfg(updated);
      flashMsg(`✓ Applied '${tier}' permissions — restart Claude Code to activate`);
    } catch (e) { flashMsg(`✗ Permissions: ${e.message}`); }
    setSavingPerms(false);
  };

  // ── Project Data helpers ──
  const flashData = (text, ok) => {
    setDataMsg({ text, ok });
    setTimeout(() => setDataMsg(null), 3500);
  };

  const doDataAction = async (key, fn) => {
    setDataWorking(key);
    try {
      const r = await fn();
      flashData(`✓ ${r?.cleared !== undefined ? r.cleared + ' items cleared' : 'Done'}`, true);
      await loadDataSummary();
    } catch (e) { flashData(`✗ ${e.message || 'Failed'}`, false); }
    setDataWorking(null);
  };

  const confirmThenRun = (key, fn) => {
    if (dataConfirm === key) {
      setDataConfirm(null);
      doDataAction(key, fn);
    } else {
      setDataConfirm(key);
      setTimeout(() => setDataConfirm(c => c === key ? null : c), 3000);
    }
  };

  // ── Project path / file count from stats ──
  const projectPath = stats?.project_path || stats?.path || "—";
  const filesIndexed = stats?.files_indexed ?? stats?.index?.count ?? "—";
  const indexStatus = stats?.index_status || (stats?.index ? "ready" : "unknown");
  const statusColor = indexStatus === "ready" ? T.accent : indexStatus === "building" ? T.warn : T.textMuted;

  // ── Data rows ──
  const dataRows = dataSummary ? [
    {
      key: 'index', label: 'Index', icon: 'search', color: T.blue,
      count: `${dataSummary.index?.count ?? 0} files`, size: dataSummary.index?.size_kb ?? 0,
      action: 'Rebuild',
      onAction: () => doDataAction('index', () => api.post('/api/index/rebuild')),
    },
    {
      key: 'sessions', label: 'Sessions', icon: 'clock', color: T.warn,
      count: dataSummary.sessions?.count ?? 0, size: dataSummary.sessions?.size_kb ?? 0,
      action: 'Keep last 5',
      onAction: () => confirmThenRun('sessions', () => api.delete('/api/data/sessions?keep=5')),
    },
    {
      key: 'cache', label: 'Compression Cache', icon: 'minimize', color: T.purple,
      count: `${dataSummary.cache?.count ?? 0} files`, size: dataSummary.cache?.size_kb ?? 0,
      action: 'Clear',
      onAction: () => confirmThenRun('cache', () => api.delete('/api/data/cache')),
    },
    {
      key: 'snapshots', label: 'Snapshots', icon: 'bookmark', color: T.accent,
      count: dataSummary.snapshots?.count ?? 0, size: dataSummary.snapshots?.size_kb ?? 0,
      action: 'Clear all',
      onAction: () => confirmThenRun('snapshots', () => api.delete('/api/data/snapshots')),
    },
    {
      key: 'file_memory', label: 'File Maps', icon: 'file', color: T.blue,
      count: `${dataSummary.file_memory?.count ?? 0} maps`, size: dataSummary.file_memory?.size_kb ?? 0,
      action: 'Clear',
      onAction: () => confirmThenRun('file_memory', () => api.delete('/api/data/file-memory')),
    },
    {
      key: 'notifications', label: 'Notifications', icon: 'zap', color: T.warn,
      count: dataSummary.notifications?.count ?? 0, size: dataSummary.notifications?.size_kb ?? 0,
      action: 'Clear',
      onAction: () => confirmThenRun('notifications', () => api.delete('/api/data/notifications')),
    },
    {
      key: 'sltm', label: 'SLTM Memory', icon: 'brain', color: T.purple,
      count: `${dataSummary.sltm?.count ?? 0} records`, size: dataSummary.sltm?.size_kb ?? 0,
    },
  ] : [];

  return (
    <div className="fade-up" style={{ display: "flex", flexDirection: "column", gap: 10 }}>

      {/* ── Global status message ── */}
      {msg && (
        <div className="mono fade-up" style={{
          padding: "8px 12px", borderRadius: 6, fontSize: 11,
          background: T.surfaceAlt,
          color: msg.startsWith("✓") ? T.accent : T.error,
          border: `1px solid ${msg.startsWith("✓") ? T.accent : T.error}30`,
        }}>
          {msg}
        </div>
      )}

      {/* ══════════════════════════════════════════
          1. PROJECT INFO
      ══════════════════════════════════════════ */}
      <Section
        label="Project Info"
        icon="folder"
        color={T.accent}
        open={sections.project}
        onToggle={() => toggleSection("project")}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {/* Path + status */}
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span style={{ color: T.textDim, fontSize: 11 }}>Project Path</span>
              <Badge color={statusColor}>
                <GlowDot color={statusColor} size={5} />
                {indexStatus}
              </Badge>
            </div>
            <div className="mono" style={{
              fontSize: 11, color: T.textMuted, background: T.surfaceAlt,
              padding: "6px 8px", borderRadius: 4, wordBreak: "break-all",
              border: `1px solid ${T.border}`,
            }}>
              {projectPath}
            </div>
          </div>

          {/* Files indexed */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 0", borderBottom: `1px solid ${T.border}22` }}>
            <span style={{ color: T.textMuted, fontSize: 12 }}>Files Indexed</span>
            <span className="mono" style={{ color: T.text, fontSize: 13, fontWeight: 600 }}>{filesIndexed}</span>
          </div>

          {/* Quick actions */}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", paddingTop: 4 }}>
            <Btn
              color={T.blue}
              onClick={async () => {
                setRebuilding(true);
                try {
                  await api.post('/api/index/rebuild');
                  flashMsg("✓ Index rebuild started");
                } catch (e) { flashMsg(`✗ Rebuild: ${e.message}`); }
                setRebuilding(false);
              }}
              disabled={rebuilding}
            >
              <I name="refresh" size={13} style={rebuilding ? { animation: "spin 0.6s linear infinite" } : {}} />
              {rebuilding ? "Rebuilding..." : "Rebuild Index"}
            </Btn>
            <Btn
              color={T.accent}
              onClick={async () => {
                setGenerating(true);
                try {
                  await api.post('/api/claudemd/sync');
                  flashMsg("✓ Instructions saved");
                } catch (e) { flashMsg(`✗ Save: ${e.message}`); }
                setGenerating(false);
              }}
              disabled={generating}
            >
              <I name="save" size={13} />
              {generating ? "Saving..." : "Save Instructions"}
            </Btn>
          </div>
        </div>
      </Section>

      {/* ══════════════════════════════════════════
          2. BUDGET & GUIDANCE
      ══════════════════════════════════════════ */}
      <Section
        label="Budget & Guidance"
        icon="zap"
        color={T.warn}
        open={sections.budget}
        onToggle={() => toggleSection("budget")}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {renderBoolToggle(
            "Budget Nudges",
            !!hybridCfg?.show_context_nudges,
            () => toggleHybridFlag("show_context_nudges"),
            "Append budget warning when over threshold. Tells the AI when to snapshot."
          )}
          {renderBoolToggle(
            "Agent Alerts",
            !!hybridCfg?.prepend_notifications,
            () => toggleHybridFlag("prepend_notifications"),
            "Prepend critical alerts inline so the AI sees and relays them."
          )}

          {budgetCfg && (
            <>
              <div style={{ paddingTop: 8, borderTop: `1px solid ${T.border}22`, marginTop: 4 }}>
                <div style={labelStyle}>Budget Threshold (tokens)</div>
                <input
                  type="number"
                  min="1000"
                  value={budgetCfg.threshold ?? ""}
                  onChange={e => setBudgetCfg(prev => ({ ...prev, threshold: parseInt(e.target.value || "0", 10) || 0 }))}
                  style={inputStyle}
                />
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 8 }}>
                <Btn color={T.warn} onClick={saveBudget} disabled={savingBudget}>
                  <I name="save" size={13} />
                  {savingBudget ? "Saving..." : "Save Budget Settings"}
                </Btn>
              </div>
            </>
          )}
          {!budgetCfg && <div style={{ color: T.textDim, fontSize: 12 }}>Loading budget settings...</div>}
        </div>
      </Section>

      {/* ══════════════════════════════════════════
          3. FEATURE FLAGS
      ══════════════════════════════════════════ */}
      <Section
        label="Feature Flags"
        icon="settings"
        color={T.purple}
        open={sections.features}
        onToggle={() => toggleSection("features")}
        badge={<Badge color={T.purple}>Hybrid</Badge>}
      >
        {hybridCfg ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {renderBoolToggle(
              "Tier 1 Output Filter",
              !hybridCfg.HYBRID_DISABLE_TIER1,
              () => toggleHybridFlag("HYBRID_DISABLE_TIER1"),
              "Compress and filter tool output to reduce token usage."
            )}
            {renderBoolToggle(
              "Tier 2 Adaptive Router",
              !hybridCfg.HYBRID_DISABLE_TIER2,
              () => toggleHybridFlag("HYBRID_DISABLE_TIER2"),
              "Route requests to the most efficient backend."
            )}
            {renderBoolToggle(
              "Tier 3 SLTM Memory",
              !hybridCfg.HYBRID_DISABLE_SLTM,
              () => toggleHybridFlag("HYBRID_DISABLE_SLTM"),
              "Enable semantic long-term memory recall."
            )}
            {renderBoolToggle(
              "Auto-Memory",
              hybridCfg?.auto_memory?.enabled !== false,
              async () => {
                const cur = hybridCfg?.auto_memory?.enabled !== false;
                try {
                  const updated = await api.put('/api/hybrid/config', { auto_memory: { enabled: !cur } });
                  setHybridCfg(updated || {});
                } catch (e) { flashMsg(`✗ Toggle auto-memory: ${e.message}`); }
              },
              "Learn from tool calls automatically."
            )}

            <div style={{ paddingTop: 8, borderTop: `1px solid ${T.border}22`, marginTop: 4 }}>
              <div style={labelStyle}>Validate Timeout (seconds)</div>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <input
                  type="number"
                  min="5"
                  value={hybridCfg?.validate_timeout ?? ""}
                  onChange={e => setHybridCfg(prev => ({ ...prev, validate_timeout: parseInt(e.target.value || "0", 10) || 0 }))}
                  style={{ ...inputStyle, flex: 1 }}
                />
                <Btn
                  color={T.purple}
                  onClick={async () => {
                    try {
                      const updated = await api.put('/api/hybrid/config', { validate_timeout: hybridCfg.validate_timeout });
                      setHybridCfg(updated || {});
                      flashMsg("✓ Saved validate timeout");
                    } catch (e) { flashMsg(`✗ Save timeout: ${e.message}`); }
                  }}
                >
                  <I name="save" size={13} /> Save
                </Btn>
              </div>
            </div>
          </div>
        ) : (
          <div style={{ color: T.textDim, fontSize: 12 }}>Loading feature flags...</div>
        )}
      </Section>

      {/* ══════════════════════════════════════════
          3b. EDIT LEDGER
      ══════════════════════════════════════════ */}
      <Section
        label="Edit Ledger"
        icon="edit"
        color={T.purple}
        open={sections.editLedger}
        onToggle={() => toggleSection("editLedger")}
        badge={<Badge color={T.purple}>Tracking</Badge>}
      >
        {hybridCfg ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {renderBoolToggle(
              "Edit Tracking",
              hybridCfg?.edit_ledger?.enabled !== false,
              async () => {
                const cur = hybridCfg?.edit_ledger?.enabled !== false;
                try {
                  const updated = await api.put('/api/hybrid/config', { edit_ledger: { ...hybridCfg?.edit_ledger, enabled: !cur } });
                  setHybridCfg(updated || {});
                } catch (e) { flashMsg(`✗ Toggle edit tracking: ${e.message}`); }
              },
              "Auto-log all Edit/Write operations to the ledger."
            )}
            {renderBoolToggle(
              "Auto-Tag",
              hybridCfg?.edit_ledger?.auto_tag !== false,
              async () => {
                const cur = hybridCfg?.edit_ledger?.auto_tag !== false;
                try {
                  const updated = await api.put('/api/hybrid/config', { edit_ledger: { ...hybridCfg?.edit_ledger, auto_tag: !cur } });
                  setHybridCfg(updated || {});
                } catch (e) { flashMsg(`✗ Toggle auto-tag: ${e.message}`); }
              },
              "Automatically tag hook-generated entries."
            )}
            <div style={{ paddingTop: 8, borderTop: `1px solid ${T.border}22`, marginTop: 4 }}>
              <div style={labelStyle}>Tracking Level</div>
              <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
                {["minimal", "standard", "detailed"].map(level => (
                  <button
                    key={level}
                    onClick={async () => {
                      try {
                        const updated = await api.put('/api/hybrid/config', { edit_ledger: { ...hybridCfg?.edit_ledger, tracking_level: level } });
                        setHybridCfg(updated || {});
                        flashMsg(`Tracking level set to ${level}`);
                      } catch (e) { flashMsg(`✗ ${e.message}`); }
                    }}
                    style={{
                      padding: "5px 12px", borderRadius: 5, fontSize: 11, fontWeight: 600,
                      cursor: "pointer",
                      border: `1px solid ${(hybridCfg?.edit_ledger?.tracking_level || "standard") === level ? T.accent : T.border}`,
                      background: (hybridCfg?.edit_ledger?.tracking_level || "standard") === level ? `${T.accent}18` : "transparent",
                      color: (hybridCfg?.edit_ledger?.tracking_level || "standard") === level ? T.accent : T.dim,
                    }}
                  >
                    {level.charAt(0).toUpperCase() + level.slice(1)}
                  </button>
                ))}
              </div>
              <div style={{ fontSize: 10, color: T.dim, marginTop: 4 }}>
                Minimal: file + type only. Standard: + git info & diffs. Detailed: + code snippets.
              </div>
            </div>
            <div style={{ paddingTop: 8, borderTop: `1px solid ${T.border}22`, marginTop: 4 }}>
              <Btn onClick={() => window.open('/edits', '_blank')} style={{ fontSize: 11 }}>
                Open Edit Ledger ↗
              </Btn>
            </div>
          </div>
        ) : (
          <div style={{ color: T.dim, fontSize: 12 }}>Loading...</div>
        )}
      </Section>

      {/* ══════════════════════════════════════════
          4. BACKGROUND AGENTS
      ══════════════════════════════════════════ */}
      <Section
        label="Background Agents"
        icon="cpu"
        color={T.blue}
        open={sections.agents}
        onToggle={() => toggleSection("agents")}
      >
        {agentsCfg ? (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 12 }}>
            {Object.entries(agentsCfg).map(([agentName, cfg]) => {
              const live = agentsStatus[agentName] || {};
              const agentStatusColor = !cfg.enabled ? T.textMuted : live.running ? T.accent : T.warn;
              const agentStatusLabel = !cfg.enabled ? "Disabled" : live.running ? "Running" : "Idle";
              return (
                <div key={agentName} style={{
                  background: T.surfaceAlt, border: `1px solid ${T.border}`,
                  borderRadius: 8, padding: 12, display: "flex", flexDirection: "column", gap: 10,
                }}>
                  {/* Header */}
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                    <span style={{ color: T.text, fontSize: 13, fontWeight: 600 }}>{agentName}</span>
                    <Badge color={agentStatusColor}>
                      <GlowDot color={agentStatusColor} size={5} />
                      {agentStatusLabel}
                    </Badge>
                  </div>

                  {/* Stats row */}
                  <div style={{ display: "flex", gap: 6 }}>
                    {[
                      { label: "Checks", value: live.check_count ?? 0 },
                      { label: "Errors", value: live.error_count ?? 0, valueColor: (live.error_count || 0) > 0 ? T.error : T.text },
                      { label: "Last Check", value: live.last_check ? timeAgo(new Date((live.last_check || 0) * 1000).toISOString()) : "Never", mono: false },
                    ].map(s => (
                      <div key={s.label} style={{
                        flex: 1, background: T.surface, border: `1px solid ${T.border}`,
                        borderRadius: 6, padding: "6px 8px",
                      }}>
                        <div style={{ color: T.textDim, fontSize: 10, textTransform: "uppercase", letterSpacing: 0.8 }}>{s.label}</div>
                        <div className={s.mono !== false ? "mono" : ""} style={{ color: s.valueColor || T.text, fontSize: s.mono !== false ? 13 : 11, marginTop: 2 }}>
                          {s.value}
                        </div>
                      </div>
                    ))}
                  </div>

                  {/* Toggles */}
                  {renderBoolToggle("Enabled", !!cfg.enabled, () => updateAgentField(agentName, "enabled", !cfg.enabled))}
                  {"use_ai" in cfg && renderBoolToggle("AI Enhancement", !!cfg.use_ai, () => updateAgentField(agentName, "use_ai", !cfg.use_ai))}

                  {/* Interval + model */}
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                    <div>
                      <div style={labelStyle}>Interval (sec)</div>
                      <input
                        type="number" min="5"
                        value={cfg.interval ?? ""}
                        onChange={e => updateAgentField(agentName, "interval", parseInt(e.target.value || "0", 10) || 0)}
                        style={inputStyle}
                      />
                    </div>
                    {"ai_model" in cfg && (
                      <div>
                        <div style={labelStyle}>AI Model</div>
                        <select value={cfg.ai_model || ""} onChange={e => updateAgentField(agentName, "ai_model", e.target.value)} style={inputStyle}>
                          {renderModelOptions()}
                        </select>
                      </div>
                    )}
                  </div>

                  {/* Actions */}
                  <div style={{ display: "flex", gap: 8 }}>
                    <Btn color={T.blue} onClick={() => saveAgent(agentName)} disabled={busy.agents}>
                      <I name="save" size={13} /> {busy.agents ? "Saving..." : "Save"}
                    </Btn>
                    <Btn color={T.accent} variant="outline" onClick={() => runAgentNow(agentName)} disabled={runningAgent === agentName || !cfg.enabled}>
                      <I name="zap" size={13} color={runningAgent === agentName ? T.textMuted : T.accent} />
                      {runningAgent === agentName ? "Running..." : "Run Now"}
                    </Btn>
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <div style={{ color: T.textDim, fontSize: 12 }}>Loading agent settings...</div>
        )}
      </Section>

      {/* ══════════════════════════════════════════
          5. DELEGATE SETTINGS
      ══════════════════════════════════════════ */}
      <Section
        label="Delegate Settings"
        icon="share"
        color={T.purple}
        open={sections.delegate}
        onToggle={() => toggleSection("delegate")}
        badge={delegateCfg && <Badge color={delegateCfg.enabled ? T.accent : T.textMuted}>{delegateCfg.enabled ? "Enabled" : "Disabled"}</Badge>}
      >
        {delegateCfg ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
              {/* Policy toggles */}
              <div style={{ background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
                {renderBoolToggle("Delegate Enabled", !!delegateCfg.enabled, () => updateDelegateField("enabled", !delegateCfg.enabled))}
                {renderBoolToggle("Threshold Policy", !!delegateCfg.threshold_enabled, () => updateDelegateField("threshold_enabled", !delegateCfg.threshold_enabled), "Delegate automatically once token threshold is met.")}
                {renderBoolToggle("Fallback Models", !!delegateCfg.allow_model_fallback, () => updateDelegateField("allow_model_fallback", !delegateCfg.allow_model_fallback))}
              </div>
              {/* Auto toggles */}
              <div style={{ background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
                {renderBoolToggle("Auto-Compress", !!delegateCfg.auto_compress, () => updateDelegateField("auto_compress", !delegateCfg.auto_compress))}
                {renderBoolToggle("Auto-Search", !!delegateCfg.auto_search, () => updateDelegateField("auto_search", !delegateCfg.auto_search))}
                {renderBoolToggle("Auto-Vector Search", !!delegateCfg.auto_vector_search, () => updateDelegateField("auto_vector_search", !delegateCfg.auto_vector_search))}
                {renderBoolToggle("Auto-Activity Log", !!delegateCfg.auto_activity_log, () => updateDelegateField("auto_activity_log", !delegateCfg.auto_activity_log))}
              </div>
            </div>

            {/* Numeric inputs */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 10 }}>
              <div>
                <div style={labelStyle}>Preferred Model</div>
                <select value={delegateCfg.preferred_model || ""} onChange={e => updateDelegateField("preferred_model", e.target.value)} style={inputStyle}>
                  {renderModelOptions()}
                </select>
              </div>
              <div>
                <div style={labelStyle}>Max Tokens</div>
                <input type="number" min="64" value={delegateCfg.max_tokens ?? ""} onChange={e => updateDelegateField("max_tokens", parseInt(e.target.value || "0", 10) || 0)} style={inputStyle} />
              </div>
              <div>
                <div style={labelStyle}>Search Top K</div>
                <input type="number" min="1" value={delegateCfg.search_top_k ?? ""} onChange={e => updateDelegateField("search_top_k", parseInt(e.target.value || "0", 10) || 0)} style={inputStyle} />
              </div>
              <div>
                <div style={labelStyle}>Max Context Tokens</div>
                <input type="number" min="100" value={delegateCfg.max_context_tokens ?? ""} onChange={e => updateDelegateField("max_context_tokens", parseInt(e.target.value || "0", 10) || 0)} style={inputStyle} />
              </div>
              <div>
                <div style={labelStyle}>Threshold Min Tokens</div>
                <input type="number" min="1" value={delegateCfg.threshold_min_total_tokens ?? ""} onChange={e => updateDelegateField("threshold_min_total_tokens", parseInt(e.target.value || "0", 10) || 0)} style={inputStyle} />
              </div>
            </div>

            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <Btn color={T.purple} onClick={saveDelegate} disabled={busy.delegate}>
                <I name="save" size={13} /> {busy.delegate ? "Saving..." : "Save Delegate Settings"}
              </Btn>
            </div>
          </div>
        ) : (
          <div style={{ color: T.textDim, fontSize: 12 }}>Loading delegate settings...</div>
        )}
      </Section>

      {/* ══════════════════════════════════════════
          5a. MEMORY LLM (distillation + recall)
      ══════════════════════════════════════════ */}
      <Section
        label="Memory LLM"
        icon="brain"
        color={T.accent}
        open={sections.memoryLlm}
        onToggle={() => toggleSection("memoryLlm")}
        badge={memoryLlmCfg && <Badge color={!memoryLlmCfg.enabled ? T.textMuted : (memoryLlmCfg.cloud_enabled ? T.purple : T.accent)}>{!memoryLlmCfg.enabled ? "Disabled" : (memoryLlmCfg.cloud_enabled ? "Cloud" : "Local only")}</Badge>}
      >
        {memoryLlmCfg ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
              {/* Capture toggles */}
              <div style={{ background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
                {renderBoolToggle("Session Digests", !!memoryLlmCfg.enabled, () => updateMemoryLlmField("enabled", !memoryLlmCfg.enabled), "Distill each session into durable facts with an LLM at session end.")}
                {renderBoolToggle("Transcript Mining", !!memoryLlmCfg.transcript_mining_enabled, () => updateMemoryLlmField("transcript_mining_enabled", !memoryLlmCfg.transcript_mining_enabled), "Extract your corrections and preferences from conversation transcripts.")}
                {renderBoolToggle("Cloud Model", !!memoryLlmCfg.cloud_enabled, () => updateMemoryLlmField("cloud_enabled", !memoryLlmCfg.cloud_enabled), "Use Ollama Cloud (Sonnet-class). Session content leaves this machine — off = local model only, fully private.")}
              </div>
              {/* Recall toggles */}
              <div style={{ background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
                {renderBoolToggle("Prompt Recall Injection", !!memoryLlmCfg.prompt_inject_enabled, () => updateMemoryLlmField("prompt_inject_enabled", !memoryLlmCfg.prompt_inject_enabled), "Inject relevant project facts into each prompt (UserPromptSubmit hook).")}
                {renderBoolToggle("Related Facts on Read", !!memoryLlmCfg.read_related_facts_enabled, () => updateMemoryLlmField("read_related_facts_enabled", !memoryLlmCfg.read_related_facts_enabled), "Append facts touching a file to c3_read output.")}
              </div>
            </div>

            {/* Model + budget inputs */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 10 }}>
              <div>
                <div style={labelStyle}>Local Model (private)</div>
                <select value={memoryLlmCfg.local_model || ""} onChange={e => updateMemoryLlmField("local_model", e.target.value)} style={inputStyle}>
                  {renderModelOptions()}
                </select>
              </div>
              <div>
                <div style={labelStyle}>Cloud Model</div>
                <input type="text" list="c3-cloud-models" value={memoryLlmCfg.cloud_model || ""} onChange={e => updateMemoryLlmField("cloud_model", e.target.value)} style={inputStyle} placeholder="glm-4.6:cloud" />
                <datalist id="c3-cloud-models">
                  <option value="glm-4.6:cloud" />
                  <option value="deepseek-v3.1:671b-cloud" />
                  <option value="qwen3-coder:480b-cloud" />
                  <option value="kimi-k2:1t-cloud" />
                  <option value="gpt-oss:120b-cloud" />
                </datalist>
              </div>
              <div>
                <div style={labelStyle}>Max Facts / Session</div>
                <input type="number" min="1" max="20" value={memoryLlmCfg.max_facts_per_session ?? ""} onChange={e => updateMemoryLlmField("max_facts_per_session", parseInt(e.target.value || "0", 10) || 0)} style={inputStyle} />
              </div>
              <div>
                <div style={labelStyle}>Inject Top K</div>
                <input type="number" min="1" max="10" value={memoryLlmCfg.inject_top_k ?? ""} onChange={e => updateMemoryLlmField("inject_top_k", parseInt(e.target.value || "0", 10) || 0)} style={inputStyle} />
              </div>
            </div>

            {/* Cloud API key (OS keyring — never written to config.json) */}
            <div style={{ background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
                <div style={{ ...labelStyle, marginBottom: 0 }}>Ollama Cloud API Key</div>
                <Badge color={memoryLlmCfg.api_key_set ? T.accent : T.textMuted}>{memoryLlmCfg.api_key_set ? "Key set" : "No key"}</Badge>
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <input type="password" value={memKeyInput} onChange={e => setMemKeyInput(e.target.value)} style={{ ...inputStyle, flex: 1 }} placeholder="Paste key — stored in OS keyring, never in config.json" />
                <Btn color={T.accent} onClick={() => memKeyInput.trim() && setMemoryLlmKey(memKeyInput)} disabled={busy.memoryLlm || !memKeyInput.trim()}>Set</Btn>
                <Btn color={T.textMuted} onClick={() => setMemoryLlmKey("")} disabled={busy.memoryLlm || !memoryLlmCfg.api_key_set}>Clear</Btn>
              </div>
              <div style={{ fontSize: 11, color: T.textDim, marginTop: 6 }}>
                Also honored: the OLLAMA_API_KEY environment variable, or a signed-in local Ollama daemon (localhost base URL) which needs no key.
              </div>
            </div>

            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <Btn color={T.accent} onClick={saveMemoryLlm} disabled={busy.memoryLlm}>
                <I name="save" size={13} /> {busy.memoryLlm ? "Saving..." : "Save Memory LLM Settings"}
              </Btn>
            </div>
          </div>
        ) : (
          <div style={{ color: T.textDim, fontSize: 12 }}>Loading memory LLM settings...</div>
        )}
      </Section>

      {/* ══════════════════════════════════════════
          5b. CODEX INTEGRATION
      ══════════════════════════════════════════ */}
      <Section
        label="Codex Integration"
        icon="terminal"
        color={T.orange || "#e67e22"}
        open={sections.codex}
        onToggle={() => toggleSection("codex")}
        badge={delegateCfg && <Badge color={delegateCfg.codex_enabled ? T.accent : T.textMuted}>{delegateCfg.codex_enabled ? "Active" : "Inactive"}</Badge>}
      >
        {delegateCfg ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ fontSize: 12, color: T.textMuted, marginBottom: 4 }}>
              Cloud delegate backend via OpenAI Codex CLI. Enables deep code review, test generation, and consensus workflows using GPT-5.x models.
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
              {/* Master toggles */}
              <div style={{ background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
                {renderBoolToggle("Codex Enabled", !!delegateCfg.codex_enabled, () => updateDelegateField("codex_enabled", !delegateCfg.codex_enabled), "Master switch for Codex as a delegate backend.")}
                {renderBoolToggle("Verify Edits", !!delegateCfg.codex_verify_edits, () => updateDelegateField("codex_verify_edits", !delegateCfg.codex_verify_edits), "Auto-review edits via Codex after enrichment.")}
                {renderBoolToggle("Memory Bridge", delegateCfg.codex_memory_bridge !== false, () => updateDelegateField("codex_memory_bridge", !(delegateCfg.codex_memory_bridge !== false)), "Auto-extract findings into c3_memory.")}
              </div>
              {/* Model & sandbox */}
              <div style={{ background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
                <div>
                  <div style={labelStyle}>Default Model</div>
                  <select value={delegateCfg.codex_default_model || "gpt-5.3-codex-spark"} onChange={e => updateDelegateField("codex_default_model", e.target.value)} style={inputStyle}>
                    <option value="gpt-5.4">gpt-5.4</option>
                    <option value="gpt-5.3-codex-spark">gpt-5.3-codex-spark</option>
                    <option value="gpt-5.3-codex">gpt-5.3-codex</option>
                  </select>
                </div>
                <div style={{ marginTop: 8 }}>
                  <div style={labelStyle}>Sandbox Mode</div>
                  <select value={delegateCfg.codex_default_sandbox || "read-only"} onChange={e => updateDelegateField("codex_default_sandbox", e.target.value)} style={inputStyle}>
                    <option value="read-only">read-only (safe)</option>
                    <option value="workspace-write">workspace-write (local edits)</option>
                    <option value="danger-full-access">danger-full-access (network)</option>
                  </select>
                </div>
                <div style={{ marginTop: 8 }}>
                  <div style={labelStyle}>Reasoning Effort</div>
                  <select value={delegateCfg.codex_reasoning_effort || "high"} onChange={e => updateDelegateField("codex_reasoning_effort", e.target.value)} style={inputStyle}>
                    <option value="xhigh">xhigh</option>
                    <option value="high">high</option>
                    <option value="medium">medium</option>
                    <option value="low">low</option>
                  </select>
                </div>
              </div>
            </div>

            {/* Numeric inputs */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 10 }}>
              <div>
                <div style={labelStyle}>Timeout (seconds)</div>
                <input type="number" min="30" max="600" value={delegateCfg.codex_timeout ?? 120} onChange={e => updateDelegateField("codex_timeout", parseInt(e.target.value || "120", 10) || 120)} style={inputStyle} />
              </div>
              <div>
                <div style={labelStyle}>Max Context Tokens</div>
                <input type="number" min="200" value={delegateCfg.codex_max_context_tokens ?? 4000} onChange={e => updateDelegateField("codex_max_context_tokens", parseInt(e.target.value || "4000", 10) || 4000)} style={inputStyle} />
              </div>
            </div>

            {/* Auto-routed task types */}
            <div style={{ background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
              <div style={labelStyle}>Auto-Routed Task Types</div>
              <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 6 }}>
                Tasks auto-routed to Codex when backend='auto'. Click to toggle.
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {["review", "diagnose", "improve", "test", "explain", "summarize", "ask", "docstring"].map(tt => {
                  const active = (delegateCfg.codex_task_types || ["review", "diagnose", "improve", "test"]).includes(tt);
                  return <button key={tt} onClick={() => {
                    const current = delegateCfg.codex_task_types || ["review", "diagnose", "improve", "test"];
                    const next = active ? current.filter(t => t !== tt) : [...current, tt];
                    updateDelegateField("codex_task_types", next);
                  }} style={{
                    padding: "3px 10px", borderRadius: 12, border: `1px solid ${active ? (T.orange || T.accent) : T.border}`,
                    background: active ? (T.orange || T.accent) + "22" : "transparent",
                    color: active ? (T.orange || T.accent) : T.textMuted,
                    cursor: "pointer", fontSize: 11, fontWeight: active ? 600 : 400,
                  }}>{tt}</button>;
                })}
              </div>
            </div>

            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <Btn color={T.orange || T.accent} onClick={saveDelegate} disabled={busy.delegate}>
                <I name="save" size={13} /> {busy.delegate ? "Saving..." : "Save Codex Settings"}
              </Btn>
            </div>
          </div>
        ) : (
          <div style={{ color: T.textDim, fontSize: 12 }}>Loading delegate settings...</div>
        )}
      </Section>

      {/* ══════════════════════════════════════════
          5c. GEMINI INTEGRATION
      ══════════════════════════════════════════ */}
      <Section
        label="Gemini Integration"
        icon="cloud"
        color={T.blue || "#3498db"}
        open={sections.gemini}
        onToggle={() => toggleSection("gemini")}
        badge={delegateCfg && <Badge color={delegateCfg.gemini_enabled ? T.accent : T.textMuted}>{delegateCfg.gemini_enabled ? "Active" : "Inactive"}</Badge>}
      >
        {delegateCfg ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ fontSize: 12, color: T.textMuted, marginBottom: 4 }}>
              Cloud delegate backend via Google Gemini CLI. Enables code review, diagnosis, and tri-consensus workflows using Gemini 2.5 models.
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
              <div style={{ background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
                {renderBoolToggle("Gemini Enabled", !!delegateCfg.gemini_enabled, () => updateDelegateField("gemini_enabled", !delegateCfg.gemini_enabled), "Master switch for Gemini as a delegate backend.")}
                {renderBoolToggle("Memory Bridge", delegateCfg.gemini_memory_bridge !== false, () => updateDelegateField("gemini_memory_bridge", !(delegateCfg.gemini_memory_bridge !== false)), "Auto-extract findings into c3_memory.")}
              </div>
              <div style={{ background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
                <div>
                  <div style={labelStyle}>Default Model</div>
                  <select value={delegateCfg.gemini_default_model || "gemini-2.5-flash"} onChange={e => updateDelegateField("gemini_default_model", e.target.value)} style={inputStyle}>
                    <option value="gemini-2.5-flash">gemini-2.5-flash</option>
                    <option value="gemini-2.5-pro">gemini-2.5-pro</option>
                  </select>
                </div>
              </div>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 10 }}>
              <div>
                <div style={labelStyle}>Timeout (seconds)</div>
                <input type="number" min="30" max="600" value={delegateCfg.gemini_timeout ?? 120} onChange={e => updateDelegateField("gemini_timeout", parseInt(e.target.value || "120", 10) || 120)} style={inputStyle} />
              </div>
              <div>
                <div style={labelStyle}>Max Context Tokens</div>
                <input type="number" min="200" value={delegateCfg.gemini_max_context_tokens ?? 8000} onChange={e => updateDelegateField("gemini_max_context_tokens", parseInt(e.target.value || "8000", 10) || 8000)} style={inputStyle} />
              </div>
            </div>

            <div style={{ background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
              <div style={labelStyle}>Auto-Routed Task Types</div>
              <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 6 }}>
                Tasks auto-routed to Gemini when backend='auto'. Click to toggle.
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {["review", "diagnose", "improve", "test", "explain", "summarize", "ask", "docstring"].map(tt => {
                  const active = (delegateCfg.gemini_task_types || ["review", "diagnose", "improve", "test"]).includes(tt);
                  return <button key={tt} onClick={() => {
                    const current = delegateCfg.gemini_task_types || ["review", "diagnose", "improve", "test"];
                    const next = active ? current.filter(t => t !== tt) : [...current, tt];
                    updateDelegateField("gemini_task_types", next);
                  }} style={{
                    padding: "3px 10px", borderRadius: 12, border: `1px solid ${active ? (T.blue || "#3498db") : T.border}`,
                    background: active ? (T.blue || "#3498db") + "22" : "transparent",
                    color: active ? (T.blue || "#3498db") : T.textMuted,
                    cursor: "pointer", fontSize: 11, fontWeight: active ? 600 : 400,
                  }}>{tt}</button>;
                })}
              </div>
            </div>

            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <Btn color={T.blue || "#3498db"} onClick={saveDelegate} disabled={busy.delegate}>
                <I name="save" size={13} /> {busy.delegate ? "Saving..." : "Save Gemini Settings"}
              </Btn>
            </div>
          </div>
        ) : (
          <div style={{ color: T.textDim, fontSize: 12 }}>Loading delegate settings...</div>
        )}
      </Section>

      {/* ══════════════════════════════════════════
          6. AGENT WORKFLOWS
      ══════════════════════════════════════════ */}
      <Section
        label="Agent Workflows"
        icon="zap"
        color={T.accent}
        open={sections.workflows}
        onToggle={() => toggleSection("workflows")}
        badge={<Badge color={hybridCfg?.agent_workflows?.enabled !== false ? T.accent : T.textMuted}>
          {hybridCfg?.agent_workflows?.enabled !== false ? "Enabled" : "Disabled"}
        </Badge>}
      >
        {hybridCfg ? (() => {
          const wf = hybridCfg.agent_workflows || {};
          const updateWfField = async (key, value) => {
            try {
              const updated = await api.put('/api/hybrid/config', {
                agent_workflows: { ...wf, [key]: value }
              });
              setHybridCfg(updated || {});
            } catch (e) { flashMsg(`✗ Update workflow setting: ${e.message}`); }
          };
          return (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <div style={{ color: T.textDim, fontSize: 12, marginBottom: 4 }}>
                Compound pipelines that run multi-step C3 operations in a single call.
              </div>
              {renderBoolToggle(
                "Workflows Enabled",
                wf.enabled !== false,
                () => updateWfField("enabled", !(wf.enabled !== false)),
                "Enable c3_agent compound workflows (review_changes, prepare_context, investigate, preflight)."
              )}
              {renderBoolToggle(
                "Delegate in Workflows",
                wf.delegate_in_workflows !== false,
                () => updateWfField("delegate_in_workflows", !(wf.delegate_in_workflows !== false)),
                "Allow Ollama AI delegation within compound workflows."
              )}
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10, paddingTop: 8, borderTop: `1px solid ${T.border}22` }}>
                <div>
                  <div style={labelStyle}>Prefetch Max Files</div>
                  <input type="number" min="1" max="10" value={wf.prefetch_max_files ?? 3}
                    onChange={e => updateWfField("prefetch_max_files", parseInt(e.target.value || "3", 10) || 3)}
                    style={inputStyle} />
                  <div style={{ color: T.textDim, fontSize: 11 }}>Files auto-compressed on search prefetch</div>
                </div>
                <div>
                  <div style={labelStyle}>Batch Validate Max</div>
                  <input type="number" min="1" max="20" value={wf.batch_validate_max_files ?? 10}
                    onChange={e => updateWfField("batch_validate_max_files", parseInt(e.target.value || "10", 10) || 10)}
                    style={inputStyle} />
                  <div style={{ color: T.textDim, fontSize: 11 }}>Max files per batch validate call</div>
                </div>
                <div>
                  <div style={labelStyle}>Compound Max Compress</div>
                  <input type="number" min="1" max="10" value={wf.compound_max_compress ?? 5}
                    onChange={e => updateWfField("compound_max_compress", parseInt(e.target.value || "5", 10) || 5)}
                    style={inputStyle} />
                  <div style={{ color: T.textDim, fontSize: 11 }}>Max files compressed in agent workflows</div>
                </div>
              </div>
            </div>
          );
        })() : (
          <div style={{ color: T.textDim, fontSize: 12 }}>Loading workflow settings...</div>
        )}
      </Section>

      {/* ══════════════════════════════════════════
          7. PROXY SETTINGS
      ══════════════════════════════════════════ */}
      <Section
        label="Proxy Settings"
        icon="terminal"
        color={T.warn}
        open={sections.proxy}
        onToggle={() => toggleSection("proxy")}
        badge={proxyCfg && <Badge color={proxyCfg.PROXY_DISABLE ? T.textMuted : T.warn}>{proxyCfg.PROXY_DISABLE ? "Disabled" : "Available"}</Badge>}
      >
        {proxyCfg ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
              {/* Boolean toggles */}
              <div style={{ background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12 }}>
                {renderBoolToggle("Proxy Enabled", !proxyCfg.PROXY_DISABLE, () => updateProxyField("PROXY_DISABLE", !proxyCfg.PROXY_DISABLE))}
                {renderBoolToggle("Filter Tools", !!proxyCfg.filter_tools, () => updateProxyField("filter_tools", !proxyCfg.filter_tools))}
                {renderBoolToggle("Use SLM", !!proxyCfg.use_slm, () => updateProxyField("use_slm", !proxyCfg.use_slm))}
                {renderBoolToggle("Inject Context Summary", !!proxyCfg.inject_context_summary, () => updateProxyField("inject_context_summary", !proxyCfg.inject_context_summary))}
              </div>
              {/* Numeric + select inputs */}
              <div style={{ background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8, padding: 12, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, alignContent: "start" }}>
                <div>
                  <div style={labelStyle}>Max Tools</div>
                  <input type="number" min="1" value={proxyCfg.max_tools ?? ""} onChange={e => updateProxyField("max_tools", parseInt(e.target.value || "0", 10) || 0)} style={inputStyle} />
                </div>
                <div>
                  <div style={labelStyle}>Context Window</div>
                  <input type="number" min="1" value={proxyCfg.context_window_size ?? ""} onChange={e => updateProxyField("context_window_size", parseInt(e.target.value || "0", 10) || 0)} style={inputStyle} />
                </div>
                <div style={{ gridColumn: "1 / -1" }}>
                  <div style={labelStyle}>SLM Model</div>
                  <select value={proxyCfg.slm_model || ""} onChange={e => updateProxyField("slm_model", e.target.value)} style={inputStyle}>
                    {renderModelOptions()}
                  </select>
                </div>
                <div style={{ gridColumn: "1 / -1" }}>
                  <div style={labelStyle}>Always Visible Categories</div>
                  <input
                    value={Array.isArray(proxyCfg.always_visible) ? proxyCfg.always_visible.join(", ") : (proxyCfg.always_visible || "")}
                    onChange={e => updateProxyField("always_visible", e.target.value)}
                    style={inputStyle}
                    placeholder="core, search, memory"
                  />
                </div>
              </div>
            </div>

            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <Btn color={T.warn} onClick={saveProxy} disabled={busy.proxy}>
                <I name="save" size={13} /> {busy.proxy ? "Saving..." : "Save Proxy Settings"}
              </Btn>
            </div>
          </div>
        ) : (
          <div style={{ color: T.textDim, fontSize: 12 }}>Loading proxy settings...</div>
        )}
      </Section>

      {/* ══════════════════════════════════════════
          8. MCP SERVERS
      ══════════════════════════════════════════ */}
      <Section
        label="MCP Servers"
        icon="cpu"
        color={T.blue}
        open={sections.mcp}
        onToggle={() => toggleSection("mcp")}
        badge={
          <Badge color={(mcpStatus?.active ?? mcpStatus?.configured) ? T.accent : T.textMuted}>
            <GlowDot color={(mcpStatus?.active ?? mcpStatus?.configured) ? T.accent : T.textDim} size={5} />
            {(mcpStatus?.active ?? mcpStatus?.configured) ? "Active" : "Inactive"}
          </Badge>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {/* Status row */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 0", borderBottom: `1px solid ${T.border}` }}>
            <span style={{ color: T.textMuted, fontSize: 13 }}>MCP Mode</span>
            <Badge color={mcpStatus?.mode === "proxy" ? T.warn : T.accent}>
              {mcpStatus?.mode === "proxy" ? "Proxy (Advanced)" : "Direct (Recommended)"}
            </Badge>
          </div>

          {/* Target IDE */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 0", borderBottom: `1px solid ${T.border}` }}>
            <span style={{ color: T.textMuted, fontSize: 13 }}>Target IDE</span>
            <select value={mcpIde} onChange={e => setMcpIde(e.target.value)} style={{ ...inputStyle, width: "auto", minWidth: 170 }}>
              <option value="auto">Auto-detect</option>
              <option value="claude">Claude Code</option>
              <option value="vscode">VS Code Copilot</option>
              <option value="cursor">Cursor</option>
              <option value="codex">OpenAI Codex</option>
              <option value="antigravity">Google Antigravity</option>
            </select>
          </div>

          {/* Installed MCPs */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ color: T.textMuted, fontSize: 12, fontWeight: 600 }}>Installed MCPs</span>
            <button onClick={() => setShowAddMcp(!showAddMcp)} style={{ background: "transparent", border: "none", color: T.accent, cursor: "pointer", fontSize: 12 }}>
              {showAddMcp ? "Cancel" : "+ Add Server"}
            </button>
          </div>

          {/* Add server form */}
          {showAddMcp && (
            <div className="fade-up" style={{ display: "flex", flexDirection: "column", gap: 8, padding: 12, background: T.surfaceAlt, borderRadius: 6 }}>
              <input value={newMcpName} onChange={e => setNewMcpName(e.target.value)} placeholder="Server name (e.g. sqlite)" style={inputStyle} />
              <input value={newMcpCmd} onChange={e => setNewMcpCmd(e.target.value)} placeholder="Command (e.g. npx)" style={inputStyle} />
              <input value={newMcpArgs} onChange={e => setNewMcpArgs(e.target.value)} placeholder="Args comma-separated (e.g. -y,@modelcontextprotocol/server-sqlite)" style={inputStyle} />
              <Btn color={T.accent} onClick={async () => {
                if (!newMcpName || !newMcpCmd) return alert("Name and command are required");
                const args = newMcpArgs ? newMcpArgs.split(',').map(a => a.trim()) : [];
                try {
                  await api.post('/api/mcp/servers', { ide: mcpIde, name: newMcpName, command: newMcpCmd, args });
                  setNewMcpName(""); setNewMcpCmd(""); setNewMcpArgs(""); setShowAddMcp(false);
                  loadMcpStatus(mcpIde);
                } catch (e) { alert("Failed to add: " + e.message); }
              }}>
                <I name="save" size={13} /> Save Server
              </Btn>
            </div>
          )}

          {/* Server cards */}
          {mcpStatus?.config?.mcpServers && Object.keys(mcpStatus.config.mcpServers).length > 0 ? (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 8 }}>
              {Object.entries(mcpStatus.config.mcpServers).map(([name, srv]) => (
                <div key={name} style={{ background: T.surfaceAlt, border: `1px solid ${T.border}`, borderRadius: 8, padding: 10, display: "flex", flexDirection: "column", gap: 6 }}>
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                    <Badge color={name === "c3" ? T.accent : T.blue}>{name}</Badge>
                    <button
                      onClick={async () => {
                        const confirmMsg = name === "c3"
                          ? `Remove MCP server '${name}' from ${mcpIde}? This also removes related C3 files/hooks.`
                          : `Remove MCP server '${name}' from ${mcpIde}?`;
                        if (!confirm(confirmMsg)) return;
                        try {
                          await api.del(`/api/mcp/servers/${encodeURIComponent(name)}?ide=${encodeURIComponent(mcpIde)}&remove_files=1`);
                          loadMcpStatus(mcpIde);
                        } catch (e) { alert("Failed to remove: " + e.message); }
                      }}
                      title={`Remove ${name}`}
                      style={{ background: "transparent", border: "none", color: T.error, cursor: "pointer", padding: 2, lineHeight: 0 }}
                    >
                      <I name="trash" size={14} color={T.error} />
                    </button>
                  </div>
                  <div className="mono" style={{ fontSize: 10, color: T.textDim, wordBreak: "break-all" }}>{srv.command || "No command"}</div>
                  <div className="mono" style={{ fontSize: 10, color: T.textDim, wordBreak: "break-all" }}>{(srv.args || []).join(" ")}</div>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ padding: "8px 0", color: T.textDim, fontSize: 12 }}>No MCP servers configured for {mcpIde}.</div>
          )}

          {/* C3 server found status */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 0", borderTop: `1px solid ${T.border}` }}>
            <span style={{ color: T.textMuted, fontSize: 13 }}>C3 Server Script</span>
            <Badge color={mcpStatus?.server_found ? T.accent : T.textDim}>{mcpStatus?.server_found ? "Found" : "Not found"}</Badge>
          </div>

          {/* Install / reinstall */}
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <select value={installIde} onChange={e => setInstallIde(e.target.value)} style={{ ...inputStyle, flex: 1, minWidth: 140, width: "auto" }}>
              <option value="auto">Auto-detect IDE</option>
              <option value="claude">Claude Code</option>
              <option value="antigravity">Google Antigravity</option>
              <option value="vscode">VS Code Copilot</option>
              <option value="cursor">Cursor</option>
              <option value="codex">OpenAI Codex</option>
            </select>
            <select value={installMcpMode} onChange={e => setInstallMcpMode(e.target.value)} style={{ ...inputStyle, width: "auto" }}>
              <option value="direct">Direct</option>
              <option value="proxy">Proxy (Advanced)</option>
            </select>
            <Btn
              color={T.purple}
              onClick={async () => {
                setInstalling(true);
                try {
                  const r = await api.post('/api/mcp/install', { ide: installIde, mcp_mode: installMcpMode });
                  flashMsg(`✓ MCP Install: ${JSON.stringify(r).slice(0, 80)}`);
                  const targetIde = installIde === "auto" ? (mcpStatus?.ide || mcpIde) : installIde;
                  setMcpIde(targetIde);
                  loadMcpStatus(targetIde);
                } catch (e) { flashMsg(`✗ MCP Install: ${e.message}`); }
                setInstalling(false);
              }}
              disabled={installing}
            >
              <I name="terminal" size={13} />
              {installing ? "Installing..." : mcpStatus?.configured ? "Reinstall C3 MCP" : "Install C3 MCP"}
            </Btn>
          </div>
        </div>
      </Section>

      {/* ══════════════════════════════════════════
          9. PERMISSIONS (Claude Code only)
      ══════════════════════════════════════════ */}
      {permsCfg?.supported !== false && <Section
        label="Permissions (Claude Code)"
        icon="lock"
        color={T.warn}
        open={sections.permissions}
        onToggle={() => toggleSection("permissions")}
        badge={permsCfg?.current_tier && <Badge color={
          permsCfg.current_tier === "read-only" ? T.blue :
          permsCfg.current_tier === "standard" ? T.accent :
          permsCfg.current_tier === "permissive" ? T.warn : T.textDim
        }>{permsCfg.current_tier}</Badge>}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <div style={{ fontSize: 11, color: T.textDim, lineHeight: 1.5 }}>
            Control what Claude Code is allowed to do in this project via <code style={{ fontSize: 10, background: T.surfaceAlt, padding: "1px 4px", borderRadius: 3 }}>.claude/settings.local.json</code>.
            C3 MCP tools are always allowed in every tier.
          </div>

          {permsCfg?.current_tier && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 10px", borderRadius: 6, background: T.surfaceAlt, border: `1px solid ${T.border}` }}>
              <I name="shield" size={14} color={
                permsCfg.current_tier === "read-only" ? T.blue :
                permsCfg.current_tier === "standard" ? T.accent : T.warn
              } />
              <div>
                <div style={{ fontSize: 12, fontWeight: 600, color: T.text }}>{permsCfg.current_tier}</div>
                <div style={{ fontSize: 10, color: T.textDim }}>{permsCfg.allow_count} allow, {permsCfg.deny_count} deny rules</div>
              </div>
            </div>
          )}

          {/* Tier selector */}
          {permsCfg?.tiers && Object.entries(permsCfg.tiers).map(([name, info]) => {
            const isActive = permsCfg.current_tier === name;
            const tierColor = name === "read-only" ? T.blue : name === "standard" ? T.accent : T.warn;
            return (
              <div key={name} style={{
                display: "flex", alignItems: "center", gap: 10, padding: "8px 10px", borderRadius: 6,
                border: `1px solid ${isActive ? tierColor + "60" : T.border}`,
                background: isActive ? tierColor + "10" : "transparent",
                cursor: savingPerms ? "default" : "pointer",
                transition: "all 0.15s",
              }} onClick={() => !savingPerms && !isActive && applyPermTier(name)}>
                <div style={{
                  width: 14, height: 14, borderRadius: "50%",
                  border: `2px solid ${isActive ? tierColor : T.textDim + "60"}`,
                  background: isActive ? tierColor : "transparent",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  flexShrink: 0,
                }}>
                  {isActive && <div style={{ width: 6, height: 6, borderRadius: "50%", background: T.bg }} />}
                </div>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 12, fontWeight: isActive ? 600 : 400, color: isActive ? tierColor : T.text }}>
                    {name}
                  </div>
                  <div style={{ fontSize: 10, color: T.textDim }}>{info.description}</div>
                </div>
                <div className="mono" style={{ fontSize: 10, color: T.textDim, whiteSpace: "nowrap" }}>
                  {info.allow_count} allow
                </div>
              </div>
            );
          })}

          {savingPerms && (
            <div style={{ fontSize: 11, color: T.accent, textAlign: "center" }}>Applying permissions...</div>
          )}
        </div>
      </Section>}

      {/* ══════════════════════════════════════════
          10. PROJECT DATA
      ══════════════════════════════════════════ */}
      <Section
        label="Project Data"
        icon="folder"
        color={T.textMuted}
        open={sections.data}
        onToggle={() => toggleSection("data")}
        badge={dataSummary && <span className="mono" style={{ fontSize: 11, color: T.textDim }}>{dataSummary.total_kb} KB</span>}
      >
        <div>
          {/* Header row */}
          <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}>
            <div style={{ flex: 1 }} />
            <button onClick={loadDataSummary} title="Refresh" disabled={dataLoading}
              style={{ padding: "2px 6px", borderRadius: 4, border: `1px solid ${T.border}`, background: "transparent", cursor: "pointer", display: "flex", alignItems: "center" }}>
              <I name="refresh" size={10} color={T.textDim} style={dataLoading ? { animation: "spin 0.6s linear infinite" } : {}} />
            </button>
          </div>

          {dataLoading && !dataSummary && <div style={{ fontSize: 12, color: T.textDim }}>Loading...</div>}

          {dataSummary && (
            <>
              {/* Column headers */}
              <div style={{
                display: "grid", gridTemplateColumns: "1fr auto auto auto",
                gap: "0 10px", fontSize: 10, color: T.textDim,
                textTransform: "uppercase", letterSpacing: 1,
                padding: "0 0 6px", borderBottom: `1px solid ${T.border}`, marginBottom: 2,
              }}>
                <span>Category</span>
                <span style={{ textAlign: "right" }}>Items</span>
                <span style={{ textAlign: "right" }}>Size</span>
                <span></span>
              </div>

              {/* Data rows */}
              {dataRows.map(row => (
                <div key={row.key} style={{
                  display: "grid", gridTemplateColumns: "1fr auto auto auto",
                  gap: "0 10px", alignItems: "center", padding: "7px 0",
                  borderBottom: `1px solid ${T.border}22`,
                }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
                    <I name={row.icon} size={12} color={row.color || T.textDim} />
                    <span style={{ fontSize: 13, color: T.textMuted }}>{row.label}</span>
                  </div>
                  <span className="mono" style={{ fontSize: 11, color: T.textDim, textAlign: "right" }}>{row.count}</span>
                  <span className="mono" style={{ fontSize: 11, color: T.textDim, textAlign: "right", minWidth: 52 }}>
                    {row.size > 0 ? `${row.size} KB` : "—"}
                  </span>
                  <div style={{ minWidth: 76, textAlign: "right" }}>
                    {row.action && (
                      <button
                        onClick={row.onAction}
                        disabled={dataWorking === row.key}
                        style={{
                          padding: "3px 8px", borderRadius: 4, fontSize: 10, cursor: dataWorking === row.key ? "default" : "pointer",
                          fontFamily: "'JetBrains Mono', monospace",
                          border: `1px solid ${dataConfirm === row.key ? T.error + "90" : T.border}`,
                          background: dataConfirm === row.key ? `${T.error}18` : "transparent",
                          color: dataConfirm === row.key ? T.error : T.textMuted,
                          transition: "all 0.15s",
                        }}
                      >
                        {dataWorking === row.key ? "..." : dataConfirm === row.key ? "Confirm?" : row.action}
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </>
          )}

          {dataMsg && (
            <div className="mono fade-up" style={{
              marginTop: 10, padding: "6px 10px", borderRadius: 5,
              background: T.surfaceAlt, fontSize: 11, color: dataMsg.ok ? T.accent : T.error,
            }}>
              {dataMsg.text}
            </div>
          )}
        </div>
      </Section>

    </div>
  );
};
