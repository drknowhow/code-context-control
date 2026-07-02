// oracle/ui/theme_tabs.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Theme toggle ──
// ═══════════════════════════════════════════════════════════
let _currentTheme = 'dark';

function applyTheme(theme) {
  _currentTheme = theme;
  document.body.dataset.theme = theme;
  document.getElementById('themeToggle').textContent = 'Theme: ' + (theme === 'dark' ? 'Dark' : 'Light');
  // Toggle highlight.js theme
  const dEl = document.getElementById('hljs-dark');
  const lEl = document.getElementById('hljs-light');
  if (dEl) dEl.disabled = theme !== 'dark';
  if (lEl) lEl.disabled = theme !== 'light';
}

function toggleTheme() {
  const next = _currentTheme === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  // Persist to config
  api('/api/config', { method: 'POST', body: { theme: next } }).catch(() => {});
}

// ═══════════════════════════════════════════════════════════
// ── Tab switching ──
// ═══════════════════════════════════════════════════════════
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    const panel = document.getElementById('panel-' + tab.dataset.tab);
    if (panel) panel.classList.add('active');
    // Show/hide main depending on whether chat is active
    const mainEl = document.querySelector('main');
    mainEl.style.display = tab.dataset.tab === 'chat' ? 'none' : 'block';
    // Full-width for the cross-graph tab; constrained for others
    if (tab.dataset.tab === 'crossgraph') {
      mainEl.classList.add('wide');
      if (!window._cgLoaded) { cgLoad(); window._cgLoaded = true; }
      else { setTimeout(() => { if (_cgCy) { _cgCy.resize(); _cgCy.fit(undefined, 40); } }, 50); }
    } else {
      mainEl.classList.remove('wide');
    }
    if (tab.dataset.tab === 'activity' && !window._actLoaded) {
      loadActivity(); window._actLoaded = true;
    }
  });
});

// ═══════════════════════════════════════════════════════════
