// ─── Hub credentials: global vault + cross-project manager ─────
// CredsManager is the shared manager (also used by the drill Credentials tab);
// HubCredentials is the top-level mainView='creds' page and owns the
// cross-project search. CredDrawer is the per-credential settings surface,
// CredMenu the right-click / kebab context menu, CredConfirm the typed
// confirmation standing in for window.confirm on anything destructive or
// exposure-raising.
// Write-only wire: values are submitted inbound-only and never returned by
// any hub route — rows show length + fingerprint, never the secret itself.
// Nothing below may render, copy or cache a value; there is no reveal path.

const HUB_CREDS_EMPTY_FORM = {
  name: '', value: '', scope: 'project', type: 'token',
  description: '', env_var: '', agent_readable: false, inject: false,
  fields: {},
};

// Structured kinds (v2.87.0; `login` v2.90.0): per-type field sets composed
// into a JSON object client-side. `hidden` fields render as password inputs.
// These entries are inject-only — the server refuses agent_readable/inject and
// there is no reveal path anywhere.
// This table MUST mirror services/credential_store._SCHEMAS — a kind the store
// accepts and this table omits is unreachable from the browser and, worse,
// renders as a plain secret with exposure toggles the server will refuse.
// tests/test_credential_ui_parity.py asserts the two stay in step.
const CREDS_STRUCTURED = {
  card:     { required: ['cardholder', 'number', 'expiry'],
              optional: ['cvc', 'billing_zip'], hidden: ['number', 'cvc'] },
  address:  { required: ['street1', 'city', 'state', 'zip'],
              optional: ['recipient', 'street2', 'country', 'phone'], hidden: [] },
  identity: { required: ['full_name'],
              optional: ['dob', 'ssn', 'phone', 'email'], hidden: ['ssn', 'dob'] },
  login:    { required: ['site_id', 'canonical_origin', 'username', 'password'],
              optional: ['totp_secret'], hidden: ['password', 'totp_secret'] },
};

const credsDisplayText = (entry) => {
  const d = entry.display || {};
  if (entry.type === 'card') return `${d.brand || 'card'} ••••${d.last4 || '????'}`;
  // login's projection carries has_totp as a BOOLEAN — the generic join below
  // would render it as the word "true". Username is absent by design.
  if (entry.type === 'login') {
    const bits = [d.site_id, d.origin].filter(Boolean);
    if (d.has_totp) bits.push('2FA');
    return bits.join(' · ');
  }
  const vals = Object.values(d).filter(Boolean);
  return vals.length ? vals.join(', ') : '';
};

// Collect the non-blank fields typed into a structured form/drawer grid.
const credsTypedFields = (fields) => {
  const typed = {};
  Object.entries(fields || {}).forEach(([k, v]) => {
    if (String(v || '').trim()) typed[k] = String(v).trim();
  });
  return typed;
};

// path falsy everywhere below → the shared global vault (~/.c3).
const credApi = {
  listProject: (path) =>
    api.get('/api/projects/credentials?path=' + encodeURIComponent(path)),
  overview: () => api.get('/api/hub/credentials/overview'),
  save: (path, payload) => api.post('/api/projects/credentials',
    path ? Object.assign({ path }, payload) : payload),
  check: (path, name) => api.post(
    `/api/projects/credentials/${encodeURIComponent(name)}/check`, path ? { path } : {}),
  remove: (path, name, scope) => api.del(
    `/api/projects/credentials/${encodeURIComponent(name)}?scope=${scope}`
    + (path ? '&path=' + encodeURIComponent(path) : '')),
  // Targets carry their own (scope, path, name); the route is reduce-only.
  batch: (action, targets) =>
    api.post('/api/hub/credentials/batch', { action, targets }),
};

const credWhen = (iso) => (iso
  ? String(iso).replace('T', ' ').replace(/\.\d+/, '').replace(/\+.*$/, '').replace(/Z$/, '')
  : '—');
// Exposure weight — drives the "exposure" sort so the risky rows float up.
const credRisk = (e) => (e.agent_readable ? 2 : 0) + (e.inject ? 1 : 0);

// Identity of a row. A name alone is ambiguous: the same name legitimately
// lives in the global vault AND in any number of projects, so anything that
// remembers a row — a selection set, a check result, a ledger entry — has to
// key on the triple or it will eventually act on the wrong credential.
const credKey = (entry, path) => `${entry.scope}|${entry.scope === 'global' ? '' : (path || '')}|${entry.name}`;

// `source` is {path, at} for an imported entry and '' for a hand-made one.
const credSourcePath = (e) => {
  const s = e && e.source;
  return (s && typeof s === 'object' && s.path) ? String(s.path) : '';
};
const credSourceAt = (e) => {
  const s = e && e.source;
  return (s && typeof s === 'object' && s.at) ? String(s.at) : '';
};
const credBaseName = (p) => String(p || '').split(/[\\/]/).filter(Boolean).pop() || String(p || '');

const CRED_STALE_DAYS = 30;
const credDaysSince = (iso) => {
  if (!iso) return Infinity;
  const t = Date.parse(String(iso).replace(' ', 'T'));
  return isNaN(t) ? Infinity : (Date.now() - t) / 86400000;
};

// Always-on narrowing chips. Each active chip must pass, so ticking two
// narrows rather than widens — the same way the qualifier box already reads.
const CRED_CHIPS = [
  { id: 'agent', label: 'agent-readable', test: e => !!e.agent_readable },
  { id: 'inject', label: 'auto-inject', test: e => !!e.inject },
  { id: 'structured', label: 'structured', test: e => !!CREDS_STRUCTURED[e.type] },
  { id: 'shadow', label: 'shadowing', test: e => !!e.shadows_global || (e.shadowed_in || []).length > 0 },
  { id: 'sourced', label: 'from .env', test: e => !!credSourcePath(e) },
  { id: 'unused', label: 'never used', test: e => !(e.use_count > 0) },
  { id: 'stale', label: `stale >${CRED_STALE_DAYS}d`,
    test: e => (e.use_count > 0) && credDaysSince(e.last_used) > CRED_STALE_DAYS },
];

const credChipPass = (entry, chips) => {
  for (let i = 0; i < CRED_CHIPS.length; i++) {
    if (chips[CRED_CHIPS[i].id] && !CRED_CHIPS[i].test(entry)) return false;
  }
  return true;
};

const credCopy = (text, label) => {
  if (!text) return;
  const done = () => notify(`Copied ${label}`);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => notify('Copy failed', 'err'));
  } else { notify('Clipboard unavailable', 'warn'); }
};

function credSortEntries(list, mode) {
  const byName = (a, b) => String(a.name).localeCompare(String(b.name));
  const out = list.slice();
  if (mode === 'recent') {
    out.sort((a, b) => String(b.last_used || '').localeCompare(String(a.last_used || '')) || byName(a, b));
  } else if (mode === 'used') {
    out.sort((a, b) => (b.use_count || 0) - (a.use_count || 0) || byName(a, b));
  } else if (mode === 'exposure') {
    out.sort((a, b) => credRisk(b) - credRisk(a) || byName(a, b));
  } else if (mode === 'created') {
    out.sort((a, b) => String(b.created || '').localeCompare(String(a.created || '')) || byName(a, b));
  } else { out.sort(byName); }
  return out;
}

// ── Search over the metadata the allowlist serializer already returns ──
// Free tokens are AND-matched against name/description/env_var/owner;
// `key:value` qualifiers narrow further. No value is ever indexed because
// no route ever returns one.
const CRED_QUALIFIERS = ['project', 'scope', 'type', 'storage', 'name', 'env',
  'inject', 'agent', 'shadow', 'source'];

function parseCredQuery(raw) {
  const terms = [];
  const quals = [];
  String(raw || '').trim().split(/\s+/).filter(Boolean).forEach((tok) => {
    const m = tok.match(/^([A-Za-z]+):(.*)$/);
    if (m && CRED_QUALIFIERS.indexOf(m[1].toLowerCase()) !== -1) {
      quals.push({ key: m[1].toLowerCase(), val: m[2].toLowerCase() });
    } else { terms.push(tok.toLowerCase()); }
  });
  return { terms, quals, empty: !terms.length && !quals.length };
}

const credFlagWanted = (v) => v === '' || v === 'true' || v === 'yes' || v === '1';

// rec = { entry, projectName, projectPath }; projectPath '' → global vault.
function credRecordMatches(rec, q) {
  const e = rec.entry;
  const hay = [e.name, e.description, e.env_var, e.type, e.storage, e.scope,
    credSourcePath(e), rec.projectName, rec.projectPath,
    rec.projectPath ? '' : 'global vault']
    .filter(Boolean).join(' ').toLowerCase();
  for (let i = 0; i < q.terms.length; i++) {
    if (hay.indexOf(q.terms[i]) === -1) return false;
  }
  for (let i = 0; i < q.quals.length; i++) {
    const key = q.quals[i].key;
    const val = q.quals[i].val;
    const owner = `${rec.projectName || ''} ${rec.projectPath || ''}`.toLowerCase();
    if (key === 'project') {
      const hit = rec.projectPath ? owner.indexOf(val) !== -1 : 'global vault'.indexOf(val) !== -1;
      if (!hit) return false;
    } else if (key === 'scope') {
      if (String(e.scope || '').toLowerCase().indexOf(val) !== 0) return false;
    } else if (key === 'type') {
      if (String(e.type || 'token').toLowerCase().indexOf(val) !== 0) return false;
    } else if (key === 'storage') {
      if (String(e.storage || '').toLowerCase().indexOf(val) !== 0) return false;
    } else if (key === 'name') {
      if (String(e.name || '').toLowerCase().indexOf(val) === -1) return false;
    } else if (key === 'env') {
      if (String(e.env_var || '').toLowerCase().indexOf(val) === -1) return false;
    } else if (key === 'inject') {
      if (!!e.inject !== credFlagWanted(val)) return false;
    } else if (key === 'agent') {
      if (!!e.agent_readable !== credFlagWanted(val)) return false;
    } else if (key === 'shadow') {
      const involved = !!e.shadows_global || ((e.shadowed_in || []).length > 0);
      if (involved !== credFlagWanted(val)) return false;
    } else if (key === 'source') {
      // `source:none` asks for the hand-made entries; anything else is a
      // substring of the .env path an import recorded.
      const src = credSourcePath(e).toLowerCase();
      if (val === 'none') { if (src) return false; }
      else if (!src || (val && src.indexOf(val) === -1)) return false;
    }
  }
  return true;
}

// ── Confirmation specs, shared by the rows, the menu and the drawer ──
// Raising exposure and deleting are the only two irreversible-ish moves;
// both get an explicit, readable modal instead of window.confirm.
function credExposureConfirm(entry, field, envVar, owner, onConfirm) {
  if (field === 'agent_readable') {
    return {
      title: 'Allow the agent to read this value?', tone: 'error',
      confirmLabel: 'Allow agent access', requireText: entry.name,
      intro: <span>Enabling <b>agent_readable</b> lets the agent pull the plaintext of{' '}
        <span className="mono">{entry.name}</span> into its context.</span>,
      bullets: [
        'The value then lands in conversation transcripts, which are stored and searchable.',
        'Injection-only use (c3_shell env_creds, {{cred:NAME}}) does NOT need this flag.',
        `Scope: ${owner}.`,
      ],
      onConfirm,
    };
  }
  return {
    title: 'Auto-inject on every shell run?', tone: 'warn',
    confirmLabel: 'Enable auto-inject',
    intro: <span><span className="mono">{entry.name}</span> will be exported as{' '}
      <span className="mono">${envVar || entry.name}</span> into every{' '}
      <span className="mono">c3_shell</span> subprocess in {owner}.</span>,
    bullets: ['Any command that runs there — including third-party tooling — can read it from the environment.'],
    onConfirm,
  };
}

function credDeleteConfirm(entry, owner, onConfirm) {
  const shadowed = (entry.shadowed_in || []).length;
  const bullets = [`Stored in ${owner} (${entry.storage || 'keyring'}).`,
    'Anything resolving this name will start failing.'];
  if (entry.scope === 'global' && shadowed) {
    bullets.push(`${shadowed} project${shadowed === 1 ? '' : 's'} override this name locally and are unaffected.`);
  }
  if (entry.shadows_global) {
    bullets.push('The same-named global entry will take over for this project.');
  }
  return {
    title: `Delete '${entry.name}'?`, tone: 'error', confirmLabel: 'Delete credential',
    requireText: entry.name,
    intro: <span>The stored value is destroyed. This cannot be undone — C3 keeps no copy.</span>,
    bullets, onConfirm,
  };
}

// The bulk twin. A single delete asks for the name; a bulk one cannot, so it
// asks for the count instead — the number is the thing worth re-reading.
function credBulkDeleteConfirm(entries, owner, onConfirm) {
  const n = entries.length;
  const reExpose = entries.filter(e => e.shadows_global);
  const names = entries.map(e => e.name);
  const bullets = [
    `Stored in ${owner}.`,
    'Anything resolving these names will start failing.',
    names.slice(0, 8).join(', ') + (n > 8 ? `, +${n - 8} more` : ''),
  ];
  if (reExpose.length) {
    // The quiet one: removing a project entry does not remove the name, it
    // hands it back to the global vault. Say so before, not after.
    bullets.push(`${reExpose.length} of these override a global entry — deleting them `
      + `re-exposes the global value to this project: ${reExpose.map(e => e.name).join(', ')}.`);
  }
  return {
    title: `Delete ${n} credential${n === 1 ? '' : 's'}?`, tone: 'error',
    confirmLabel: `Delete ${n}`, requireText: `DELETE ${n}`,
    intro: <span>The stored values are destroyed. This cannot be undone — C3 keeps no copy.</span>,
    bullets, onConfirm,
  };
}

// Bulk exposure changes only ever REDUCE. Raising stays one entry at a time
// behind credExposureConfirm: a bulk grant widens access to many secrets from
// a single checkbox, and the row the user did not mean to include is the one
// that matters. The server enforces the same allowlist.
const CRED_BULK_ACTIONS = [
  { action: 'check', label: 'Check resolution', icon: 'refresh' },
  { action: 'revoke_agent_read', label: 'Revoke agent read', icon: 'eye' },
  { action: 'disable_inject', label: 'Disable auto-inject', icon: 'zap' },
];

// ── Right-click / kebab context menu ───────────────────────────
// items: [{label, icon, onClick, danger, disabled, hint} | {separator:true}]
function CredMenu({ x, y, items, onClose }) {
  const boxRef = useRef(null);
  const [pos, setPos] = useState({ left: -9999, top: -9999 });
  const [active, setActive] = useState(-1);
  const rows = items.filter(it => !it.separator);

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setPos({
      left: Math.max(6, Math.min(x, window.innerWidth - r.width - 6)),
      top: Math.max(6, Math.min(y, window.innerHeight - r.height - 6)),
    });
    el.focus();
  }, [x, y]);

  useEffect(() => {
    const bail = () => onClose();
    window.addEventListener('resize', bail);
    window.addEventListener('scroll', bail, true);
    return () => {
      window.removeEventListener('resize', bail);
      window.removeEventListener('scroll', bail, true);
    };
  }, [onClose]);

  const run = (it) => { if (it.disabled) return; onClose(); it.onClick(); };

  const onKeyDown = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); onClose(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!rows.length) return;
      const dir = e.key === 'ArrowDown' ? 1 : -1;
      let n = active;
      for (let i = 0; i < rows.length; i++) {
        n = (n + dir + rows.length) % rows.length;
        if (!rows[n].disabled) break;
      }
      setActive(n);
      return;
    }
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      if (rows[active]) run(rows[active]);
    }
  };

  let idx = -1;
  return (
    <div onClick={onClose} onContextMenu={(e) => { e.preventDefault(); onClose(); }}
      style={{ position: 'fixed', inset: 0, zIndex: 290 }}>
      <div ref={boxRef} tabIndex={-1} role="menu" onKeyDown={onKeyDown}
        onClick={(e) => e.stopPropagation()}
        style={{
          position: 'fixed', left: pos.left, top: pos.top, minWidth: 224,
          background: T.surface, border: `1px solid ${T.borderHover}`,
          borderRadius: 8, padding: '5px 0', outline: 'none', fontSize: 12,
          boxShadow: '0 12px 34px #00000075',
        }}>
        {items.map((it, i) => {
          if (it.separator) {
            return <div key={`sep${i}`} style={{ height: 1, background: T.border, margin: '5px 0' }} />;
          }
          idx += 1;
          const mine = idx;
          const color = it.disabled ? T.textDim : (it.danger ? T.error : T.text);
          return (
            <div key={it.label} role="menuitem" onMouseEnter={() => setActive(mine)}
              onClick={() => run(it)}
              style={{
                display: 'flex', alignItems: 'center', gap: 9, padding: '7px 13px',
                cursor: it.disabled ? 'default' : 'pointer', color,
                background: (active === mine && !it.disabled)
                  ? (it.danger ? `${T.error}18` : T.surfaceAlt) : 'transparent',
              }}>
              <I name={it.icon || 'chevron'} size={13} color={color} />
              <span style={{ flex: 1, whiteSpace: 'nowrap' }}>{it.label}</span>
              {it.hint && (
                <span className="mono" style={{ fontSize: 10, color: T.textDim }}>{it.hint}</span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Typed confirmation (stands in for window.confirm) ──────────
function CredConfirm({ spec, onClose }) {
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  if (!spec) return null;
  const need = spec.requireText;
  const ok = !need || typed.trim() === need;
  const tone = spec.tone === 'warn' ? T.warn : T.error;
  const go = async () => {
    if (!ok || busy) return;
    setBusy(true);
    try { await spec.onConfirm(); } finally { setBusy(false); onClose(); }
  };
  return (
    <Modal title={spec.title} width={470} onClose={onClose}>
      <div style={{ fontSize: 12.5, color: T.text, lineHeight: 1.65 }}>{spec.intro}</div>
      {(spec.bullets || []).length > 0 && (
        <ul style={{ margin: '10px 0 0', paddingLeft: 18, fontSize: 12, color: T.textMuted, lineHeight: 1.75 }}>
          {spec.bullets.map((b, i) => <li key={i}>{b}</li>)}
        </ul>
      )}
      {need && (
        <div>
          <MdlLabel>Type <span className="mono" style={{ color: tone }}>{need}</span> to confirm</MdlLabel>
          <input value={typed} autoFocus onChange={e => setTyped(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') go(); }} className="mono"
            style={mdlInputStyle()} autoComplete="off" spellCheck={false} />
        </div>
      )}
      <MdlFooter>
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
        <Btn color={tone} disabled={!ok || busy} onClick={go}>
          {busy ? 'Working…' : (spec.confirmLabel || 'Confirm')}
        </Btn>
      </MdlFooter>
    </Modal>
  );
}

// ── Per-credential settings drawer ─────────────────────────────
// The only surface that replaces a value or moves an exposure switch.
// The secret field starts empty, is never prefilled, and is cleared the
// moment the request settles.
const CredSec = ({ title, note, children, tone }) => (
  <div style={{ padding: '14px 18px', borderTop: `1px solid ${T.border}` }}>
    <div style={{
      fontSize: 10.5, fontWeight: 700, letterSpacing: 1, textTransform: 'uppercase',
      color: tone || T.textMuted, marginBottom: note ? 3 : 10,
    }}>{title}</div>
    {note && <div style={{ fontSize: 11, color: T.textDim, marginBottom: 10, lineHeight: 1.5 }}>{note}</div>}
    {children}
  </div>
);

// ── .env import: choose → preview → commit ────────────────────────────────
// The panel never renders a value or any prefix of one. Rows carry a length
// and a sha256[:8] fingerprint, which is enough to tell two keys apart and to
// spot a truncated paste, and keeps the vault's rule that a stored value never
// travels back to the browser.
const HUB_CREDS_IMPORT_STATUS_TONE = (reason) => (
  !reason ? "ok" : (reason === "no-assignment" || reason === "duplicate") ? "note" : "skip"
);

// What a preview row means, in words. `current` is the re-sync answer that
// matters most: the vault already holds this exact value, so there is nothing
// to write — established by comparing digests server-side, never values.
const HUB_CREDS_ACTION_TEXT = {
  create: "new — will be added",
  replace: "changed — will be replaced",
  current: "unchanged",
  skip: "skipped",
};

const HubCredsImportPanel = ({ T, post, scopes, defaultScope, onDone, inputStyle,
                               labelStyle, envPath }) => {
  // envPath set → a RE-SYNC of a file the vault already remembers. The server
  // re-reads it and compares, so the browser never holds the file at all.
  const resync = !!envPath;
  const [text, setText] = useState("");
  const [fileName, setFileName] = useState("");
  const [scope, setScope] = useState(defaultScope || scopes[0]);
  const [overwrite, setOverwrite] = useState(resync);
  const [rows, setRows] = useState(null);     // null = nothing previewed yet
  const [vanished, setVanished] = useState([]);
  const [digest, setDigest] = useState("");
  const [picked, setPicked] = useState({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [dragging, setDragging] = useState(false);

  // Any change to how the file would be classified invalidates the preview,
  // so the table can never disagree with the button beneath it.
  const invalidate = () => { setRows(null); setVanished([]); setDigest(""); };

  const takeFile = async (file) => {
    if (!file) return;
    try {
      const body = await file.text();
      setFileName(file.name);
      setText(body);
      setErr("");
      invalidate();
    } catch (e) { setErr(String(e)); }
  };

  // A re-sync sends the path and lets the server read; a fresh import sends
  // the text the browser read. Never both.
  // `env_path` ONLY — on the hub route `path` is the PROJECT selector, and
  // setting it to the .env would retarget the whole request at the file,
  // making every existing entry look new and writing to the wrong vault.
  const body = (extra) => Object.assign(
    resync ? { env_path: envPath, compare: true } : { text },
    { scope, overwrite }, extra);

  const runPreview = async () => {
    if (!resync && !text.trim()) {
      setErr("Nothing to import — choose a file or paste some lines."); return;
    }
    setBusy(true); setErr("");
    try {
      const resp = await post(body({ preview: true }));
      if (resp && resp.error) setErr(resp.error);
      else {
        const got = (resp && resp.rows) || [];
        setRows(got);
        setVanished((resp && resp.vanished) || []);
        setDigest((resp && resp.digest) || "");
        const sel = {};
        // Rows already matching the vault are left unticked: re-writing an
        // identical value is a keyring write and a ledger row for nothing.
        got.forEach(r => { if (!r.reason && r.action !== "current") sel[r.name] = true; });
        setPicked(sel);
      }
    } catch (e) { setErr(String(e)); }
    setBusy(false);
  };

  const runImport = async () => {
    const only = Object.keys(picked).filter(n => picked[n]);
    if (!only.length) return;
    setBusy(true); setErr("");
    try {
      // expect_digest makes the commit refuse a file that moved under the
      // preview the user ticked, rather than importing the new content.
      const resp = await post(body({ preview: false, only, expect_digest: digest }));
      if (resp && resp.error) {
        setErr(resp.stale_preview
          ? `${resp.error} — press Re-check to see what it says now.`
          : resp.error);
        if (resp.stale_preview) invalidate();
      } else {
        setText(""); setFileName(""); invalidate(); setPicked({});
        onDone(`Imported ${(resp.created || []).length}, skipped ${(resp.skipped || []).length}`);
      }
    } catch (e) { setErr(String(e)); }
    setBusy(false);
  };

  const eligible = (rows || []).filter(r => !r.reason && r.action !== "current");
  const chosen = Object.keys(picked).filter(n => picked[n]).length;
  const cell = { padding: "3px 8px", textAlign: "left", whiteSpace: "nowrap" };
  const tone = {
    ok: T.accent,
    note: T.textMuted,
    skip: T.error,
  };

  return (
    <div style={{
      border: `1px solid ${T.border}`, borderRadius: 8, padding: 14,
      marginBottom: 14, background: T.surface,
    }}>
      {/* 1 — choose (a re-sync already knows the file; the server reads it) */}
      {resync ? (
        <div style={{
          border: `1px solid ${T.border}`, borderRadius: 6, padding: "10px 12px",
          marginBottom: 10, fontSize: 12, color: T.textMuted,
        }}>
          Re-syncing <span className="mono" style={{ color: T.text }}>{envPath}</span>
          <div style={{ fontSize: 10.5, marginTop: 4, lineHeight: 1.5 }}>
            C3 reads the file on the server and compares each value against the
            stored one by digest. Neither value is sent to this page.
          </div>
        </div>
      ) : (
      <div
        onDragOver={e => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={e => {
          e.preventDefault(); setDragging(false);
          takeFile(e.dataTransfer.files && e.dataTransfer.files[0]);
        }}
        style={{
          border: `1px dashed ${dragging ? T.accent : T.border}`,
          background: dragging ? `${T.accent}11` : "transparent",
          borderRadius: 6, padding: "14px 12px", textAlign: "center",
          marginBottom: 10, fontSize: 12, color: T.textMuted,
        }}>
        <div style={{ marginBottom: 8 }}>
          {fileName
            ? <span className="mono" style={{ color: T.text }}>{fileName}</span>
            : "Drop a .env file here"}
        </div>
        <label className="btn" style={{
          background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
          padding: "5px 12px", borderRadius: 6, fontSize: 12, cursor: "pointer",
          display: "inline-block",
        }}>
          Choose file…
          <input type="file" style={{ display: "none" }}
            onChange={e => takeFile(e.target.files && e.target.files[0])} />
        </label>
      </div>
      )}

      {!resync && (
        <React.Fragment>
          <span style={labelStyle}>…or paste KEY=VALUE lines (comments and `export` prefixes are tolerated)</span>
          <textarea rows={4} value={text}
            onChange={e => { setText(e.target.value); setFileName(""); invalidate(); }}
            style={{ ...inputStyle, fontFamily: "monospace", resize: "vertical" }}
            autoComplete="off" spellCheck={false} />
        </React.Fragment>
      )}

      <div style={{ display: "flex", gap: 10, marginTop: 8, alignItems: "center", flexWrap: "wrap" }}>
        {scopes.length > 1 && (
          <select value={scope}
            onChange={e => { setScope(e.target.value); invalidate(); }}
            style={{ ...inputStyle, width: 140 }}>
            {scopes.map(s => <option key={s} value={s}>{s} scope</option>)}
          </select>
        )}
        <label style={{ fontSize: 11, color: T.textMuted, display: "flex", alignItems: "center", gap: 5 }}>
          <input type="checkbox" checked={overwrite}
            onChange={e => { setOverwrite(e.target.checked); invalidate(); }} />
          replace entries that already exist
        </label>
        {overwrite && (
          <span style={{ fontSize: 10.5, color: T.textDim }}>
            (rotates the value only — description, env var and exposure settings are kept)
          </span>
        )}
        <div style={{ flex: 1 }} />
        <button className="btn" disabled={busy} onClick={runPreview} style={{
          background: rows ? T.surfaceAlt : T.accent,
          color: rows ? T.text : "#fff",
          border: rows ? `1px solid ${T.border}` : "none",
          padding: "6px 14px", borderRadius: 6, fontSize: 12, cursor: "pointer",
        }}>{rows ? "Re-check" : "Preview"}</button>
      </div>

      {err && (
        <div style={{
          padding: "8px 12px", borderRadius: 6, marginTop: 10, fontSize: 12,
          background: `${T.error}22`, color: T.error, border: `1px solid ${T.error}55`,
        }}>{err}</div>
      )}

      {/* 2 — preview */}
      {rows && (
        <div style={{ marginTop: 12 }}>
          {rows.length === 0 ? (
            <div style={{ fontSize: 12, color: T.textMuted }}>
              No KEY=VALUE lines found in that file.
            </div>
          ) : eligible.length === 0 && vanished.length === 0
              && rows.every(r => r.action === "current") ? (
            <div style={{ fontSize: 12, color: T.accent }}>
              Already up to date — all {rows.length} values match the vault.
            </div>
          ) : (
            <React.Fragment>
              <div style={{ maxHeight: 260, overflow: "auto", border: `1px solid ${T.border}`, borderRadius: 6 }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
                  <thead>
                    <tr style={{ background: T.surfaceAlt, color: T.textMuted }}>
                      <th style={{ ...cell, width: 28 }}>
                        <input type="checkbox"
                          checked={eligible.length > 0 && chosen === eligible.length}
                          onChange={e => {
                            const sel = {};
                            if (e.target.checked) eligible.forEach(r => { sel[r.name] = true; });
                            setPicked(sel);
                          }} />
                      </th>
                      <th style={cell}>name</th>
                      <th style={cell}>line</th>
                      <th style={cell}>type</th>
                      <th style={cell}>len</th>
                      <th style={cell}>fingerprint</th>
                      <th style={{ ...cell, whiteSpace: "normal" }}>status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r, i) => (
                      <tr key={`${r.name}-${r.line}-${i}`}
                        style={{ borderTop: `1px solid ${T.border}`, opacity: r.reason ? 0.65 : 1 }}>
                        <td style={cell}>
                          <input type="checkbox" disabled={!!r.reason}
                            checked={!!picked[r.name]}
                            onChange={e => setPicked({ ...picked, [r.name]: e.target.checked })} />
                        </td>
                        <td style={{ ...cell, color: T.text }} className="mono">{r.name}</td>
                        <td style={{ ...cell, color: T.textMuted }}>{r.line}</td>
                        <td style={{ ...cell, color: T.textMuted }}>{r.ctype}</td>
                        <td style={{ ...cell, color: T.textMuted }}>{r.value_len}</td>
                        <td style={{ ...cell, color: T.textMuted }} className="mono">{r.fingerprint || "—"}</td>
                        <td style={{
                          ...cell, whiteSpace: "normal",
                          color: r.action === "current" ? T.textMuted
                            : tone[HUB_CREDS_IMPORT_STATUS_TONE(r.reason)],
                        }}>
                          {r.detail || HUB_CREDS_ACTION_TEXT[r.action] || r.action}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {vanished.length > 0 && (
                <div style={{
                  marginTop: 10, padding: "8px 12px", borderRadius: 6, fontSize: 11.5,
                  background: `${T.warn}18`, color: T.text,
                  border: `1px solid ${T.warn}55`,
                }}>
                  <b>{vanished.length}</b> entr{vanished.length === 1 ? "y" : "ies"} imported
                  from this file {vanished.length === 1 ? "is" : "are"} no longer in it:{" "}
                  <span className="mono">{vanished.join(", ")}</span>.
                  <div style={{ fontSize: 10.5, color: T.textMuted, marginTop: 4, lineHeight: 1.5 }}>
                    Nothing is deleted — a key can leave a .env and still be in
                    use. Select them in the list and delete them there if you
                    mean to.
                  </div>
                </div>
              )}
              <div style={{ display: "flex", gap: 10, marginTop: 10, alignItems: "center" }}>
                <span style={{ fontSize: 11, color: T.textMuted }}>
                  {chosen} of {eligible.length} importable selected
                  {rows.filter(r => r.action === "current").length > 0
                    && ` · ${rows.filter(r => r.action === "current").length} already up to date`}
                </span>
                <div style={{ flex: 1 }} />
                <button className="btn" disabled={busy || !chosen} onClick={runImport} style={{
                  background: chosen ? T.accent : T.surfaceAlt,
                  color: chosen ? "#fff" : T.textMuted,
                  border: chosen ? "none" : `1px solid ${T.border}`,
                  padding: "6px 14px", borderRadius: 6, fontSize: 12,
                  cursor: chosen ? "pointer" : "default",
                }}>Import {chosen} selected</button>
              </div>
            </React.Fragment>
          )}
        </div>
      )}

      <div style={{ fontSize: 10, color: T.textMuted, marginTop: 10, lineHeight: 1.5 }}>
        Nothing is written until you press Import. Values are shown only as a
        length and a fingerprint — never the value itself. Importing does not
        delete the source file: once the entries are in the vault, remove the
        .env or confirm it is gitignored.
      </div>
    </div>
  );
};

const CredKV = ({ k, v }) => (
  <div style={{ display: 'flex', gap: 10, fontSize: 11.5, padding: '3px 0' }}>
    <span style={{ color: T.textMuted, minWidth: 96 }}>{k}</span>
    <span className="mono" style={{ color: T.text, wordBreak: 'break-all' }}>{v}</span>
  </div>
);

function CredSwitch({ on, label, note, tone, onToggle, disabled }) {
  const c = on ? (tone || T.accent) : T.textMuted;
  return (
    <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start', padding: '7px 0' }}>
      <button onClick={() => !disabled && onToggle()} role="switch" aria-checked={on}
        aria-label={label} disabled={disabled}
        style={{
          width: 34, height: 19, flexShrink: 0, marginTop: 1, borderRadius: 10,
          border: `1px solid ${on ? c : T.border}`, background: on ? `${c}30` : T.surfaceAlt,
          cursor: disabled ? 'default' : 'pointer', padding: 0, position: 'relative',
          transition: 'all .15s',
        }}>
        <span style={{
          position: 'absolute', top: 2, left: on ? 16 : 2, width: 13, height: 13,
          borderRadius: '50%', background: on ? c : T.textMuted, transition: 'left .15s',
        }} />
      </button>
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: on ? c : T.text }}>{label}</div>
        <div style={{ fontSize: 11, color: T.textDim, lineHeight: 1.5, marginTop: 2 }}>{note}</div>
      </div>
    </div>
  );
}

function CredDrawer({ entry, path, projectName, onClose, onChanged, initialReplace }) {
  const [meta, setMeta] = useState({
    description: entry.description || '', type: entry.type || 'token',
    env_var: entry.env_var || '',
  });
  const [flags, setFlags] = useState({
    inject: !!entry.inject, agent_readable: !!entry.agent_readable,
  });
  const [secret, setSecret] = useState('');
  const [secretFields, setSecretFields] = useState({});
  const [replaceOpen, setReplaceOpen] = useState(!!initialReplace);
  const [chk, setChk] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [confirm, setConfirm] = useState(null);
  const [usage, setUsage] = useState(null);   // {aggregate, recent} — names/previews only
  const structured = CREDS_STRUCTURED[entry.type];

  useEffect(() => {
    let live = true;
    api.get('/api/projects/credentials/usage?name=' + encodeURIComponent(entry.name)
      + (path ? '&path=' + encodeURIComponent(path) : '') + '&limit=25')
      .then(d => { if (live) setUsage(d); })
      .catch(() => {});
    return () => { live = false; };
  }, [entry.name, path]);

  const owner = entry.scope === 'global'
    ? 'the global vault (~/.c3)'
    : (projectName || path || 'this project');
  const dirty = meta.description !== (entry.description || '')
    || meta.type !== (entry.type || 'token')
    || meta.env_var !== (entry.env_var || '');

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape' && !confirm) onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, confirm]);

  const patch = async (fields, msg) => {
    setBusy(true); setErr('');
    try {
      const resp = await credApi.save(path,
        Object.assign({ name: entry.name, scope: entry.scope }, fields));
      if (resp && resp.error) setErr(resp.error);
      else { notify(msg); if (onChanged) onChanged(); }
    } catch (e) { setErr(apiErr(e)); }
    setBusy(false);
  };

  const applyFlag = (field, next) => {
    setFlags(prev => Object.assign({}, prev, { [field]: next }));
    return patch({ [field]: next },
      `${field === 'inject' ? 'Auto-inject' : 'Agent access'} ${next ? 'enabled' : 'disabled'} for '${entry.name}'`);
  };

  const toggle = (field) => {
    const next = !flags[field];
    if (!next) { applyFlag(field, false); return; }
    setConfirm(credExposureConfirm(entry, field, meta.env_var, owner,
      () => applyFlag(field, true)));
  };

  const runCheck = async () => {
    setBusy(true); setErr('');
    try { setChk(await credApi.check(path, entry.name)); }
    catch (e) { setErr(apiErr(e)); }
    setBusy(false);
  };

  // Full re-store: set_credential rewrites the entry, so every field the
  // drawer knows about rides along or it would be silently reset.
  // Structured entries submit only the TYPED fields — the store merges a
  // partial payload, so one field can change without retyping the rest.
  const replaceSecret = async () => {
    const value = structured ? credsTypedFields(secretFields) : secret;
    if (structured ? !Object.keys(value).length : !value) return;
    setBusy(true); setErr('');
    try {
      const resp = await credApi.save(path, {
        name: entry.name, scope: entry.scope, value, type: meta.type,
        description: meta.description, env_var: meta.env_var,
        agent_readable: structured ? false : flags.agent_readable,
        inject: structured ? false : flags.inject,
      });
      if (resp && resp.error) setErr(resp.error);
      else { notify(`Updated the value of '${entry.name}'`); setChk(null); if (onChanged) onChanged(); }
    } catch (e) { setErr(apiErr(e)); }
    setSecret(''); setSecretFields({}); setReplaceOpen(false); setBusy(false);
  };

  const doDelete = () => setConfirm(credDeleteConfirm(entry, owner, async () => {
    try {
      await credApi.remove(path, entry.name, entry.scope);
      notify(`Deleted '${entry.name}'`);
      if (onChanged) onChanged();
      onClose();
    } catch (e) { setErr(apiErr(e)); }
  }));

  const fld = drillFieldStyle({ width: '100%', boxSizing: 'border-box' });
  const lbl = { fontSize: 11, color: T.textMuted, marginBottom: 4, display: 'block' };
  const shadowedIn = entry.shadowed_in || [];

  return (
    <React.Fragment>
      <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: '#00000060', zIndex: 259 }} />
      <div role="dialog" aria-label={`Settings for ${entry.name}`} style={{
        position: 'fixed', top: 0, right: 0, bottom: 0, width: 470, maxWidth: '96vw',
        background: T.surface, borderLeft: `1px solid ${T.border}`, zIndex: 260,
        display: 'flex', flexDirection: 'column', animation: 'slideInRight 0.25s ease',
      }}>
        {/* Header */}
        <div style={{ padding: '14px 18px', display: 'flex', alignItems: 'flex-start', gap: 10 }}>
          <I name="lock" size={16} color={T.accent} style={{ marginTop: 2 }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="mono" style={{ fontSize: 14, fontWeight: 700, color: T.text, wordBreak: 'break-all' }}>
              {entry.name}
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 7 }}>
              <Badge color={entry.scope === 'global' ? T.accent : T.blue}>{entry.scope}</Badge>
              <Badge color={T.textMuted}>{entry.type || 'token'}</Badge>
              <Badge color={T.textMuted}>{entry.storage || 'keyring'}</Badge>
              {structured && !!credsDisplayText(entry) && (
                <Badge color={T.accent}>{credsDisplayText(entry)}</Badge>
              )}
              {!!entry.inject && <Badge color={T.warn}>inject</Badge>}
              {!!entry.agent_readable && <Badge color={T.error}>agent_readable</Badge>}
            </div>
            <div style={{ fontSize: 11, color: T.textDim, marginTop: 7 }}>in {owner}</div>
          </div>
          <button onClick={onClose} aria-label="Close"
            style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 4, display: 'flex' }}>
            <I name="xSmall" size={14} color={T.textMuted} />
          </button>
        </div>

        {err && (
          <div style={{
            margin: '0 18px 10px', padding: '7px 11px', borderRadius: 6, fontSize: 12,
            background: `${T.error}22`, color: T.error, border: `1px solid ${T.error}55`,
          }}>{err}</div>
        )}

        <div style={{ flex: 1, overflowY: 'auto' }}>
          <CredSec title="General" note="Name is immutable — there is no rename; create a new entry instead.">
            <span style={lbl}>Description</span>
            <input value={meta.description} style={fld} autoComplete="off"
              onChange={e => setMeta(Object.assign({}, meta, { description: e.target.value }))} />
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginTop: 10 }}>
              <div>
                <span style={lbl}>Type</span>
                {structured ? (
                  <input value={entry.type} disabled style={fld}
                    title="The plain/structured boundary is immutable — delete and re-create to change it" />
                ) : (
                  <select value={meta.type} style={fld}
                    onChange={e => setMeta(Object.assign({}, meta, { type: e.target.value }))}>
                    <option value="token">token</option>
                    <option value="env">env</option>
                    <option value="multiline">multiline</option>
                  </select>
                )}
              </div>
              <div>
                <span style={lbl}>Env var (default: name)</span>
                <input value={meta.env_var} style={fld} className="mono" autoComplete="off" spellCheck={false}
                  placeholder={entry.name}
                  onChange={e => setMeta(Object.assign({}, meta, { env_var: e.target.value }))} />
              </div>
            </div>
            <div style={{ marginTop: 10 }}>
              <Btn color={T.accent} disabled={!dirty || busy} style={{ padding: '6px 14px' }}
                onClick={() => patch(meta, `Updated '${entry.name}'`)}>Save changes</Btn>
            </div>
          </CredSec>

          {structured ? (
            <CredSec title="Exposure" tone={T.textMuted}
              note="Structured entries are inject-only by construction.">
              <div style={{ fontSize: 12, color: T.textMuted, lineHeight: 1.55 }}>
                🔒 The agent can use single fields at the subprocess boundary
                (<span className="mono">{'{{cred:' + entry.name + '.field}}'}</span> or{' '}
                <span className="mono">env_creds='{entry.name}.field'</span>) but can
                never reveal them, and they never auto-inject. These switches do
                not exist for {entry.type} entries.
              </div>
            </CredSec>
          ) : (
          <CredSec title="Exposure" tone={flags.agent_readable ? T.error : (flags.inject ? T.warn : T.textMuted)}
            note="How far this secret is allowed to travel. Both default to off.">
            <CredSwitch on={flags.inject} tone={T.warn} disabled={busy}
              label="Auto-inject into every c3_shell run"
              note={`Exported as $${meta.env_var || entry.name} for every subprocess in ${owner}.`}
              onToggle={() => toggle('inject')} />
            <CredSwitch on={flags.agent_readable} tone={T.error} disabled={busy}
              label="Agent may read the value"
              note="Lets the agent pull the plaintext into its context and transcripts. Injection-only use does not need this."
              onToggle={() => toggle('agent_readable')} />
          </CredSec>
          )}

          <CredSec title="Secret"
            note="Values never leave the host. The check computes a fingerprint server-side; it is not stored.">
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <Btn variant="ghost" disabled={busy} style={{ padding: '6px 12px' }} onClick={runCheck}>
                <I name="refresh" size={12} /> Check resolution
              </Btn>
              {chk && (
                <span className="mono" style={{ fontSize: 11.5, color: chk.resolvable ? T.accent : T.error }}>
                  {chk.resolvable ? `✓ resolves · ${chk.fingerprint}` : '✗ unresolvable'}
                </span>
              )}
              <span className="mono" style={{ fontSize: 11, color: T.textDim }}>
                {structured ? credsDisplayText(entry) : `•••• len=${entry.value_len}`}
              </span>
            </div>
            {structured && (
              <div className="mono" style={{ fontSize: 11, color: T.textDim, marginTop: 8 }}>
                fields: {(entry.fields || []).join(', ') || 'none recorded'}
              </div>
            )}
            {!replaceOpen ? (
              <div style={{ marginTop: 10 }}>
                <Btn variant="ghost" style={{ padding: '6px 12px' }} onClick={() => setReplaceOpen(true)}>
                  <I name="edit" size={12} /> {structured ? 'Update fields…' : 'Replace secret…'}
                </Btn>
              </div>
            ) : structured ? (
              <div style={{ marginTop: 10 }}>
                <span style={lbl}>
                  Type only the fields to change — blank fields keep their stored value.
                </span>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                  {[...structured.required, ...structured.optional].map(fname => (
                    <div key={fname}>
                      <span style={Object.assign({}, lbl, { marginBottom: 2 })}>{fname}</span>
                      <input
                        type={structured.hidden.includes(fname) ? 'password' : 'text'}
                        value={secretFields[fname] || ''}
                        onChange={e => setSecretFields(Object.assign({}, secretFields, { [fname]: e.target.value }))}
                        style={fld} autoComplete="new-password" spellCheck={false} />
                    </div>
                  ))}
                </div>
                <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                  <Btn color={T.warn}
                    disabled={busy || !Object.keys(credsTypedFields(secretFields)).length}
                    style={{ padding: '6px 14px' }} onClick={replaceSecret}>Update</Btn>
                  <Btn variant="ghost" style={{ padding: '6px 14px' }}
                    onClick={() => { setSecretFields({}); setReplaceOpen(false); }}>Cancel</Btn>
                </div>
              </div>
            ) : (
              <div style={{ marginTop: 10 }}>
                <span style={lbl}>New value (write-only — never echoed back)</span>
                {meta.type === 'multiline' ? (
                  <textarea rows={4} value={secret} onChange={e => setSecret(e.target.value)}
                    style={Object.assign({}, fld, { fontFamily: 'monospace', resize: 'vertical' })}
                    autoComplete="new-password" spellCheck={false} />
                ) : (
                  <input type="password" value={secret} autoFocus
                    onChange={e => setSecret(e.target.value)} style={fld} autoComplete="new-password" />
                )}
                <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                  <Btn color={T.warn} disabled={!secret || busy} style={{ padding: '6px 14px' }}
                    onClick={replaceSecret}>Replace</Btn>
                  <Btn variant="ghost" style={{ padding: '6px 14px' }}
                    onClick={() => { setSecret(''); setReplaceOpen(false); }}>Cancel</Btn>
                </div>
                <div style={{ fontSize: 11, color: T.textDim, marginTop: 7 }}>
                  Consumers resolving this name pick up the new value on their next run.
                </div>
              </div>
            )}
          </CredSec>

          <CredSec title="Usage & relationships">
            <CredKV k="created" v={credWhen(entry.created)} />
            <CredKV k="updated" v={credWhen(entry.updated)} />
            <CredKV k="last used" v={credWhen(entry.last_used)} />
            <CredKV k="use count" v={String(entry.use_count || 0)} />
            <CredKV k="storage" v={entry.storage || 'keyring'} />
            {usage && usage.aggregate && usage.aggregate.total > 0 && (
              <div style={{ marginTop: 9 }}>
                <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 5 }}>
                  History — {usage.aggregate.total} recorded use(s)
                  {(() => {
                    const surf = Object.entries(usage.aggregate.by_surface || {})
                      .map(([s, c]) => `${s}:${c}`).join('  ');
                    return surf ? ` · ${surf}` : '';
                  })()}
                </div>
                <div style={{
                  border: `1px solid ${T.border}`, borderRadius: 6,
                  maxHeight: 180, overflowY: 'auto',
                }}>
                  {(usage.recent.events || []).map((e, i) => {
                    const ref = e.name + (e.field ? `.${e.field}` : '');
                    return (
                      <div key={`${e.ts}|${i}`} title={e.cmd || ''} style={{
                        display: 'flex', gap: 8, alignItems: 'center',
                        padding: '4px 8px', fontSize: 11,
                        borderTop: i === 0 ? 'none' : `1px solid ${T.border}`,
                        background: i % 2 ? `${T.surfaceAlt}70` : T.surface,
                      }}>
                        <span className="mono" style={{ color: T.textDim }}>
                          {credWhen(e.ts)}
                        </span>
                        <Badge color={e.action === 'reveal' ? T.error : T.blue}>
                          {e.action}
                        </Badge>
                        <span className="mono" style={{ color: T.text }}>{ref}</span>
                        <span className="mono" style={{ color: T.textMuted }}>
                          [{e.surface}]
                        </span>
                        {'exit' in e && (
                          <span className="mono" style={{
                            color: e.exit === 0 ? T.accent : T.error,
                          }}>exit={e.exit}</span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
            {!!entry.shadows_global && (
              <div style={{
                marginTop: 9, padding: '7px 10px', borderRadius: 6, fontSize: 11.5, lineHeight: 1.5,
                background: `${T.warn}18`, color: T.warn, border: `1px solid ${T.warn}44`,
              }}>Overrides the same-named <b>global</b> entry — this project resolves to the value stored here.</div>
            )}
            {shadowedIn.length > 0 && (
              <div style={{ marginTop: 9 }}>
                <div style={{ fontSize: 11, color: T.warn, marginBottom: 5 }}>
                  Overridden in {shadowedIn.length} project{shadowedIn.length === 1 ? '' : 's'} — those resolve to their own value:
                </div>
                {shadowedIn.map(s => (
                  <div key={s.path} className="mono" style={{ fontSize: 11, color: T.textMuted, padding: '2px 0' }}>
                    {s.name || s.path}
                  </div>
                ))}
              </div>
            )}
          </CredSec>

          <CredSec title="Danger zone" tone={T.error}>
            <Btn color={T.error} style={{ padding: '6px 14px' }} onClick={doDelete}>
              <I name="trash" size={12} /> Delete credential
            </Btn>
          </CredSec>
        </div>
      </div>
      {confirm && <CredConfirm spec={confirm} onClose={() => setConfirm(null)} />}
    </React.Fragment>
  );
}

// ── One credential row: two lines, one menu, no naked danger icons ──
function CredRow({ entry, striped, owner, onOpen, onMenu, check,
                  selectMode, picked, onPick }) {
  const [hover, setHover] = useState(false);
  const shadowedIn = entry.shadowed_in || [];
  const sourcePath = credSourcePath(entry);
  const openMenuAt = (e) => {
    e.preventDefault(); e.stopPropagation();
    onMenu(e.clientX, e.clientY);
  };
  // In select mode the row toggles its checkbox instead of opening the
  // drawer: a click that opens a panel mid-selection loses the selection.
  const activate = (e) => {
    if (selectMode) { onPick(!!(e && e.shiftKey)); return; }
    onOpen();
  };
  const onKeyDown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(e); }
    else if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) {
      e.preventDefault();
      const r = e.currentTarget.getBoundingClientRect();
      onMenu(r.left + 24, r.bottom - 6);
    }
  };
  return (
    <div tabIndex={0} role="button" onKeyDown={onKeyDown} onClick={activate}
      onContextMenu={openMenuAt}
      onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      style={{
        display: 'flex', alignItems: 'center', gap: 10, padding: '9px 12px',
        borderTop: `1px solid ${T.border}`, cursor: 'pointer', outline: 'none',
        background: picked ? `${T.accent}18`
          : (hover ? T.surfaceAlt : (striped ? `${T.surfaceAlt}70` : T.surface)),
      }}>
      {selectMode && (
        <input type="checkbox" checked={!!picked} readOnly tabIndex={-1}
          aria-label={`Select ${entry.name}`}
          style={{ flexShrink: 0, pointerEvents: 'none' }} />
      )}
      <I name="lock" size={13} color={entry.agent_readable ? T.error : T.textMuted} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 7 }}>
          <span className="mono" style={{ fontWeight: 600, color: T.text, fontSize: 12.5 }}>{entry.name}</span>
          <Badge color={entry.scope === 'global' ? T.accent : T.blue}>{entry.scope}</Badge>
          {!!entry.shadows_global && (
            <span title="This project's value wins over the global entry of the same name">
              <Badge color={T.warn}>overrides global</Badge>
            </span>
          )}
          {shadowedIn.length > 0 && (
            <span title={'Overridden by a project entry in: ' + shadowedIn.map(s => s.name || s.path).join(', ')}>
              <Badge color={T.warn}>overridden ×{shadowedIn.length}</Badge>
            </span>
          )}
          {!!entry.inject && <Badge color={T.warn}>inject</Badge>}
          {!!entry.agent_readable && <Badge color={T.error}>agent-readable</Badge>}
          {check && (
            <span className="mono" style={{ fontSize: 11, color: check.resolvable ? T.accent : T.error }}>
              {/* A bulk check reports resolvability without fingerprinting
                  every entry — one keyring read per row is the single-entry
                  check's job, not a sweep's. */}
              {check.resolvable ? (check.fingerprint ? `✓ ${check.fingerprint}` : '✓ resolves') : '✗ unresolvable'}
            </span>
          )}
        </div>
        <div style={{
          display: 'flex', flexWrap: 'wrap', gap: 12, marginTop: 3,
          fontSize: 11, color: T.textDim,
        }}>
          {owner && <span>{owner}</span>}
          <span className="mono">
            {CREDS_STRUCTURED[entry.type]
              ? `${entry.type} · ${credsDisplayText(entry)} 🔒`
              : `${entry.type || 'token'} · ••••${entry.value_len}`}
          </span>
          {entry.env_var && <span className="mono">→ ${entry.env_var}</span>}
          {sourcePath && (
            <span className="mono" title={`Imported from ${sourcePath}`}
              style={{ color: T.textMuted }}>⇠ {credBaseName(sourcePath)}</span>
          )}
          <span>used {entry.use_count || 0}× · {credWhen(entry.last_used)}</span>
          {entry.description && (
            <span style={{ color: T.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 300 }}>
              {entry.description}
            </span>
          )}
        </div>
      </div>
      <button aria-label={`Actions for ${entry.name}`} onClick={openMenuAt}
        style={{
          background: 'transparent', border: `1px solid ${hover ? T.border : 'transparent'}`,
          borderRadius: 6, cursor: 'pointer', width: 28, height: 28, flexShrink: 0,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}>
        <I name="kebab" size={14} color={T.textMuted} />
      </button>
    </div>
  );
}

// Shared item list for both the row menu and the search-result menu.
function credMenuItems({ entry, check, cb }) {
  const structured = !!CREDS_STRUCTURED[entry.type];
  const items = [
    { label: 'Open settings…', icon: 'settings', onClick: cb.open, hint: '↵' },
    { label: 'Check resolution', icon: 'refresh', onClick: cb.check },
    { label: structured ? 'Update fields…' : 'Replace secret…',
      icon: 'edit', onClick: cb.replace },
    { separator: true },
    // Exposure toggles do not exist for structured entries: they are
    // inject-only by construction and the server refuses the flags.
    ...(structured ? [] : [
    {
      label: entry.inject ? 'Disable auto-inject' : 'Enable auto-inject…',
      icon: 'zap', onClick: () => cb.toggle('inject'),
    },
    {
      label: entry.agent_readable ? 'Revoke agent read access' : 'Allow agent to read…',
      icon: 'eye', danger: !entry.agent_readable, onClick: () => cb.toggle('agent_readable'),
    },
    { separator: true },
    ]),
    { label: 'Copy name', icon: 'copy', onClick: () => credCopy(entry.name, 'name') },
    {
      label: 'Copy env var', icon: 'copy', disabled: !entry.env_var,
      onClick: () => credCopy(entry.env_var || entry.name, 'env var'),
    },
    {
      label: 'Copy fingerprint', icon: 'copy',
      disabled: !(check && check.resolvable),
      hint: check && check.resolvable ? check.fingerprint : 'run check',
      onClick: () => credCopy((check || {}).fingerprint, 'fingerprint'),
    },
  ];
  if (cb.audit) {
    items.push({ separator: true });
    items.push({ label: 'View audit trail…', icon: 'clock', onClick: cb.audit });
  }
  if (cb.openProject) {
    if (!cb.audit) items.push({ separator: true });
    items.push({ label: 'Open project drill', icon: 'external', onClick: cb.openProject });
  }
  items.push({ separator: true });
  items.push({ label: 'Delete credential…', icon: 'trash', danger: true, onClick: cb.remove });
  return items;
}

// Sticky bulk bar. z-index stays under the drawer scrim (259) so opening a
// drawer covers it rather than the other way round.
function CredBulkBar({ count, busy, onAction, onDelete, onExport, onCancel }) {
  const btn = (bg, color, border) => ({
    background: bg, color, border: border || 'none', borderRadius: 6,
    padding: '5px 11px', fontSize: 12, cursor: busy ? 'default' : 'pointer',
    opacity: busy ? 0.6 : 1, whiteSpace: 'nowrap',
  });
  return (
    <div style={{
      position: 'sticky', bottom: 10, zIndex: 40, marginTop: 10,
      display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
      background: T.surface, border: `1px solid ${T.accent}66`, borderRadius: 8,
      padding: '8px 12px', boxShadow: '0 6px 18px rgba(0,0,0,0.28)',
    }}>
      <span style={{ fontSize: 12, color: T.text, fontWeight: 600 }}>
        {count} selected
      </span>
      <div style={{ flex: 1 }} />
      {CRED_BULK_ACTIONS.map(a => (
        <button key={a.action} className="btn" disabled={busy}
          onClick={() => onAction(a.action, a.label)}
          style={btn(T.surfaceAlt, T.text, `1px solid ${T.border}`)}>
          {a.label}
        </button>
      ))}
      <button className="btn" disabled={busy} onClick={onExport}
        style={btn(T.surfaceAlt, T.text, `1px solid ${T.border}`)}>Export CSV</button>
      <button className="btn" disabled={busy} onClick={onDelete}
        style={btn(`${T.error}22`, T.error, `1px solid ${T.error}66`)}>Delete…</button>
      <button className="btn" disabled={busy} onClick={onCancel}
        style={btn('transparent', T.textMuted, `1px solid ${T.border}`)}>Cancel</button>
    </div>
  );
}

// Which .env files this vault remembers. DERIVED from the entries rather than
// kept in a second list — a hand-maintained copy of something the entries
// already imply is exactly what goes stale without anything failing.
function CredSourcesStrip({ sources, onResync, busy }) {
  if (!sources.length) return null;
  return (
    <div style={{
      border: `1px solid ${T.border}`, borderRadius: 8, padding: '8px 10px',
      marginBottom: 10, background: T.surfaceAlt,
    }}>
      <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 6 }}>
        Imported from — re-sync reads the file again and shows what changed.
      </div>
      {sources.map(s => (
        <div key={s.path} style={{
          display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap',
          padding: '3px 0',
        }}>
          <I name="file" size={12} color={T.textMuted} />
          <span className="mono" title={s.path}
            style={{ fontSize: 11.5, color: T.text }}>{credBaseName(s.path)}</span>
          <span style={{ fontSize: 11, color: T.textDim }}>
            {s.count} {s.count === 1 ? 'entry' : 'entries'}
            {s.at ? ` · last synced ${credWhen(s.at)}` : ''}
          </span>
          <div style={{ flex: 1 }} />
          <button className="btn" disabled={busy} onClick={() => onResync(s.path)}
            style={{
              background: 'transparent', color: T.accent,
              border: `1px solid ${T.accent}55`, borderRadius: 6,
              padding: '3px 10px', fontSize: 11.5, cursor: 'pointer',
            }}>Re-sync</button>
        </div>
      ))}
    </div>
  );
}

// path=null → the global vault (~/.c3): scope locked to 'global'.
// path=string → that project's merged view (global entries + project shadows).
function CredsManager({ path, projectName, onChanged, bindSlash }) {
  const isGlobal = !path;
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState(null);        // null = closed; {…} = create/edit
  const [checks, setChecks] = useState({});      // credKey -> {resolvable, fingerprint}
  const [importOpen, setImportOpen] = useState(false);
  const [importPreset, setImportPreset] = useState(null);  // {envPath} for a re-sync
  const [filter, setFilter] = useState('');
  const [chips, setChips] = useState({});        // chip id -> true
  const [sort, setSort] = useState('name');
  const [menu, setMenu] = useState(null);        // {x, y, entry}
  const [drawer, setDrawer] = useState(null);    // entry
  const [replaceOnOpen, setReplaceOnOpen] = useState(false);
  const [confirm, setConfirm] = useState(null);  // CredConfirm spec
  const [auditFor, setAuditFor] = useState(null);   // '' = all, 'NAME' = one
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState({});  // credKey -> entry
  const [bulkBusy, setBulkBusy] = useState(false);
  const filterRef = useRef(null);
  const lastPickedRef = useRef(-1);              // anchor for shift-click ranges

  const withPath = (obj) => (path ? Object.assign({ path }, obj) : obj);

  const load = useCallback(async () => {
    try {
      if (path) {
        const data = await api.get('/api/projects/credentials?path=' + encodeURIComponent(path));
        setEntries((data && data.entries) || []);
      } else {
        const data = await api.get('/api/hub/credentials/overview');
        setEntries((((data || {}).global) || {}).entries || []);
      }
      setError('');
    } catch (e) { setError(String(e)); }
    setLoading(false);
  }, [path]);

  useEffect(() => {
    setLoading(true); setChecks({}); setSelected({}); setSelectMode(false);
    load();
  }, [load]);

  // `/` focuses the filter, as it does on every other hub board — but only
  // where nothing above already claims the key. The hub Credentials page owns
  // `/` for its cross-project search, so the manager binds it solely when
  // mounted standalone (the drill tab). Two handlers on one key means the
  // wrong box takes focus.
  useEffect(() => {
    if (!bindSlash) return undefined;
    const onKey = (e) => {
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
      const el = e.target;
      const tag = el && el.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
          || (el && el.isContentEditable)) return;
      if (drawer || menu || confirm) return;
      e.preventDefault();
      if (filterRef.current) filterRef.current.focus();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [bindSlash, drawer, menu, confirm]);

  const done = (msg) => { notify(msg); load(); if (onChanged) onChanged(); };

  const saveForm = async () => {
    if (!form || !form.name.trim()) return;
    setBusy(true);
    try {
      const structured = !!CREDS_STRUCTURED[form.type];
      const payload = withPath({
        name: form.name.trim(), scope: isGlobal ? 'global' : form.scope,
        type: form.type, description: form.description, env_var: form.env_var,
        agent_readable: structured ? false : !!form.agent_readable,
        inject: structured ? false : !!form.inject,
      });
      if (structured) {
        const typed = credsTypedFields(form.fields);
        if (Object.keys(typed).length) payload.value = typed;
      } else if (form.value) payload.value = form.value; // blank on edit = keep stored value
      const resp = await api.post('/api/projects/credentials', payload);
      if (resp && resp.error) { setError(resp.error); }
      else {
        setForm(null);
        done(`Saved '${payload.name}' (${payload.scope})`);
      }
    } catch (e) { setError(String(e)); }
    setBusy(false);
  };

  const ownerLabel = isGlobal
    ? 'the global vault (~/.c3)'
    : (projectName || path || 'this project');

  // Destructive + exposure-raising paths go through CredConfirm, never
  // window.confirm — the modal can show blast radius and demand the name.
  const removeEntry = (entry) => setConfirm(credDeleteConfirm(entry, ownerLabel, async () => {
    setBusy(true);
    try {
      await credApi.remove(path, entry.name, entry.scope);
      done(`Deleted '${entry.name}'`);
    } catch (e) { setError(apiErr(e)); }
    setBusy(false);
  }));

  const checkEntry = async (entry) => {
    try {
      const data = await credApi.check(path, entry.name);
      setChecks(prev => Object.assign({}, prev, { [credKey(entry, path)]: data }));
      notify(data && data.resolvable
        ? `${entry.name} resolves · ${data.fingerprint}`
        : `${entry.name} does not resolve`, data && data.resolvable ? 'ok' : 'warn');
    } catch (e) { setError(apiErr(e)); }
  };

  const applyFlag = async (entry, field, next) => {
    try {
      const resp = await credApi.save(path, {
        name: entry.name, scope: entry.scope, [field]: next,
      });
      if (resp && resp.error) setError(resp.error);
      else {
        notify(`${field === 'inject' ? 'Auto-inject' : 'Agent access'} ${next ? 'enabled' : 'disabled'} for '${entry.name}'`);
        load();
        if (onChanged) onChanged();
      }
    } catch (e) { setError(apiErr(e)); }
  };

  const toggleFlag = (entry, field) => {
    const next = !entry[field];
    if (!next) { applyFlag(entry, field, false); return; }
    setConfirm(credExposureConfirm(entry, field, entry.env_var, ownerLabel,
      () => applyFlag(entry, field, true)));
  };

  const inputStyle = drillFieldStyle({ width: '100%', boxSizing: 'border-box' });
  const labelStyle = { fontSize: 11, color: T.textMuted, marginBottom: 4, display: 'block' };

  // Create only. Editing an existing entry happens in CredDrawer, which is
  // also the only place a stored value can be replaced.
  const openCreate = () => setForm(
    Object.assign({}, HUB_CREDS_EMPTY_FORM, isGlobal ? { scope: 'global' } : {}));

  // Local filter accepts the same `key:value` qualifiers as the hub search.
  const query = useMemo(() => parseCredQuery(filter), [filter]);
  const shown = useMemo(() => {
    const kept = entries.filter(e => credChipPass(e, chips) && (query.empty
      || credRecordMatches(
        { entry: e, projectName: projectName || '', projectPath: path || '' }, query)));
    return credSortEntries(kept, sort);
  }, [entries, query, chips, sort, path, projectName]);

  const chipsOn = CRED_CHIPS.filter(c => chips[c.id]).length;
  const narrowed = chipsOn > 0 || !query.empty;
  const clearNarrowing = () => { setFilter(''); setChips({}); };

  // Derived, not stored: the .env files this vault remembers importing from.
  const sources = useMemo(() => {
    const seen = {};
    entries.forEach(e => {
      const sp = credSourcePath(e);
      if (!sp) return;
      const row = seen[sp] || (seen[sp] = { path: sp, count: 0, at: '' });
      row.count += 1;
      const at = credSourceAt(e);
      if (at > row.at) row.at = at;
    });
    return Object.keys(seen).sort().map(k => seen[k]);
  }, [entries]);

  // ── selection ──────────────────────────────────────────────
  const selectedRows = useMemo(
    () => shown.filter(e => selected[credKey(e, path)]), [shown, selected, path]);
  const allShownPicked = shown.length > 0 && selectedRows.length === shown.length;

  const togglePick = (entry, index, shiftKey) => {
    setSelected((prev) => {
      const next = Object.assign({}, prev);
      const anchor = lastPickedRef.current;
      // Shift extends from the last row touched, matching every other list.
      const span = (shiftKey && anchor >= 0 && anchor < shown.length)
        ? [Math.min(anchor, index), Math.max(anchor, index)]
        : [index, index];
      const turningOn = !prev[credKey(entry, path)];
      for (let i = span[0]; i <= span[1]; i++) {
        const k = credKey(shown[i], path);
        if (turningOn) next[k] = shown[i]; else delete next[k];
      }
      return next;
    });
    lastPickedRef.current = index;
  };

  const pickAllShown = () => setSelected(() => {
    if (allShownPicked) return {};
    const next = {};
    shown.forEach(e => { next[credKey(e, path)] = e; });
    return next;
  });

  const exitSelect = () => { setSelectMode(false); setSelected({}); lastPickedRef.current = -1; };

  const bulkTargets = () => selectedRows.map(e => ({
    name: e.name, scope: e.scope,
    path: e.scope === 'global' ? '' : (path || ''),
  }));

  // One request, one aggregated result. Reporting "12 done" when 3 failed is
  // the failure mode worth designing against, so a partial run says so.
  const runBulk = async (action, label) => {
    const rows = selectedRows;
    if (!rows.length) return;
    setBulkBusy(true);
    try {
      const data = await credApi.batch(action, bulkTargets());
      const ok = (data && data.ok_count) || 0;
      const bad = (data && data.fail_count) || 0;
      if (action === 'check') {
        setChecks(prev => {
          const next = Object.assign({}, prev);
          ((data && data.results) || []).forEach(r => {
            if (!r.ok) return;
            const match = rows.find(e => e.name === r.name && e.scope === r.scope);
            if (match) next[credKey(match, path)] = { resolvable: !!r.resolvable, fingerprint: '' };
          });
          return next;
        });
      }
      if (bad) {
        const first = ((data && data.results) || []).find(r => !r.ok) || {};
        notify(`${label}: ${ok} ok, ${bad} failed — ${first.name || '?'}: ${first.error || 'unknown'}`, 'err');
      } else {
        notify(`${label}: ${ok} ${ok === 1 ? 'entry' : 'entries'}`);
      }
      if (action !== 'check') { exitSelect(); load(); if (onChanged) onChanged(); }
    } catch (e) { notify(apiErr(e), 'err'); }
    setBulkBusy(false);
  };

  const bulkDelete = () => {
    const rows = selectedRows;
    if (!rows.length) return;
    setConfirm(credBulkDeleteConfirm(rows, ownerLabel,
      () => runBulk('delete', 'Deleted')));
  };

  // Metadata only, and built from what the browser already holds — there is
  // no route that could hand a value to a CSV even if one asked.
  const bulkExport = () => {
    const cols = ['name', 'scope', 'project', 'type', 'storage', 'env_var',
      'inject', 'agent_readable', 'use_count', 'last_used', 'created', 'source', 'description'];
    const esc = (v) => `"${String(v === undefined || v === null ? '' : v).replace(/"/g, '""')}"`;
    const lines = [cols.join(',')].concat(selectedRows.map(e => cols.map(c => esc(
      c === 'project' ? (e.scope === 'global' ? 'global vault' : (projectName || path || ''))
        : c === 'source' ? credSourcePath(e)
          : e[c])).join(',')));
    const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `c3-credentials-${isGlobal ? 'global' : (projectName || 'project')}.csv`;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
    notify(`Exported ${selectedRows.length} rows (metadata only)`);
  };

  const openResync = (envPath) => {
    setImportPreset({ envPath });
    setImportOpen(true);
  };

  const openMenu = (entry, x, y) => setMenu({ x, y, entry });
  const menuItems = menu && credMenuItems({
    entry: menu.entry, check: checks[credKey(menu.entry, path)],
    cb: {
      open: () => { setReplaceOnOpen(false); setDrawer(menu.entry); },
      check: () => checkEntry(menu.entry),
      replace: () => { setReplaceOnOpen(true); setDrawer(menu.entry); },
      toggle: (f) => toggleFlag(menu.entry, f),
      remove: () => removeEntry(menu.entry),
      audit: () => setAuditFor(menu.entry.name),
    },
  });

  return (
    <div className="fade-up">
      {/* Toolbar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 6, flex: '1 1 220px', minWidth: 180,
          border: `1px solid ${T.border}`, borderRadius: 6, padding: '0 9px',
          background: T.surfaceAlt,
        }}>
          <I name="search" size={12} color={T.textMuted} />
          <input value={filter} onChange={e => setFilter(e.target.value)}
            ref={filterRef}
            onKeyDown={e => {
              // Same layering rule as the hub search: topmost layer first.
              if (e.key === 'Escape' && !drawer && !menu && !confirm) setFilter('');
            }}
            placeholder="Filter — name, description, or scope:global inject:true source:.env"
            style={{
              flex: 1, background: 'transparent', border: 'none', outline: 'none',
              color: T.text, fontSize: 12, padding: '6px 0',
            }} autoComplete="off" spellCheck={false} />
          {filter && (
            <button onClick={() => setFilter('')} aria-label="Clear filter"
              style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 2, display: 'flex' }}>
              <I name="xSmall" size={11} color={T.textMuted} />
            </button>
          )}
        </div>
        <select value={sort} onChange={e => setSort(e.target.value)} title="Sort"
          style={drillFieldStyle({ width: 132, fontSize: 11.5 })}>
          <option value="name">sort: name</option>
          <option value="recent">sort: last used</option>
          <option value="used">sort: most used</option>
          <option value="exposure">sort: exposure</option>
          <option value="created">sort: newest</option>
        </select>
        <button className="btn"
          onClick={() => { exitSelect(); setAuditFor(auditFor === null ? '' : null); }}
          title="Every change and use recorded for these credentials" style={{
            background: auditFor !== null ? T.accent : T.surfaceAlt,
            color: auditFor !== null ? T.bg : T.text,
            border: auditFor !== null ? 'none' : `1px solid ${T.border}`,
            padding: '5px 11px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
          }}>Audit</button>
        <button className="btn" onClick={() => (selectMode ? exitSelect() : setSelectMode(true))}
          title="Select multiple entries for a bulk action" style={{
            background: selectMode ? T.accent : T.surfaceAlt,
            color: selectMode ? T.bg : T.text,
            border: selectMode ? 'none' : `1px solid ${T.border}`,
            padding: '5px 11px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
          }}>{selectMode ? 'Cancel select' : 'Select'}</button>
        <button className="btn" onClick={() => { setImportPreset(null); setImportOpen(!importOpen); }} style={{
          background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
          padding: '5px 11px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
        }}>Import .env</button>
        <button className="btn" onClick={openCreate} style={{
          background: T.accent, color: T.bg, border: 'none', fontWeight: 600,
          padding: '5px 11px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
        }}>+ Add credential</button>
      </div>
      {/* Narrowing chips — always on, not only while searching. Each active
          chip must pass, so ticking two narrows instead of widening. */}
      {auditFor === null && entries.length > 0 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 7 }}>
          {CRED_CHIPS.map((c) => {
            const on = !!chips[c.id];
            const n = entries.filter(c.test).length;
            return (
              <button key={c.id} className="btn"
                onClick={() => setChips(prev => {
                  const next = Object.assign({}, prev);
                  if (next[c.id]) delete next[c.id]; else next[c.id] = true;
                  return next;
                })}
                style={{
                  background: on ? `${T.accent}22` : 'transparent',
                  color: on ? T.accent : (n ? T.textDim : T.textMuted),
                  border: `1px solid ${on ? T.accent + '88' : T.border}`,
                  borderRadius: 999, padding: '2px 10px', fontSize: 11,
                  cursor: 'pointer', opacity: n || on ? 1 : 0.5,
                }}>{c.label} {n}</button>
            );
          })}
        </div>
      )}

      <div style={{ fontSize: 11, color: T.textDim, marginBottom: 6 }}>
        {isGlobal ? 'Shared vault — entries visible in every C3 project.'
          : `Merged view for ${projectName || 'this project'} — project entries override same-named globals.`}
        {entries.length > 0 && (
          <span> · showing {shown.length} of {entries.length}
            {entries.filter(e => e.agent_readable).length > 0
              && ` · ${entries.filter(e => e.agent_readable).length} agent-readable`}
            {selectMode && ` · ${selectedRows.length} selected`}
          </span>
        )}
      </div>

      {auditFor === null && !loading && sources.length > 0 && (
        <CredSourcesStrip sources={sources} busy={busy || bulkBusy}
          onResync={openResync} />
      )}

      {error && (
        <div style={{
          padding: '8px 12px', borderRadius: 6, marginBottom: 10, fontSize: 12,
          background: `${T.error}22`, color: T.error, border: `1px solid ${T.error}55`,
        }}>{error}</div>
      )}

      {/* .env import */}
      {importOpen && (
        <HubCredsImportPanel
          key={(importPreset && importPreset.envPath) || 'new'}
          T={T}
          scopes={isGlobal ? ['global'] : ['project', 'global']}
          envPath={importPreset ? importPreset.envPath : ''}
          inputStyle={inputStyle}
          labelStyle={labelStyle}
          post={body => api.post('/api/projects/credentials/import', withPath(body))}
          onDone={msg => { setImportOpen(false); setImportPreset(null); done(msg); }}
        />
      )}

      {/* Create / edit form */}
      {form && (
        <div style={{
          border: `1px solid ${T.accent}55`, borderRadius: 8, padding: 12,
          marginBottom: 12, background: T.surface,
        }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: T.text, marginBottom: 10 }}>
            {isGlobal ? 'New global credential' : 'New credential'}
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <div>
              <span style={labelStyle}>Name (env-var safe)</span>
              <input value={form.name} autoFocus
                onChange={e => setForm(Object.assign({}, form, { name: e.target.value }))}
                style={inputStyle} autoComplete="off" spellCheck={false} />
            </div>
            <div>
              <span style={labelStyle}>Scope</span>
              {isGlobal ? (
                <div style={Object.assign({}, inputStyle, { color: T.textMuted })}>global (all C3 projects)</div>
              ) : (
                <select value={form.scope}
                  onChange={e => setForm(Object.assign({}, form, { scope: e.target.value }))}
                  style={inputStyle}>
                  <option value="project">project (this project only)</option>
                  <option value="global">global (all C3 projects)</option>
                </select>
              )}
            </div>
            {CREDS_STRUCTURED[form.type] ? (
              <div style={{ gridColumn: '1 / -1' }}>
                <span style={labelStyle}>Fields (write-only — never echoed back)</span>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                  {[...CREDS_STRUCTURED[form.type].required,
                    ...CREDS_STRUCTURED[form.type].optional].map(fname => (
                    <div key={fname}>
                      <span style={Object.assign({}, labelStyle, { marginBottom: 2 })}>
                        {fname}
                        {CREDS_STRUCTURED[form.type].required.includes(fname) ? '' : ' (optional)'}
                      </span>
                      <input
                        type={CREDS_STRUCTURED[form.type].hidden.includes(fname) ? 'password' : 'text'}
                        value={(form.fields || {})[fname] || ''}
                        onChange={e => setForm(Object.assign({}, form,
                          { fields: Object.assign({}, form.fields, { [fname]: e.target.value }) }))}
                        style={inputStyle} autoComplete="new-password" spellCheck={false} />
                    </div>
                  ))}
                </div>
              </div>
            ) : (
            <div style={{ gridColumn: '1 / -1' }}>
              <span style={labelStyle}>Value (write-only — never echoed back)</span>
              {form.type === 'multiline' ? (
                <textarea rows={4} value={form.value}
                  onChange={e => setForm(Object.assign({}, form, { value: e.target.value }))}
                  style={Object.assign({}, inputStyle, { fontFamily: 'monospace', resize: 'vertical' })}
                  autoComplete="new-password" spellCheck={false} />
              ) : (
                <input type="password" value={form.value}
                  onChange={e => setForm(Object.assign({}, form, { value: e.target.value }))}
                  style={inputStyle} autoComplete="new-password" />
              )}
            </div>
            )}
            <div>
              <span style={labelStyle}>Type</span>
              <select value={form.type}
                onChange={e => setForm(Object.assign({}, form, { type: e.target.value }))}
                style={inputStyle}>
                <option value="token">token — single secret</option>
                <option value="env">env — env-style value</option>
                <option value="multiline">multiline — .env blob / PEM</option>
                <option value="card">card — credit/debit card (inject-only)</option>
                <option value="address">address — postal address (inject-only)</option>
                <option value="identity">identity — personal info (inject-only)</option>
                <option value="login">login — website login (inject-only, storage only)</option>
              </select>
            </div>
            <div>
              <span style={labelStyle}>Env var at injection (default: name)</span>
              <input value={form.env_var}
                onChange={e => setForm(Object.assign({}, form, { env_var: e.target.value }))}
                style={inputStyle} autoComplete="off" spellCheck={false} />
            </div>
            <div style={{ gridColumn: '1 / -1' }}>
              <span style={labelStyle}>Description</span>
              <input value={form.description}
                onChange={e => setForm(Object.assign({}, form, { description: e.target.value }))}
                style={inputStyle} autoComplete="off" />
            </div>
          </div>
          {CREDS_STRUCTURED[form.type] ? (
            <div style={{
              marginTop: 10, padding: '6px 10px', borderRadius: 6, fontSize: 11,
              background: `${T.accent}15`, color: T.textMuted, border: `1px solid ${T.border}`,
            }}>
              🔒 {form.type} entries are inject-only: the agent can use single fields
              (<span className="mono">{'{{cred:NAME.field}}'}</span> /{' '}
              <span className="mono">env_creds='NAME.field'</span>) but can never
              reveal them, and they never auto-inject.
              {form.type === 'login' && (
                <div style={{ marginTop: 4 }}>
                  Storage only — C3 has no browser and does not log in to anything.{' '}
                  <span className="mono">canonical_origin</span> is https-only and
                  stored normalized so a separate out-of-process runner can pin this
                  credential to exactly one origin before typing it.{' '}
                  <span className="mono">totp_secret</span> is base32.
                </div>
              )}
            </div>
          ) : (
          <div style={{ display: 'flex', gap: 18, marginTop: 10, fontSize: 12, color: T.text }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
              <input type="checkbox" checked={!!form.inject}
                onChange={e => setForm(Object.assign({}, form, { inject: e.target.checked }))} />
              auto-inject into every c3_shell run
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
              <input type="checkbox" checked={!!form.agent_readable}
                onChange={e => setForm(Object.assign({}, form, { agent_readable: e.target.checked }))} />
              agent_readable
            </label>
          </div>
          )}
          {form.agent_readable && (
            <div style={{
              marginTop: 8, padding: '6px 10px', borderRadius: 6, fontSize: 11,
              background: `${T.warn}22`, color: T.warn, border: `1px solid ${T.warn}55`,
            }}>
              ⚠ The agent will be able to reveal this value into its context and
              conversation transcripts. Leave off for injection-only use.
            </div>
          )}
          <div style={{ display: 'flex', gap: 10, marginTop: 12 }}>
            {(() => {
              const hasPayload = CREDS_STRUCTURED[form.type]
                ? !!Object.keys(credsTypedFields(form.fields)).length
                : !!form.value;
              const blocked = busy || !form.name.trim() || !hasPayload;
              return (
                <button className="btn" disabled={blocked}
                  onClick={saveForm} style={{
                    background: T.accent, color: T.bg, border: 'none', fontWeight: 600,
                    padding: '5px 15px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
                    opacity: blocked ? 0.5 : 1,
                  }}>Create</button>
              );
            })()}
            <button className="btn" onClick={() => setForm(null)} style={{
              background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
              padding: '5px 15px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
            }}>Cancel</button>
          </div>
        </div>
      )}

      {/* Audit takes over the list area rather than opening beside it: the
          two answer different questions and stacking them buries both. */}
      {auditFor !== null ? (
        <CredAuditView path={path || null} globalOnly={isGlobal}
          projectName={projectName || (isGlobal ? 'the global vault' : '')}
          initialName={auditFor} />
      ) : loading ? (
        <div style={{ color: T.textMuted, fontSize: 13 }}>Loading…</div>
      ) : entries.length === 0 ? (
        <div style={{
          border: `1px dashed ${T.border}`, borderRadius: 8, padding: 26,
          textAlign: 'center', color: T.textMuted, fontSize: 13,
        }}>
          {isGlobal
            ? <span>No global credentials yet. Add one here or run{' '}
              <span className="mono">c3 creds set NAME --global</span>.</span>
            : <span>No credentials registered for this project.</span>}
        </div>
      ) : shown.length === 0 ? (
        <div style={{
          border: `1px dashed ${T.border}`, borderRadius: 8, padding: 22,
          textAlign: 'center', color: T.textMuted, fontSize: 12.5,
        }}>
          Nothing matches {filter
            ? <span className="mono" style={{ color: T.text }}>{filter}</span>
            : 'the active filters'}.{' '}
          <span onClick={clearNarrowing} style={{ color: T.accent, cursor: 'pointer' }}>Clear filters</span>
        </div>
      ) : (
        <div style={{ border: `1px solid ${T.border}`, borderRadius: 8, overflow: 'hidden' }}>
          {selectMode && (
            <label style={{
              display: 'flex', alignItems: 'center', gap: 10, padding: '7px 12px',
              background: T.surfaceAlt, fontSize: 11.5, color: T.textMuted,
              cursor: 'pointer',
            }}>
              <input type="checkbox" checked={allShownPicked} onChange={pickAllShown} />
              Select all {shown.length} shown
            </label>
          )}
          {shown.map((entry, i) => (
            <CredRow key={credKey(entry, path)} entry={entry} striped={!!(i % 2)}
              check={checks[credKey(entry, path)]}
              selectMode={selectMode}
              picked={!!selected[credKey(entry, path)]}
              onPick={(shiftKey) => togglePick(entry, i, shiftKey)}
              onOpen={() => setDrawer(entry)}
              onMenu={(x, y) => openMenu(entry, x, y)} />
          ))}
        </div>
      )}

      {auditFor === null && selectMode && selectedRows.length > 0 && (
        <CredBulkBar count={selectedRows.length} busy={bulkBusy}
          onAction={runBulk} onDelete={bulkDelete} onExport={bulkExport}
          onCancel={exitSelect} />
      )}

      {menu && <CredMenu x={menu.x} y={menu.y} items={menuItems} onClose={() => setMenu(null)} />}
      {drawer && (
        <CredDrawer entry={drawer} path={path} projectName={projectName}
          initialReplace={replaceOnOpen}
          onClose={() => { setDrawer(null); setReplaceOnOpen(false); }}
          onChanged={() => { load(); if (onChanged) onChanged(); }} />
      )}
      {confirm && <CredConfirm spec={confirm} onClose={() => setConfirm(null)} />}
    </div>
  );
}

// ── Cross-project search results ───────────────────────────────
// Grouped by credential name so "where is STRIPE_KEY defined" is one
// glance instead of forty accordions. Every definition is actionable.
function CredSearchResults({ groups, total, selected, setSelected, onOpen, onMenu }) {
  if (!groups.length) {
    return (
      <div style={{
        border: `1px dashed ${T.border}`, borderRadius: 8, padding: 30,
        textAlign: 'center', color: T.textMuted, fontSize: 13,
      }}>No credential matches that query.</div>
    );
  }
  let flat = -1;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div style={{ fontSize: 11, color: T.textDim }}>
        {total} definition{total === 1 ? '' : 's'} across {groups.length} name{groups.length === 1 ? '' : 's'}
        {' '}· ↑↓ to move, ↵ to open, right-click for actions
      </div>
      {groups.map(g => (
        <div key={g.name} style={{
          border: `1px solid ${T.border}`, borderRadius: 8,
          background: T.surface, overflow: 'hidden',
        }}>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8, padding: '8px 13px',
            background: T.surfaceAlt, fontSize: 12,
          }}>
            <I name="lock" size={13} color={T.textMuted} />
            <span className="mono" style={{ fontWeight: 700, color: T.text }}>{g.name}</span>
            <Badge color={T.textMuted}>
              {g.defs.length} definition{g.defs.length === 1 ? '' : 's'}
            </Badge>
            {g.defs.some(d => d.entry.agent_readable) && <Badge color={T.error}>agent-readable</Badge>}
            {g.defs.some(d => d.entry.inject) && <Badge color={T.warn}>inject</Badge>}
            {g.defs.length > 1 && <Badge color={T.warn}>overridden</Badge>}
          </div>
          {g.defs.map(def => {
            flat += 1;
            const mine = flat;
            const e = def.entry;
            const open = (ev) => { ev.stopPropagation(); onOpen(def); };
            return (
              <div key={`${def.projectPath}|${e.scope}`} tabIndex={0} role="button"
                onClick={open}
                onMouseEnter={() => setSelected(mine)}
                onContextMenu={(ev) => { ev.preventDefault(); ev.stopPropagation(); onMenu(def, ev.clientX, ev.clientY); }}
                onKeyDown={(ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onOpen(def); } }}
                style={{
                  display: 'flex', alignItems: 'center', gap: 9, padding: '8px 13px',
                  borderTop: `1px solid ${T.border}`, cursor: 'pointer', fontSize: 12,
                  outline: 'none',
                  background: selected === mine ? `${T.accent}12` : 'transparent',
                  borderLeft: `2px solid ${selected === mine ? T.accent : 'transparent'}`,
                }}>
                <span style={{
                  color: def.projectPath ? T.text : T.accent, fontWeight: 600,
                  minWidth: 150, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{def.projectName}</span>
                <Badge color={e.scope === 'global' ? T.accent : T.blue}>{e.scope}</Badge>
                {!!e.shadows_global && <Badge color={T.warn}>overrides global</Badge>}
                <span className="mono" style={{ fontSize: 11, color: T.textDim }}>
                  {e.type || 'token'} · ••••{e.value_len}
                </span>
                {e.env_var && (
                  <span className="mono" style={{ fontSize: 11, color: T.textDim }}>→ ${e.env_var}</span>
                )}
                {!!e.inject && <Badge color={T.warn}>inject</Badge>}
                {!!e.agent_readable && <Badge color={T.error}>agent-readable</Badge>}
                <div style={{ flex: 1 }} />
                <span style={{ fontSize: 11, color: T.textDim }}>
                  used {e.use_count || 0}× · {credWhen(e.last_used)}
                </span>
                <I name="chevron" size={13} color={T.textMuted} />
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}

// Top-level mainView='creds' page: search + Global vault | Projects sub-tabs.
function HubCredentials({ projects, onOpenDrill }) {
  const [sub, setSub] = useState('global');
  // Set when arriving from a row's "View audit" so the timeline lands already
  // filtered to that credential rather than dumping every event.
  const [auditName, setAuditName] = useState('');
  const [ov, setOv] = useState(null);
  const [ovErr, setOvErr] = useState(null);
  const [openPath, setOpenPath] = useState('');   // single-open accordion
  const [q, setQ] = useState('');
  const [scopeChip, setScopeChip] = useState('all');
  const [sort, setSort] = useState('name');
  const [sel, setSel] = useState(0);
  const [projFilter, setProjFilter] = useState('');
  const [drawer, setDrawer] = useState(null);     // {entry, path, projectName}
  const [menu, setMenu] = useState(null);         // {x, y, def}
  const [checks, setChecks] = useState({});       // "path|name" -> check
  const [confirm, setConfirm] = useState(null);
  const searchRef = useRef(null);

  const loadOverview = useCallback(async () => {
    setOvErr(null);
    try { setOv(await credApi.overview()); }
    catch (e) { setOvErr(apiErr(e)); }
  }, []);
  // The overview backs both the Projects tab and the search index, so it is
  // fetched once up front rather than lazily per sub-tab.
  useEffect(() => { loadOverview(); }, [loadOverview]);

  const norm = (s) => String(s || '').replace(/\\/g, '/').toLowerCase();
  const findProject = (p) => (projects || []).find(x => norm(x.path) === norm(p));

  // ── Search index: every definition, global + project-scoped ──
  const records = useMemo(() => {
    const out = [];
    ((((ov || {}).global) || {}).entries || []).forEach(e =>
      out.push({ entry: e, projectName: 'Global vault', projectPath: '' }));
    ((ov || {}).projects || []).forEach(p => (p.entries || []).forEach(e =>
      out.push({ entry: e, projectName: p.name || p.path, projectPath: p.path })));
    return out;
  }, [ov]);

  const query = useMemo(() => parseCredQuery(q), [q]);
  const searching = !query.empty;

  const groups = useMemo(() => {
    if (!searching) return [];
    const hits = records.filter(r => {
      if (scopeChip === 'global' && r.projectPath) return false;
      if (scopeChip === 'project' && !r.projectPath) return false;
      return credRecordMatches(r, query);
    });
    const byName = {};
    hits.forEach(r => { (byName[r.entry.name] = byName[r.entry.name] || []).push(r); });
    const list = Object.keys(byName).map(name => ({
      name,
      defs: byName[name].slice().sort((a, b) =>
        (a.projectPath ? 1 : 0) - (b.projectPath ? 1 : 0)
        || String(a.projectName).localeCompare(String(b.projectName))),
    }));
    const recentOf = (g) => g.defs.reduce((m, d) => (String(d.entry.last_used || '') > m ? String(d.entry.last_used || '') : m), '');
    const usedOf = (g) => g.defs.reduce((m, d) => m + (d.entry.use_count || 0), 0);
    const riskOf = (g) => g.defs.reduce((m, d) => Math.max(m, credRisk(d.entry)), 0);
    if (sort === 'recent') list.sort((a, b) => recentOf(b).localeCompare(recentOf(a)) || a.name.localeCompare(b.name));
    else if (sort === 'used') list.sort((a, b) => usedOf(b) - usedOf(a) || a.name.localeCompare(b.name));
    else if (sort === 'exposure') list.sort((a, b) => riskOf(b) - riskOf(a) || a.name.localeCompare(b.name));
    else if (sort === 'spread') list.sort((a, b) => b.defs.length - a.defs.length || a.name.localeCompare(b.name));
    else list.sort((a, b) => a.name.localeCompare(b.name));
    return list;
  }, [records, query, searching, scopeChip, sort]);

  const flatDefs = useMemo(() => groups.reduce((acc, g) => acc.concat(g.defs), []), [groups]);
  useEffect(() => { setSel(0); }, [q, scopeChip, sort]);

  // `/` and Ctrl/Cmd-K focus search; ↑↓ walk results; ↵ opens one.
  useEffect(() => {
    const onKey = (e) => {
      const tag = (e.target.tagName || '').toLowerCase();
      const typing = tag === 'input' || tag === 'textarea' || tag === 'select';
      if ((e.key === '/' && !typing) || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k')) {
        // Synchronous focus on purpose: preventDefault already stops the
        // character from being inserted, and deferring (rAF/timeout) would
        // silently no-op whenever the tab is backgrounded or occluded.
        e.preventDefault();
        if (searchRef.current) searchRef.current.focus();
        return;
      }
      if (!searching || drawer || menu || confirm) return;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (!flatDefs.length) return;
        e.preventDefault();
        setSel(prev => (prev + (e.key === 'ArrowDown' ? 1 : -1) + flatDefs.length) % flatDefs.length);
      } else if (e.key === 'Enter' && typing && flatDefs[sel]) {
        e.preventDefault();
        openDef(flatDefs[sel]);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [searching, flatDefs, sel, drawer, menu, confirm]);

  const openDef = (def, replace) => setDrawer({
    entry: def.entry, path: def.projectPath, projectName: def.projectName,
    replace: !!replace,
  });

  const defKey = (def) => `${def.projectPath}|${def.entry.name}`;
  const ownerOf = (def) => (def.projectPath ? def.projectName : 'the global vault (~/.c3)');

  const checkDef = async (def) => {
    try {
      const data = await credApi.check(def.projectPath, def.entry.name);
      setChecks(prev => Object.assign({}, prev, { [defKey(def)]: data }));
      notify(data && data.resolvable
        ? `${def.entry.name} resolves · ${data.fingerprint}`
        : `${def.entry.name} does not resolve`, data && data.resolvable ? 'ok' : 'warn');
    } catch (e) { notify(apiErr(e), 'err'); }
  };

  const flagDef = async (def, field, next) => {
    try {
      await credApi.save(def.projectPath, {
        name: def.entry.name, scope: def.entry.scope, [field]: next,
      });
      notify(`${field === 'inject' ? 'Auto-inject' : 'Agent access'} ${next ? 'enabled' : 'disabled'} for '${def.entry.name}'`);
      loadOverview();
    } catch (e) { notify(apiErr(e), 'err'); }
  };

  const menuItems = menu && credMenuItems({
    entry: menu.def.entry, check: checks[defKey(menu.def)],
    cb: {
      open: () => openDef(menu.def),
      check: () => checkDef(menu.def),
      replace: () => openDef(menu.def, true),
      toggle: (f) => {
        const def = menu.def;
        const next = !def.entry[f];
        if (!next) { flagDef(def, f, false); return; }
        setConfirm(credExposureConfirm(def.entry, f, def.entry.env_var, ownerOf(def),
          () => flagDef(def, f, true)));
      },
      remove: () => {
        const def = menu.def;
        setConfirm(credDeleteConfirm(def.entry, ownerOf(def), async () => {
          try {
            await credApi.remove(def.projectPath, def.entry.name, def.entry.scope);
            notify(`Deleted '${def.entry.name}'`);
            loadOverview();
          } catch (e) { notify(apiErr(e), 'err'); }
        }));
      },
      audit: () => {
        setAuditName(menu.def.entry.name);
        setSub('audit');
        setQ('');
      },
      openProject: menu.def.projectPath && findProject(menu.def.projectPath) && onOpenDrill
        ? () => onOpenDrill(findProject(menu.def.projectPath), 'creds')
        : null,
    },
  });

  const subBtn = (id, label) => (
    <button key={id} onClick={() => setSub(id)} style={{
      display: 'inline-flex', alignItems: 'center', gap: 6, height: 28,
      padding: '0 12px', border: 'none', cursor: 'pointer', fontSize: 12,
      background: sub === id ? T.accentDim : 'transparent',
      color: sub === id ? T.accent : T.textMuted,
      fontWeight: sub === id ? 700 : 400,
    }}>{label}</button>
  );

  const chip = (id, label) => (
    <button key={id} onClick={() => setScopeChip(id)} style={{
      padding: '3px 10px', borderRadius: 20, fontSize: 11, cursor: 'pointer',
      border: `1px solid ${scopeChip === id ? T.accent : T.border}`,
      background: scopeChip === id ? T.accentDim : 'transparent',
      color: scopeChip === id ? T.accent : T.textMuted,
    }}>{label}</button>
  );

  const projRows = ((ov || {}).projects || []).filter(row => {
    const f = projFilter.trim().toLowerCase();
    if (!f) return true;
    return `${row.name || ''} ${row.path || ''}`.toLowerCase().indexOf(f) !== -1;
  });
  const totalProjEntries = ((ov || {}).projects || []).reduce((n, r) => n + (r.entries || []).length, 0);
  const globalCount = ((((ov || {}).global) || {}).entries || []).length;

  return (
    <div className="fade-up" style={{ maxWidth: 1100 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
        <I name="lock" size={18} color={T.accent} />
        <span style={{ fontSize: 15, fontWeight: 600, color: T.text }}>Credentials</span>
        <div style={{
          display: 'inline-flex', marginLeft: 10, border: `1px solid ${T.border}`,
          borderRadius: 6, overflow: 'hidden',
        }}>
          {subBtn('global', `Global vault${ov ? ` (${globalCount})` : ''}`)}
          {subBtn('projects', `Projects${ov ? ` (${totalProjEntries})` : ''}`)}
          {subBtn('audit', 'Audit')}
        </div>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={loadOverview} title="Reload the cross-project index" style={{
          background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
          padding: '5px 11px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
          display: 'inline-flex', alignItems: 'center', gap: 6,
        }}><I name="refresh" size={12} /> Refresh</button>
      </div>

      {/* Cross-project search — always visible, above the sub-tabs */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 7, flex: '1 1 320px', minWidth: 240,
          border: `1px solid ${searching ? T.accent : T.border}`, borderRadius: 7,
          padding: '0 10px', background: T.surfaceAlt,
        }}>
          <I name="search" size={13} color={searching ? T.accent : T.textMuted} />
          <input ref={searchRef} value={q} onChange={e => setQ(e.target.value)}
            onKeyDown={e => {
              // Escape closes the topmost layer only — with a drawer, menu or
              // confirm open it must not also wipe the query behind them.
              if (e.key === 'Escape' && !drawer && !menu && !confirm) {
                setQ(''); e.currentTarget.blur();
              }
            }}
            placeholder="Search every project — name, description, or project:api scope:global agent:true"
            style={{
              flex: 1, background: 'transparent', border: 'none', outline: 'none',
              color: T.text, fontSize: 12.5, padding: '8px 0',
            }} autoComplete="off" spellCheck={false} />
          {q ? (
            <button onClick={() => setQ('')} aria-label="Clear search"
              style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 2, display: 'flex' }}>
              <I name="xSmall" size={12} color={T.textMuted} />
            </button>
          ) : (
            <span className="mono" style={{
              fontSize: 10, color: T.textDim, border: `1px solid ${T.border}`,
              borderRadius: 4, padding: '1px 5px',
            }}>/</span>
          )}
        </div>
        {searching && (
          <React.Fragment>
            {chip('all', 'All')}
            {chip('global', 'Global')}
            {chip('project', 'Project')}
            <select value={sort} onChange={e => setSort(e.target.value)}
              style={drillFieldStyle({ width: 140, fontSize: 11.5 })}>
              <option value="name">sort: name</option>
              <option value="spread">sort: most defined</option>
              <option value="recent">sort: last used</option>
              <option value="used">sort: most used</option>
              <option value="exposure">sort: exposure</option>
            </select>
          </React.Fragment>
        )}
      </div>

      <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 14, lineHeight: 1.5 }}>
        Values live in the OS keyring (large values in an encrypted sidecar), are
        submitted inbound-only and are <b>never</b> returned by any hub route — search
        indexes metadata only. Agents use them by name via{' '}
        <span className="mono">c3_shell env_creds</span> or{' '}
        <span className="mono">{'{{cred:NAME}}'}</span>, decoded only at the subprocess
        boundary. <b>Global</b> entries are visible in every C3 project;{' '}
        <b>project</b> entries override same-named globals in their project.
      </div>

      {ovErr && (
        <div style={{
          padding: '8px 12px', borderRadius: 6, fontSize: 12, marginBottom: 10,
          background: `${T.error}22`, color: T.error, border: `1px solid ${T.error}55`,
        }}>Failed to load the cross-project index: {ovErr}</div>
      )}

      {searching ? (
        !ov ? (
          <div style={{ color: T.textMuted, fontSize: 13 }}>Building index…</div>
        ) : (
          <CredSearchResults groups={groups} total={flatDefs.length}
            selected={sel} setSelected={setSel}
            onOpen={openDef}
            onMenu={(def, x, y) => setMenu({ def, x, y })} />
        )
      ) : sub === 'audit' ? (
        <CredAuditView path={null} initialName={auditName}
          onOpenDrill={onOpenDrill} />
      ) : sub === 'global' ? (
        <CredsManager path={null} onChanged={loadOverview} />
      ) : !ov ? (
        <div style={{ color: T.textMuted, fontSize: 13 }}>Loading…</div>
      ) : (ov.projects || []).length === 0 ? (
        <div style={{
          border: `1px dashed ${T.border}`, borderRadius: 8, padding: 26,
          textAlign: 'center', color: T.textMuted, fontSize: 13,
        }}>No projects registered in the hub.</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 7, maxWidth: 320,
            border: `1px solid ${T.border}`, borderRadius: 6, padding: '0 9px',
            background: T.surfaceAlt, marginBottom: 2,
          }}>
            <I name="filter" size={12} color={T.textMuted} />
            <input value={projFilter} onChange={e => setProjFilter(e.target.value)}
              placeholder="Filter projects…" style={{
                flex: 1, background: 'transparent', border: 'none', outline: 'none',
                color: T.text, fontSize: 12, padding: '6px 0',
              }} autoComplete="off" spellCheck={false} />
          </div>
          {projRows.map(row => {
            // Single-open accordion: only one CredsManager is ever mounted,
            // so N projects never means N concurrent fetchers going stale.
            const open = openPath === row.path;
            const proj = findProject(row.path);
            const muted = !row.initialized || row.error;
            const shadowing = (row.entries || []).filter(e => e.shadows_global).length;
            const exposed = (row.entries || []).filter(e => e.agent_readable).length;
            return (
              <div key={row.path} style={{
                border: `1px solid ${open ? T.borderHover : T.border}`, borderRadius: 8,
                background: T.surface, overflow: 'hidden',
              }}>
                <div onClick={() => !muted && setOpenPath(open ? '' : row.path)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 10, padding: '9px 14px',
                    cursor: muted ? 'default' : 'pointer', opacity: muted ? 0.55 : 1,
                    fontSize: 12,
                  }}>
                  <span style={{ color: T.textMuted, fontSize: 10, width: 10 }}>
                    {muted ? '' : (open ? '▾' : '▸')}
                  </span>
                  <span style={{ fontWeight: 600, color: T.text }}>{row.name || row.path}</span>
                  <span className="mono" style={{
                    color: T.textDim, fontSize: 11, overflow: 'hidden',
                    textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 340,
                  }}>{row.path}</span>
                  <div style={{ flex: 1 }} />
                  {row.error ? (
                    <Badge color={T.error}>error</Badge>
                  ) : !row.initialized ? (
                    <Badge color={T.textMuted}>not initialized</Badge>
                  ) : (
                    <React.Fragment>
                      {exposed > 0 && <Badge color={T.error}>{exposed} agent-readable</Badge>}
                      {shadowing > 0 && <Badge color={T.warn}>{shadowing} overriding global</Badge>}
                      <span style={{ color: T.textMuted, fontSize: 11 }}>
                        {row.entries.length} {row.entries.length === 1 ? 'entry' : 'entries'}
                      </span>
                      {proj && onOpenDrill && (
                        <button className="btn" onClick={(e) => { e.stopPropagation(); onOpenDrill(proj, 'creds'); }}
                          style={{
                            background: T.surfaceAlt, color: T.text, border: `1px solid ${T.border}`,
                            padding: '3px 9px', borderRadius: 6, fontSize: 11, cursor: 'pointer',
                          }}>Open drill</button>
                      )}
                    </React.Fragment>
                  )}
                </div>
                {row.error && (
                  <div style={{ padding: '0 14px 9px 34px', fontSize: 11, color: T.error }}>{row.error}</div>
                )}
                {open && !muted && (
                  <div style={{ padding: '4px 14px 14px', borderTop: `1px solid ${T.border}` }}>
                    <CredsManager path={row.path} projectName={row.name}
                      onChanged={loadOverview} />
                  </div>
                )}
              </div>
            );
          })}
          {projRows.length === 0 && (
            <div style={{
              border: `1px dashed ${T.border}`, borderRadius: 8, padding: 22,
              textAlign: 'center', color: T.textMuted, fontSize: 12.5,
            }}>No project matches “{projFilter}”.</div>
          )}
        </div>
      )}

      {menu && <CredMenu x={menu.x} y={menu.y} items={menuItems} onClose={() => setMenu(null)} />}
      {drawer && (
        <CredDrawer entry={drawer.entry} path={drawer.path} projectName={drawer.projectName}
          initialReplace={drawer.replace}
          onClose={() => setDrawer(null)} onChanged={loadOverview} />
      )}
      {confirm && <CredConfirm spec={confirm} onClose={() => setConfirm(null)} />}
    </div>
  );
}
