// oracle/ui/busy.js — extracted verbatim from oracle.html (UI v3 concat bundle).
// One shared script scope: function declarations hoist across bundle files; app.js runs last.
// ── Activity / busy indicator ──
// ═══════════════════════════════════════════════════════════
let _busyCount = 0;
let _busyLabel = '';

function busyStart(label) {
  _busyCount++;
  _busyLabel = label || _busyLabel;
  document.getElementById('activitySpinner').classList.add('active');
  document.getElementById('progressTrack').classList.add('active');
  const lbl = document.getElementById('activityLabel');
  lbl.textContent = _busyLabel;
  lbl.classList.add('active');
}

function busyEnd() {
  _busyCount = Math.max(0, _busyCount - 1);
  if (_busyCount === 0) {
    document.getElementById('activitySpinner').classList.remove('active');
    document.getElementById('progressTrack').classList.remove('active');
    document.getElementById('activityLabel').classList.remove('active');
    _busyLabel = '';
  }
}

/** Wrap an async action with busy indicator + toast notifications. */
async function tracked(label, fn, { successMsg, errorMsg, silent } = {}) {
  busyStart(label);
  try {
    const result = await fn();
    if (!silent) toast(label, successMsg || 'Done', 'success', 3000);
    return result;
  } catch (e) {
    toast(label, errorMsg || e.message || 'Request failed', 'error', 6000);
    throw e;
  } finally {
    busyEnd();
  }
}

// ═══════════════════════════════════════════════════════════
// ── Global busy indicator ──
// ═══════════════════════════════════════════════════════════
const _busyStack = [];
let _busyTimer = null;
function oracleBusy(label, opts = {}) {
  const id = Math.random().toString(36).slice(2, 9);
  _busyStack.push({ id, label, started: Date.now(), panel: opts.panel || null });
  _renderBusy();
  if (opts.panel) {
    const el = document.getElementById('panel-' + opts.panel);
    if (el) { el.classList.add('panel-busy'); el.setAttribute('data-busy-label', label); }
  }
  return id;
}
function oracleIdle(id) {
  const idx = _busyStack.findIndex(b => b.id === id);
  if (idx >= 0) {
    const [removed] = _busyStack.splice(idx, 1);
    if (removed.panel) {
      const el = document.getElementById('panel-' + removed.panel);
      if (el && !_busyStack.some(b => b.panel === removed.panel)) {
        el.classList.remove('panel-busy');
        el.removeAttribute('data-busy-label');
      }
    }
  }
  _renderBusy();
}
function _renderBusy() {
  const pill = document.getElementById('oracleBusy');
  const lbl = document.getElementById('oracleBusyLabel');
  const ela = document.getElementById('oracleBusyElapsed');
  const track = document.getElementById('progressTrack');
  if (_busyStack.length === 0) {
    pill.classList.remove('active');
    if (track) track.classList.remove('active');
    if (_busyTimer) { clearInterval(_busyTimer); _busyTimer = null; }
    return;
  }
  const top = _busyStack[_busyStack.length - 1];
  const suffix = _busyStack.length > 1 ? ` (+${_busyStack.length - 1})` : '';
  lbl.textContent = top.label + suffix;
  pill.classList.add('active');
  if (track) track.classList.add('active');
  const tick = () => {
    if (!_busyStack.length) return;
    const s = Math.floor((Date.now() - _busyStack[_busyStack.length - 1].started) / 1000);
    ela.textContent = s >= 1 ? `${s}s` : '';
  };
  tick();
  if (!_busyTimer) _busyTimer = setInterval(tick, 500);
}
async function withBusy(label, fn, opts) {
  const id = oracleBusy(label, opts);
  try { return await fn(); }
  finally { oracleIdle(id); }
}

// ═══════════════════════════════════════════════════════════
