// ─── Memory ───────────────────────────────
// Globals: T, I, GlowDot, Badge, StatBox, Btn, api, timeAgo, localDate, useState, useEffect, useRef

const CAT_COLORS_GRAPH = {
  general: "#9aa0a6",
  architecture: "#4c8bf5",
  convention: "#b388ff",
  bug: "#ef5350",
  preference: "#ffb74d",
};

const GRAPH_LEGEND = [
  { key: "general", label: "General", desc: "uncategorized facts" },
  { key: "architecture", label: "Architecture", desc: "system design, structure" },
  { key: "convention", label: "Convention", desc: "coding style, patterns" },
  { key: "bug", label: "Bug", desc: "defects, issues" },
  { key: "preference", label: "Preference", desc: "user choices" },
];

const MemoryGraph = ({ onSelectFact }) => {
  const containerRef = useRef(null);
  const cyRef = useRef(null);
  const [stats, setStats] = useState(null);
  const [minWeight, setMinWeight] = useState(0);
  const [includeNonFact, setIncludeNonFact] = useState(false);
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showHelp, setShowHelp] = useState(true);

  const fitGraph = () => { if (cyRef.current) cyRef.current.fit(undefined, 40); };
  const relayout = () => {
    if (!cyRef.current) return;
    try { cyRef.current.layout({ name: 'fcose', animate: true, quality: 'default', nodeRepulsion: 4500 }).run(); }
    catch (e) { cyRef.current.layout({ name: 'cose', animate: true }).run(); }
  };

  const loadGraph = async () => {
    try {
      const qs = `?min_weight=${minWeight}&include_non_fact=${includeNonFact ? 1 : 0}`;
      const data = await api.get('/api/memory/graph' + qs);
      setStats(data.stats || {});

      const elements = [
        ...data.nodes.map(n => ({
          data: {
            id: n.id,
            label: n.label || n.id,
            kind: n.kind,
            category: n.category || "general",
            relevance: n.relevance || 0,
          },
        })),
        ...data.edges.map((e, i) => ({
          data: {
            id: `e${i}`,
            source: e.src,
            target: e.dst,
            type: e.type,
            weight: e.weight,
          },
        })),
      ];

      if (!cyRef.current && containerRef.current && window.cytoscape) {
        cyRef.current = window.cytoscape({
          container: containerRef.current,
          elements,
          style: [
            {
              selector: 'node',
              style: {
                'background-color': ele => {
                  const kind = ele.data('kind');
                  if (kind === 'fact') return CAT_COLORS_GRAPH[ele.data('category')] || CAT_COLORS_GRAPH.general;
                  if (kind === 'file') return '#5f6368';
                  return '#3c4043';
                },
                'label': 'data(label)',
                'color': '#e8eaed',
                'font-size': 9,
                'text-wrap': 'ellipsis',
                'text-max-width': 120,
                'text-valign': 'bottom',
                'text-margin-y': 4,
                'width': ele => 12 + Math.min(20, (ele.data('relevance') || 0) * 2),
                'height': ele => 12 + Math.min(20, (ele.data('relevance') || 0) * 2),
                'border-width': 1,
                'border-color': '#1a1a1a',
              },
            },
            {
              selector: 'node:selected',
              style: { 'border-width': 3, 'border-color': '#ffd54f' },
            },
            {
              selector: 'edge',
              style: {
                'curve-style': 'bezier',
                'width': ele => Math.max(0.5, Math.min(4, (ele.data('weight') || 1) * 1.2)),
                'line-color': ele => ele.data('type') === 'co_recalled' ? '#4c8bf5' : '#9aa0a6',
                'opacity': 0.55,
                'target-arrow-shape': 'none',
              },
            },
          ],
        });
        cyRef.current.on('tap', 'node', evt => {
          const id = evt.target.data('id');
          setSelected(id);
          if (onSelectFact) onSelectFact(id);
        });
      } else if (cyRef.current) {
        cyRef.current.elements().remove();
        cyRef.current.add(elements);
      }

      if (cyRef.current && elements.length) {
        try {
          cyRef.current.layout({ name: 'fcose', animate: false, quality: 'default', nodeRepulsion: 4500 }).run();
        } catch (e) {
          cyRef.current.layout({ name: 'cose', animate: false }).run();
        }
      }
      setLoading(false);
    } catch (e) {
      setLoading(false);
    }
  };

  useEffect(() => { loadGraph(); }, [minWeight, includeNonFact]);
  useEffect(() => () => { if (cyRef.current) { cyRef.current.destroy(); cyRef.current = null; } }, []);

  return (
    <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8, padding: 14, display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1 }}>
          Memory Graph
          {stats && (
            <span className="mono" style={{ fontSize: 10, color: T.textDim, marginLeft: 10, textTransform: "none", letterSpacing: 0 }}>
              {stats.total_nodes} nodes · {stats.total_edges} edges · {stats.clusters} clusters
            </span>
          )}
        </div>
        <div style={{ display: "flex", gap: 12, alignItems: "center", fontSize: 11, color: T.textMuted }}>
          <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
            min-weight
            <input type="range" min="0" max="5" step="0.1" value={minWeight}
              onChange={e => setMinWeight(parseFloat(e.target.value))} />
            <span className="mono" style={{ width: 28 }}>{minWeight.toFixed(1)}</span>
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <input type="checkbox" checked={includeNonFact}
              onChange={e => setIncludeNonFact(e.target.checked)} />
            files/symbols
          </label>
          <Btn color={T.blue} onClick={loadGraph}><I name="refresh" size={12} /> Reload</Btn>
          <Btn color={T.accent} onClick={relayout}><I name="git-branch" size={12} /> Relayout</Btn>
          <Btn color={T.textMuted} onClick={fitGraph}><I name="search" size={12} /> Fit</Btn>
          <button onClick={() => setShowHelp(!showHelp)}
            title="Toggle legend & help"
            style={{ background: "none", border: `1px solid ${T.border}`, color: T.textMuted, padding: "4px 8px", borderRadius: 4, cursor: "pointer", fontSize: 11 }}>
            {showHelp ? "Hide help" : "Show help"}
          </button>
        </div>
      </div>

      {showHelp && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, padding: 12, background: T.surfaceAlt, borderRadius: 6, border: `1px solid ${T.border}` }}>
          <div>
            <div style={{ fontSize: 10, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>
              Node colors (fact category)
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {GRAPH_LEGEND.map(g => (
                <div key={g.key} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11, color: T.text }}>
                  <span style={{ width: 12, height: 12, borderRadius: "50%", background: CAT_COLORS_GRAPH[g.key], border: "1px solid #1a1a1a", flexShrink: 0 }} />
                  <span style={{ fontWeight: 600, minWidth: 90 }}>{g.label}</span>
                  <span style={{ color: T.textMuted }}>{g.desc}</span>
                </div>
              ))}
              <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11, color: T.text, marginTop: 6 }}>
                <span style={{ width: 12, height: 12, borderRadius: "50%", background: "#5f6368", border: "1px solid #1a1a1a" }} />
                <span style={{ fontWeight: 600, minWidth: 90 }}>File / symbol</span>
                <span style={{ color: T.textMuted }}>shown when "files/symbols" toggled</span>
              </div>
            </div>
            <div style={{ fontSize: 10, color: T.textDim, marginTop: 8, lineHeight: 1.5 }}>
              <strong style={{ color: T.textMuted }}>Node size</strong> = recall count (how often the fact has been retrieved).
              Bigger = more relevant.
            </div>
          </div>
          <div>
            <div style={{ fontSize: 10, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>
              Edges
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 11, color: T.text }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ width: 26, height: 2, background: "#4c8bf5" }} />
                <span><strong>co-recalled</strong> — facts retrieved together</span>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ width: 26, height: 2, background: "#9aa0a6" }} />
                <span><strong>touches / other</strong> — file or symbol links</span>
              </div>
              <div style={{ color: T.textDim, fontSize: 10, marginTop: 4, lineHeight: 1.5 }}>
                <strong style={{ color: T.textMuted }}>Thickness</strong> = edge weight (decays over time, strengthens with co-recall).
                Use <strong style={{ color: T.textMuted }}>min-weight</strong> slider to hide weak/stale links.
              </div>
            </div>
            <div style={{ fontSize: 10, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1, margin: "12px 0 6px" }}>
              Interactions
            </div>
            <ul style={{ margin: 0, padding: "0 0 0 16px", fontSize: 10, color: T.textDim, lineHeight: 1.6 }}>
              <li><strong style={{ color: T.textMuted }}>Click node</strong> → open fact + neighbors in side panel</li>
              <li><strong style={{ color: T.textMuted }}>Drag</strong> node to reposition · <strong style={{ color: T.textMuted }}>drag canvas</strong> to pan</li>
              <li><strong style={{ color: T.textMuted }}>Scroll</strong> to zoom · <strong style={{ color: T.textMuted }}>Fit</strong> recenters view</li>
              <li><strong style={{ color: T.textMuted }}>Relayout</strong> re-runs force-directed layout</li>
              <li><strong style={{ color: T.textMuted }}>Ground</strong> (side panel) verifies a fact against current code</li>
            </ul>
          </div>
        </div>
      )}

      <div
        ref={containerRef}
        style={{ width: "100%", height: 520, background: T.surfaceAlt, borderRadius: 6, border: `1px solid ${T.border}` }}
      />
      {loading && <div style={{ fontSize: 11, color: T.textDim }}>Loading graph...</div>}
      {stats && stats.total_nodes === 0 && !loading && (
        <div style={{ padding: 16, textAlign: "center", color: T.textMuted, fontSize: 12 }}>
          No graph edges yet. Co-recall edges form after facts are retrieved together.
        </div>
      )}
    </div>
  );
};

const Memory = () => {
  const [view, setView] = useState("list");
  const [selectedFactId, setSelectedFactId] = useState(null);
  const [selectedDetail, setSelectedDetail] = useState(null);

  useEffect(() => {
    if (!selectedFactId) { setSelectedDetail(null); return; }
    api.get(`/api/memory/fact/${selectedFactId}`)
      .then(d => setSelectedDetail(d))
      .catch(() => setSelectedDetail(null));
  }, [selectedFactId]);
  const [facts, setFacts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [newFact, setNewFact] = useState("");
  const [category, setCategory] = useState("general");
  const [storing, setStoring] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState(null);
  const [searching, setSearching] = useState(false);
  const [decisions, setDecisions] = useState([]);
  const [decisionsExpanded, setDecisionsExpanded] = useState(false);

  const categories = ["general", "architecture", "convention", "bug", "preference"];
  const catColors = {
    general: T.textMuted,
    architecture: T.blue,
    convention: T.purple,
    bug: T.error,
    preference: T.warn,
  };

  const loadFacts = () => {
    api.get('/api/memory/facts')
      .then(f => { setFacts(f); setLoading(false); })
      .catch(() => setLoading(false));
  };

  const loadDecisions = () => {
    api.get('/api/activity?type=decision&limit=50')
      .then(d => setDecisions(d))
      .catch(() => {});
  };

  useEffect(() => {
    loadFacts();
    loadDecisions();
    const iv = setInterval(() => { loadFacts(); loadDecisions(); }, 5000);
    return () => clearInterval(iv);
  }, []);

  const handleRemember = async () => {
    if (!newFact.trim()) return;
    setStoring(true);
    try {
      await api.post('/api/memory/remember', { fact: newFact, category });
      setNewFact("");
      loadFacts();
    } catch (e) {}
    setStoring(false);
  };

  const handleSearch = async () => {
    if (!searchQuery.trim()) return;
    setSearching(true);
    try {
      const r = await api.post('/api/memory/recall', { query: searchQuery, top_k: 10 });
      setSearchResults(r);
    } catch (e) {}
    setSearching(false);
  };

  const handleDelete = async (id) => {
    await api.del(`/api/memory/facts/${id}`);
    loadFacts();
    if (searchResults) {
      setSearchResults(searchResults.filter(f => f.id !== id));
    }
  };

  const [exportMsg, setExportMsg] = useState(null);
  const handleExport = async () => {
    try {
      const r = await api.get('/api/memory/export');
      await navigator.clipboard.writeText(r.markdown);
      setExportMsg(`Copied ${r.count} facts as markdown`);
      setTimeout(() => setExportMsg(null), 3000);
    } catch (e) {
      setExportMsg("Export failed");
      setTimeout(() => setExportMsg(null), 3000);
    }
  };

  const totalRecalls = facts.reduce((s, f) => s + (f.relevance_count || 0), 0);

  // Group facts by category
  const grouped = {};
  facts.forEach(f => {
    const cat = f.category || "general";
    if (!grouped[cat]) grouped[cat] = [];
    grouped[cat].push(f);
  });

  return (
    <div className="fade-up" style={{ display: "flex", flexDirection: "column", gap: 16 }}>

      {/* Stats row */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center" }}>
        <StatBox label="Stored Facts" value={facts.length} color={T.purple} loading={loading} />
        <StatBox label="Total Recalls" value={totalRecalls} sub="relevance score sum" color={T.accent} loading={loading} />
        <StatBox label="Decisions" value={decisions.length} sub="from sessions" color={T.blue} />
        <div style={{ marginLeft: "auto", display: "flex", gap: 4, padding: 3, background: T.surfaceAlt, borderRadius: 6, border: `1px solid ${T.border}` }}>
          {["list", "graph"].map(v => (
            <button key={v} onClick={() => setView(v)}
              style={{
                padding: "6px 12px", borderRadius: 4, border: "none", cursor: "pointer",
                background: view === v ? T.surface : "transparent",
                color: view === v ? T.text : T.textMuted,
                fontSize: 11, textTransform: "uppercase", letterSpacing: 1, fontWeight: 600,
              }}>
              <I name={v === "graph" ? "git-branch" : "list"} size={11} /> {v}
            </button>
          ))}
        </div>
      </div>

      {view === "graph" && (
        <div style={{ display: "grid", gridTemplateColumns: selectedDetail ? "1fr 320px" : "1fr", gap: 12 }}>
          <MemoryGraph onSelectFact={setSelectedFactId} />
          {selectedDetail && (
            <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8, padding: 14, height: "fit-content" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                <Badge color={catColors[selectedDetail.fact?.category] || T.textMuted}>
                  {selectedDetail.fact?.category || "general"}
                </Badge>
                <button onClick={() => setSelectedFactId(null)} style={{ background: "none", border: "none", cursor: "pointer", color: T.textMuted }}>
                  <I name="x" size={14} />
                </button>
              </div>
              <div style={{ fontSize: 13, color: T.text, lineHeight: 1.5, marginBottom: 10 }}>
                {selectedDetail.fact?.fact}
              </div>
              <div className="mono" style={{ fontSize: 10, color: T.textDim, marginBottom: 12 }}>
                recalls: {selectedDetail.fact?.relevance_count} · confidence: {(selectedDetail.fact?.confidence ?? 1).toFixed(2)}
              </div>
              <div style={{ fontSize: 10, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1, marginBottom: 6 }}>
                Neighbors ({selectedDetail.neighbors?.length || 0})
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4, maxHeight: 260, overflowY: "auto" }}>
                {(selectedDetail.neighbors || []).map(n => (
                  <div key={n.id} onClick={() => n.kind === "fact" && setSelectedFactId(n.id)}
                    style={{ padding: "6px 8px", borderRadius: 4, background: T.surfaceAlt, cursor: n.kind === "fact" ? "pointer" : "default", fontSize: 11, color: T.text }}>
                    <div>{n.label}</div>
                    <div className="mono" style={{ fontSize: 9, color: T.textDim, marginTop: 2 }}>
                      {n.type} · w={n.weight?.toFixed(2)}
                    </div>
                  </div>
                ))}
                {(!selectedDetail.neighbors || selectedDetail.neighbors.length === 0) && (
                  <div style={{ fontSize: 11, color: T.textDim, fontStyle: "italic" }}>No neighbors yet.</div>
                )}
              </div>
              <div style={{ display: "flex", gap: 6, marginTop: 12 }}>
                <Btn color={T.accent} onClick={async () => {
                  try { const r = await api.post(`/api/memory/ground/${selectedFactId}`, {}); alert(r.grounded ? "Grounded ✓" : `Issues: ${(r.issues || []).join(", ")}`); } catch (e) {}
                }}><I name="check" size={12} /> Ground</Btn>
                <Btn color={T.error} onClick={async () => {
                  if (!confirm("Delete fact?")) return;
                  await api.del(`/api/memory/facts/${selectedFactId}`);
                  setSelectedFactId(null);
                  loadFacts();
                }}><I name="trash" size={12} /> Delete</Btn>
              </div>
            </div>
          )}
        </div>
      )}

      {view === "list" && <>


      {/* Remember form */}
      <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8, padding: 18 }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1, marginBottom: 12 }}>
          Remember a Fact
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
          <div style={{ flex: 1, minWidth: 250 }}>
            <input
              value={newFact}
              onChange={e => setNewFact(e.target.value)}
              onKeyDown={e => e.key === "Enter" && handleRemember()}
              placeholder="Enter a fact to remember..."
              className="mono"
              style={{
                width: "100%", padding: "9px 12px", borderRadius: 6,
                background: T.surfaceAlt, border: `1px solid ${T.border}`,
                color: T.text, fontSize: 12, outline: "none",
              }}
            />
          </div>
          <select
            value={category}
            onChange={e => setCategory(e.target.value)}
            className="mono"
            style={{
              padding: "9px 12px", borderRadius: 6,
              background: T.surfaceAlt, border: `1px solid ${T.border}`,
              color: T.text, fontSize: 12, outline: "none",
            }}
          >
            {categories.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
          <Btn color={T.purple} onClick={handleRemember} disabled={!newFact.trim() || storing}>
            <I name="bookmark" size={14} /> {storing ? "Storing..." : "Remember"}
          </Btn>
        </div>
      </div>

      {/* Search */}
      <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8, padding: 18 }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1, marginBottom: 12 }}>
          Search Facts
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <div style={{
            flex: 1, display: "flex", alignItems: "center", gap: 8,
            padding: "0 14px", borderRadius: 6,
            background: T.surfaceAlt, border: `1px solid ${T.border}`,
          }}>
            <I name="search" size={14} color={T.textMuted} />
            <input
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              onKeyDown={e => e.key === "Enter" && handleSearch()}
              placeholder="Search stored facts..."
              className="mono"
              style={{
                flex: 1, padding: "10px 0", background: "transparent",
                border: "none", color: T.text, fontSize: 13, outline: "none",
              }}
            />
          </div>
          <Btn color={T.blue} onClick={handleSearch} disabled={searching || !searchQuery.trim()}>
            <I name="search" size={14} /> {searching ? "Searching..." : "Search"}
          </Btn>
        </div>
        {searchResults && (
          <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 6 }}>
            {searchResults.length === 0 && (
              <div style={{ padding: 16, textAlign: "center", color: T.textMuted, fontSize: 13 }}>
                No matching facts found.
              </div>
            )}
            {searchResults.map((f, i) => (
              <div
                key={f.id}
                style={{
                  display: "flex", alignItems: "center", gap: 10,
                  padding: "10px 12px", borderRadius: 6,
                  background: T.surfaceAlt, border: `1px solid ${T.border}`,
                }}
              >
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 13, color: T.text, marginBottom: 4 }}>{f.fact}</div>
                  <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                    <Badge color={catColors[f.category] || T.textMuted}>{f.category}</Badge>
                    <span className="mono" style={{ fontSize: 10, color: T.textDim }}>recalls: {f.relevance_count}</span>
                    {f.score !== undefined && <Badge color={T.accent}>score: {f.score}</Badge>}
                  </div>
                </div>
                <button onClick={() => handleDelete(f.id)} style={{ background: "none", border: "none", cursor: "pointer", padding: 4 }}>
                  <I name="trash" size={14} color={T.error} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Decisions (collapsible) */}
      {decisions.length > 0 && (
        <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8, overflow: "hidden" }}>
          <div
            onClick={() => setDecisionsExpanded(!decisionsExpanded)}
            style={{
              display: "flex", alignItems: "center", justifyContent: "space-between",
              padding: "12px 18px", background: T.surfaceAlt, cursor: "pointer",
              borderBottom: decisionsExpanded ? `1px solid ${T.border}` : "none",
            }}
          >
            <span style={{
              fontSize: 12, fontWeight: 600, color: T.textMuted,
              textTransform: "uppercase", letterSpacing: 1,
              display: "flex", alignItems: "center", gap: 6,
            }}>
              <I name="brain" size={13} color={T.blue} /> Decisions
              <Badge color={T.blue}>{decisions.length}</Badge>
            </span>
            <I
              name="chevron"
              size={14}
              color={T.textMuted}
              style={{ transform: decisionsExpanded ? "rotate(90deg)" : "none", transition: "transform 0.15s" }}
            />
          </div>
          {decisionsExpanded && (
            <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 6 }}>
              {decisions.map((d, i) => (
                <div
                  key={i}
                  style={{
                    display: "flex", gap: 10, padding: "10px 12px",
                    borderRadius: 6, background: T.surfaceAlt,
                    border: `1px solid ${T.border}20`,
                  }}
                >
                  <GlowDot color={T.blue} size={6} />
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 12, color: T.text, lineHeight: 1.5 }}>{d.decision}</div>
                    {d.reasoning && (
                      <div style={{ fontSize: 11, color: T.textMuted, marginTop: 4, fontStyle: "italic" }}>
                        {d.reasoning}
                      </div>
                    )}
                    <div className="mono" style={{ fontSize: 10, color: T.textDim, marginTop: 4 }}>
                      {timeAgo(d.timestamp)}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* All facts grouped by category */}
      <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8, padding: 18 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: T.textMuted, textTransform: "uppercase", letterSpacing: 1 }}>
            All Facts
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            {exportMsg && <span style={{ fontSize: 11, color: T.accent }}>{exportMsg}</span>}
            <Btn color={T.purple} onClick={handleExport} disabled={facts.length === 0}>
              <I name="copy" size={13} /> Export Markdown
            </Btn>
          </div>
        </div>
        {facts.length === 0 && !loading && (
          <div style={{ padding: 20, textAlign: "center", color: T.textMuted, fontSize: 13 }}>
            No facts stored yet. Use the form above or the MCP remember tool.
          </div>
        )}
        {Object.entries(grouped).map(([cat, items]) => (
          <div key={cat} style={{ marginBottom: 14 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
              <Badge color={catColors[cat] || T.textMuted}>{cat}</Badge>
              <span style={{ fontSize: 11, color: T.textDim }}>({items.length})</span>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {items.map(f => (
                <div
                  key={f.id}
                  style={{
                    display: "flex", alignItems: "center", gap: 10,
                    padding: "8px 12px", borderRadius: 6, background: T.surfaceAlt,
                  }}
                >
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 12, color: T.text }}>{f.fact}</div>
                    <div className="mono" style={{ fontSize: 10, color: T.textDim, marginTop: 2 }}>
                      {localDate(f.timestamp)} | recalls: {f.relevance_count}
                      {f.source_session && <> | session: {f.source_session.slice(0, 8)}</>}
                    </div>
                  </div>
                  <button onClick={() => handleDelete(f.id)} style={{ background: "none", border: "none", cursor: "pointer", padding: 4 }}>
                    <I name="trash" size={14} color={T.error} />
                  </button>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
      </>}

    </div>
  );
};
