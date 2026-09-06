// oracle/ui/app.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Init ──
// ═══════════════════════════════════════════════════════════
(async function init() {
  await Promise.all([refreshHeader(), loadProjects(), loadInsights(), loadSuggestions(), loadSettings(), loadDiscoveryKey(), loadPairedClients(), chatLoadConversations(), chatLoadCommands()]);
  // Restore persisted UI preferences (loadSettings cached the config).
  restoreLastTab(window.oracleConfig || {});
  // Auto-refresh every 60s
  setInterval(() => { refreshHeader(); loadProjects(); }, 60000);
})();
