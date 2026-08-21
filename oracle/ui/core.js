// oracle/ui/core.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
const API = '';
const ORACLE_BUILD_TIME = "2026-07-02 oracle-v3 (ui concat bundle)";

// ═══════════════════════════════════════════════════════════
// ─�� Toast notification system ──
// ═══════════════════════════════════════════════════════════
const TOAST_ICONS = { success: '\u2713', error: '\u2717', info: '\u24D8', warning: '\u26A0' };

function toast(title, msg, type = 'info', duration = 4000) {
  const container = document.getElementById('toastContainer');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.innerHTML = `
    <span class="toast-icon">${TOAST_ICONS[type] || TOAST_ICONS.info}</span>
    <div class="toast-body">
      <div class="toast-title">${esc(title)}</div>
      ${msg ? `<div class="toast-msg">${esc(msg)}</div>` : ''}
    </div>
    <button class="toast-close" onclick="this.closest('.toast').remove()">\u2715</button>
  `;
  container.appendChild(el);
  if (duration > 0) {
    setTimeout(() => {
      el.classList.add('leaving');
      setTimeout(() => el.remove(), 250);
    }, duration);
  }
}

// ═══════════════════════════════════════════════════════════
// ── Session state ──
// ═══════════════════════════════════════════════════════════
// A dashboard opened by pasting the URL never redeemed a bootstrap code, so
// it holds no session cookie. Reads are ungated and keep working, which is
// why the header stays green while chat, Save and Test Ollama all answer 401.
// Name that state once, here, instead of letting each caller guess at it —
// "unauthorized" on a chat send used to read as an Ollama outage.
const SIGNED_OUT_MSG = 'Signed out — this dashboard is read-only. Reopen it with the '
  + 'hub’s Open Oracle button, or run  c3 oracle open  and follow the link it prints.';
let _oracleSignedOut = false;

function oracleSignedOut() {
  if (_oracleSignedOut) return;   // one banner per page load, not one per call
  _oracleSignedOut = true;
  toast('Signed out', SIGNED_OUT_MSG, 'error', 0);
}

// ═══════════════════════════════════════════════════════════
// ── API helpers ──
// ═══════════════════════════════════════════════════════════
async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    if (res.status === 401) { oracleSignedOut(); throw new Error(SIGNED_OUT_MSG); }
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  return res.json();
}

// ═══════════════════════════════════════════════════════════
// ── Utilities ──
// ═══════════════════════════════════════════════════════════
function esc(s) { if (!s) return ''; const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function timeAgo(ts) {
  const diff = Date.now() - ts;
  if (diff < 60000) return 'just now';
  if (diff < 3600000) return Math.floor(diff/60000) + 'm ago';
  if (diff < 86400000) return Math.floor(diff/3600000) + 'h ago';
  return Math.floor(diff/86400000) + 'd ago';
}

// ═══════════════════════════════════════════════════════════
