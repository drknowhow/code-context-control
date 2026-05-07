// BitbucketPanel — Bitbucket Data Center / Server browser (v2.30.0)
// Globals: T, I, api, useState, useEffect, useCallback, Badge

const BitbucketPanel = () => {
  const [view, setView] = useState("overview"); // overview | prs | branches | activity | admin
  const [status, setStatus] = useState(null);
  const [prs, setPrs] = useState([]);
  const [prState, setPrState] = useState("OPEN");
  const [branches, setBranches] = useState([]);
  const [activity, setActivity] = useState([]);
  const [webhooks, setWebhooks] = useState([]);
  const [permissions, setPermissions] = useState({ users: [], groups: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [activePr, setActivePr] = useState(null);

  const loadStatus = useCallback(async () => {
    try {
      const s = await api.get("/api/bitbucket/status");
      setStatus(s);
      setError("");
    } catch (e) { setError(String(e)); }
    setLoading(false);
  }, []);

  const loadPrs = useCallback(async () => {
    try {
      const data = await api.get(`/api/bitbucket/prs?state=${encodeURIComponent(prState)}`);
      setPrs(Array.isArray(data && data.values) ? data.values : (Array.isArray(data) ? data : []));
    } catch (e) { setError(String(e)); }
  }, [prState]);

  const loadBranches = useCallback(async () => {
    try {
      const data = await api.get("/api/bitbucket/branches");
      setBranches(Array.isArray(data && data.values) ? data.values : (Array.isArray(data) ? data : []));
    } catch (e) { setError(String(e)); }
  }, []);

  const loadActivity = useCallback(async () => {
    try {
      const data = await api.get("/api/bitbucket/activity");
      setActivity(Array.isArray(data && data.values) ? data.values : (Array.isArray(data) ? data : []));
    } catch (e) { setError(String(e)); }
  }, []);

  const loadAdmin = useCallback(async () => {
    try {
      const wh = await api.get("/api/bitbucket/webhooks");
      setWebhooks(Array.isArray(wh && wh.values) ? wh.values : (Array.isArray(wh) ? wh : []));
      const perms = await api.get("/api/bitbucket/permissions");
      setPermissions({ users: perms.users || [], groups: perms.groups || [] });
    } catch (e) { setError(String(e)); }
  }, []);

  useEffect(() => {
    loadStatus();
    if (view === "prs") loadPrs();
    if (view === "branches") loadBranches();
    if (view === "activity") loadActivity();
    if (view === "admin") loadAdmin();
  }, [view, loadStatus, loadPrs, loadBranches, loadActivity, loadAdmin]);

  // ── Render helpers ────────────────────────────────────

  const subTabBtn = (id, label) => React.createElement("button", {
    onClick: () => setView(id),
    style: {
      padding: "6px 14px",
      borderRadius: 6,
      border: `1px solid ${view === id ? T.accent : T.border}`,
      background: view === id ? `${T.accent}18` : "transparent",
      color: view === id ? T.accent : T.textMuted,
      cursor: "pointer",
      fontSize: 12,
      fontFamily: "'JetBrains Mono', monospace",
      fontWeight: 600,
    },
  }, label);

  const renderOverview = () => {
    if (!status) return React.createElement("div", { style: { color: T.textMuted } }, "Loading…");
    const active = (status && status.config && status.config.active) || {};
    const conn = (status && status.connection) || {};
    return React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 12 } },
      React.createElement("div", {
        style: { padding: 14, borderRadius: 8, background: T.surface, border: `1px solid ${T.border}` }
      },
        React.createElement("div", { style: { fontSize: 11, color: T.textMuted, marginBottom: 6 } }, "ACTIVE ACCOUNT"),
        React.createElement("div", { style: { fontSize: 14, color: T.text } },
          (active.username || "—") + "@" + (active.base_url || "—")),
        React.createElement("div", { style: { marginTop: 10, display: "flex", gap: 16, fontSize: 12, color: T.textMuted } },
          React.createElement("div", null, "Project: ",
            React.createElement("span", { style: { color: T.text } },
              (status.config && status.config.default_project) || "—")),
          React.createElement("div", null, "Repo: ",
            React.createElement("span", { style: { color: T.text } },
              (status.config && status.config.default_repo) || "—")),
          React.createElement("div", null, "TLS: ",
            React.createElement("span", { style: { color: T.text } },
              (status.config && status.config.verify_tls) ? "verified" : "skipped")),
        )
      ),
      React.createElement("div", {
        style: { padding: 14, borderRadius: 8, background: T.surface, border: `1px solid ${T.border}` }
      },
        React.createElement("div", { style: { fontSize: 11, color: T.textMuted, marginBottom: 6 } }, "CONNECTION"),
        conn.ok
          ? React.createElement("div", { style: { fontSize: 13, color: "#22c55e" } },
              `OK — server version ${conn.version || "?"}`)
          : React.createElement("div", { style: { fontSize: 13, color: "#ef4444" } },
              "FAIL — " + (conn.error || "no connection")),
      ),
      status.config && Array.isArray(status.config.accounts) && React.createElement("div", {
        style: { padding: 14, borderRadius: 8, background: T.surface, border: `1px solid ${T.border}` }
      },
        React.createElement("div", { style: { fontSize: 11, color: T.textMuted, marginBottom: 6 } },
          `ALL ACCOUNTS (${status.config.accounts.length})`),
        status.config.accounts.map((a, i) =>
          React.createElement("div", {
            key: i,
            style: {
              fontSize: 12, color: T.text, padding: "4px 0",
              borderBottom: i === status.config.accounts.length - 1 ? "none" : `1px solid ${T.border}`,
            }
          }, (a.username === active.username && a.base_url === active.base_url ? "* " : "  ")
             + a.username + "@" + a.base_url)
        )
      ),
    );
  };

  const renderPrs = () => {
    return React.createElement("div", null,
      React.createElement("div", { style: { display: "flex", gap: 6, marginBottom: 12 } },
        ["OPEN", "MERGED", "DECLINED", "ALL"].map(s =>
          React.createElement("button", {
            key: s, onClick: () => setPrState(s),
            style: {
              padding: "4px 10px", borderRadius: 6,
              border: `1px solid ${prState === s ? T.accent : T.border}`,
              background: prState === s ? `${T.accent}18` : "transparent",
              color: prState === s ? T.accent : T.textMuted,
              fontSize: 11, cursor: "pointer", fontFamily: "'JetBrains Mono', monospace",
            },
          }, s),
        )
      ),
      prs.length === 0
        ? React.createElement("div", { style: { color: T.textMuted, fontSize: 13 } },
            `No ${prState.toLowerCase()} PRs.`)
        : React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 6 } },
            prs.map(pr =>
              React.createElement("div", {
                key: pr.id,
                onClick: () => setActivePr(pr),
                style: {
                  padding: 10, borderRadius: 6, background: T.surface,
                  border: `1px solid ${activePr && activePr.id === pr.id ? T.accent : T.border}`,
                  cursor: "pointer",
                }
              },
                React.createElement("div", { style: { display: "flex", justifyContent: "space-between" } },
                  React.createElement("div", { style: { fontSize: 13, color: T.text, fontWeight: 600 } },
                    `#${pr.id} ${pr.title || ""}`),
                  React.createElement(Badge, { color: T.accent }, pr.state),
                ),
                React.createElement("div", { style: { fontSize: 11, color: T.textMuted, marginTop: 4 } },
                  ((pr.fromRef || {}).displayId || "?") + " → " + ((pr.toRef || {}).displayId || "?")
                  + "  · by " + (((pr.author || {}).user || {}).displayName || "?")
                ),
              ),
            )
          ),
      activePr && React.createElement("pre", {
        style: {
          marginTop: 16, padding: 12, borderRadius: 6,
          background: T.bg, border: `1px solid ${T.border}`,
          fontFamily: "'JetBrains Mono', monospace", fontSize: 11,
          color: T.text, whiteSpace: "pre-wrap", maxHeight: 360, overflowY: "auto",
        }
      }, JSON.stringify(activePr, null, 2)),
    );
  };

  const renderBranches = () =>
    branches.length === 0
      ? React.createElement("div", { style: { color: T.textMuted, fontSize: 13 } }, "No branches.")
      : React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 4 } },
          branches.map(b =>
            React.createElement("div", {
              key: b.id,
              style: {
                padding: "8px 12px", background: T.surface, borderRadius: 6,
                border: `1px solid ${T.border}`,
                display: "flex", justifyContent: "space-between", alignItems: "center",
              }
            },
              React.createElement("div", null,
                React.createElement("span", { style: { fontSize: 13, color: T.text, fontWeight: 600 } },
                  b.displayId || b.id || "?"),
                b.isDefault && React.createElement(Badge, { color: T.accent, style: { marginLeft: 8 } },
                  "default"),
              ),
              React.createElement("div", { style: { fontSize: 11, color: T.textMuted, fontFamily: "'JetBrains Mono', monospace" } },
                (b.latestCommit || "").slice(0, 8))
            )
          )
        );

  const renderActivity = () =>
    activity.length === 0
      ? React.createElement("div", { style: { color: T.textMuted, fontSize: 13 } }, "No recent activity.")
      : React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 4 } },
          activity.map(c =>
            React.createElement("div", {
              key: c.id,
              style: { padding: "6px 10px", background: T.surface, borderRadius: 6, border: `1px solid ${T.border}` }
            },
              React.createElement("div", { style: { display: "flex", gap: 10 } },
                React.createElement("span", { style: { fontSize: 11, color: T.accent, fontFamily: "'JetBrains Mono', monospace" } },
                  (c.id || "").slice(0, 8)),
                React.createElement("span", { style: { fontSize: 11, color: T.textMuted } },
                  ((c.author || {}).displayName || "?")),
              ),
              React.createElement("div", { style: { fontSize: 12, color: T.text, marginTop: 2 } },
                ((c.message || "").split("\n")[0] || ""))
            )
          )
        );

  const renderAdmin = () =>
    React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 16 } },
      React.createElement("div", null,
        React.createElement("div", { style: { fontSize: 11, color: T.textMuted, marginBottom: 6 } },
          `WEBHOOKS (${webhooks.length})`),
        webhooks.length === 0
          ? React.createElement("div", { style: { color: T.textMuted, fontSize: 12 } }, "(none)")
          : webhooks.map(h =>
              React.createElement("div", {
                key: h.id,
                style: { padding: "6px 10px", background: T.surface, borderRadius: 6, border: `1px solid ${T.border}`, marginBottom: 4 }
              },
                React.createElement("div", { style: { fontSize: 12, color: T.text } },
                  `#${h.id} ${h.active ? "[on]" : "[off]"} ${h.name || "?"} → ${h.url || "?"}`),
                Array.isArray(h.events) && h.events.length > 0 && React.createElement("div", {
                  style: { fontSize: 10, color: T.textMuted, marginTop: 2 }
                }, "events: " + h.events.join(", ")),
              )
            )
      ),
      React.createElement("div", null,
        React.createElement("div", { style: { fontSize: 11, color: T.textMuted, marginBottom: 6 } },
          `USER PERMISSIONS (${permissions.users.length})`),
        permissions.users.length === 0
          ? React.createElement("div", { style: { color: T.textMuted, fontSize: 12 } }, "(none)")
          : permissions.users.map((u, i) =>
              React.createElement("div", { key: i, style: { fontSize: 12, color: T.text, padding: "2px 0" } },
                `${(u.user && u.user.name) || "?"} — ${u.permission || "?"}`)
            )
      ),
      React.createElement("div", null,
        React.createElement("div", { style: { fontSize: 11, color: T.textMuted, marginBottom: 6 } },
          `GROUP PERMISSIONS (${permissions.groups.length})`),
        permissions.groups.length === 0
          ? React.createElement("div", { style: { color: T.textMuted, fontSize: 12 } }, "(none)")
          : permissions.groups.map((g, i) =>
              React.createElement("div", { key: i, style: { fontSize: 12, color: T.text, padding: "2px 0" } },
                `${(g.group && g.group.name) || "?"} — ${g.permission || "?"}`)
            )
      ),
    );

  return React.createElement("div", { style: { padding: 0 } },
    React.createElement("div", { style: { display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" } },
      subTabBtn("overview", "Overview"),
      subTabBtn("prs", "Pull Requests"),
      subTabBtn("branches", "Branches"),
      subTabBtn("activity", "Activity"),
      subTabBtn("admin", "Admin"),
    ),
    error && React.createElement("div", {
      style: {
        padding: "8px 12px", background: "#ef444418", border: "1px solid #ef4444",
        borderRadius: 6, color: "#ef4444", fontSize: 12, marginBottom: 12, fontFamily: "'JetBrains Mono', monospace",
      }
    }, error),
    loading
      ? React.createElement("div", { style: { color: T.textMuted } }, "Loading…")
      : view === "overview" ? renderOverview()
      : view === "prs" ? renderPrs()
      : view === "branches" ? renderBranches()
      : view === "activity" ? renderActivity()
      : view === "admin" ? renderAdmin()
      : null,
  );
};
