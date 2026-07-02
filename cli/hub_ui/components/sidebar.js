// ─── Sidebar ───────────────────────────────────────────────────
// Fixed filters (All / Active / Idle) plus hierarchical tag groups
// from buildTagTree. Collapsed mode (44px) keeps icon-only nav.

function HubSidebar({ projects, filter, setFilter, collapsed, setCollapsed }) {
  const activeCount = projects.filter(p => p.active).length;
  const idleCount = projects.length - activeCount;
  const groups = buildTagTree(projects);
  const hasGroups = Object.keys(groups).length > 0;

  const itemStyle = (selected) => ({
    display: 'flex', alignItems: 'center', gap: 8,
    padding: collapsed ? '7px 0' : '6px 10px',
    justifyContent: collapsed ? 'center' : 'flex-start',
    borderRadius: 6, cursor: 'pointer', fontSize: 12, userSelect: 'none',
    color: selected ? T.accent : T.textMuted,
    background: selected ? T.accentDim : 'transparent',
  });

  const fixedItem = (value, icon, label, count) => {
    const selected = filter === value;
    return (
      <div key={value} style={itemStyle(selected)} onClick={() => setFilter(value)}
        title={`${label} (${count})`}>
        <I name={icon} size={13} color={selected ? T.accent : T.textMuted} />
        {!collapsed && <span style={{ flex: 1, color: selected ? T.accent : T.text }}>{label}</span>}
        {!collapsed &&
          <span className="mono" style={{ fontSize: 11, color: selected ? T.accent : T.textDim }}>{count}</span>}
      </div>
    );
  };

  // Nested tag folders — click filters by `tag:<full>`. Collapsed mode
  // shows top-level folders only (children need the expanded rail).
  const renderGroups = (children, depth) =>
    Object.keys(children).sort((a, b) => a.localeCompare(b)).map(key => {
      const node = children[key];
      const value = `tag:${node.full}`;
      const selected = (filter || '').toLowerCase() === value.toLowerCase();
      return (
        <React.Fragment key={node.full}>
          <div onClick={() => setFilter(value)}
            title={`${node.full}${node.count ? ` (${node.count})` : ''}`}
            style={{ ...itemStyle(selected), paddingLeft: collapsed ? 0 : 10 + depth * 12 }}>
            <I name="folder" size={13} color={selected ? T.accent : T.textMuted} />
            {!collapsed && (
              <span style={{
                flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                color: selected ? T.accent : T.text,
              }}>{key}</span>
            )}
            {!collapsed && node.count > 0 &&
              <span className="mono" style={{ fontSize: 11, color: selected ? T.accent : T.textDim }}>{node.count}</span>}
          </div>
          {!collapsed && node.children && renderGroups(node.children, depth + 1)}
        </React.Fragment>
      );
    });

  return (
    <aside style={{
      width: collapsed ? 44 : 200, flexShrink: 0, minHeight: 0,
      display: 'flex', flexDirection: 'column',
      background: T.surface, borderRight: `1px solid ${T.border}`,
      transition: 'width .15s ease',
    }}>
      <nav style={{
        flex: 1, overflowY: 'auto', overflowX: 'hidden',
        padding: collapsed ? '12px 6px' : '14px 10px',
        display: 'flex', flexDirection: 'column', gap: 2,
      }}>
        {fixedItem('all', 'layers', 'All', projects.length)}
        {fixedItem('active', 'zap', 'Active', activeCount)}
        {fixedItem('idle', 'clock', 'Idle', idleCount)}

        {hasGroups && (
          <React.Fragment>
            {!collapsed && (
              <div style={{
                fontSize: 11, letterSpacing: 1.2, textTransform: 'uppercase',
                color: T.textDim, margin: '16px 4px 4px',
              }}>Groups</div>
            )}
            {collapsed && <div style={{ height: 1, background: T.border, margin: '10px 4px' }} />}
            {renderGroups(groups, 0)}
          </React.Fragment>
        )}
      </nav>

      <div onClick={() => setCollapsed(!collapsed)}
        title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        style={{
          borderTop: `1px solid ${T.border}`, padding: '9px 12px', cursor: 'pointer',
          display: 'flex', justifyContent: collapsed ? 'center' : 'flex-end',
        }}>
        <span style={{
          display: 'inline-flex',
          transform: collapsed ? 'rotate(-90deg)' : 'rotate(90deg)',
          transition: 'transform .15s ease',
        }}>
          <I name="chevron" size={13} color={T.textMuted} />
        </span>
      </div>
    </aside>
  );
}
