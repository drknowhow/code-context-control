// JiraPanel — Jira Cloud / Data Center browser (v2.56.0)
// Globals: T, I, api, useState, useEffect, useCallback, Badge
// v1 scope: My Work board (statusCategory grouping — no agile API),
// JQL search, and an issue drawer with transition/comment/assign.

const JIRA_COLUMNS = [
  { id: "new", label: "To Do" },
  { id: "indeterminate", label: "In Progress" },
  { id: "done", label: "Done" },
];

const JiraPanel = () => {
  const [view, setView] = useState("board"); // board | search
  const [status, setStatus] = useState(null);
  const [columns, setColumns] = useState({ new: [], indeterminate: [], done: [] });
  const [jql, setJql] = useState("");
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [activeIssue, setActiveIssue] = useState(null);
  const [commentText, setCommentText] = useState("");
  const [busy, setBusy] = useState(false);

  const loadStatus = useCallback(async () => {
    try {
      const s = await api.get("/api/jira/status");
      setStatus(s);
      setError("");
    } catch (e) { setError(String(e)); }
    setLoading(false);
  }, []);

  const loadBoard = useCallback(async () => {
    try {
      const data = await api.get("/api/jira/board");
      setColumns((data && data.columns) || { new: [], indeterminate: [], done: [] });
      setError("");
    } catch (e) { setError(String(e)); }
  }, []);

  const runSearch = useCallback(async () => {
    if (!jql.trim()) return;
    try {
      const data = await api.get(`/api/jira/search?jql=${encodeURIComponent(jql)}`);
      setResults((data && data.issues) || []);
      setError("");
    } catch (e) { setError(String(e)); }
  }, [jql]);

  const openIssue = useCallback(async (key) => {
    try {
      const issue = await api.get(`/api/jira/issue/${encodeURIComponent(key)}`);
      setActiveIssue(issue);
      setCommentText("");
    } catch (e) { setError(String(e)); }
  }, []);

  const doTransition = async (transitionId) => {
    if (!activeIssue) return;
    setBusy(true);
    try {
      await api.post(`/api/jira/issue/${encodeURIComponent(activeIssue.key)}/transition`,
        { transition: transitionId });
      await openIssue(activeIssue.key);
      loadBoard();
    } catch (e) { setError(String(e)); }
    setBusy(false);
  };

  const postComment = async () => {
    if (!activeIssue || !commentText.trim()) return;
    setBusy(true);
    try {
      await api.post(`/api/jira/issue/${encodeURIComponent(activeIssue.key)}/comment`,
        { body: commentText });
      await openIssue(activeIssue.key);
    } catch (e) { setError(String(e)); }
    setBusy(false);
  };

  useEffect(() => {
    loadStatus();
    if (view === "board") loadBoard();
  }, [view, loadStatus, loadBoard]);

  // ── Render helpers ────────────────────────────────────

  const mono = "'JetBrains Mono', monospace";

  const subTabBtn = (id, label) => React.createElement("button", {
    key: id,
    onClick: () => setView(id),
    style: {
      padding: "6px 14px",
      borderRadius: 6,
      border: `1px solid ${view === id ? T.accent : T.border}`,
      background: view === id ? `${T.accent}18` : "transparent",
      color: view === id ? T.accent : T.textMuted,
      cursor: "pointer",
      fontSize: 12,
      fontFamily: mono,
      fontWeight: 600,
    },
  }, label);

  const connection = status && status.connection;
  const connLine = !status ? "…" : (connection && connection.ok
    ? `connected — Jira ${connection.version} as ${connection.user}`
    : `not connected${connection && connection.error ? ` — ${connection.error}` : ""}`);

  const issueCard = (issue) => React.createElement("div", {
    key: issue.key,
    onClick: () => openIssue(issue.key),
    style: {
      border: `1px solid ${T.border}`,
      borderRadius: 8,
      padding: "8px 10px",
      marginBottom: 8,
      cursor: "pointer",
      background: "transparent",
    },
  },
    React.createElement("div", {
      style: { fontFamily: mono, fontSize: 11, color: T.accent, marginBottom: 4 },
    }, `${issue.key} · ${issue.status || ""}`),
    React.createElement("div", {
      style: { fontSize: 12, marginBottom: 4 },
    }, issue.summary || ""),
    React.createElement("div", {
      style: { fontSize: 11, color: T.textMuted },
    }, issue.assignee || "unassigned"),
  );

  const boardView = React.createElement("div", {
    style: { display: "flex", gap: 12, alignItems: "flex-start" },
  }, JIRA_COLUMNS.map((col) => React.createElement("div", {
    key: col.id,
    style: { flex: 1, minWidth: 0 },
  },
    React.createElement("div", {
      style: {
        fontFamily: mono, fontSize: 11, fontWeight: 700,
        color: T.textMuted, textTransform: "uppercase",
        margin: "0 0 8px 2px",
      },
    }, `${col.label} (${(columns[col.id] || []).length})`),
    (columns[col.id] || []).map(issueCard),
  )));

  const searchView = React.createElement("div", null,
    React.createElement("div", { style: { display: "flex", gap: 8, marginBottom: 12 } },
      React.createElement("input", {
        value: jql,
        onChange: (e) => setJql(e.target.value),
        onKeyDown: (e) => { if (e.key === "Enter") runSearch(); },
        placeholder: 'JQL — e.g. project = PROJ AND statusCategory != Done',
        style: {
          flex: 1, padding: "8px 10px", borderRadius: 6,
          border: `1px solid ${T.border}`, background: "transparent",
          color: "inherit", fontFamily: mono, fontSize: 12,
        },
      }),
      React.createElement("button", {
        onClick: runSearch,
        style: {
          padding: "8px 16px", borderRadius: 6, border: `1px solid ${T.accent}`,
          background: `${T.accent}18`, color: T.accent, cursor: "pointer",
          fontFamily: mono, fontSize: 12, fontWeight: 600,
        },
      }, "Search"),
    ),
    results.map(issueCard),
  );

  const drawer = activeIssue && React.createElement("div", {
    style: {
      position: "fixed", top: 0, right: 0, bottom: 0, width: 420,
      maxWidth: "90vw", overflowY: "auto", zIndex: 60,
      background: T.bg || "#111", borderLeft: `1px solid ${T.border}`,
      padding: 16, boxShadow: "-8px 0 24px rgba(0,0,0,0.4)",
    },
  },
    React.createElement("div", {
      style: { display: "flex", justifyContent: "space-between", marginBottom: 8 },
    },
      React.createElement("div", {
        style: { fontFamily: mono, fontWeight: 700, color: T.accent },
      }, activeIssue.key),
      React.createElement("button", {
        onClick: () => setActiveIssue(null),
        style: {
          border: "none", background: "transparent", color: T.textMuted,
          cursor: "pointer", fontSize: 16,
        },
      }, "✕"),
    ),
    React.createElement("div", { style: { fontSize: 14, marginBottom: 6 } },
      activeIssue.summary),
    React.createElement("div", { style: { fontSize: 11, color: T.textMuted, marginBottom: 10 } },
      `${activeIssue.issue_type} · ${activeIssue.status} · ${activeIssue.assignee || "unassigned"}`),
    activeIssue.description && React.createElement("pre", {
      style: {
        fontSize: 12, whiteSpace: "pre-wrap", fontFamily: "inherit",
        borderLeft: `2px solid ${T.border}`, paddingLeft: 8, marginBottom: 12,
      },
    }, activeIssue.description),
    (activeIssue.transitions || []).length > 0 && React.createElement("div", {
      style: { display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 12 },
    }, activeIssue.transitions.map((t) => React.createElement("button", {
      key: t.id,
      disabled: busy,
      onClick: () => doTransition(t.id),
      style: {
        padding: "4px 10px", borderRadius: 6, border: `1px solid ${T.border}`,
        background: "transparent", color: "inherit", cursor: "pointer",
        fontSize: 11, fontFamily: mono,
      },
    }, `→ ${t.name}`))),
    (activeIssue.comments || []).map((c, idx) => React.createElement("div", {
      key: c.id || idx,
      style: {
        borderTop: `1px solid ${T.border}`, padding: "8px 0", fontSize: 12,
      },
    },
      React.createElement("div", { style: { color: T.textMuted, fontSize: 11, marginBottom: 2 } },
        `${c.author} · ${c.created}`),
      React.createElement("div", { style: { whiteSpace: "pre-wrap" } }, c.body),
    )),
    React.createElement("div", { style: { display: "flex", gap: 6, marginTop: 10 } },
      React.createElement("input", {
        value: commentText,
        onChange: (e) => setCommentText(e.target.value),
        placeholder: "Add a comment…",
        style: {
          flex: 1, padding: "6px 8px", borderRadius: 6,
          border: `1px solid ${T.border}`, background: "transparent",
          color: "inherit", fontSize: 12,
        },
      }),
      React.createElement("button", {
        disabled: busy,
        onClick: postComment,
        style: {
          padding: "6px 12px", borderRadius: 6, border: `1px solid ${T.accent}`,
          background: `${T.accent}18`, color: T.accent, cursor: "pointer",
          fontSize: 12, fontFamily: mono,
        },
      }, "Post"),
    ),
  );

  if (loading) {
    return React.createElement("div", {
      style: { padding: 24, color: T.textMuted, fontFamily: mono, fontSize: 12 },
    }, "Loading Jira…");
  }

  const noAccount = !connection || (!connection.ok &&
    (connection.error || "").indexOf("no default account") !== -1);

  return React.createElement("div", { style: { padding: 16, height: "100%", overflowY: "auto" } },
    React.createElement("div", {
      style: { display: "flex", alignItems: "center", gap: 10, marginBottom: 12 },
    },
      subTabBtn("board", "My Work"),
      subTabBtn("search", "Search"),
      React.createElement("span", {
        style: { fontSize: 11, color: T.textMuted, fontFamily: mono, marginLeft: "auto" },
      }, connLine),
    ),
    error && React.createElement("div", {
      style: { color: "#e5534b", fontSize: 12, marginBottom: 10, fontFamily: mono },
    }, error),
    noAccount
      ? React.createElement("div", {
          style: { color: T.textMuted, fontSize: 13, padding: 12 },
        }, "No Jira account configured. Run `c3 jira login --url https://yoursite.atlassian.net` and reload.")
      : (view === "board" ? boardView : searchView),
    drawer,
  );
};
