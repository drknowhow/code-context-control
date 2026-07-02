// ─── Add project modal ─────────────────────────────────────────
// Registers a project with the hub via POST /api/projects.
// C3 init for fresh projects happens later from the drill-in panel.

function AddProjectModal({ onClose, onChanged }) {
  const [path, setPath] = React.useState('');
  const [name, setName] = React.useState('');
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const submit = async () => {
    const p = path.trim();
    if (!p) { notify('Enter an absolute project path', 'warn'); return; }
    setBusy(true);
    try {
      const d = await api.post('/api/projects', { path: p, name: name.trim() || null });
      notify(`Registered ${(d && d.name) || name.trim() || p}`);
      onChanged();
      onClose();
    } catch (e) {
      notify(e.message || 'Failed to register project', 'err');
      setBusy(false);
    }
  };

  const onEnter = (e) => { if (e.key === 'Enter' && !busy) submit(); };

  const labelStyle = {
    fontSize: 11, letterSpacing: 1.2, textTransform: 'uppercase',
    color: T.textMuted, marginBottom: 6,
  };
  const inputStyle = {
    width: '100%', boxSizing: 'border-box', background: T.bg,
    border: `1px solid ${T.border}`, borderRadius: 6,
    padding: '8px 10px', fontSize: 12, color: T.text, outline: 'none',
  };

  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, background: '#00000090', zIndex: 300,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <div className="fade-up" onClick={e => e.stopPropagation()} style={{
        width: 460, maxWidth: '90vw', background: T.surface,
        border: `1px solid ${T.border}`, borderRadius: 10, padding: 24,
      }}>
        <div style={{ fontSize: 14, fontWeight: 700, color: T.text, marginBottom: 16 }}>
          Add project
        </div>

        <div style={{ marginBottom: 14 }}>
          <div style={labelStyle}>Absolute path</div>
          <input className="mono" autoFocus value={path} onChange={e => setPath(e.target.value)}
            onKeyDown={onEnter} placeholder="C:/projects/myapp" style={inputStyle} />
        </div>

        <div style={{ marginBottom: 14 }}>
          <div style={labelStyle}>Display name</div>
          <input value={name} onChange={e => setName(e.target.value)}
            onKeyDown={onEnter} placeholder="Optional — defaults to the folder name" style={inputStyle} />
        </div>

        <div style={{ fontSize: 11, color: T.textDim, lineHeight: 1.5, marginBottom: 18 }}>
          Projects without C3 initialized can be set up later from the project panel.
        </div>

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
          <Btn onClick={submit} disabled={busy}>{busy ? 'Adding…' : 'Add'}</Btn>
        </div>
      </div>
    </div>
  );
}
