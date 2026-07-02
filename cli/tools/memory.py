"""c3_memory — Facts, graph, scoring, grounding, and cross-session recall."""
from datetime import datetime, timezone


def handle_memory(action: str, query: str, fact: str, category: str,
                  top_k: int, svc, finalize, fact_id: str = "",
                  include_scores: bool = False, scope: str = "") -> str:
    if action == "add":
        if not fact or not fact.strip():
            return finalize("c3_memory", {"action": action},
                            "fact is required to add a memory (got empty/whitespace)",
                            "missing fact")
        sid = (svc.session_mgr.current_session or {}).get("id", "")
        res = svc.memory.remember(fact, category or "general", sid)
        return finalize("c3_memory", {"action": action},
                        f"[remembered:{res['id']}] total:{res['total_facts']}", res['id'])

    if action == "recall":
        session_id = (svc.session_mgr.current_session or {}).get("id", "")
        results = svc.memory.recall(query, top_k=top_k, session_id=session_id)
        # Small recalls skip graph spreading to stay fast — agents using
        # top_k<=3 want quick lookups, not full enrichment. (Salience scoring
        # is opt-in via include_scores, independent of this.)
        fast_mode = top_k <= 3
        backend = "tfidf"
        if svc.vector_store:
            v_res = svc.vector_store.search(query, top_k=top_k)
            for r in v_res:
                semantic_text = (r.get("content") or r.get("text") or r.get("fact") or "").strip()
                if not semantic_text:
                    continue
                if not any(f.get("fact") == semantic_text for f in results):
                    metadata = r.get("metadata") or {}
                    results.append({
                        "category": metadata.get("category", r.get("category", "semantic")),
                        "fact": semantic_text,
                    })
            if v_res:
                backend = "hybrid"

        # Record co-recall edges in the memory graph
        graph = getattr(svc, "memory_graph", None)
        if graph and len(results) >= 2:
            recalled_ids = [r["id"] for r in results if r.get("id")]
            if len(recalled_ids) >= 2:
                graph.record_co_recall(recalled_ids[:top_k])

        # Enrich results with salience scores — opt-in only. Per-fact scores
        # on every recall were display boilerplate; callers who want them ask
        # via include_scores=True (explicit request overrides fast_mode).
        scorer = getattr(svc, "memory_scorer", None)
        if scorer and include_scores:
            for r in results:
                if r.get("id"):
                    s = scorer.score(r, graph)
                    r["salience"] = s["salience"]
                    r["tier"] = s["tier"]

        # Spreading activation: find related facts via graph (skipped in fast_mode)
        activated_extra = []
        if graph and results and not fast_mode:
            seed_ids = [r["id"] for r in results if r.get("id")][:5]
            if seed_ids:
                activated = graph.spreading_activation(seed_ids, max_depth=2, max_results=5)
                facts_by_id = {f["id"]: f for f in svc.memory.facts}
                for a in activated:
                    fact = facts_by_id.get(a["id"])
                    if fact and not any(r.get("id") == a["id"] for r in results):
                        activated_extra.append(fact)

        # Local RAG Pipeline: auto-retrieve project docs on first recall
        precontext = ""
        if hasattr(svc, "preloader") and svc.preloader:
            if session_id:
                precontext = svc.preloader.preload(query, session_id, top_k=top_k)

        # Sub-project rollup: union linked children's facts (tagged by origin).
        # On by default via hybrid.subprojects.memory_rollup; scope overrides:
        # ''/config-default, 'all', '<child name>', 'project'/'self' = off.
        sub_lines = []
        scope_clean = (scope or "").strip()
        if scope_clean not in ("project", "self"):
            try:
                from core.config import load_hybrid_config
                sub_cfg = load_hybrid_config(getattr(svc, "project_path", "")).get("subprojects") or {}
                if scope_clean or bool(sub_cfg.get("memory_rollup", True)):
                    from cli.tools.federate import federated_recall
                    sub_lines = federated_recall(query, top_k, svc,
                                                 scope=scope_clean or "all")
            except Exception:
                sub_lines = []

        if not results and not activated_extra and not precontext and not sub_lines:
            return finalize("c3_memory", {"action": action},
                            f"[memory:recall:{query}] 0 results (backend:{backend})", "0")
        parts = []
        for f in results[:top_k]:
            sal = (f" sal={f['salience']:.2f}/{f['tier']}"
                   if include_scores and f.get("salience") is not None else "")
            parts.append(f"[{f['category']}]{sal} {f['fact']}")
        if activated_extra:
            parts.append(f"  [graph:activated] {len(activated_extra)} related facts:")
            for f in activated_extra[:3]:
                parts.append(f"    [{f.get('category','')}] {f['fact'][:80]}")
        parts.extend(sub_lines)
        recall_text = (f"[recall:{query}] {len(results)} facts (backend:{backend}"
                       + (f", +{len(sub_lines)} sub-project" if sub_lines else "")
                       + ")\n" + "\n".join(parts))

        if precontext:
            recall_text = precontext + recall_text

        return finalize("c3_memory", {"action": action}, recall_text, f"{len(results)}f")

    if action == "index":
        # Compact index — IDs + one-liners. Follow up with fetch(fact_id=...) for full text.
        session_id = (svc.session_mgr.current_session or {}).get("id", "")
        results = svc.memory.recall(query, top_k=top_k, session_id=session_id)
        scorer = getattr(svc, "memory_scorer", None)
        graph = getattr(svc, "memory_graph", None)
        if not results:
            return finalize("c3_memory", {"action": action},
                            f"[memory:index] 0 results for '{query}'", "0")
        lines = [f"[memory:index] {len(results)} facts — use fetch(fact_id='id1,id2,...') for full text"]
        for f in results:
            sal = ""
            if scorer and f.get("id"):
                s = scorer.score(f, graph)
                sal = f" sal={s['salience']:.2f}/{s['tier']}"
            snippet = f["fact"][:80].replace("\n", " ")
            lines.append(f"  {f['id']} [{f['category']}]{sal} {snippet}")
        return finalize("c3_memory", {"action": action}, "\n".join(lines), f"{len(results)}f")

    if action == "fetch":
        # Full details for specific fact IDs (comma-separated).
        if not fact_id:
            return "[memory:error] fetch requires fact_id (comma-separated IDs from index)"
        ids = [i.strip() for i in fact_id.split(",") if i.strip()]
        facts_by_id = {f["id"]: f for f in svc.memory.facts}
        lines = []
        found = 0
        for fid in ids:
            f = facts_by_id.get(fid)
            if not f:
                lines.append(f"  {fid} — not found")
                continue
            found += 1
            rc = f.get("relevance_count", 0)
            ts = (f.get("timestamp") or "")[:10]
            lines.append(f"[{fid}] [{f['category']}] rc={rc} added={ts}")
            lines.append(f"  {f['fact']}")
        header = f"[memory:fetch] {found}/{len(ids)} facts"
        return finalize("c3_memory", {"action": action}, header + "\n" + "\n".join(lines), f"{found}f")

    if action == "query":
        res = svc.memory.query_all(query, top_k=top_k)
        backend = "tfidf"
        if svc.vector_store and svc.vector_store.vector_enabled:
            backend = "hybrid"
        parts = [f"[{f['category']}] {f['fact'][:80]}" for f in res['facts']]
        parts += [f"[session:{s['session_id'][:12]}] {s.get('summary', '')[:80]}"
                  for s in res.get('sessions', [])]
        parts += [f"[conversation:{c['session_id'][:12]}] {(c.get('snippet') or c.get('text', ''))[:80]}"
                  for c in res.get('conversations', [])[:top_k]]
        parts += [f"[file:{f['path']}] {(f.get('summary') or '')[:80]}"
                  for f in res.get('files', [])[:top_k]]
        return finalize("c3_memory", {"action": action},
                        f"[query:{query}] {len(parts)} hits (backend:{backend})\n" + "\n".join(parts),
                        f"{len(parts)}h")

    if action == "update":
        if not fact_id:
            return "[memory:error] update requires fact_id"
        res = svc.memory.update_fact(fact_id, fact=fact, category=category)
        if res.get("error"):
            return f"[memory:error] {res['error']} (id={fact_id})"
        return finalize("c3_memory", {"action": action},
                        f"[updated:{fact_id}]", fact_id)

    if action == "delete":
        if not fact_id:
            return "[memory:error] delete requires fact_id"
        res = svc.memory.delete_fact(fact_id)
        if res.get("error"):
            return f"[memory:error] {res['error']} (id={fact_id})"
        return finalize("c3_memory", {"action": action},
                        f"[deleted:{fact_id}]", fact_id)

    if action == "list":
        all_facts = svc.memory.facts
        active = [f for f in all_facts if f.get("lifecycle") != "archived"]
        facts = active
        if category:
            facts = [f for f in facts if f.get("category") == category]
        if not facts:
            total = len(all_facts)
            active_n = len(active)
            if category and active_n > 0:
                cats = sorted({f.get("category", "general") for f in active})
                hint = f" (no match for category='{category}'; active categories: {', '.join(cats)})"
            else:
                hint = ""
            return finalize("c3_memory", {"action": action},
                            f"[memory:list] 0 facts (total={total} active={active_n}){hint}", "0")
        by_cat: dict = {}
        for f in facts:
            by_cat.setdefault(f.get("category", "general"), []).append(f)
        header_scope = f"category='{category}'" if category else "all"
        lines = [f"[memory:list] {len(facts)} fact(s) scope={header_scope} "
                 f"(total={len(all_facts)} active={len(active)})"]
        for cat, entries in sorted(by_cat.items()):
            lines.append(f"  [{cat}] ({len(entries)})")
            for e in entries:
                rc = e.get("relevance_count", 0)
                lines.append(f"    {e['id']} (rc={rc}) {e['fact'][:80]}")
        return finalize("c3_memory", {"action": action},
                        "\n".join(lines), f"{len(facts)}f")

    if action == "review":
        facts = [f for f in svc.memory.facts if f.get("lifecycle") != "archived"]
        total = len(facts)
        scorer = getattr(svc, "memory_scorer", None)
        graph = getattr(svc, "memory_graph", None)

        # Score all facts and partition by tier
        tier_counts = {"core": 0, "active": 0, "dormant": 0, "ephemeral": 0}
        scored_facts = []
        if scorer:
            for f in facts:
                s = scorer.score(f, graph)
                scored_facts.append((f, s))
                tier_counts[s["tier"]] += 1

        # Unused: never recalled
        unused = [f for f in facts if f.get("relevance_count", 0) == 0]
        # Simple Jaccard duplicate detection
        def _tokens(text):
            return set(text.lower().split())
        pairs = []
        for i in range(len(facts)):
            for j in range(i + 1, len(facts)):
                a, b = facts[i], facts[j]
                ta, tb = _tokens(a["fact"]), _tokens(b["fact"])
                if not ta or not tb:
                    continue
                sim = len(ta & tb) / len(ta | tb)
                if sim >= 0.6:
                    pairs.append((a, b, round(sim, 2)))
            if len(pairs) >= 5:
                break

        lines = [f"[memory:review] {total} facts total"]

        # Tier breakdown
        if scorer:
            lines.append(f"  Tiers: core={tier_counts['core']} active={tier_counts['active']} "
                         f"dormant={tier_counts['dormant']} ephemeral={tier_counts['ephemeral']}")

        # Graph stats
        if graph:
            gs = graph.stats()
            lines.append(f"  Graph: {gs['total_edges']} edges, {gs['total_nodes']} nodes, "
                         f"{gs['clusters']} clusters")

        if pairs:
            lines.append(f"  Potential duplicates ({len(pairs)}):")
            for a, b, sim in pairs[:5]:
                lines.append(f"    {a['id']} ≈ {b['id']} (sim={sim})")
                lines.append(f"      A: {a['fact'][:60]}")
                lines.append(f"      B: {b['fact'][:60]}")
        if unused:
            lines.append(f"  Never-recalled facts ({len(unused)}) — consider deleting:")
            for f in unused[:5]:
                lines.append(f"    {f['id']} [{f.get('category','?')}] {f['fact'][:70]}")
        # Verbose facts: >500 chars, never recalled
        verbose = [
            f for f in facts
            if len(f.get("fact", "")) > 500 and f.get("relevance_count", 0) == 0
        ]
        if verbose:
            lines.append(f"  Verbose never-recalled ({len(verbose)}):")
            for f in verbose[:5]:
                lines.append(f"    {f['id']} {len(f['fact'])}ch — {f['fact'][:60]}...")

        # Low-salience facts (ephemeral tier)
        if scored_facts:
            ephemeral = [(f, s) for f, s in scored_facts if s["tier"] == "ephemeral"]
            if ephemeral:
                lines.append(f"  Ephemeral (auto-prune candidates): {len(ephemeral)}")
                for f, s in ephemeral[:3]:
                    lines.append(f"    {f['id']} sal={s['salience']:.2f} — {f['fact'][:60]}")

        # Stale session summaries: auto:session older than 14 days
        now_dt = datetime.now(timezone.utc)
        stale_sessions = []
        for f in facts:
            if f.get("category") != "auto:session":
                continue
            try:
                age = (now_dt - datetime.fromisoformat(f.get("timestamp", ""))).days
            except (ValueError, TypeError):
                age = 0
            if age >= 14:
                stale_sessions.append((f, age))
        if stale_sessions:
            lines.append(f"  Stale sessions ({len(stale_sessions)}, >14d):")
            for f, age in stale_sessions[:5]:
                lines.append(f"    {f['id']} ({age}d) — {f['fact'][:60]}")

        if not pairs and not unused and not verbose and not stale_sessions and not ephemeral:
            lines.append("  No issues found.")
        lines.append("  Actions: consolidate, consolidate_deep, score, graph, ground, trends, lifespan")
        return finalize("c3_memory", {"action": action},
                        "\n".join(lines), f"{total}f")

    if action == "export":
        facts = [f for f in svc.memory.facts if f.get("lifecycle") != "archived"]
        if category:
            facts = [f for f in facts if f.get("category") == category]
        if not facts:
            return finalize("c3_memory", {"action": action},
                            "[memory:export] 0 facts to export", "0")
        # Sort by relevance_count desc, then recency
        facts.sort(key=lambda f: (f.get("relevance_count", 0), f.get("last_accessed_at") or ""), reverse=True)
        # Group by category
        by_cat: dict = {}
        for f in facts:
            by_cat.setdefault(f.get("category", "general"), []).append(f)
        lines = ["# C3 Memory Export", ""]
        for cat, entries in sorted(by_cat.items()):
            lines.append(f"## {cat}")
            lines.append("")
            for e in entries:
                lines.append(f"- {e['fact']}")
            lines.append("")
        md = "\n".join(lines).rstrip() + "\n"
        return finalize("c3_memory", {"action": action}, md, f"{len(facts)}f")

    if action == "consolidate":
        if not hasattr(svc, "auto_memory"):
            return finalize("c3_memory", {"action": action},
                            "[memory:consolidate] auto_memory not available", "skip")
        stats = svc.auto_memory.consolidate()
        lines = [
            "[memory:consolidate] done",
            f"  Merged: {stats['merged']} duplicate pairs",
            f"  Archived: {stats['archived']} stale auto-facts",
            f"  Remaining: {stats['total']} facts",
        ]
        return finalize("c3_memory", {"action": action},
                        "\n".join(lines), f"m{stats['merged']}a{stats['archived']}")

    if action == "consolidate_deep":
        consolidator = getattr(svc, "memory_consolidator", None)
        if not consolidator:
            return finalize("c3_memory", {"action": action},
                            "[memory:consolidate_deep] consolidator not available", "skip")
        session = svc.session_mgr.current_session
        stats = consolidator.run(current_session=session)
        phases = stats.get("phases", {})
        lines = ["[memory:consolidate_deep] 4-phase pipeline complete"]
        for phase_name, phase_stats in phases.items():
            lines.append(f"  {phase_name}: {phase_stats}")
        lines.append(f"  Total active facts: {stats.get('total_facts', '?')}")
        return finalize("c3_memory", {"action": action},
                        "\n".join(lines), f"deep:{stats.get('total_facts', 0)}")

    if action == "score":
        scorer = getattr(svc, "memory_scorer", None)
        graph = getattr(svc, "memory_graph", None)
        if not scorer:
            return finalize("c3_memory", {"action": action},
                            "[memory:score] scorer not available", "skip")
        facts = [f for f in svc.memory.facts if f.get("lifecycle") == "active"]
        if fact_id:
            facts = [f for f in facts if f["id"] == fact_id]
        if not facts:
            return finalize("c3_memory", {"action": action},
                            "[memory:score] no matching facts", "0")
        scores = scorer.score_batch(facts[:20], graph)
        lines = [f"[memory:score] {len(scores)} facts scored"]
        for s in scores:
            lines.append(f"  {s['id']} sal={s['salience']:.3f} tier={s['tier']}")
            sig = s.get("signals", {})
            top_signals = sorted(sig.items(), key=lambda x: x[1], reverse=True)[:3]
            lines.append(f"    top: {', '.join(f'{k}={v:.2f}' for k, v in top_signals)}")
        return finalize("c3_memory", {"action": action},
                        "\n".join(lines), f"{len(scores)}s")

    if action == "graph":
        graph = getattr(svc, "memory_graph", None)
        if not graph:
            return finalize("c3_memory", {"action": action},
                            "[memory:graph] graph not available", "skip")
        if fact_id:
            # Show edges for a specific fact
            edges = graph.get_edges(fact_id)
            neighbors = graph.get_neighbors(fact_id)
            lines = [f"[memory:graph] node={fact_id} edges={len(edges)} neighbors={len(neighbors)}"]
            for e in edges[:10]:
                other = e["dst"] if e["src"] == fact_id else e["src"]
                lines.append(f"  --{e['type']}--> {other} (w={e['weight']:.2f}, hits={e.get('hit_count', 0)})")
            # Show clusters containing this fact
            clusters = graph.detect_clusters()
            for i, c in enumerate(clusters):
                if fact_id in c:
                    lines.append(f"  Cluster #{i}: {len(c)} members — {', '.join(c[:5])}")
        else:
            # Show overall graph stats
            gs = graph.stats()
            clusters = graph.detect_clusters()
            lines = [
                f"[memory:graph] {gs['total_edges']} edges, {gs['total_nodes']} nodes",
                f"  Edge types: {gs['edge_types']}",
                f"  Clusters: {len(clusters)}",
            ]
            facts_by_id = {f["id"]: f for f in svc.memory.facts}
            for i, c in enumerate(clusters[:5]):
                member_facts = [facts_by_id.get(fid, {}).get("fact", "?")[:40] for fid in c[:3]]
                lines.append(f"  Cluster #{i} ({len(c)} nodes): {'; '.join(member_facts)}")
        return finalize("c3_memory", {"action": action},
                        "\n".join(lines), f"g:{graph.stats()['total_edges']}e")

    if action == "ground":
        grounder = getattr(svc, "memory_grounder", None)
        if not grounder:
            return finalize("c3_memory", {"action": action},
                            "[memory:ground] grounder not available", "skip")
        if fact_id:
            fact = svc.memory._facts_by_id.get(fact_id)
            if not fact:
                return finalize("c3_memory", {"action": action},
                                f"[memory:ground] fact {fact_id} not found", "0")
            gr = grounder.ground_fact(fact)
            result = gr.to_dict()
            lines = [f"[memory:ground] fact={fact_id} grounded={result['grounded']}"]
            for fr in result["file_refs"]:
                lines.append(f"  file: {fr['path']} exists={fr['exists']}")
            for sr in result["symbol_refs"]:
                lines.append(f"  symbol: {sr['name']} found={sr['found']} file={sr.get('file', '')}")
            for issue in result["issues"]:
                lines.append(f"  issue: {issue}")
            lines.append(f"  confidence_delta: {result['confidence_delta']}")
        else:
            result = grounder.ground_all()
            lines = [
                f"[memory:ground] {result.get('total', 0)} facts checked",
                f"  Grounded: {result.get('grounded', 0)}",
                f"  Ungrounded: {result.get('ungrounded', 0)}",
                f"  Confidence updates: {result.get('confidence_updates', 0)}",
            ]
            for detail in result.get("ungrounded_details", [])[:5]:
                lines.append(f"  {detail['fact_id']}: {', '.join(detail.get('issues', []))}")
        return finalize("c3_memory", {"action": action},
                        "\n".join(lines), f"g:{result.get('grounded', 0)}/{result.get('total', 0)}")

    if action == "trends":
        consolidator = getattr(svc, "memory_consolidator", None)
        if not consolidator:
            return finalize("c3_memory", {"action": action},
                            "[memory:trends] consolidator not available", "skip")
        trends = consolidator.detect_trends()
        lines = [f"[memory:trends] {trends['sessions_analyzed']} sessions analyzed"]
        if trends["hot_files"]:
            lines.append("  Hot files (active development):")
            for hf in trends["hot_files"]:
                lines.append(f"    {hf['file']} — {hf['sessions']} sessions")
        if trends["hot_facts"]:
            lines.append("  Hot facts (frequently recalled):")
            for hf in trends["hot_facts"]:
                lines.append(f"    {hf['fact_id']} ({hf['sessions']}x) — {hf.get('fact', '?')}")
        if not trends["hot_files"] and not trends["hot_facts"]:
            lines.append("  No significant trends detected yet.")
        return finalize("c3_memory", {"action": action},
                        "\n".join(lines), f"t:{trends['sessions_analyzed']}")

    if action == "lifespan":
        consolidator = getattr(svc, "memory_consolidator", None)
        if not consolidator:
            return finalize("c3_memory", {"action": action},
                            "[memory:lifespan] consolidator not available", "skip")
        analysis = consolidator.fact_lifespan_analysis()
        lines = [f"[memory:lifespan] {analysis['total_facts']} facts analyzed"]
        if analysis["foundational"]:
            lines.append(f"  Foundational ({len(analysis['foundational'])} — recalled across 3+ sessions):")
            for f in analysis["foundational"][:5]:
                lines.append(f"    {f['id']} spread={f['session_spread']} sal={f['salience']:.2f} — {f['fact']}")
        if analysis["contextual"]:
            lines.append(f"  Contextual ({len(analysis['contextual'])} — single-session, 7+ days old):")
            for f in analysis["contextual"][:5]:
                lines.append(f"    {f['id']} sal={f['salience']:.2f} — {f['fact']}")
        if not analysis["foundational"] and not analysis["contextual"]:
            lines.append("  Not enough session history for classification yet.")
        return finalize("c3_memory", {"action": action},
                        "\n".join(lines), f"f:{len(analysis['foundational'])}c:{len(analysis['contextual'])}")

    return f"[memory:error] Unknown action: {action}"
