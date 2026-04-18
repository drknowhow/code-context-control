"""Unified benchmark dashboard.

Aggregates results from the three C3 benchmark tiers into one HTML index:
  - Quick  (local synthetic) -> .c3/benchmark/runs/benchmark_*.json
  - Session (workflow synthetic) -> .c3/session_benchmark/runs/session_*.json
  - E2E    (real AI CLI calls)  -> .c3/e2e_benchmark/runs/*.json
  - Delegate (Ollama vs Codex)  -> .c3/e2e_benchmark/runs/delegate_*.json

Output: .c3/benchmarks/index.html — a single entry point with tier badges,
latest metrics, run history tables, and links to the detailed per-tier reports.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Optional


TIER_BADGES = {
    "quick": ("Synthetic", "#818cf8"),
    "session": ("Synthetic", "#818cf8"),
    "e2e": ("Live AI", "#34d399"),
    "delegate": ("Live AI", "#34d399"),
    "external": ("External", "#fbbf24"),
}


def _load_runs(runs_dir: Path, prefix: str = "") -> list[dict]:
    if not runs_dir.exists():
        return []
    runs: list[dict] = []
    for f in sorted(runs_dir.glob("*.json")):
        if prefix and not f.name.startswith(prefix):
            continue
        try:
            runs.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    runs.sort(key=lambda r: r.get("timestamp", ""))
    return runs


def _latest_quick_metrics(run: dict) -> dict[str, Any]:
    sc = run.get("scorecard", {})
    tok = sc.get("token_usage", {}) if isinstance(sc, dict) else {}
    perf = sc.get("performance", {}) if isinstance(sc, dict) else {}
    return {
        "timestamp": run.get("timestamp", ""),
        "token_savings_pct": tok.get("savings_pct", 0),
        "budget_multiplier": tok.get("prompt_budget_multiplier", 0),
        "quality_c3": perf.get("with_c3_quality_pct", 0),
        "quality_baseline": perf.get("without_c3_quality_pct", 0),
        "files_considered": run.get("files_considered", 0),
    }


def _latest_session_metrics(run: dict) -> dict[str, Any]:
    sc = run.get("scorecard", {})
    lon = run.get("session_longevity", {})
    return {
        "timestamp": run.get("timestamp", ""),
        "token_savings_pct": sc.get("token_savings_pct", 0),
        "budget_multiplier": sc.get("budget_multiplier", 0),
        "quality_c3": sc.get("avg_quality_c3", 0),
        "quality_baseline": sc.get("avg_quality_baseline", 0),
        "turns_c3": lon.get("estimated_turns_c3", 0),
        "turns_baseline": lon.get("estimated_turns_baseline", 0),
        "turn_multiplier": lon.get("turn_multiplier", 0),
        "scenarios": len(run.get("scenarios", [])),
    }


def _latest_e2e_metrics(run: dict) -> dict[str, Any]:
    sc = run.get("scorecard", {})
    eff = run.get("efficiency_summary", {})
    return {
        "timestamp": run.get("timestamp", ""),
        "win_rate_c3": sc.get("win_rate_c3", 0),
        "avg_score_c3": sc.get("avg_score_c3", 0),
        "avg_score_baseline": sc.get("avg_score_baseline", 0),
        "score_delta": sc.get("avg_score_delta", 0),
        "time_saved_s": eff.get("total_time_saved_s", 0),
        "cost_saved_usd": eff.get("total_cost_saved_usd", 0),
        "tokens_saved": eff.get("total_tokens_saved", 0),
        "providers": run.get("providers_tested", []),
        "tasks": run.get("tasks_run", 0),
    }


def _latest_delegate_metrics(run: dict) -> dict[str, Any]:
    backends = run.get("backends", {})
    return {
        "timestamp": run.get("timestamp", ""),
        "backends": backends,
        "total_results": run.get("total_results", 0),
    }


def _latest_external_metrics(run: dict) -> dict[str, Any]:
    sc = run.get("scorecard", {})
    return {
        "timestamp": run.get("timestamp", ""),
        "suite": run.get("suite", run.get("benchmark_type", "external")),
        "model": run.get("model", ""),
        "languages": run.get("languages", []),
        "exercises_run": run.get("exercises_run", 0),
        "with_c3_pass_rate": sc.get("with_c3_pass_rate", 0),
        "baseline_pass_rate": sc.get("baseline_pass_rate", 0),
        "pass_rate_delta": sc.get("pass_rate_delta", 0),
        "with_c3_avg_latency_s": sc.get("with_c3_avg_latency_s", 0),
        "baseline_avg_latency_s": sc.get("baseline_avg_latency_s", 0),
        "with_c3_total_cost_usd": sc.get("with_c3_total_cost_usd", 0),
        "baseline_total_cost_usd": sc.get("baseline_total_cost_usd", 0),
    }


def _fmt_ts(ts: str) -> str:
    if not ts:
        return "—"
    return html.escape(ts.replace("T", " ").replace("Z", ""))


def _badge(tier: str) -> str:
    label, color = TIER_BADGES.get(tier, ("Unknown", "#888"))
    return f'<span class="tier-badge" style="background:{color}">{label}</span>'


def _card_quick(runs: list[dict], detail_url: str) -> str:
    if not runs:
        return _empty_card("Quick", "quick",
                           "Local synthetic micro-benchmark: compression, retrieval, file maps.",
                           "c3 bench quick")
    m = _latest_quick_metrics(runs[-1])
    metrics_html = f"""
      <div class="card-metric"><span class="metric-label">Token savings</span><span class="metric-val good">{m['token_savings_pct']}%</span></div>
      <div class="card-metric"><span class="metric-label">Budget multiplier</span><span class="metric-val">{m['budget_multiplier']}x</span></div>
      <div class="card-metric"><span class="metric-label">Quality (C3 / base)</span><span class="metric-val">{m['quality_c3']:.0f}% / {m['quality_baseline']:.0f}%</span></div>
      <div class="card-metric"><span class="metric-label">Files sampled</span><span class="metric-val">{m['files_considered']}</span></div>
    """
    return _wrap_card("Quick", "quick", metrics_html, m["timestamp"], len(runs), detail_url)


def _card_session(runs: list[dict], detail_url: str) -> str:
    if not runs:
        return _empty_card("Session", "session",
                           "6 workflow scenarios (bug, feature, review, log, refactor, onboarding).",
                           "c3 bench session")
    m = _latest_session_metrics(runs[-1])
    metrics_html = f"""
      <div class="card-metric"><span class="metric-label">Token savings</span><span class="metric-val good">{m['token_savings_pct']}%</span></div>
      <div class="card-metric"><span class="metric-label">Budget multiplier</span><span class="metric-val">{m['budget_multiplier']}x</span></div>
      <div class="card-metric"><span class="metric-label">Quality (C3 / base)</span><span class="metric-val">{m['quality_c3']:.0f}% / {m['quality_baseline']:.0f}%</span></div>
      <div class="card-metric"><span class="metric-label">Session turns</span><span class="metric-val">{m['turns_c3']:.0f} vs {m['turns_baseline']:.0f} ({m['turn_multiplier']}x)</span></div>
    """
    return _wrap_card("Session", "session", metrics_html, m["timestamp"], len(runs), detail_url)


def _card_e2e(runs: list[dict], detail_url: str) -> str:
    if not runs:
        return _empty_card("E2E", "e2e",
                           "Real AI CLI calls (claude / gemini / codex). The most credible tier.",
                           "c3 bench e2e")
    m = _latest_e2e_metrics(runs[-1])
    providers = ", ".join(m["providers"]) or "—"
    metrics_html = f"""
      <div class="card-metric"><span class="metric-label">Win rate (C3)</span><span class="metric-val good">{m['win_rate_c3']:.1f}%</span></div>
      <div class="card-metric"><span class="metric-label">Avg score (C3 / base)</span><span class="metric-val">{m['avg_score_c3']:.3f} / {m['avg_score_baseline']:.3f}</span></div>
      <div class="card-metric"><span class="metric-label">Time / cost saved</span><span class="metric-val">{m['time_saved_s']:.0f}s / ${m['cost_saved_usd']:.4f}</span></div>
      <div class="card-metric"><span class="metric-label">Providers</span><span class="metric-val">{html.escape(providers)}</span></div>
    """
    return _wrap_card("E2E", "e2e", metrics_html, m["timestamp"], len(runs), detail_url)


def _card_delegate(runs: list[dict]) -> str:
    if not runs:
        return _empty_card("Delegate", "delegate",
                           "Ollama vs Codex on the same tasks. Measures delegate backend quality.",
                           "c3 bench delegate")
    m = _latest_delegate_metrics(runs[-1])
    rows = []
    for backend, stats in m["backends"].items():
        rows.append(
            f'<div class="card-metric"><span class="metric-label">{html.escape(backend)}</span>'
            f'<span class="metric-val">{stats.get("success_rate", 0)}% ({stats.get("successes", 0)}/{stats.get("tasks_run", 0)}) '
            f'· {stats.get("avg_latency_s", 0)}s</span></div>'
        )
    metrics_html = "\n".join(rows) or '<div class="card-metric"><span class="metric-label">No backends</span></div>'
    return _wrap_card("Delegate", "delegate", metrics_html, m["timestamp"], len(runs), "")


def _card_external(runs: list[dict]) -> str:
    if not runs:
        return _empty_card("External", "external",
                           "Aider Polyglot / SWE-bench. Third-party benchmarks for credible cross-tool comparisons.",
                           "c3 bench external --suite aider-polyglot")
    m = _latest_external_metrics(runs[-1])
    delta = m["pass_rate_delta"]
    delta_color = "#34d399" if delta > 0 else ("#f87171" if delta < 0 else "#fbbf24")
    langs = ", ".join(m["languages"]) or "—"
    metrics_html = f"""
      <div class="card-metric"><span class="metric-label">Suite</span><span class="metric-val">{html.escape(m['suite'])}</span></div>
      <div class="card-metric"><span class="metric-label">Pass rate (C3)</span><span class="metric-val good">{m['with_c3_pass_rate']}%</span></div>
      <div class="card-metric"><span class="metric-label">Pass rate (base)</span><span class="metric-val">{m['baseline_pass_rate']}%</span></div>
      <div class="card-metric"><span class="metric-label">Delta</span><span class="metric-val" style="color:{delta_color}">{delta:+.1f} pp</span></div>
      <div class="card-metric"><span class="metric-label">Exercises / langs</span><span class="metric-val">{m['exercises_run']} · {html.escape(langs)}</span></div>
    """
    return _wrap_card("External", "external", metrics_html, m["timestamp"], len(runs), "")


def _wrap_card(title: str, tier: str, metrics_html: str, ts: str, run_count: int, detail_url: str) -> str:
    link = f'<a class="card-link" href="{html.escape(detail_url)}">Open detail →</a>' if detail_url else ""
    return f"""
    <div class="card">
      <div class="card-head">
        <span class="card-title">{html.escape(title)}</span>
        {_badge(tier)}
      </div>
      <div class="card-metrics">{metrics_html}</div>
      <div class="card-foot">
        <span class="card-ts">Last run: {_fmt_ts(ts)}</span>
        <span class="card-runs">{run_count} run{'' if run_count == 1 else 's'}</span>
        {link}
      </div>
    </div>"""


def _empty_card(title: str, tier: str, desc: str, cmd: str) -> str:
    return f"""
    <div class="card empty">
      <div class="card-head">
        <span class="card-title">{html.escape(title)}</span>
        {_badge(tier)}
      </div>
      <div class="card-metrics"><p class="empty-desc">{html.escape(desc)}</p></div>
      <div class="card-foot">
        <span class="card-ts">Not yet run</span>
        <code class="card-cmd">{html.escape(cmd)}</code>
      </div>
    </div>"""


def _runs_table(runs: list[dict], metric_extractor, headers: list[str]) -> str:
    if not runs:
        return '<p class="muted">No runs yet.</p>'
    rows = []
    for run in reversed(runs[-20:]):  # newest first, cap at 20
        m = metric_extractor(run)
        cells = [f'<td>{_fmt_ts(m["timestamp"])}</td>']
        for h in headers[1:]:
            val = m.get(h["key"], "")
            fmt = h.get("fmt", "{}")
            if isinstance(val, float):
                cells.append(f'<td>{fmt.format(val)}</td>')
            elif isinstance(val, list):
                cells.append(f'<td>{html.escape(", ".join(str(v) for v in val))}</td>')
            else:
                cells.append(f'<td>{fmt.format(val) if val else "—"}</td>')
        rows.append(f'<tr>{"".join(cells)}</tr>')
    head = "".join(f'<th>{h["label"]}</th>' for h in headers)
    return f'<table class="runs-table"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'


def _project_rel(project_path: Path, target: Path) -> str:
    """Best-effort relative path for in-dashboard links."""
    try:
        return str(target.relative_to(project_path.parent)).replace("\\", "/")
    except Exception:
        return target.as_uri() if target.exists() else ""


def generate_dashboard(project_path: str) -> Path:
    """Generate the unified benchmark dashboard HTML.

    Returns the path to the written HTML file.
    """
    root = Path(project_path).resolve()
    quick_runs = _load_runs(root / ".c3" / "benchmark" / "runs")
    session_runs = _load_runs(root / ".c3" / "session_benchmark" / "runs")
    all_e2e = _load_runs(root / ".c3" / "e2e_benchmark" / "runs")
    e2e_runs = [r for r in all_e2e if r.get("benchmark_type") != "delegate_comparison"
                and "backends" not in r]
    delegate_runs = [r for r in all_e2e if r.get("benchmark_type") == "delegate_comparison"
                     or "backends" in r]
    external_runs = _load_runs(root / ".c3" / "external_benchmark" / "runs")

    quick_html = root / ".c3" / "benchmark" / "latest.html"
    session_html = root / ".c3" / "session_benchmark" / "latest.html"
    e2e_html = root / ".c3" / "e2e_benchmark" / "latest.html"

    def _rel_or_empty(p: Path) -> str:
        return f"../{p.relative_to(root).as_posix()}" if p.exists() else ""

    cards = [
        _card_quick(quick_runs, _rel_or_empty(quick_html)),
        _card_session(session_runs, _rel_or_empty(session_html)),
        _card_e2e(e2e_runs, _rel_or_empty(e2e_html)),
        _card_delegate(delegate_runs),
        _card_external(external_runs),
    ]

    quick_table = _runs_table(
        quick_runs, _latest_quick_metrics,
        [
            {"key": "timestamp", "label": "Run"},
            {"key": "token_savings_pct", "label": "Token savings", "fmt": "{}%"},
            {"key": "budget_multiplier", "label": "Budget", "fmt": "{}x"},
            {"key": "quality_c3", "label": "Quality C3", "fmt": "{:.0f}%"},
            {"key": "quality_baseline", "label": "Quality base", "fmt": "{:.0f}%"},
            {"key": "files_considered", "label": "Files"},
        ],
    )
    session_table = _runs_table(
        session_runs, _latest_session_metrics,
        [
            {"key": "timestamp", "label": "Run"},
            {"key": "token_savings_pct", "label": "Token savings", "fmt": "{}%"},
            {"key": "budget_multiplier", "label": "Budget", "fmt": "{}x"},
            {"key": "quality_c3", "label": "Quality C3", "fmt": "{:.0f}%"},
            {"key": "turn_multiplier", "label": "Turn mult", "fmt": "{}x"},
            {"key": "scenarios", "label": "Scenarios"},
        ],
    )
    e2e_table = _runs_table(
        e2e_runs, _latest_e2e_metrics,
        [
            {"key": "timestamp", "label": "Run"},
            {"key": "win_rate_c3", "label": "Win rate", "fmt": "{:.1f}%"},
            {"key": "avg_score_c3", "label": "Score C3", "fmt": "{:.3f}"},
            {"key": "avg_score_baseline", "label": "Score base", "fmt": "{:.3f}"},
            {"key": "time_saved_s", "label": "Time saved", "fmt": "{:.0f}s"},
            {"key": "cost_saved_usd", "label": "Cost saved", "fmt": "${:.4f}"},
            {"key": "providers", "label": "Providers"},
            {"key": "tasks", "label": "Tasks"},
        ],
    )
    external_table = _runs_table(
        external_runs, _latest_external_metrics,
        [
            {"key": "timestamp", "label": "Run"},
            {"key": "suite", "label": "Suite"},
            {"key": "model", "label": "Model"},
            {"key": "with_c3_pass_rate", "label": "Pass C3", "fmt": "{}%"},
            {"key": "baseline_pass_rate", "label": "Pass base", "fmt": "{}%"},
            {"key": "pass_rate_delta", "label": "Delta", "fmt": "{:+.1f}pp"},
            {"key": "exercises_run", "label": "Exercises"},
            {"key": "languages", "label": "Languages"},
        ],
    )

    out_dir = root / ".c3" / "benchmarks"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "index.html"

    html_doc = _render_dashboard_html(
        project_path=str(root),
        cards_html="\n".join(cards),
        quick_table=quick_table,
        session_table=session_table,
        e2e_table=e2e_table,
        external_table=external_table,
        delegate_runs=delegate_runs,
        counts={
            "quick": len(quick_runs),
            "session": len(session_runs),
            "e2e": len(e2e_runs),
            "delegate": len(delegate_runs),
            "external": len(external_runs),
        },
    )
    out_path.write_text(html_doc, encoding="utf-8")
    return out_path


def _render_dashboard_html(
    *,
    project_path: str,
    cards_html: str,
    quick_table: str,
    session_table: str,
    e2e_table: str,
    external_table: str,
    delegate_runs: list[dict],
    counts: dict[str, int],
) -> str:
    import time as _time
    generated_at = _time.strftime("%Y-%m-%d %H:%M:%S")
    project_name = html.escape(Path(project_path).name)

    delegate_detail = ""
    if delegate_runs:
        latest = delegate_runs[-1]
        backends = latest.get("backends", {})
        rows = []
        for backend, stats in backends.items():
            rows.append(f"""
              <tr>
                <td><strong>{html.escape(backend)}</strong></td>
                <td>{stats.get('success_rate', 0)}%</td>
                <td>{stats.get('successes', 0)}/{stats.get('tasks_run', 0)}</td>
                <td>{stats.get('avg_latency_s', 0)}s</td>
                <td>{stats.get('avg_output_tokens', 0)}</td>
                <td>{html.escape(', '.join(stats.get('models_used', [])))}</td>
              </tr>""")
        delegate_detail = f"""
        <table class="runs-table">
          <thead><tr>
            <th>Backend</th><th>Success</th><th>Runs</th><th>Avg latency</th>
            <th>Avg tokens</th><th>Models</th>
          </tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>"""
    else:
        delegate_detail = '<p class="muted">No delegate runs yet. Try: <code>c3 bench delegate</code></p>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>C3 Benchmark Dashboard · {project_name}</title>
<style>
  :root {{
    --bg: #0b1020;
    --surface: #131932;
    --surface2: #1c2444;
    --border: #2a3560;
    --text: #e6ebff;
    --text-dim: #9aa3c7;
    --accent: #818cf8;
    --good: #34d399;
    --warn: #fbbf24;
    --bad: #f87171;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 2rem 1.5rem; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    line-height: 1.5;
  }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  header {{
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 2rem; padding-bottom: 1rem; border-bottom: 1px solid var(--border);
  }}
  h1 {{ margin: 0; font-size: 1.6rem; font-weight: 600; }}
  .subtitle {{ color: var(--text-dim); font-size: 0.9rem; margin-top: 0.25rem; }}
  .meta {{ text-align: right; color: var(--text-dim); font-size: 0.85rem; }}
  .cards {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 1rem; margin-bottom: 2rem;
  }}
  .card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 1.1rem; display: flex; flex-direction: column;
  }}
  .card.empty {{ opacity: 0.65; border-style: dashed; }}
  .card-head {{
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 0.9rem;
  }}
  .card-title {{ font-size: 1.05rem; font-weight: 600; }}
  .tier-badge {{
    padding: 0.15rem 0.55rem; border-radius: 999px; font-size: 0.7rem;
    font-weight: 600; color: #0b1020;
  }}
  .card-metrics {{ flex: 1; }}
  .card-metric {{
    display: flex; justify-content: space-between; padding: 0.4rem 0;
    border-bottom: 1px dashed var(--surface2); font-size: 0.9rem;
  }}
  .card-metric:last-child {{ border-bottom: none; }}
  .metric-label {{ color: var(--text-dim); }}
  .metric-val {{ font-weight: 600; }}
  .metric-val.good {{ color: var(--good); }}
  .empty-desc {{ color: var(--text-dim); font-size: 0.85rem; margin: 0; }}
  .card-foot {{
    margin-top: 0.9rem; padding-top: 0.6rem; border-top: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between; gap: 0.5rem;
    font-size: 0.8rem; color: var(--text-dim); flex-wrap: wrap;
  }}
  .card-link {{ color: var(--accent); text-decoration: none; font-weight: 500; }}
  .card-link:hover {{ text-decoration: underline; }}
  .card-cmd {{
    background: var(--surface2); padding: 0.15rem 0.4rem; border-radius: 4px;
    font-size: 0.75rem; color: var(--accent);
  }}
  .tabs {{
    display: flex; gap: 0.25rem; margin-bottom: 1rem; border-bottom: 1px solid var(--border);
    overflow-x: auto;
  }}
  .tab {{
    padding: 0.6rem 1rem; background: none; border: none; color: var(--text-dim);
    cursor: pointer; font-size: 0.95rem; border-bottom: 2px solid transparent;
    font-family: inherit;
  }}
  .tab.active {{ color: var(--text); border-bottom-color: var(--accent); }}
  .tab .count {{ color: var(--text-dim); font-size: 0.8rem; margin-left: 0.3rem; }}
  .panel {{ display: none; }}
  .panel.active {{ display: block; }}
  h2 {{ font-size: 1.15rem; margin: 0 0 0.8rem 0; }}
  .panel-desc {{ color: var(--text-dim); font-size: 0.9rem; margin-bottom: 1rem; }}
  .runs-table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  .runs-table th, .runs-table td {{
    padding: 0.55rem 0.7rem; text-align: left; border-bottom: 1px solid var(--border);
  }}
  .runs-table th {{ color: var(--text-dim); font-weight: 500; background: var(--surface); }}
  .runs-table tbody tr:hover {{ background: var(--surface); }}
  .muted {{ color: var(--text-dim); }}
  code {{ background: var(--surface2); padding: 0.1rem 0.35rem; border-radius: 3px; font-size: 0.85em; }}
  footer {{ margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--border); color: var(--text-dim); font-size: 0.8rem; }}
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>C3 Benchmark Dashboard</h1>
      <div class="subtitle">{project_name} · aggregated results across all benchmark tiers</div>
    </div>
    <div class="meta">Generated {generated_at}</div>
  </header>

  <section class="cards">
    {cards_html}
  </section>

  <nav class="tabs" role="tablist">
    <button class="tab active" data-tab="quick" role="tab">Quick<span class="count">{counts['quick']}</span></button>
    <button class="tab" data-tab="session" role="tab">Session<span class="count">{counts['session']}</span></button>
    <button class="tab" data-tab="e2e" role="tab">E2E<span class="count">{counts['e2e']}</span></button>
    <button class="tab" data-tab="delegate" role="tab">Delegate<span class="count">{counts['delegate']}</span></button>
    <button class="tab" data-tab="external" role="tab">External<span class="count">{counts['external']}</span></button>
    <button class="tab" data-tab="about" role="tab">About</button>
  </nav>

  <section class="panel active" data-panel="quick">
    <h2>Quick Benchmark — local synthetic <span class="tier-badge" style="background:#818cf8">Synthetic</span></h2>
    <p class="panel-desc">Measures C3's local compression, retrieval, and file-map savings on sampled project files. No AI calls. Run with <code>c3 bench quick</code>.</p>
    {quick_table}
  </section>

  <section class="panel" data-panel="session">
    <h2>Session Benchmark — 6 workflow scenarios <span class="tier-badge" style="background:#818cf8">Synthetic</span></h2>
    <p class="panel-desc">Simulates bug investigation, feature exploration, code review, log triage, refactor planning, and onboarding workflows. Baseline is synthesised heuristically. Run with <code>c3 bench session</code>.</p>
    {session_table}
  </section>

  <section class="panel" data-panel="e2e">
    <h2>End-to-End Benchmark <span class="tier-badge" style="background:#34d399">Live AI</span></h2>
    <p class="panel-desc">Runs real claude / gemini / codex CLI calls against the same tasks, with and without the C3 MCP server. Most credible tier. Run with <code>c3 bench e2e</code>.</p>
    {e2e_table}
  </section>

  <section class="panel" data-panel="delegate">
    <h2>Delegate Benchmark — Ollama vs Codex <span class="tier-badge" style="background:#34d399">Live AI</span></h2>
    <p class="panel-desc">Compares local Ollama against OpenAI Codex as delegate backends across the same task set. Run with <code>c3 bench delegate</code>.</p>
    {delegate_detail}
  </section>

  <section class="panel" data-panel="external">
    <h2>External Benchmarks — Aider Polyglot / SWE-bench <span class="tier-badge" style="background:#fbbf24">External</span></h2>
    <p class="panel-desc">Third-party benchmark suites for credibility outside the C3 repo. Requires cloning the benchmark corpus and installing the CLI (e.g. aider-chat). Run with <code>c3 bench external --suite aider-polyglot</code>.</p>
    {external_table}
  </section>

  <section class="panel" data-panel="about">
    <h2>About the tiers</h2>
    <p class="panel-desc">Three complementary benchmark tiers. Prefer E2E numbers when citing to external audiences; Quick and Session are useful for CI + development iteration.</p>
    <table class="runs-table">
      <thead><tr><th>Tier</th><th>Label</th><th>Cost</th><th>Runtime</th><th>What it measures</th></tr></thead>
      <tbody>
        <tr><td><strong>Quick</strong></td><td><span class="tier-badge" style="background:#818cf8">Synthetic</span></td><td>Free (local)</td><td>~30s</td><td>Compression + retrieval + file-map token savings on sample files</td></tr>
        <tr><td><strong>Session</strong></td><td><span class="tier-badge" style="background:#818cf8">Synthetic</span></td><td>Free (local)</td><td>~2min</td><td>Simulated multi-step workflows; budget multiplier + session longevity</td></tr>
        <tr><td><strong>E2E</strong></td><td><span class="tier-badge" style="background:#34d399">Live AI</span></td><td>Per-token</td><td>10–60min</td><td>Actual AI CLI calls; win rate, tool use, cost, judged quality</td></tr>
        <tr><td><strong>Delegate</strong></td><td><span class="tier-badge" style="background:#34d399">Live AI</span></td><td>Mostly free</td><td>~5min</td><td>Ollama vs Codex delegate backend quality / latency</td></tr>
        <tr><td><strong>External</strong></td><td><span class="tier-badge" style="background:#fbbf24">External</span></td><td>Per-token</td><td>Hours</td><td>Aider Polyglot (live); SWE-bench Lite (planned). Third-party credibility anchor.</td></tr>
      </tbody>
    </table>
  </section>

  <footer>
    Regenerate: <code>c3 bench dashboard</code> · Full run: <code>c3 bench all</code>
  </footer>
</div>
<script>
  document.querySelectorAll('.tab').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const tab = btn.dataset.tab;
      document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t === btn));
      document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.dataset.panel === tab));
    }});
  }});
</script>
</body>
</html>
"""
