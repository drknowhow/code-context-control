"""Federated search + recall across a parent project's sub-projects.

Children carry their own ``.c3`` (the parent's index excludes them — see
``services/subprojects.py``), so cross-scope visibility is restored here:
``c3_search(scope=...)`` fans out to child indexes and ``c3_memory`` recall
unions child facts. Results stay in per-scope sections — TF-IDF scores are
not comparable across corpora, so no interleaved ranking.
"""


# Budget split for scope='all': the parent keeps the lion's share.
_PARENT_BUDGET_SHARE = 0.6
_CHILD_TOP_K_CAP = 3
_MIN_CHILD_TOKENS = 150


def _child_runtime(path: str):
    """Indirection point (monkeypatched in tests) -> child ``C3Runtime``."""
    from services.project_runtime import shared_cache
    return shared_cache().get(path)


def _noop_finalize(_name, _args, resp, _summ="", **_kw):
    return resp


def _noop_facts(*_a, **_kw):
    return ""


def subproject_scopes(svc, scope: str = "all") -> list:
    """Resolve ``scope`` to child targets ``[{name, path}]``.

    ``'all'`` -> every linked DESCENDANT that still has a usable ``.c3``,
    nearest level first; ``'<name>'`` -> that descendant only. Capped by
    ``hybrid.subprojects.max_children_per_query``.

    The walk is breadth-first so that when the cap bites it drops the most
    distant relatives rather than an arbitrary slice: a direct child is more
    likely to be relevant to the parent's query than a grandchild.
    """
    project_path = str(getattr(svc, "project_path", "") or "")
    if not project_path:
        return []
    try:
        from services.subprojects import SubprojectManager
        children = [c for c in SubprojectManager(project_path).descendants()
                    if c["status"] not in ("missing_folder", "missing_c3")]
    except Exception:
        return []
    if not children:
        return []
    if scope and scope != "all":
        want = scope.strip().lower()
        children = [c for c in children if (c.get("name") or "").lower() == want]
    try:
        from core.config import load_hybrid_config
        cfg = load_hybrid_config(project_path).get("subprojects") or {}
        cap = int(cfg.get("max_children_per_query", 8) or 8)
    except Exception:
        cap = 8
    return [{"name": c["name"], "path": c["path"]} for c in children[:cap]]


def federated_search(query: str, action: str, top_k: int, max_tokens: int,
                     svc, finalize, maybe_facts, scope: str,
                     ignore_case: bool = False, path: str = "", lang: str = "",
                     kind: str = "") -> str:
    """Sectioned fan-out search: parent (scope='all') + linked children.

    A child failure renders as a one-line error inside its section — it never
    fails the whole call.
    """
    from cli.tools.search import handle_search

    targets = subproject_scopes(svc, scope)
    parts = []
    extra = {"ignore_case": ignore_case, "path": path, "lang": lang, "kind": kind}

    if scope == "all":
        parent_budget = max(200, int(max_tokens * _PARENT_BUDGET_SHARE)) if targets else max_tokens
        parts.append(handle_search(query, action, top_k, parent_budget, svc,
                                   _noop_finalize, maybe_facts, **extra))

    if not targets:
        if scope != "all":
            return finalize("c3_search", {"action": action, "scope": scope},
                            f"[search:scope:{scope}] no linked sub-project matches "
                            "(c3_project(action='subprojects') lists them)", "error")
    else:
        share = (1.0 - _PARENT_BUDGET_SHARE) if scope == "all" else 1.0
        child_budget = max(_MIN_CHILD_TOKENS, int(max_tokens * share / len(targets)))
        child_k = max(1, min(int(top_k), _CHILD_TOP_K_CAP))
        for t in targets:
            try:
                fsvc = _child_runtime(t["path"])
                body = handle_search(query, action, child_k, child_budget, fsvc,
                                     _noop_finalize, _noop_facts, **extra)
            except Exception as e:
                body = f"[error] {type(e).__name__}: {e}"
            parts.append(f"=== [sub:{t['name']}] ===\n{body}")

    resp = "\n\n".join(p for p in parts if p)
    return finalize("c3_search", {"action": action, "scope": scope},
                    resp, f"federated:{len(targets)} sub(s)")


def federated_recall(query: str, top_k: int, svc, scope: str = "all") -> list:
    """Tagged child fact lines for the parent's recall union.

    Cheap by design: instantiates each child's ``MemoryStore`` directly
    (facts.json load) instead of building a full child runtime.
    """
    from services.memory import MemoryStore

    lines = []
    child_k = max(1, min(int(top_k), _CHILD_TOP_K_CAP))
    for t in subproject_scopes(svc, scope):
        try:
            store = MemoryStore(t["path"])
            for f in store.recall(query, top_k=child_k):
                fact = (f.get("fact") or "").strip()
                if fact:
                    lines.append(f"[sub:{t['name']}][{f.get('category', 'general')}] {fact}")
        except Exception:
            continue
    return lines
