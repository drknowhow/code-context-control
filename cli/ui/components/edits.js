// EditsPanel — Edit Ledger: AI-tracked versioning & edit history
// Globals: T, I, GlowDot, Badge, StatBox, Btn, api, timeAgo, useState, useEffect, useCallback

const EditsPanel = () => {
  const [edits, setEdits] = useState([]);
  const [stats, setStats] = useState(null);
  const [fileFilter, setFileFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [subView, setSubView] = useState("history"); // "history" | "stats"
  const [expandedId, setExpandedId] = useState(null);

  const loadEdits = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit: "100" });
      if (fileFilter) params.set("file", fileFilter);
      const data = await api.get(`/api/edits?${params}`);
      setEdits(Array.isArray(data) ? data : []);
    } catch { setEdits([]); }
    setLoading(false);
  }, [fileFilter]);

  const loadStats = useCallback(async () => {
    try {
      const s = await api.get("/api/edits/stats");
      setStats(s);
    } catch {}
  }, []);

  useEffect(() => {
    loadEdits();
    loadStats();
    const iv = setInterval(() => { loadEdits(); loadStats(); }, 15000);
    return () => clearInterval(iv);
  }, [loadEdits, loadStats]);

  const typeColors = {
    created: "#22c55e",
    modified: "#3b82f6",
    deleted: "#ef4444",
    renamed: "#f59e0b",
  };

  // ── Diff renderer ────────────────────────────────────────────────────────
  const renderDiffBlock = (label, text, bg, color) =>
    React.createElement("div", { style: { flex: 1, minWidth: 0 } },
      React.createElement("div", {
        style: { fontSize: 10, fontWeight: 700, color, marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.05em" }
      }, label),
      React.createElement("pre", {
        style: {
          margin: 0, padding: "8px 10px", borderRadius: 4, fontSize: 11,
          background: bg, color: T.text, whiteSpace: "pre-wrap", wordBreak: "break-all",
          fontFamily: "'JetBrains Mono', monospace", border: `1px solid ${color}30`,
          maxHeight: 260, overflowY: "auto",
        }
      }, text || "(empty)")
    );

  const renderDetail = (detail) => {
    if (!detail) return null;

    // Batch edits: detail.patches is an array
    if (detail.patches && detail.patches.length) {
      return React.createElement("div", {
        style: { display: "flex", flexDirection: "column", gap: 12, marginTop: 4 }
      },
        detail.patches.map((p, i) =>
          React.createElement("div", { key: i },
            p.summary && React.createElement("div", {
              style: { fontSize: 11, color: T.textMuted, marginBottom: 6, fontStyle: "italic" }
            }, `Patch ${i + 1}: ${p.summary}`),
            React.createElement("div", { style: { display: "flex", gap: 8 } },
              renderDiffBlock("removed", p.old_string, "#ef444418", "#ef4444"),
              renderDiffBlock("added", p.new_string, "#22c55e18", "#22c55e"),
            )
          )
        )
      );
    }

    // Single edit: detail.old_string / detail.new_string
    if (detail.old_string !== undefined || detail.new_string !== undefined) {
      return React.createElement("div", { style: { display: "flex", gap: 8, marginTop: 4 } },
        renderDiffBlock("removed", detail.old_string, "#ef444418", "#ef4444"),
        renderDiffBlock("added", detail.new_string, "#22c55e18", "#22c55e"),
      );
    }

    return null;
  };

  // ── Stats view ───────────────────────────────────────────────────────────
  const renderStats = () => {
    if (!stats) return null;
    return React.createElement("div", { style: { display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 16 } },
      React.createElement(StatBox, { label: "Total Edits", value: stats.total || 0 }),
      React.createElement(StatBox, { label: "Files Edited", value: stats.files || 0 }),
      ...(stats.by_type ? Object.entries(stats.by_type).map(([k, v]) =>
        React.createElement(StatBox, { key: k, label: k, value: v })
      ) : [])
    );
  };

  const renderMostEdited = () => {
    if (!stats || !stats.most_edited || !stats.most_edited.length) return null;
    return React.createElement("div", { style: { marginTop: 16 } },
      React.createElement("div", { style: { fontSize: 13, fontWeight: 600, color: T.text, marginBottom: 8 } }, "Most Edited Files"),
      ...stats.most_edited.slice(0, 8).map((m, i) =>
        React.createElement("div", {
          key: i,
          style: {
            display: "flex", justifyContent: "space-between", padding: "6px 10px",
            background: i % 2 === 0 ? T.surface : "transparent", borderRadius: 4, fontSize: 12
          }
        },
          React.createElement("span", { className: "mono", style: { color: T.accent } }, m.file),
          React.createElement(Badge, { color: T.textMuted }, `${m.count} edits`)
        )
      )
    );
  };

  // ── History view ─────────────────────────────────────────────────────────
  const renderHistory = () => {
    if (loading) return React.createElement("div", { style: { color: T.textMuted, fontSize: 13 } }, "Loading...");
    if (!edits.length) return React.createElement("div", { style: { color: T.textMuted, fontSize: 13 } }, "No edits recorded yet.");

    return React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 2 } },
      edits.slice().reverse().map((e, i) => {
        const hasDiff = e.detail && (e.detail.patches || e.detail.old_string !== undefined || e.detail.new_string !== undefined);
        const isExpanded = expandedId === (e.id || i);

        return React.createElement("div", { key: e.id || i },
          // ── Row ──────────────────────────────────────────────────────────
          React.createElement("div", {
            onClick: hasDiff ? () => setExpandedId(isExpanded ? null : (e.id || i)) : undefined,
            style: {
              display: "grid", gridTemplateColumns: "140px 1fr 70px 70px auto",
              gap: 8, padding: "7px 10px", fontSize: 12, alignItems: "center",
              background: isExpanded ? `${T.accent}10` : i % 2 === 0 ? T.surface : "transparent",
              borderRadius: isExpanded ? "4px 4px 0 0" : 4,
              cursor: hasDiff ? "pointer" : "default",
              borderLeft: isExpanded ? `2px solid ${T.accent}` : "2px solid transparent",
            }
          },
            // Timestamp
            React.createElement("span", { className: "mono", style: { color: T.textMuted, fontSize: 11 } },
              e.timestamp ? e.timestamp.slice(0, 19).replace("T", " ") : ""
            ),
            // File + summary
            React.createElement("div", { style: { minWidth: 0 } },
              React.createElement("span", { className: "mono", style: { color: T.accent, fontSize: 12 } }, e.file),
              e.summary ? React.createElement("span", { style: { color: T.textMuted, marginLeft: 8, fontSize: 11 } }, e.summary) : null
            ),
            // Version badge
            React.createElement(Badge, { color: "#8b5cf6" }, e.version || ""),
            // Change type
            React.createElement("span", {
              style: { color: typeColors[e.change_type] || T.textMuted, fontSize: 11, fontWeight: 600 }
            }, e.change_type || ""),
            // Git info + diff + tags + expand chevron
            React.createElement("div", { style: { display: "flex", gap: 6, alignItems: "center" } },
              e.diff_summary ? React.createElement("span", { className: "mono", style: { fontSize: 10, color: T.textMuted } }, e.diff_summary) : null,
              e.git && e.git.commit ? React.createElement("span", { className: "mono", style: { fontSize: 10, color: T.textDim } }, e.git.commit.slice(0, 7)) : null,
              e.tags && e.tags.length ? e.tags.map((tag, ti) =>
                React.createElement(Badge, { key: ti, color: T.warn }, tag)
              ) : null,
              hasDiff ? React.createElement("span", {
                style: { fontSize: 14, color: T.textMuted, lineHeight: 1, userSelect: "none" }
              }, isExpanded ? "▲" : "▼") : null,
            )
          ),
          // ── Diff panel ───────────────────────────────────────────────────
          isExpanded && hasDiff ? React.createElement("div", {
            style: {
              padding: "10px 12px 12px",
              background: `${T.accent}08`,
              borderRadius: "0 0 4px 4px",
              borderLeft: `2px solid ${T.accent}`,
              borderRight: `1px solid ${T.border}`,
              borderBottom: `1px solid ${T.border}`,
            }
          }, renderDetail(e.detail)) : null
        );
      })
    );
  };

  // ── Layout ────────────────────────────────────────────────────────────────
  const toggleStyle = (active) => ({
    padding: "4px 12px", borderRadius: 4, fontSize: 12, fontWeight: 600, cursor: "pointer",
    border: `1px solid ${active ? T.accent : T.border}`,
    background: active ? `${T.accent}20` : "transparent",
    color: active ? T.accent : T.textMuted,
  });

  return React.createElement("div", null,
    // Header row: toggle + filter
    React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 } },
      React.createElement("div", { style: { display: "flex", gap: 6 } },
        React.createElement("button", { style: toggleStyle(subView === "history"), onClick: () => setSubView("history") }, "History"),
        React.createElement("button", { style: toggleStyle(subView === "stats"), onClick: () => setSubView("stats") }, "Stats")
      ),
      React.createElement("input", {
        type: "text", placeholder: "Filter by file path...", value: fileFilter,
        onChange: (e) => setFileFilter(e.target.value),
        style: {
          width: 260, padding: "5px 10px", borderRadius: 6, fontSize: 12,
          border: `1px solid ${T.border}`, background: T.surface, color: T.text,
          fontFamily: "'JetBrains Mono', monospace"
        }
      })
    ),

    // Content
    subView === "stats"
      ? React.createElement("div", null, renderStats(), renderMostEdited())
      : renderHistory()
  );
};
