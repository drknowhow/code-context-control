// ─── Summary bar ───────────────────────────────────────────────
// One row above the project tree: count, active-filter chip, search,
// list/grid toggle, conditional "Update all", and "+ Add project".

function SummaryBar({ projects, search, setSearch, view, setView, filter, setFilter, onUpdateAll, onAddProject }) {
  // Hub's own version — a project is outdated when its c3_version differs
  // (mirrors the old "Update All Outdated" logic in hub.html).
  const [hubVersion, setHubVersion] = React.useState('');
  React.useEffect(() => {
    let alive = true;
    api.get('/api/version')
      .then(v => { if (alive) setHubVersion(v.c3_version || ''); })
      .catch(() => { });
    return () => { alive = false; };
  }, []);

  const outdated = projects.filter(p =>
    p.update_available || (p.c3_version && hubVersion && p.c3_version !== hubVersion));

  const viewBtn = (name) => {
    const active = view === name;
    return (
      <button key={name} onClick={() => setView(name)} title={`${name} view`} style={{
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        width: 30, height: 28, border: 'none', cursor: 'pointer',
        background: active ? T.accentDim : 'transparent',
      }}>
        <I name={name} size={13} color={active ? T.accent : T.textMuted} />
      </button>
    );
  };

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
      <span style={{ fontSize: 12, color: T.textMuted, whiteSpace: 'nowrap' }}>
        {projects.length} project{projects.length === 1 ? '' : 's'}
      </span>

      {filter && filter !== 'all' && (
        <button className="mono" onClick={() => setFilter('all')} title="Clear filter" style={{
          display: 'inline-flex', alignItems: 'center', gap: 6, padding: '3px 9px',
          borderRadius: 999, border: `1px solid ${T.border}`, background: 'transparent',
          color: T.accent, fontSize: 11, cursor: 'pointer',
        }}>
          {filter.startsWith('tag:') ? filter.slice(4) : filter}
          <I name="xSmall" size={11} color={T.textMuted} />
        </button>
      )}

      <div style={{ flex: 1 }} />

      <input value={search} onChange={e => setSearch(e.target.value)}
        placeholder="Filter by name, path, IDE…" style={{
          width: 260, background: T.surface, border: `1px solid ${T.border}`,
          borderRadius: 6, padding: '7px 10px', fontSize: 12, color: T.text, outline: 'none',
        }} />

      <div style={{
        display: 'inline-flex', border: `1px solid ${T.border}`,
        borderRadius: 6, overflow: 'hidden',
      }}>
        {viewBtn('list')}
        {viewBtn('grid')}
      </div>

      {outdated.length > 0 &&
        <Btn variant="ghost" onClick={onUpdateAll}>Update all ({outdated.length})</Btn>}
      <Btn onClick={onAddProject}>+ Add project</Btn>
    </div>
  );
}
