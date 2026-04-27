// Instructions component — manages all IDE instruction documents
// Globals: T, I, GlowDot, Badge, StatBox, Btn, api, useState, useEffect

const Instructions = () => {
  const IDE_DOCS = [
    { key: 'claude',   label: 'Claude',  file: 'CLAUDE.md' },
    { key: 'codex',    label: 'Codex',   file: 'AGENTS.md' },
    { key: 'gemini',   label: 'Gemini',  file: 'GEMINI.md' },
    { key: 'copilot',  label: 'Copilot', file: '.github/copilot-instructions.md' },
  ];

  const [selectedKey, setSelectedKey]   = useState('claude');
  const [preview, setPreview]           = useState('');
  const [previewLoading, setPreviewLoading] = useState(false);
  const [existsMap, setExistsMap]       = useState({});  // key -> bool
  const [busy, setBusy]                 = useState(false);
  const [actionResult, setActionResult] = useState(null); // { type, data }
  const [error, setError]               = useState('');

  // ── helpers ──────────────────────────────────────────────────────────────
  const sevColor = (sev) => {
    if (!sev) return T.textMuted;
    const s = sev.toLowerCase();
    if (s === 'error')   return T.error;
    if (s === 'warning') return '#ffb224';
    return T.textMuted;
  };

  const lineCount = preview ? preview.split('\n').length : 0;
  // rough token estimate: ~4 chars per token
  const tokenEst  = preview ? Math.round(preview.length / 4) : 0;

  const selectedDoc = IDE_DOCS.find(d => d.key === selectedKey);

  // ── data loading ─────────────────────────────────────────────────────────
  const loadPreview = async () => {
    setPreviewLoading(true);
    setError('');
    try {
      const data = await api.get('/api/claudemd');
      // backend may return { content } or a plain string
      const content = typeof data === 'string' ? data : (data.content || data.generated || '');
      setPreview(content);
      // infer which docs exist from a check call (optional, best-effort)
      try {
        const check = await api.get('/api/claudemd/check');
        if (check && check.docs_exist) {
          setExistsMap(check.docs_exist);
        }
      } catch (_) {}
    } catch (e) {
      setError(e.message || 'Failed to load preview');
    }
    setPreviewLoading(false);
  };

  useEffect(() => {
    loadPreview();
  }, []);

  // ── actions ───────────────────────────────────────────────────────────────
  const runSyncAll = async () => {
    setBusy(true);
    setActionResult(null);
    setError('');
    try {
      const res = await api.post('/api/claudemd/save', {});
      setActionResult({ type: 'sync', data: res });
      await loadPreview();
    } catch (e) {
      setError(e.message || 'Sync failed');
    }
    setBusy(false);
  };

  const runGenerate = async () => {
    setBusy(true);
    setActionResult(null);
    setError('');
    try {
      const res = await api.post('/api/claudemd/save', {});
      setActionResult({ type: 'generate', data: res });
      await loadPreview();
    } catch (e) {
      setError(e.message || 'Generate failed');
    }
    setBusy(false);
  };

  const runHealthCheck = async () => {
    setBusy(true);
    setActionResult(null);
    setError('');
    try {
      const res = await api.get('/api/claudemd/check');
      setActionResult({ type: 'check', data: res });
    } catch (e) {
      setError(e.message || 'Health check failed');
    }
    setBusy(false);
  };

  const runCompact = async () => {
    setBusy(true);
    setActionResult(null);
    setError('');
    try {
      const res = await api.post('/api/claudemd/compact', { target_lines: 150 });
      setActionResult({ type: 'compact', data: res });
      await loadPreview();
    } catch (e) {
      setError(e.message || 'Compact failed');
    }
    setBusy(false);
  };

  const runPromote = async () => {
    setBusy(true);
    setActionResult(null);
    setError('');
    try {
      const res = await api.get('/api/claudemd/promote');
      setActionResult({ type: 'promote', data: res });
    } catch (e) {
      setError(e.message || 'Promote failed');
    }
    setBusy(false);
  };

  // ── sub-renders ───────────────────────────────────────────────────────────
  const renderActionResult = () => {
    if (!actionResult) return null;
    const { type, data } = actionResult;

    if (type === 'check') {
      const issues = data.issues || [];
      return (
        <div style={{ animation: 'fadeUp 0.25s ease' }}>
          <div style={{
            fontSize: 11, fontWeight: 700, color: T.textMuted,
            textTransform: 'uppercase', letterSpacing: 1, marginBottom: 10
          }}>
            Health Check — {issues.length === 0 ? 'No issues' : `${issues.length} issue${issues.length > 1 ? 's' : ''}`}
          </div>
          {issues.length === 0 ? (
            <div style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px',
              background: `${T.accent}12`, border: `1px solid ${T.accent}30`, borderRadius: 6,
              fontSize: 12, color: T.accent
            }}>
              <I name="check" size={14} color={T.accent} /> All documents look healthy.
            </div>
          ) : (
            issues.map((iss, i) => (
              <div key={i} style={{
                display: 'flex', gap: 10, alignItems: 'flex-start',
                padding: '9px 12px', marginBottom: 6,
                background: `${sevColor(iss.severity)}10`,
                border: `1px solid ${sevColor(iss.severity)}30`,
                borderLeft: `3px solid ${sevColor(iss.severity)}`,
                borderRadius: '0 6px 6px 6px', fontSize: 12
              }}>
                <span style={{
                  fontSize: 9, padding: '2px 6px', borderRadius: 3, fontWeight: 700,
                  background: `${sevColor(iss.severity)}20`, color: sevColor(iss.severity),
                  textTransform: 'uppercase', letterSpacing: 0.5, flexShrink: 0, marginTop: 1
                }}>
                  {iss.severity || 'info'}
                </span>
                <span style={{ color: T.text, lineHeight: 1.6 }}>{iss.message || JSON.stringify(iss)}</span>
              </div>
            ))
          )}
        </div>
      );
    }

    if (type === 'compact') {
      const before = data.lines_before ?? data.original_lines ?? '?';
      const after  = data.lines_after  ?? data.compacted_lines ?? '?';
      const saved  = typeof before === 'number' && typeof after === 'number' ? before - after : null;
      return (
        <div style={{ animation: 'fadeUp 0.25s ease' }}>
          <div style={{
            fontSize: 11, fontWeight: 700, color: T.textMuted,
            textTransform: 'uppercase', letterSpacing: 1, marginBottom: 10
          }}>
            Compact Result
          </div>
          <div style={{
            display: 'flex', gap: 12, flexWrap: 'wrap'
          }}>
            <StatBox label="Before" value={before} color={T.warn} />
            <StatBox label="After"  value={after}  color={T.accent} />
            {saved !== null && (
              <StatBox label="Saved" value={`-${saved}`} color={T.blue} />
            )}
          </div>
          {data.message && (
            <div style={{ marginTop: 12, fontSize: 12, color: T.textMuted, lineHeight: 1.6 }}>
              {data.message}
            </div>
          )}
        </div>
      );
    }

    if (type === 'promote') {
      const raw = data.candidates || data.sections || {};
      // Backend returns { "Section Name": [items...] } dict, not an array
      const entries = typeof raw === 'object' && !Array.isArray(raw)
        ? Object.entries(raw)
        : [];
      const totalCount = data.total_candidates || entries.reduce((s, [, v]) => s + (Array.isArray(v) ? v.length : 0), 0);
      return (
        <div style={{ animation: 'fadeUp 0.25s ease' }}>
          <div style={{
            fontSize: 11, fontWeight: 700, color: T.textMuted,
            textTransform: 'uppercase', letterSpacing: 1, marginBottom: 10
          }}>
            Promote Insights — {totalCount} candidate{totalCount !== 1 ? 's' : ''}
          </div>
          {data.message && (
            <div style={{ fontSize: 12, color: T.textMuted, marginBottom: 10 }}>{data.message}</div>
          )}
          {totalCount === 0 ? (
            <div style={{ fontSize: 12, color: T.textMuted, fontStyle: 'italic' }}>
              No promotable insights found. Use C3 tools more to build up facts.
            </div>
          ) : (
            entries.map(([section, items]) => (
              <div key={section} style={{ marginBottom: 12 }}>
                <div style={{
                  fontSize: 10, fontWeight: 700, color: T.purple,
                  textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6
                }}>
                  {section}
                </div>
                {(Array.isArray(items) ? items : []).map((c, i) => (
                  <div key={i} style={{
                    marginBottom: 6, padding: '8px 14px',
                    background: T.surface, border: `1px solid ${T.border}`,
                    borderLeft: `3px solid ${T.purple}60`,
                    borderRadius: '0 6px 6px 6px'
                  }}>
                    <div style={{ fontSize: 12, color: T.text, lineHeight: 1.6 }}>
                      {c.fact || c.text || c.insight || c.snippet || JSON.stringify(c)}
                    </div>
                    <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
                      {c.category && <Badge color={T.purple}>{c.category}</Badge>}
                      {c.relevance_count > 0 && (
                        <span className="mono" style={{ fontSize: 9, color: T.textDim }}>
                          relevance: {c.relevance_count}
                        </span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            ))
          )}
        </div>
      );
    }

    // sync / generate — simple status message
    const msg = data.message || data.status || (data.saved ? 'Documents saved.' : 'Done.');
    return (
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px',
        background: `${T.accent}12`, border: `1px solid ${T.accent}30`, borderRadius: 6,
        fontSize: 12, color: T.accent, animation: 'fadeUp 0.25s ease'
      }}>
        <I name="check" size={14} color={T.accent} /> {msg}
      </div>
    );
  };

  // ── spinner ────────────────────────────────────────────────────────────────
  const Spinner = () => (
    <div style={{
      display: 'inline-block', width: 14, height: 14, borderRadius: '50%',
      border: `2px solid ${T.border}`, borderTopColor: T.accent,
      animation: 'spin 0.7s linear infinite', flexShrink: 0
    }} />
  );

  // ── layout ─────────────────────────────────────────────────────────────────
  return (
    <div style={{
      display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden',
      background: T.bg
    }}>
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '18px 24px 14px',
        borderBottom: `1px solid ${T.border}`,
        flexShrink: 0
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <I name="file" size={18} color={T.accent} />
          <span style={{ fontSize: 16, fontWeight: 700, color: T.text, letterSpacing: -0.3 }}>
            Instruction Documents
          </span>
          <Badge color={T.blue}>{IDE_DOCS.length} IDEs</Badge>
        </div>
        <Btn
          color={T.accent}
          variant="solid"
          onClick={runSyncAll}
          disabled={busy}
          style={{ gap: 6 }}
        >
          {busy ? <Spinner /> : <I name="refresh" size={13} />}
          Sync All
        </Btn>
      </div>

      {/* IDE selector */}
      <div style={{
        display: 'flex', gap: 6, padding: '12px 24px',
        borderBottom: `1px solid ${T.border}`,
        flexShrink: 0, flexWrap: 'wrap'
      }}>
        {IDE_DOCS.map(doc => {
          const isActive  = doc.key === selectedKey;
          const docExists = existsMap[doc.key] ?? existsMap[doc.file] ?? null;
          return (
            <button
              key={doc.key}
              onClick={() => setSelectedKey(doc.key)}
              style={{
                display: 'flex', alignItems: 'center', gap: 6,
                padding: '6px 14px', borderRadius: 6, cursor: 'pointer',
                border: isActive ? `1px solid ${T.accent}` : `1px solid ${T.border}`,
                background: isActive ? `${T.accent}18` : 'transparent',
                color: isActive ? T.accent : T.textMuted,
                fontSize: 12, fontWeight: 600,
                transition: 'all 0.15s',
              }}
              onMouseEnter={e => { if (!isActive) { e.currentTarget.style.borderColor = T.borderHover; e.currentTarget.style.color = T.text; }}}
              onMouseLeave={e => { if (!isActive) { e.currentTarget.style.borderColor = T.border; e.currentTarget.style.color = T.textMuted; }}}
            >
              {docExists === true  && <GlowDot color={T.accent} size={6} />}
              {docExists === false && <GlowDot color={T.textDim} size={6} />}
              {docExists === null  && <span style={{ width: 6, height: 6 }} />}
              {doc.label}
            </button>
          );
        })}
      </div>

      {/* Main body — scrollable */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '20px 24px', display: 'flex', flexDirection: 'column', gap: 20 }}>

        {/* Error banner */}
        {error && (
          <div style={{
            padding: '10px 14px', background: `${T.error}12`,
            border: `1px solid ${T.error}30`, borderRadius: 6,
            fontSize: 12, color: T.error, lineHeight: 1.5
          }}>
            {error}
          </div>
        )}

        {/* Document info row */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12, color: T.textMuted, fontFamily: "'JetBrains Mono', monospace" }}>
            {selectedDoc?.file}
          </span>
          {!previewLoading && preview && (
            <>
              <Badge color={T.blue}>{lineCount} lines</Badge>
              <Badge color={T.textMuted}>~{tokenEst} tokens</Badge>
            </>
          )}
          {previewLoading && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: T.textMuted }}>
              <Spinner /> Loading…
            </div>
          )}
        </div>

        {/* Preview */}
        <div style={{
          background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8,
          overflow: 'hidden', flexShrink: 0
        }}>
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            padding: '8px 14px', borderBottom: `1px solid ${T.border}`,
            background: T.surfaceAlt
          }}>
            <span style={{ fontSize: 11, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: 1 }}>
              Preview
            </span>
            <span style={{ fontSize: 10, color: T.textDim, fontFamily: "'JetBrains Mono', monospace" }}>
              {selectedDoc?.file}
            </span>
          </div>
          {previewLoading ? (
            <div style={{
              height: 200, display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: T.textDim, fontSize: 13, gap: 8
            }}>
              <Spinner /> Loading preview…
            </div>
          ) : preview ? (
            <div style={{ overflowY: 'auto', maxHeight: 400 }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', tableLayout: 'fixed' }}>
                <tbody>
                  {preview.split('\n').map((line, idx) => (
                    <tr key={idx} style={{ verticalAlign: 'top' }}>
                      <td style={{
                        width: 48, paddingLeft: 10, paddingRight: 10,
                        textAlign: 'right', fontSize: 11, lineHeight: '20px',
                        color: T.textDim, userSelect: 'none', flexShrink: 0,
                        fontFamily: "'JetBrains Mono', monospace",
                        borderRight: `1px solid ${T.border}30`
                      }}>
                        {idx + 1}
                      </td>
                      <td style={{
                        paddingLeft: 12, paddingRight: 12,
                        fontSize: 12, lineHeight: '20px', color: T.text,
                        fontFamily: "'JetBrains Mono', monospace",
                        whiteSpace: 'pre-wrap', wordBreak: 'break-word'
                      }}>
                        {line || '\u00A0'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div style={{
              height: 120, display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: T.textDim, fontSize: 13
            }}>
              No content — click Generate &amp; Save to create this document.
            </div>
          )}
        </div>

        {/* Action buttons */}
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <Btn color={T.accent} variant="solid" onClick={runGenerate} disabled={busy}>
            {busy ? <Spinner /> : <I name="save" size={13} />}
            Generate &amp; Save
          </Btn>
          <Btn color={T.blue} variant="ghost" onClick={runHealthCheck} disabled={busy}>
            {busy ? <Spinner /> : <I name="zap" size={13} />}
            Health Check
          </Btn>
          <Btn color={T.warn} variant="ghost" onClick={runCompact} disabled={busy}>
            {busy ? <Spinner /> : <I name="minimize" size={13} />}
            Compact
          </Btn>
          <Btn color={T.purple} variant="ghost" onClick={runPromote} disabled={busy}>
            {busy ? <Spinner /> : <I name="bookmark" size={13} />}
            Promote Insights
          </Btn>
        </div>

        {/* Results area */}
        {actionResult && (
          <div style={{
            background: T.surface, border: `1px solid ${T.border}`,
            borderRadius: 8, padding: '16px 18px'
          }}>
            {renderActionResult()}
          </div>
        )}

      </div>
    </div>
  );
};
