// ─── API Client ───────────────────────────
const API = window.location.origin;
const parseApiResponse = async (r) => {
  const ct = (r.headers.get('content-type') || '').toLowerCase();
  let json = null;
  let text = '';
  if (ct.includes('application/json')) {
    try { json = await r.json(); } catch { json = null; }
  } else {
    try { text = await r.text(); } catch { text = ''; }
    if (text) {
      try { json = JSON.parse(text); } catch { json = null; }
    }
  }
  if (!r.ok) {
    const msg = (json && (json.error || json.message)) || text || `HTTP ${r.status}`;
    const err = new Error(msg);
    err.status = r.status;
    err.payload = json;
    throw err;
  }
  if (json !== null) return json;
  return {};
};
// Digs the real failure reason out of hub error bodies: sub-project
// endpoints fail as 500 {success, result:{error}, output} with no
// top-level `error`, so err.message alone is just "HTTP 500".
const apiErr = (e) => {
  const p = (e && e.payload) || {};
  const r = p.result || {};
  return r.error || p.error || (e && e.message) || 'unknown error';
};
const api = {
  get: async (path) => parseApiResponse(await fetch(`${API}${path}`)),
  post: async (path, body) => parseApiResponse(await fetch(`${API}${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })),
  put: async (path, body) => parseApiResponse(await fetch(`${API}${path}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })),
  del: async (path, body) => parseApiResponse(await fetch(`${API}${path}`, {
    method: 'DELETE',
    ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
  })),
};
