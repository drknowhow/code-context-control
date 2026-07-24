// ─── Toasts ────────────────────────────────────────────────────
// Global `notify(msg, kind)` + `notifyProgress(id, {...})` — ToastHost
// (mounted once in App) registers the dispatch hooks.
let _toastDispatch = null;

const notify = (msg, kind = 'ok') => {
  if (_toastDispatch) _toastDispatch({ type: 'push', msg, kind });
};

// Progress toast: call repeatedly with the same id to update; done:true removes
// after a grace period. spec: {label, current, total, done, error, cancelled, onCancel}
const notifyProgress = (id, spec) => {
  if (_toastDispatch) _toastDispatch({ type: 'progress', id, spec });
};

function ToastHost() {
  const [toasts, setToasts] = React.useState([]);

  React.useEffect(() => {
    _toastDispatch = (action) => {
      if (action.type === 'push') {
        const id = `t${Date.now()}${Math.random()}`;
        setToasts(ts => [...ts, { id, msg: action.msg, kind: action.kind }]);
        setTimeout(() => setToasts(ts => ts.filter(t => t.id !== id)), 4200);
      } else if (action.type === 'progress') {
        setToasts(ts => {
          const rest = ts.filter(t => t.id !== action.id);
          const next = [...rest, { id: action.id, progress: action.spec }];
          if (action.spec && action.spec.done) {
            setTimeout(() => setToasts(cur => cur.filter(t => t.id !== action.id)), 3500);
          }
          return next;
        });
      }
    };
    return () => { _toastDispatch = null; };
  }, []);

  const kindColor = (k) => k === 'err' ? T.error : k === 'warn' ? T.warn : T.accent;

  return (
    <div style={{
      position: 'fixed', bottom: 18, right: 18, zIndex: 500,
      display: 'flex', flexDirection: 'column', gap: 8, maxWidth: 380,
    }}>
      {toasts.map(t => t.progress ? (
        <div key={t.id} className="fade-up" style={{
          background: T.surface, border: `1px solid ${T.border}`, borderRadius: 8,
          padding: '10px 14px', fontSize: 12,
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, marginBottom: 6 }}>
            <span style={{ color: T.text, fontWeight: 600 }}>{t.progress.label}</span>
            <span className="mono" style={{ color: t.progress.error ? T.error : T.textMuted }}>
              {t.progress.done
                ? (t.progress.error ? 'failed' : (t.progress.cancelled ? 'cancelled' : 'done'))
                : `${t.progress.current || 0}/${t.progress.total || '?'}`}
            </span>
            {!t.progress.done && t.progress.onCancel && (
              <span onClick={t.progress.onCancel} style={{
                cursor: 'pointer', color: T.textMuted, fontSize: 11,
                textDecoration: 'underline', flexShrink: 0,
              }}>cancel</span>
            )}
          </div>
          <ProgressBar value={t.progress.current || 0} max={t.progress.total || 1}
            color={t.progress.error ? T.error : T.accent} height={4} />
        </div>
      ) : (
        <div key={t.id} className="fade-up" style={{
          background: T.surface, border: `1px solid ${kindColor(t.kind)}50`,
          borderLeft: `3px solid ${kindColor(t.kind)}`, borderRadius: 8,
          padding: '10px 14px', fontSize: 12, color: T.text,
        }}>{t.msg}</div>
      ))}
    </div>
  );
}
