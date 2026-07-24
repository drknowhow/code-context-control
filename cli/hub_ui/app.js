// ─── Hub App root ──────────────────────────────────────────────
const BUILD_TIME = "2026-07-02 HUB-v2";
const { useState, useEffect, useCallback, useRef, useMemo } = React;

function App() {
  // Theme
  const [darkMode, setDarkMode] = useState(true);
  T = darkMode ? DARK : LIGHT;
  useEffect(() => {
    document.body.dataset.theme = darkMode ? 'dark' : 'light';
    document.body.style.background = T.bg;
    document.body.style.color = T.text;
  }, [darkMode]);

  // Core state
  const [hubConfig, setHubConfig] = useState({});
  const [version, setVersion] = useState('');
  const [projects, setProjects] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [mainView, setMainView] = useState('projects'); // projects | board
  const [filter, setFilter] = useState('all');          // all | active | idle | tag:<x>
  const [search, setSearch] = useState('');
  const [view, setView] = useState('list');             // list | grid
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [drill, setDrill] = useState(null);             // project object or null
  const [drillTab, setDrillTab] = useState('overview');
  const [modal, setModal] = useState(null);             // {name, project?, props?}
  const [searchOpen, setSearchOpen] = useState(false);
  const [drawerProject, setDrawerProject] = useState(null);

  const loadProjects = useCallback(async () => {
    try {
      const list = await api.get('/api/projects');
      setProjects(Array.isArray(list) ? list : []);
    } catch { /* keep last good list */ }
    setLoaded(true);
  }, []);

  const loadConfig = useCallback(async () => {
    try {
      const cfg = await api.get('/api/hub/config');
      setHubConfig(cfg);
      if (cfg.theme) setDarkMode(cfg.theme !== 'light');
      if (cfg.projects_view === 'grid') setView('grid');
      if (cfg.sidebar_collapsed != null) setSidebarCollapsed(!!cfg.sidebar_collapsed);
      if (cfg.sidebar_group) setFilter(cfg.sidebar_group);
      if (cfg.main_view === 'board') setMainView('board');
    } catch { }
    try { const v = await api.get('/api/version'); setVersion(v.c3_version || ''); } catch { }
  }, []);

  useEffect(() => { loadConfig(); loadProjects(); }, []);
  usePoll(loadProjects, 5000);

  // Persisted preferences
  const toggleTheme = () => {
    const next = !darkMode;
    setDarkMode(next);
    api.post('/api/hub/config', { theme: next ? 'dark' : 'light' }).catch(() => { });
  };
  const changeView = (v) => {
    setView(v);
    api.post('/api/hub/config', { projects_view: v }).catch(() => { });
  };
  const changeFilter = (f) => {
    setFilter(f);
    api.post('/api/hub/config', { sidebar_group: f }).catch(() => { });
  };
  const changeSidebar = (c) => {
    setSidebarCollapsed(c);
    api.post('/api/hub/config', { sidebar_collapsed: c }).catch(() => { });
  };
  const changeMainView = (v) => {
    setMainView(v);
    api.post('/api/hub/config', { main_view: v }).catch(() => { });
  };

  // Keyboard: Ctrl/Cmd-K opens cross-project search, Esc closes overlays
  useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setSearchOpen(true);
      }
      if (e.key === 'Escape') { setSearchOpen(false); setModal(null); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const openDrill = (p, tab = 'overview') => { setDrill(p); setDrillTab(tab); };
  const openModal = (name, project = null, props = {}) => setModal({ name, project, props });

  const visible = useMemo(() => filterProjects(projects, filter, search),
    [projects, filter, search]);
  const activeCount = projects.filter(p => p.active).length;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', background: T.bg, color: T.text }}>
      <TopBar version={version} activeCount={activeCount} darkMode={darkMode}
        hubConfig={hubConfig} mainView={mainView} setMainView={changeMainView}
        onToggleTheme={toggleTheme}
        onOpenSettings={() => openModal('settings')}
        onOpenSearch={() => setSearchOpen(true)}
        onRefresh={loadProjects} />
      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
        <HubSidebar projects={projects} filter={filter} setFilter={changeFilter}
          collapsed={sidebarCollapsed} setCollapsed={changeSidebar} />
        <main style={{
          flex: 1, minWidth: 0, overflowY: 'auto', padding: '18px 22px',
          display: 'flex', flexDirection: 'column', gap: 12,
        }}>
          {mainView === 'board' ? (
            <TaskBoard projects={projects} onOpenDrill={openDrill} />
          ) : (
            <React.Fragment>
              <SummaryBar projects={projects} search={search} setSearch={setSearch}
                view={view} setView={changeView} filter={filter} setFilter={changeFilter}
                onUpdateAll={() => openModal('batch')}
                onAddProject={() => openModal('add')} />
              <ProjectTree projects={visible} allProjects={projects} view={view} loaded={loaded}
                onChanged={loadProjects} onOpenDrill={openDrill} onOpenModal={openModal}
                onOpenDrawer={setDrawerProject} />
            </React.Fragment>
          )}
        </main>
      </div>
      {drawerProject &&
        <SessionDrawer project={drawerProject} onClose={() => setDrawerProject(null)} />}
      {drill &&
        <DrillPanel project={drill} tab={drillTab} setTab={setDrillTab} projects={projects}
          onClose={() => setDrill(null)} onChanged={loadProjects} onOpenModal={openModal} />}
      <GlobalSearch open={searchOpen} onClose={() => setSearchOpen(false)} projects={projects}
        onOpenProject={(p, tab) => { setSearchOpen(false); openDrill(p, tab || 'overview'); }} />
      {modal &&
        <HubModals modal={modal} projects={projects}
          onClose={() => setModal(null)} onChanged={loadProjects} />}
      <ToastHost />
    </div>
  );
}

ReactDOM.render(<App />, document.getElementById('root'));
