// ─── Project tree (list + grid) ────────────────────────────────
// The only consumer of ProjectCard. Groups registry entries by
// parent_path via buildProjectTree; list view renders indented child
// rows under each parent, grid view lists children compactly inside
// the parent card. Fetches the hub version once so cards can offer
// the "update" affordance when a project's c3_version is behind.

function ProjectTree({ projects, allProjects, view, loaded, onChanged, onOpenDrill, onOpenModal, onOpenDrawer }) {
  const [collapsed, setCollapsed] = useState({});   // parent path -> true when collapsed
  const [hubVersion, setHubVersion] = useState('');

  useEffect(() => {
    let live = true;
    api.get('/api/version')
      .then(v => { if (live) setHubVersion((v && v.c3_version) || ''); })
      .catch(() => { });
    return () => { live = false; };
  }, []);

  if (!loaded) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {[0, 1, 2].map(i => (
          <div key={i} style={{
            height: 44, borderRadius: 8, background: T.surface,
            border: `1px solid ${T.border}`, opacity: 0.6,
            animation: 'pulse 1.4s ease-in-out infinite',
          }} />
        ))}
      </div>
    );
  }

  if (projects.length === 0) {
    return (
      <div style={{ textAlign: 'center', padding: '64px 0', color: T.textMuted, fontSize: 13 }}>
        No projects yet — add one above.
      </div>
    );
  }

  const tree = buildProjectTree(projects);

  // Rollups reflect ALL registered children, even when filtering hides rows.
  const allChildrenOf = {};
  (allProjects || projects).forEach(p => {
    if (!p.parent_path) return;
    const key = (p.parent_path || '').toLowerCase();
    (allChildrenOf[key] = allChildrenOf[key] || []).push(p);
  });

  const toggle = (path) => setCollapsed(c => ({ ...c, [path]: !c[path] }));
  const common = { onChanged, onOpenDrill, onOpenModal, onOpenDrawer, view, hubVersion };

  if (view === 'grid') {
    return (
      <div style={{
        display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))',
        gap: 12, alignItems: 'start',
      }}>
        {tree.map(({ project, children }) => {
          const rollChildren = allChildrenOf[(project.path || '').toLowerCase()] || children;
          return (
            <ProjectCard key={project.path} p={project}
              rollup={rollChildren.length ? treeRollup(rollChildren) : null}
              childRows={children}
              {...common} />
          );
        })}
      </div>
    );
  }

  // List view: parent row, then indented child rows behind a hairline.
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {tree.map(({ project, children }) => {
        const rollChildren = allChildrenOf[(project.path || '').toLowerCase()] || children;
        const hasKids = rollChildren.length > 0;
        const expanded = !collapsed[project.path];
        return (
          <div key={project.path}>
            <ProjectCard p={project}
              rollup={hasKids ? treeRollup(rollChildren) : null}
              expanded={expanded}
              onToggleExpand={hasKids ? () => toggle(project.path) : null}
              {...common} />
            {hasKids && expanded && children.length > 0 && (
              <div style={{
                paddingLeft: 26, borderLeft: `1px solid ${T.border}`,
                marginTop: 6, display: 'flex', flexDirection: 'column', gap: 6,
              }}>
                {children.map(c => (
                  <ProjectCard key={c.path} p={c} isChild {...common} />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
