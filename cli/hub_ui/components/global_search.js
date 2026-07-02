// ─── Global cross-project search (Ctrl-K overlay) ──────────────
// POST /api/search/global {query, kind} → results grouped per project.
// Esc close is handled by app.js; backdrop click + the X button close too.

function GlobalSearch({ open, onClose, projects, onOpenProject }) {
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('both');
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const inputRef = useRef(null);
  const seqRef = useRef(0);

  // Autofocus the input whenever the overlay opens.
  useEffect(() => {
    if (open && inputRef.current) inputRef.current.focus();
  }, [open]);

  // Debounced search: 400ms after typing stops, min 2 chars.
  useEffect(() => {
    if (!open) return;
    const q = query.trim();
    if (q.length < 2) { setData(null); setError(null); setLoading(false); return; }
    setLoading(true);
    const seq = ++seqRef.current;
    const t = setTimeout(async () => {
      try {
        const d = await api.post('/api/search/global', { query: q, kind });
        if (seq !== seqRef.current) return;   // superseded by a newer search
        setData(d); setError(null); setLoading(false);
      } catch (e) {
        if (seq !== seqRef.current) return;
        setError(e.message); setData(null); setLoading(false);
      }
    }, 400);
    return () => clearTimeout(t);
  }, [query, kind, open]);

  if (!open) return null;

  // Map a result's {name, path} back to the full project object from props.
  const findProject = (pr) => {
    const key = ((pr && pr.path) || '').toLowerCase();
    return (projects || []).find(p => (p.path || '').toLowerCase() === key)
      || { path: (pr && pr.path) || '', name: (pr && pr.name) || '' };
  };

  const fmtLines = (lines) => Array.isArray(lines) ? lines.join('-') : (lines || '');

  const chip = (id, label) => (
    <button key={id} onClick={() => setKind(id)} className="mono" style={{
      padding: '4px 12px', borderRadius: 999, fontSize: 11, fontWeight: 700, cursor: 'pointer',
      border: `1px solid ${kind === id ? T.accent : T.border}`,
      background: kind === id ? T.accentDim : 'transparent',
      color: kind === id ? T.accent : T.textMuted,
    }}>{label}</button>
  );

  const hoverOn = e => { e.currentTarget.style.background = T.surfaceAlt; };
  const hoverOff = e => { e.currentTarget.style.background = 'transparent'; };

  const hasHits = data && (data.results || [])
    .some(r => (r.code || []).length || (r.memory || []).length || r.error);

  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, background: '#00000090', zIndex: 400,
      display: 'flex', justifyContent: 'center', alignItems: 'flex-start', paddingTop: '12vh',
    }}>
      <div onClick={e => e.stopPropagation()} className="fade-up" style={{
        width: 640, maxWidth: '94vw', maxHeight: '70vh', display: 'flex', flexDirection: 'column',
        background: T.surface, border: `1px solid ${T.border}`, borderRadius: 10, overflow: 'hidden',
      }}>
        {/* Search input */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '14px 16px', borderBottom: `1px solid ${T.border}` }}>
          <I name="search" size={16} color={T.textMuted} />
          <input ref={inputRef} value={query} onChange={e => setQuery(e.target.value)}
            placeholder="Search code & memory across all projects…"
            style={{
              flex: 1, background: 'transparent', border: 'none', outline: 'none',
              color: T.text, fontSize: 15, fontFamily: 'inherit',
            }} />
          <button onClick={onClose} title="Close (Esc)"
            style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 4, display: 'flex' }}>
            <I name="xSmall" size={14} color={T.textMuted} />
          </button>
        </div>

        {/* Kind chips */}
        <div style={{ display: 'flex', gap: 6, padding: '10px 16px', borderBottom: `1px solid ${T.border}` }}>
          {chip('both', 'Both')}{chip('code', 'Code')}{chip('memory', 'Memory')}
        </div>

        {/* Results */}
        <div style={{ overflowY: 'auto', flex: 1 }}>
          {loading && (
            <div style={{ padding: '22px 16px', fontSize: 12, color: T.textMuted, animation: 'pulse 1s infinite' }}>
              Searching — warming project runtimes… (the first search per project is slow)
            </div>
          )}
          {!loading && error && (
            <div style={{ padding: '22px 16px', fontSize: 12, color: T.error }}>{error}</div>
          )}
          {!loading && !error && !data && (
            <div style={{ padding: '22px 16px', fontSize: 12, color: T.textDim }}>
              Type at least 2 characters to search every registered project.
            </div>
          )}
          {!loading && !error && data && !hasHits && (
            <div style={{ padding: '22px 16px', fontSize: 12, color: T.textMuted }}>
              No matches for "{data.query}".
            </div>
          )}
          {!loading && !error && data && (data.results || []).map(row => {
            const codeHits = row.code || [];
            const memHits = row.memory || [];
            if (!codeHits.length && !memHits.length && !row.error) return null;
            const proj = row.project || {};
            return (
              <div key={proj.path || proj.name} style={{ borderBottom: `1px solid ${T.border}` }}>
                {/* Project header */}
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, padding: '10px 16px 6px' }}>
                  <span style={{ fontSize: 13, fontWeight: 700, color: T.text, whiteSpace: 'nowrap' }}>
                    {proj.name || proj.path}
                  </span>
                  <span className="mono" style={{
                    fontSize: 11, color: T.textDim, flex: 1,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>{proj.path}</span>
                  {row.error && <Badge color={T.error}>error</Badge>}
                </div>
                {row.error && (
                  <div style={{ padding: '0 16px 10px', fontSize: 11, color: T.error }}>{row.error}</div>
                )}
                {/* Code hits */}
                {codeHits.map((hit, i) => (
                  <div key={`c${i}`} onClick={() => onOpenProject(findProject(proj), 'overview')}
                    onMouseEnter={hoverOn} onMouseLeave={hoverOff}
                    style={{ padding: '8px 16px', cursor: 'pointer', borderTop: `1px solid ${T.border}40` }}>
                    <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap' }}>
                      <span className="mono" style={{ fontSize: 11, color: T.blue }}>
                        {hit.file}{fmtLines(hit.lines) ? `:${fmtLines(hit.lines)}` : ''}
                      </span>
                      <span style={{ fontSize: 12, color: T.text, fontWeight: 600 }}>{hit.name}</span>
                      {hit.type && <span className="mono" style={{ fontSize: 10, color: T.textDim }}>{hit.type}</span>}
                    </div>
                    {hit.snippet && (
                      <div className="mono" style={{
                        fontSize: 11, color: T.textMuted, marginTop: 3, whiteSpace: 'pre-wrap',
                        display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden',
                      }}>{hit.snippet}</div>
                    )}
                  </div>
                ))}
                {/* Memory hits */}
                {memHits.map((hit, i) => (
                  <div key={`m${i}`} onClick={() => onOpenProject(findProject(proj), 'memory')}
                    onMouseEnter={hoverOn} onMouseLeave={hoverOff}
                    style={{
                      padding: '8px 16px', cursor: 'pointer', borderTop: `1px solid ${T.border}40`,
                      display: 'flex', gap: 8, alignItems: 'flex-start',
                    }}>
                    <Badge color={T.purple}>{hit.category || 'memory'}</Badge>
                    <span style={{ fontSize: 12, color: T.text, lineHeight: 1.45 }}>{hit.fact}</span>
                  </div>
                ))}
              </div>
            );
          })}
        </div>

        {/* Footer */}
        {data && !loading && !error && (
          <div className="mono" style={{
            padding: '8px 16px', borderTop: `1px solid ${T.border}`,
            fontSize: 11, color: T.textMuted,
          }}>
            {data.projects_searched} projects · {data.elapsed_ms}ms · {(data.skipped || []).length} skipped
          </div>
        )}
      </div>
    </div>
  );
}
