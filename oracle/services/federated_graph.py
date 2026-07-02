"""Federated memory graph across up to ~99 C3 projects.

Merges per-project fact graphs into a unified graph, adds cross-project
"cross_similar" edges via embeddings (Ollama, when reachable) with a
TF-IDF fallback so Ollama is optional.

Cache: ~/.c3/oracle/federated_graph.json, invalidated per-project via
.c3/facts/facts.json mtime. Rebuilds only changed projects.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any

from oracle.config import ORACLE_DIR, load_config
from oracle.services.cross_memory import CrossMemory
from oracle.services.memory_reader import MemoryReader

_CACHE_FILE = ORACLE_DIR / "federated_graph.json"
_EMBED_CACHE_FILE = ORACLE_DIR / "federated_embeddings.json"


def _slugify(project_path: str) -> str:
    name = Path(project_path).name or "project"
    digest = hashlib.md5(project_path.encode("utf-8")).hexdigest()[:6]
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower() or "project"
    return f"{slug}-{digest}"


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_]{2,}", (text or "").lower())


def _tfidf_vectors(docs: list[str]) -> tuple[list[dict[str, float]], dict[str, float]]:
    """Return (per-doc sparse tf-idf dict, idf dict)."""
    from collections import Counter
    tokenized = [_tokenize(d) for d in docs]
    df: Counter = Counter()
    for toks in tokenized:
        for t in set(toks):
            df[t] += 1
    n = max(1, len(docs))
    idf = {t: math.log((n + 1) / (df_t + 1)) + 1 for t, df_t in df.items()}
    vectors: list[dict[str, float]] = []
    for toks in tokenized:
        if not toks:
            vectors.append({})
            continue
        tf = Counter(toks)
        length = len(toks)
        vec = {t: (count / length) * idf.get(t, 0.0) for t, count in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors.append({t: v / norm for t, v in vec.items()})
    return vectors, idf


def _sparse_cos(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    shared = set(a) & set(b)
    if not shared:
        return 0.0
    return sum(a[t] * b[t] for t in shared)


def _dense_cos_matrix(matrix: list[list[float]]) -> Any:
    import numpy as np  # lazy
    arr = np.array(matrix, dtype="float32")
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    arr = arr / norms
    return arr @ arr.T


class FederatedGraph:
    """Build and cache a cross-project memory graph."""

    def __init__(self, reader: MemoryReader | None = None,
                 cross_memory: CrossMemory | None = None,
                 ollama_bridge: Any | None = None):
        self.reader = reader or MemoryReader()
        self.cross = cross_memory or CrossMemory()
        self.ollama = ollama_bridge
        self._cfg = load_config()
        self._embed_cache: dict[str, list[float]] = self._load_embed_cache()

    # ── Cache ────────────────────────────────────────────────────────

    def _load_embed_cache(self) -> dict[str, list[float]]:
        if _EMBED_CACHE_FILE.exists():
            try:
                return json.loads(_EMBED_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_embed_cache(self):
        ORACLE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            _EMBED_CACHE_FILE.write_text(
                json.dumps(self._embed_cache), encoding="utf-8"
            )
        except Exception:
            pass

    def _cache_key(self, fact_text: str, model: str) -> str:
        return hashlib.md5(f"{model}:{fact_text}".encode("utf-8")).hexdigest()

    def _project_mtime(self, project_path: str) -> float:
        f = Path(project_path) / ".c3" / "facts" / "facts.json"
        g = Path(project_path) / ".c3" / "facts" / "memory_graph.json"
        m = 0.0
        for p in (f, g):
            if p.is_file():
                try:
                    m = max(m, p.stat().st_mtime)
                except Exception:
                    pass
        return m

    # ── Public API ──────────────────────────────────────────────────

    def build(self, project_paths: list[str], force: bool = False,
              min_sim: float | None = None,
              top_k: int | None = None,
              max_facts_per_project: int | None = None) -> dict:
        if not project_paths:
            return {"nodes": [], "edges": [], "clusters": [], "projects": [], "stats": {}}

        project_paths = project_paths[:99]
        min_sim = float(min_sim if min_sim is not None else self._cfg.get("cross_sim_threshold", 0.75))
        top_k = int(top_k if top_k is not None else self._cfg.get("cross_top_k_neighbors", 3))
        max_per = int(max_facts_per_project if max_facts_per_project is not None
                      else self._cfg.get("cross_max_facts_per_project", 200))

        cached = self._try_cached(project_paths, min_sim, top_k, max_per)
        if cached is not None and not force:
            return self._apply_hierarchy(cached, project_paths)

        projects: list[dict] = []
        nodes: list[dict] = []
        edges: list[dict] = []
        all_fact_nodes: list[dict] = []  # for cross-similarity

        for path in project_paths:
            slug = _slugify(path)
            facts = self.reader.read_facts(path)
            facts = [f for f in facts if f.get("lifecycle") != "archived"]
            facts.sort(key=lambda f: f.get("relevance_count", 0), reverse=True)
            facts = facts[:max_per]
            fact_ids_local = {f.get("id") for f in facts if f.get("id")}

            projects.append({
                "slug": slug,
                "path": path,
                "name": Path(path).name,
                "fact_count": len(facts),
            })

            for f in facts:
                nid = f"{slug}:{f['id']}"
                node = {
                    "id": nid,
                    "kind": "fact",
                    "project": slug,
                    "project_path": path,
                    "local_id": f["id"],
                    "label": (f.get("fact", "")[:80]),
                    "text": f.get("fact", ""),
                    "category": f.get("category", "general"),
                    "relevance": f.get("relevance_count", 0),
                    "confidence": f.get("confidence", 1.0),
                }
                nodes.append(node)
                all_fact_nodes.append(node)

            graph = self.reader.read_graph(path)
            for e in graph.get("edges", []):
                src = e.get("src")
                dst = e.get("dst")
                if src not in fact_ids_local or dst not in fact_ids_local:
                    continue  # skip file/symbol targets + orphaned edges
                edges.append({
                    "src": f"{slug}:{src}",
                    "dst": f"{slug}:{dst}",
                    "type": e.get("type", "co_recalled"),
                    "weight": e.get("weight", 1.0),
                    "scope": "within_project",
                })

        cross_edges, sim_method = self._cross_similar_edges(all_fact_nodes, min_sim, top_k)
        edges.extend(cross_edges)

        insight_edges = self._insight_edges(project_paths, {n["id"] for n in nodes})
        edges.extend(insight_edges)

        clusters = self._clusters(nodes, [e for e in edges if e["scope"] == "within_project"])

        result = {
            "nodes": nodes,
            "edges": edges,
            "clusters": clusters,
            "projects": projects,
            "stats": {
                "total_nodes": len(nodes),
                "total_edges": len(edges),
                "within_project": sum(1 for e in edges if e["scope"] == "within_project"),
                "cross_similar": sum(1 for e in edges if e["scope"] == "cross_similar"),
                "linked_via_insight": sum(1 for e in edges if e["scope"] == "linked_via_insight"),
                "projects": len(projects),
                "similarity_method": sim_method,
                "min_sim": min_sim,
                "top_k": top_k,
            },
            "generated_at": time.time(),
            "inputs": {
                "projects": sorted(project_paths),
                "min_sim": min_sim,
                "top_k": top_k,
                "max_facts_per_project": max_per,
                "mtimes": {p: self._project_mtime(p) for p in project_paths},
            },
        }
        self._save_cache(result)
        return self._apply_hierarchy(result, project_paths)

    def invalidate(self):
        try:
            if _CACHE_FILE.exists():
                _CACHE_FILE.unlink()
        except Exception:
            pass

    # ── Internals ───────────────────────────────────────────────────

    def _apply_hierarchy(self, result: dict, project_paths: list[str]) -> dict:
        """Serve-time overlay of parent/child project links.

        Hierarchy lives in ``.c3/config.json``, which the mtime cache key
        (facts files) never sees — recomputing per serve keeps it fresh
        without invalidating the expensive embedding cache. Applied to both
        fresh builds and cache hits, AFTER the cache write so nothing stale
        is baked in. Project-level links only: fact-level hierarchy edges
        would pollute similarity clustering.
        """
        links = self._hierarchy(project_paths)
        result["hierarchy"] = links
        parent_by_child = {link["child"]: link["parent"] for link in links}
        for proj in result.get("projects", []):
            proj["parent_slug"] = parent_by_child.get(proj.get("slug"), "")
        result.setdefault("stats", {})["parent_child"] = len(links)
        return result

    def _hierarchy(self, project_paths: list[str]) -> list[dict]:
        """``parent_child`` project links for children whose parent is also in
        ``project_paths`` (depth-1 model). Best-effort per project."""
        def _key(p: str) -> str:
            return os.path.normcase(str(Path(p).resolve()))

        by_key: dict[str, str] = {}
        for p in project_paths:
            try:
                by_key[_key(p)] = p
            except Exception:
                continue
        links: list[dict] = []
        for p in project_paths:
            try:
                from services.subprojects import _read_config
                back = _read_config(p).get("parent") or {}
                parent = str(back.get("path") or "")
                if not parent:
                    continue
                parent_in_set = by_key.get(_key(parent))
                if parent_in_set:
                    links.append({
                        "parent": _slugify(parent_in_set),
                        "child": _slugify(p),
                        "type": "parent_child",
                    })
            except Exception:
                continue
        return links

    def _try_cached(self, project_paths: list[str], min_sim: float,
                    top_k: int, max_per: int) -> dict | None:
        if not _CACHE_FILE.exists():
            return None
        try:
            data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
        inputs = data.get("inputs", {})
        if sorted(inputs.get("projects", [])) != sorted(project_paths):
            return None
        if inputs.get("min_sim") != min_sim or inputs.get("top_k") != top_k:
            return None
        if inputs.get("max_facts_per_project") != max_per:
            return None
        ttl = float(self._cfg.get("federated_graph_ttl_sec", 3600))
        if time.time() - float(data.get("generated_at", 0)) > ttl:
            return None
        cached_mtimes = inputs.get("mtimes", {})
        for p in project_paths:
            if self._project_mtime(p) > float(cached_mtimes.get(p, 0)) + 0.001:
                return None
        return data

    def _save_cache(self, data: dict):
        ORACLE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            _CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass

    def _cross_similar_edges(self, fact_nodes: list[dict], min_sim: float,
                             top_k: int) -> tuple[list[dict], str]:
        if len(fact_nodes) < 2:
            return [], "none"

        # Attempt embedding path (Ollama)
        vectors: list[list[float]] | None = None
        method = "tfidf"
        model = self._cfg.get("embedding_model", "nomic-embed-text")
        if self.ollama is not None and hasattr(self.ollama, "embed"):
            try:
                vectors = self._embed_all(fact_nodes, model)
                if vectors:
                    method = f"embedding:{model}"
            except Exception:
                vectors = None

        edges: list[dict] = []
        if vectors:
            try:
                sim = _dense_cos_matrix(vectors)
                edges = self._top_k_edges_from_dense(fact_nodes, sim, min_sim, top_k)
            except Exception:
                vectors = None

        if not vectors:
            docs = [n["text"] for n in fact_nodes]
            tfidf, _ = _tfidf_vectors(docs)
            edges = self._top_k_edges_from_sparse(fact_nodes, tfidf, min_sim, top_k)
            method = "tfidf"

        return edges, method

    def _embed_all(self, fact_nodes: list[dict], model: str) -> list[list[float]] | None:
        uncached_idx: list[int] = []
        uncached_texts: list[str] = []
        result: list[list[float] | None] = [None] * len(fact_nodes)

        for i, n in enumerate(fact_nodes):
            key = self._cache_key(n["text"], model)
            vec = self._embed_cache.get(key)
            if vec:
                result[i] = vec
            else:
                uncached_idx.append(i)
                uncached_texts.append(n["text"])

        # batch in chunks of 32
        for start in range(0, len(uncached_texts), 32):
            batch = uncached_texts[start:start + 32]
            batch_idx = uncached_idx[start:start + 32]
            vecs = None
            if hasattr(self.ollama, "embed_batch"):
                vecs = self.ollama.embed_batch(batch, model=model)
            if not vecs:
                vecs = []
                for t in batch:
                    v = self.ollama.embed(t, model=model)
                    if not v:
                        return None
                    vecs.append(v)
            for j, v in zip(batch_idx, vecs):
                if not v:
                    return None
                result[j] = v
                key = self._cache_key(fact_nodes[j]["text"], model)
                self._embed_cache[key] = v

        if any(v is None for v in result):
            return None
        if uncached_idx:
            self._save_embed_cache()
        return result  # type: ignore[return-value]

    def _top_k_edges_from_dense(self, fact_nodes, sim_matrix, min_sim: float,
                                top_k: int) -> list[dict]:
        import numpy as np
        n = len(fact_nodes)
        seen: set[tuple[str, str]] = set()
        edges: list[dict] = []
        for i in range(n):
            row = sim_matrix[i].copy()
            row[i] = -1.0
            # mask same-project
            proj_i = fact_nodes[i]["project"]
            for k in range(n):
                if fact_nodes[k]["project"] == proj_i:
                    row[k] = -1.0
            if not np.any(row > min_sim):
                continue
            order = np.argsort(-row)[:top_k]
            for j in order:
                s = float(row[j])
                if s < min_sim:
                    break
                a, b = fact_nodes[i]["id"], fact_nodes[int(j)]["id"]
                key = (a, b) if a < b else (b, a)
                if key in seen:
                    continue
                seen.add(key)
                edges.append({
                    "src": key[0],
                    "dst": key[1],
                    "type": "cross_similar",
                    "weight": round(s, 4),
                    "scope": "cross_similar",
                })
        return edges

    def _top_k_edges_from_sparse(self, fact_nodes, vectors, min_sim: float,
                                 top_k: int) -> list[dict]:
        n = len(fact_nodes)
        seen: set[tuple[str, str]] = set()
        edges: list[dict] = []
        for i in range(n):
            scored: list[tuple[float, int]] = []
            proj_i = fact_nodes[i]["project"]
            for j in range(n):
                if i == j or fact_nodes[j]["project"] == proj_i:
                    continue
                s = _sparse_cos(vectors[i], vectors[j])
                if s >= min_sim:
                    scored.append((s, j))
            scored.sort(reverse=True)
            for s, j in scored[:top_k]:
                a, b = fact_nodes[i]["id"], fact_nodes[j]["id"]
                key = (a, b) if a < b else (b, a)
                if key in seen:
                    continue
                seen.add(key)
                edges.append({
                    "src": key[0],
                    "dst": key[1],
                    "type": "cross_similar",
                    "weight": round(s, 4),
                    "scope": "cross_similar",
                })
        return edges

    def _insight_edges(self, project_paths: list[str], node_ids: set[str]) -> list[dict]:
        """Link pairs of projects that share an insight (project-level edge)."""
        edges: list[dict] = []
        try:
            insights = self.cross.get_all_insights()
        except Exception:
            return edges
        slug_by_path = {p: _slugify(p) for p in project_paths}
        paths_set = set(project_paths)
        for ins in insights:
            srcs = [p for p in ins.get("source_projects", []) if p in paths_set]
            if len(srcs) < 2:
                continue
            for i in range(len(srcs)):
                for j in range(i + 1, len(srcs)):
                    a = f"project:{slug_by_path[srcs[i]]}"
                    b = f"project:{slug_by_path[srcs[j]]}"
                    edges.append({
                        "src": a,
                        "dst": b,
                        "type": ins.get("type", "insight"),
                        "weight": 1.0,
                        "scope": "linked_via_insight",
                        "insight_id": ins.get("id", ""),
                    })
        return edges

    def _clusters(self, nodes: list[dict], within_edges: list[dict]) -> list[list[str]]:
        from collections import defaultdict
        adj: dict[str, set[str]] = defaultdict(set)
        for e in within_edges:
            adj[e["src"]].add(e["dst"])
            adj[e["dst"]].add(e["src"])
        visited: set[str] = set()
        clusters: list[list[str]] = []
        for nid in (n["id"] for n in nodes):
            if nid in visited or nid not in adj:
                continue
            stack = [nid]
            cluster: list[str] = []
            while stack:
                cur = stack.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                cluster.append(cur)
                for nb in adj.get(cur, ()):
                    if nb not in visited:
                        stack.append(nb)
            if len(cluster) >= 3:
                clusters.append(cluster)
        clusters.sort(key=len, reverse=True)
        return clusters
