// ─── AgentCI — local CI execution (docs/agent-ci.md) ──────────────────────
// Run the repository's REAL CI (.github/workflows/*.yml) on this machine
// instead of pushing to get feedback.
//
// The honesty rule this view exists to enforce, and must never soften:
// FULL_CI_PASS is the only verdict that means "safe to push". Everything else
// is PARTIAL — some job did not run here, whether because it targets another
// OS, uses an action we cannot execute, or was deselected. A green tick on a
// quarter of the matrix, shown at the moment someone decides to push, is the
// exact failure this module was built to prevent. So: partial never renders
// green, and the reason always travels with the verdict.

const CI_STATUS_STYLE = {
  passed: ['PASS', 'accent'],
  failed: ['FAIL', 'error'],
  timeout: ['TIMEOUT', 'error'],
  skipped: ['SKIP', 'warn'],
  unsupported: ['UNSUP', 'warn'],
  foreign: ['OTHER-OS', 'blue'],
  deselected: ['—', 'textDim'],
  // capability labels, used by the job graph only
  cap_native: ['NATIVE', 'accent'],
  cap_container: ['CONTAINER', 'accent'],
  cap_unreachable: ['OTHER-OS', 'blue'],
  cap_unsupported: ['UNSUP', 'warn'],
};

function CiStatusPill({ status, title }) {
  const [label, tone] = CI_STATUS_STYLE[status] || [String(status || '?').toUpperCase(), 'textMuted'];
  const color = T[tone] || T.textMuted;
  return (
    <span className="mono" title={title || status} style={{
      fontSize: 10, padding: '2px 7px', borderRadius: 4, whiteSpace: 'nowrap',
      color, border: `1px solid ${color}44`,
    }}>{label}</span>
  );
}

function CiVerdict({ verdict, note }) {
  const full = verdict === 'FULL_CI_PASS';
  const color = full ? T.accent : (verdict === 'FAIL' ? T.error : T.warn);
  return (
    <div style={{
      border: `1px solid ${color}`, borderRadius: 6, padding: '9px 11px',
      background: T.surface, display: 'flex', gap: 10, alignItems: 'flex-start',
    }}>
      <I name={full ? 'check' : (verdict === 'FAIL' ? 'xCircle' : 'alertTriangle')}
        size={14} color={color} />
      <div style={{ minWidth: 0 }}>
        <div className="mono" style={{ fontSize: 12, color }}>{verdict || 'no run yet'}</div>
        {note && <div style={{ fontSize: 11, color: T.textMuted, marginTop: 3, lineHeight: 1.5 }}>{note}</div>}
        {!full && verdict && (
          <div style={{ fontSize: 11, color: T.textDim, marginTop: 4 }}>
            Only FULL_CI_PASS means every job ran on this host and passed.
          </div>
        )}
      </div>
    </div>
  );
}

function CiFailureList({ job }) {
  const failures = job.failures || [];
  if (!failures.length) return null;
  return (
    <div style={{ padding: '4px 10px 8px 26px' }}>
      {failures.map((f, i) => (
        <div key={i} style={{
          fontSize: 11, borderLeft: `2px solid ${T.error}`, paddingLeft: 8, marginBottom: 6,
        }}>
          <span className="mono" style={{ color: T.blue }}>
            {f.file ? `${f.file}${f.line ? ':' + f.line : ''}` : (f.test || '')}
          </span>
          {f.rule && <span className="mono" style={{ color: T.purple, marginLeft: 6 }}>{f.rule}</span>}
          <div style={{ color: T.textMuted, marginTop: 2 }}>{f.message}</div>
          {f.excerpt && (
            <pre className="mono" style={{
              margin: '4px 0 0', fontSize: 10, color: T.textDim, whiteSpace: 'pre-wrap',
              maxHeight: 130, overflow: 'auto',
            }}>{f.excerpt}</pre>
          )}
        </div>
      ))}
    </div>
  );
}

function HubCI({ projects, onOpenDrill }) {
  const { useState, useCallback, useEffect } = React;
  const [selected, setSelected] = useState('');
  const [inspect, setInspect] = useState(null);
  const [run, setRun] = useState(null);
  const [runs, setRuns] = useState([]);
  const [busy, setBusy] = useState(false);
  const [allowForeign, setAllowForeign] = useState(false);
  const [err, setErr] = useState('');
  const [log, setLog] = useState(null);

  const path = selected || (projects[0] && projects[0].path) || '';

  const loadAll = useCallback(async () => {
    if (!path) return;
    try {
      const [ins, hist, status] = await Promise.all([
        api.get(`/api/ci/inspect?path=${encodeURIComponent(path)}`),
        api.get(`/api/ci/runs?path=${encodeURIComponent(path)}`),
        api.get(`/api/ci/status?path=${encodeURIComponent(path)}`),
      ]);
      setInspect(ins);
      setRuns((hist && hist.runs) || []);
      setBusy(!!(status && status.running));
      const detail = await api.get(`/api/ci/run?path=${encodeURIComponent(path)}`);
      setRun(detail && detail.run_id ? detail : null);
      setErr('');
    } catch (e) {
      setErr(String(e && e.message ? e.message : e));
    }
  }, [path]);

  useEffect(() => { loadAll(); }, [loadAll]);
  // Poll faster while a run is in flight; a CI run is minutes long and the
  // whole point is watching it move.
  usePoll(loadAll, busy ? 2000 : 10000);

  const start = async (job) => {
    if (!path || busy) return;
    setBusy(true);
    try {
      await api.post('/api/ci/run', { path, job: job || '', allow_foreign: allowForeign });
    } catch (e) {
      setErr(String(e && e.message ? e.message : e));
      setBusy(false);
    }
    loadAll();
  };

  const openLog = async (jobKey) => {
    if (!run) return;
    try {
      const r = await api.get(
        `/api/ci/logs?path=${encodeURIComponent(path)}`
        + `&run_id=${encodeURIComponent(run.run_id)}&job=${encodeURIComponent(jobKey)}`);
      setLog({ job: jobKey, text: (r && r.log) || '(empty)' });
    } catch (e) {
      setLog({ job: jobKey, text: `could not read log: ${e}` });
    }
  };

  const jobs = (inspect && inspect.jobs) || [];
  const engines = inspect && inspect.engines;
  const runnableNative = new Set((inspect && inspect.runnable_native) || []);
  const runnableContainer = new Set((inspect && inspect.runnable_container) || []);
  const unsupportedKeys = new Set(((inspect && inspect.unsupported) || []).map(u => u.key));
  const runJobs = ((run && run.jobs) || []).filter(j => j.status !== 'deselected');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <I name="gitBranch" size={15} color={T.accent} />
        <span style={{ fontSize: 14, color: T.text }}>Local CI</span>
        <span className="mono" style={{ fontSize: 11, color: T.textMuted }}>
          {inspect
            ? `${(inspect.runnable || []).length} of ${jobs.length} runnable here`
              + ` (${runnableNative.size} native, ${runnableContainer.size} container)`
            : 'loading…'}
        </span>
        <div style={{ flex: 1 }} />
        <select value={path} onChange={e => setSelected(e.target.value)}
          className="mono" style={{
            background: T.surface, color: T.text, border: `1px solid ${T.border}`,
            borderRadius: 5, fontSize: 11, padding: '4px 7px', maxWidth: 320,
          }}>
          {projects.map(p => <option key={p.path} value={p.path}>{p.name || p.path}</option>)}
        </select>
        <div onClick={loadAll} title="Refresh" style={{ cursor: 'pointer', padding: 4 }}>
          <I name="refresh" size={13} color={T.textMuted} />
        </div>
      </div>

      <div style={{
        fontSize: 11, color: T.textMuted, background: T.surface,
        border: `1px solid ${T.border}`, borderRadius: 6, padding: '8px 10px', lineHeight: 1.5,
      }}>
        Runs the repository's real <span className="mono">.github/workflows</span> here —
        C3 does not define a second CI config.{' '}
        {engines && engines.ok ? (
          <span>Linux jobs run in a container via <span className="mono">act</span>{' '}
            ({engines.act_version}, docker {engines.docker_version}), real actions included.</span>
        ) : (
          <span style={{ color: T.warn }}>
            Container engine unavailable — {(engines && engines.reason) || 'checking…'}
          </span>
        )}{' '}
        macOS jobs can never run locally: there are no macOS containers.
      </div>

      {err && (
        <div style={{
          fontSize: 11, color: T.error, background: T.errorDim,
          border: `1px solid ${T.error}`, borderRadius: 6, padding: '8px 10px',
        }}>{err}</div>
      )}

      {inspect && !jobs.length && (
        <div style={{
          fontSize: 12, color: T.textMuted, padding: '18px 10px', textAlign: 'center',
          border: `1px dashed ${T.border}`, borderRadius: 6,
        }}>
          No GitHub workflows found in this project.
        </div>
      )}

      {!!jobs.length && (
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <button onClick={() => start('')} disabled={busy} style={{
            display: 'flex', alignItems: 'center', gap: 6, cursor: busy ? 'wait' : 'pointer',
            background: busy ? T.surfaceAlt : T.accent, color: busy ? T.textMuted : T.bg,
            border: 'none', borderRadius: 5, padding: '6px 12px', fontSize: 12,
          }}>
            <I name={busy ? 'clock' : 'play'} size={12} color={busy ? T.textMuted : T.bg} />
            {busy ? 'running…' : 'Run full CI'}
          </button>
          <label title="Attempt jobs whose runs-on targets a different operating system. They are labelled cross-OS and can never produce FULL_CI_PASS."
            style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, color: T.textMuted, cursor: 'pointer' }}>
            <input type="checkbox" checked={allowForeign}
              onChange={e => setAllowForeign(e.target.checked)} />
            also run other-OS jobs (cross-OS)
          </label>
          {run && !!(run.failed || []).length && (
            <button onClick={() => start('')} disabled={busy} style={{
              background: 'transparent', color: T.warn, border: `1px solid ${T.warn}`,
              borderRadius: 5, padding: '5px 10px', fontSize: 11, cursor: 'pointer',
            }}>Re-run all</button>
          )}
        </div>
      )}

      {run && <CiVerdict verdict={run.verdict} note={run.note} />}

      {!!runJobs.length && (
        <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 6 }}>
          <div style={{ padding: '7px 10px', fontSize: 11, color: T.textDim, borderBottom: `1px solid ${T.border}` }}>
            RUN {run.run_id} · {(run.fingerprint || {}).branch || '?'}
            {(run.fingerprint || {}).dirty
              ? ` · ${run.fingerprint.dirty_files} uncommitted file(s)` : ' · clean tree'}
          </div>
          {runJobs.map(j => (
            <div key={j.key}>
              <div style={{
                display: 'flex', alignItems: 'center', gap: 9, padding: '7px 10px',
                borderTop: `1px solid ${T.border}`, fontSize: 12,
              }}>
                <CiStatusPill status={j.status} title={j.reason || j.status} />
                <span className="mono" style={{
                  color: T.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{j.key}</span>
                {j.status === 'passed' && j.fidelity === 'container' && (
                  <span className="mono" title={`Ran in a container of ${j.runs_on} via act — real actions included.`}
                    style={{ fontSize: 10, color: T.accent }}>container</span>
                )}
                {j.status === 'passed' && j.fidelity === 'cross-os' && (
                  <span className="mono" title={`Ran on this host, but targets ${j.runs_on}. Indicative, not equivalent.`}
                    style={{ fontSize: 10, color: T.blue }}>cross-OS</span>
                )}
                <div style={{ flex: 1 }} />
                {!!j.duration_ms && (
                  <span className="mono" style={{ fontSize: 10, color: T.textDim }}>
                    {(j.duration_ms / 1000).toFixed(1)}s
                  </span>
                )}
                {j.log_path && (
                  <div onClick={() => openLog(j.key)} title="View log"
                    style={{ cursor: 'pointer', padding: 3 }}>
                    <I name="terminal" size={12} color={T.textMuted} />
                  </div>
                )}
              </div>
              {j.reason && j.status !== 'passed' && (
                <div style={{ fontSize: 11, color: T.textDim, padding: '0 10px 6px 26px' }}>
                  {j.reason}
                </div>
              )}
              <CiFailureList job={j} />
            </div>
          ))}
        </div>
      )}

      {/* The graph, including what we cannot run — silence here would read as
          "this repo has three jobs" when it has fifteen. */}
      {!!jobs.length && (
        <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 6 }}>
          <div style={{ padding: '7px 10px', fontSize: 11, color: T.textDim, borderBottom: `1px solid ${T.border}` }}>
            JOB GRAPH — dependency order
          </div>
          {jobs.map(j => (
            <div key={j.key} style={{
              display: 'flex', alignItems: 'center', gap: 9, padding: '5px 10px',
              borderTop: `1px solid ${T.border}`, fontSize: 11,
            }}>
              {/* Read the server's partition; never re-derive it here. The
                  first attempt inferred runnability in JS from act_could_run,
                  which means "act has no blockers with this job" and NOT "act
                  can run it here" — act only does Linux. Every macOS cell
                  rendered as a green PASS it could never earn. */}
              <CiStatusPill
                status={runnableNative.has(j.key) ? 'cap_native'
                  : runnableContainer.has(j.key) ? 'cap_container'
                    : unsupportedKeys.has(j.key) ? 'cap_unsupported'
                      : 'cap_unreachable'}
                title={runnableNative.has(j.key) ? 'runnable natively on this host'
                  : runnableContainer.has(j.key)
                    ? `runs in a container (${j.runs_on}) via act`
                    : unsupportedKeys.has(j.key)
                      ? (j.blockers || []).join('; ')
                      : `targets ${j.runs_on} — no available engine can run it here`} />
              <span className="mono" style={{ color: T.textMuted }}>{j.key}</span>
              {!!(j.needs || []).length && (
                <span className="mono" style={{ fontSize: 10, color: T.textDim }}>
                  needs {j.needs.join(', ')}
                </span>
              )}
              <div style={{ flex: 1 }} />
              {j.supported && !j.foreign_runner && (
                <div onClick={() => start(j.key)} title="Run just this job"
                  style={{ cursor: busy ? 'wait' : 'pointer', padding: 3 }}>
                  <I name="play" size={11} color={T.textMuted} />
                </div>
              )}
              {!j.supported && (
                <span style={{ fontSize: 10, color: T.warn, maxWidth: 420, textAlign: 'right' }}>
                  {(j.blockers || [])[0]}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {!!runs.length && (
        <div style={{ background: T.surface, border: `1px solid ${T.border}`, borderRadius: 6 }}>
          <div style={{ padding: '7px 10px', fontSize: 11, color: T.textDim, borderBottom: `1px solid ${T.border}` }}>
            RECENT RUNS
          </div>
          {runs.slice(0, 8).map(r => (
            <div key={r.run_id} style={{
              display: 'flex', alignItems: 'center', gap: 9, padding: '5px 10px',
              borderTop: `1px solid ${T.border}`, fontSize: 11,
            }}>
              <span className="mono" style={{
                color: r.verdict === 'FULL_CI_PASS' ? T.accent
                  : (r.verdict === 'FAIL' ? T.error : T.warn),
              }}>{r.verdict}</span>
              <span className="mono" style={{ color: T.textDim }}>{r.run_id}</span>
              <span style={{ color: T.textMuted }}>{(r.started_at || '').slice(0, 19).replace('T', ' ')}</span>
              <div style={{ flex: 1 }} />
              <span className="mono" style={{ fontSize: 10, color: T.textDim }}>
                {Object.entries(r.counts || {}).map(([k, v]) => `${k}=${v}`).join(' ')}
              </span>
            </div>
          ))}
        </div>
      )}

      {log && (
        <div onClick={() => setLog(null)} style={{
          position: 'fixed', inset: 0, background: '#0008', zIndex: 50,
          display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 30,
        }}>
          <div onClick={e => e.stopPropagation()} style={{
            background: T.bg, border: `1px solid ${T.border}`, borderRadius: 8,
            width: '100%', maxWidth: 1000, maxHeight: '80vh', display: 'flex',
            flexDirection: 'column',
          }}>
            <div style={{
              padding: '9px 12px', borderBottom: `1px solid ${T.border}`,
              display: 'flex', alignItems: 'center', gap: 8,
            }}>
              <I name="terminal" size={13} color={T.textMuted} />
              <span className="mono" style={{ fontSize: 12, color: T.text }}>{log.job}</span>
              <div style={{ flex: 1 }} />
              <div onClick={() => setLog(null)} style={{ cursor: 'pointer' }}>
                <I name="xSmall" size={14} color={T.textMuted} />
              </div>
            </div>
            <pre className="mono" style={{
              margin: 0, padding: 12, overflow: 'auto', fontSize: 11,
              color: T.textMuted, whiteSpace: 'pre-wrap',
            }}>{log.text}</pre>
          </div>
        </div>
      )}
    </div>
  );
}
