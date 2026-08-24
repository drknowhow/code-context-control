"""
Smart Local Index Service

Builds a searchable index of your codebase using TF-IDF and code structure analysis.
Retrieves only the most relevant code snippets for a given query, dramatically reducing
the amount of code Claude needs to read.
"""
import json
import math
import os
import re
from collections import Counter, OrderedDict
from pathlib import Path

from core import count_tokens
from services.scanner import SKIP_DIRS, iter_files


def _masked_content(fpath, project_path):
    """Index-time content for one file: the masked view when masked, else raw.

    Returns ``None`` when the path is masked but its view cannot be produced —
    the caller must skip the file rather than fall back to raw bytes.
    """
    from services import access_guard, mask_mirror
    try:
        v = access_guard.verdict(str(fpath), "read", str(project_path))
    except Exception:
        return None
    if v.denial:
        return None
    if not v.masked:
        return fpath.read_text(encoding="utf-8", errors="replace")
    try:
        return mask_mirror.render_for_path(fpath, project_path).text
    except Exception:
        return None


# Co-occurrence is O(unique_tokens^2) per chunk, so one minified vendor
# bundle can dominate a whole index build. Both bounds below are deliberate
# and their effect is REPORTED in build_index()'s stats rather than applied
# silently — a capped build must never look like a complete one.
#
# 200: chunks above this are minified/bundled/data blobs, not code. The
# pruning step keeps only the top-5 co-occurring terms per token with at
# least 3 hits, so such a chunk contributes noise that is discarded anyway.
_COOC_MAX_CHUNK_TOKENS = 200
# 20M: whole-pass ceiling on pair updates, so no repository shape can make
# this step unbounded. ~10s of CPU on the boxes this runs on.
_COOC_MAX_PAIR_UPDATES = 20_000_000


class CodeIndex:
    """TF-IDF based code search index with structural awareness."""

    def __init__(self, project_path: str, index_dir: str = ".c3/index"):
        self.project_path = Path(project_path)
        self.index_dir = self.project_path / index_dir
        self.index_dir.mkdir(parents=True, exist_ok=True)

        # Index data
        self.documents = {}       # doc_id -> {path, content, chunks}
        self.chunks = {}          # chunk_id -> {doc_id, content, type, name, line_start, line_end}
        self.idf = {}             # term -> IDF score
        self.chunk_tfidf = {}     # chunk_id -> {term: tfidf_score}
        self.symbols = {}         # symbol_name -> [chunk_ids]
        # Bounded LRU — an unbounded dict grew indefinitely over long sessions.
        self._search_cache: "OrderedDict" = OrderedDict()
        self._search_cache_max = 128
        # Memoized query expansion + bigrams. Agents repeat the same queries.
        self._expand_cache: dict = {}
        self._cooccurrence = {}   # term -> {term: count} for auto-synonyms
        self._file_mtimes = {}    # doc_id -> mtime for recency bias

        # Config - shared pruned-walk skip set (services/scanner.py)
        self.skip_dirs = set(SKIP_DIRS)
        self.code_exts = {
            # Python
            '.py', '.pyi', '.pyx',
            # JavaScript / TypeScript
            '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs',
            # Web
            '.html', '.htm', '.css', '.scss', '.sass', '.less', '.vue', '.svelte',
            # Markdown
            '.md', '.mdx',
            # Data / Config
            '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.env.example',
            '.xml', '.csv',
            # Systems
            '.c', '.h', '.cpp', '.cxx', '.cc', '.hpp', '.hxx',
            '.rs', '.go', '.java', '.kt', '.kts', '.scala',
            '.cs', '.fs', '.vb',
            # Scripting
            '.sh', '.bash', '.zsh', '.fish', '.ps1', '.bat', '.cmd',
            '.rb', '.pl', '.pm', '.lua', '.php',
            '.r', '.R', '.jl',
            # Query / Schema
            '.sql', '.graphql', '.gql', '.prisma',
            # Functional
            '.hs', '.ex', '.exs', '.erl', '.clj', '.cljs', '.elm', '.ml', '.mli',
            # Mobile
            '.swift', '.m', '.mm', '.dart',
            # Docs / Markup
            '.md', '.mdx', '.rst', '.tex', '.adoc',
            # DevOps / IaC
            '.tf', '.hcl', '.dockerfile', '.nix',
            # Other
            '.proto', '.thrift', '.zig', '.nim', '.v',
            '.makefile', '.cmake',
        }

        # Sub-project exclusion: designated child folders carry their own
        # .c3 index, so the parent skips them (relative-path-prefix match —
        # name-based skip_dirs would catch same-named siblings).
        try:
            from services.subprojects import exclusion_prefixes, is_excluded
            self.exclude_prefixes = exclusion_prefixes(self.project_path)
            self._is_excluded = is_excluded
        except Exception:
            self.exclude_prefixes = []
            self._is_excluded = None

    _DEFAULT_MAX_FILES = 2000

    def _configured_max_files(self) -> int:
        """``index_max_files`` from .c3/config.json; default 2000."""
        try:
            cfg = json.loads((self.project_path / '.c3' / 'config.json')
                             .read_text(encoding='utf-8'))
            val = int(cfg.get('index_max_files', self._DEFAULT_MAX_FILES))
            return val if val > 0 else self._DEFAULT_MAX_FILES
        except Exception:
            return self._DEFAULT_MAX_FILES

    def build_index(self, max_files: int = None, on_progress=None) -> dict:
        """Build the full code index.

        max_files: cap on files indexed. None reads ``index_max_files``
            from .c3/config.json (default 2000). Traversal continues past
            the cap only to count what was left out, so callers can report
            "indexed N of M" instead of silently truncating.
        on_progress: callable(entries_seen, files_indexed, chunks_created),
            invoked during the scan (directory granularity plus per file).
        """
        if max_files is None:
            max_files = self._configured_max_files()

        self.documents = {}
        self.chunks = {}
        self.symbols = {}
        self._search_cache: OrderedDict = OrderedDict()

        files_indexed = 0
        chunks_created = 0
        files_capped = 0
        scan_stats = {"entries": 0}

        def _report(entries_seen=None, _yielded=None):
            if entries_seen is not None:
                scan_stats["entries"] = entries_seen
            if on_progress is not None:
                try:
                    on_progress(scan_stats["entries"], files_indexed,
                                chunks_created)
                except Exception:
                    pass

        exclude_parts = None
        if self.exclude_prefixes and self._is_excluded is not None:
            def exclude_parts(parts, _c=self._is_excluded,
                              _p=self.exclude_prefixes):
                return _c(parts, _p)

        # Mask Guard: gate the per-file verdict on a single cheap check, so a
        # project with no mask rules pays nothing for this (docs/mask-guard.md
        # §2). When rules exist, masked files are indexed FROM THE VIEW — the
        # index must never hold content the agent is not allowed to read.
        try:
            from services import access_guard as _ag
            _mask_active = _ag.has_mask_rules(str(self.project_path))
        except Exception:
            _mask_active = False

        for fpath in iter_files(self.project_path, exts=self.code_exts,
                                skip_dirs=self.skip_dirs,
                                exclude_parts=exclude_parts,
                                on_progress=_report):
            if files_indexed >= max_files:
                # Walk on (cheap after pruning) to count coverage;
                # reading and chunking stop at the cap.
                files_capped += 1
                continue

            try:
                if _mask_active:
                    content = _masked_content(fpath, self.project_path)
                    if content is None:
                        # Masked but unrenderable — indexing the raw bytes
                        # would put pre-mask content into search snippets
                        # (docs/mask-guard.md §6, row 2). Skip it instead.
                        continue
                else:
                    content = fpath.read_text(encoding='utf-8',
                                              errors='replace')
            except Exception:
                continue

            rel_path = str(fpath.relative_to(self.project_path))
            doc_id = rel_path

            # Create document entry
            self.documents[doc_id] = {
                "path": rel_path,
                "full_path": str(fpath),
                "lines": len(content.splitlines()),
                "tokens": count_tokens(content),
            }

            # Chunk the file
            file_chunks = self._chunk_file(content, fpath.suffix.lower(), doc_id)
            for chunk in file_chunks:
                self.chunks[chunk["id"]] = chunk
                chunks_created += 1

                # Index symbols
                if chunk.get("name"):
                    sym = chunk["name"].lower()
                    if sym not in self.symbols:
                        self.symbols[sym] = []
                    self.symbols[sym].append(chunk["id"])

            # Track file modification time for recency bias
            try:
                self._file_mtimes[doc_id] = os.path.getmtime(str(fpath))
            except Exception:
                pass

            files_indexed += 1
            _report()

        # Build TF-IDF and co-occurrence synonyms
        self._build_tfidf()
        self._build_cooccurrence()

        # Save index
        self._save_index()

        return {
            "files_indexed": files_indexed,
            "chunks_created": chunks_created,
            "unique_symbols": len(self.symbols),
            "index_path": str(self.index_dir),
            "entries_scanned": scan_stats["entries"],
            "files_capped": files_capped,
            "max_files": max_files,
            "cooccurrence": getattr(self, "_cooccurrence_stats", {}),
        }

    def _chunk_file(self, content: str, ext: str, doc_id: str) -> list:
        """Split a file into meaningful chunks (functions, classes, blocks)."""
        lines = content.split('\n')
        chunks = []

        try:
            from services.parser import extract_sections_ast
            ast_sections = extract_sections_ast(content, ext)
            if ast_sections:
                ast_chunks = self._chunk_by_ast(ast_sections, lines, doc_id)
                if ast_chunks:
                    return ast_chunks
        except Exception:
            pass

        # Try structural chunking first
        if ext in ('.py', '.r', '.R'):
            chunks = self._chunk_by_indent(lines, doc_id, ext)
        elif ext in ('.js', '.ts', '.tsx', '.jsx'):
            chunks = self._chunk_by_braces(lines, doc_id, ext)

        # Fallback: fixed-size chunks with overlap
        if not chunks:
            chunks = self._chunk_fixed(lines, doc_id, chunk_size=40, overlap=10)

        return chunks

    def _chunk_by_ast(self, sections: list, lines: list, doc_id: str) -> list:
        chunks = []
        from core import count_tokens

        def process_section(sec, parent_name=""):
            name = sec.get("name", "unnamed")
            full_name = f"{parent_name}.{name}" if parent_name else name
            start = sec["line_start"] - 1 # 0-indexed
            end = sec["line_end"] - 1
            chunk_content = '\n'.join(lines[start:end+1])

            if sec.get("type") != "import":
                chunks.append({
                    "id": f"{doc_id}::{full_name}",
                    "doc_id": doc_id,
                    "content": chunk_content,
                    "tokens": count_tokens(chunk_content),
                    "type": sec.get("type", "block"),
                    "name": full_name,
                    "line_start": start,
                    "line_end": end,
                })

            for child in sec.get("children", []):
                process_section(child, full_name)

        for sec in sections:
            process_section(sec)

        return chunks

    def _chunk_by_indent(self, lines: list, doc_id: str, ext: str) -> list:
        """Chunk Python/R files by definitions, including class methods."""
        chunks = []
        current_chunk = []
        current_name = None
        current_type = None
        chunk_start = 0
        class_stack = []

        def flush_chunk(end_index: int):
            nonlocal current_chunk, chunk_start, current_name, current_type
            if not current_chunk:
                return
            content = '\n'.join(current_chunk)
            chunks.append({
                "id": f"{doc_id}::{current_name or f'block_{chunk_start}'}",
                "doc_id": doc_id,
                "content": content,
                "tokens": count_tokens(content),
                "type": current_type or "block",
                "name": current_name,
                "line_start": chunk_start,
                "line_end": end_index,
            })
            current_chunk = []

        for i, line in enumerate(lines):
            stripped = line.rstrip()
            lstripped = line.lstrip()
            indent = len(line) - len(lstripped)

            while class_stack and indent <= class_stack[-1][0] and lstripped:
                class_stack.pop()

            # Detect definitions
            is_definition = False
            name = None
            ctype = None

            if ext == '.py':
                m = re.match(r'^(class|(?:async\s+)?def)\s+(\w+)', lstripped)
                if m:
                    is_definition = True
                    ctype = 'class' if m.group(1) == 'class' else 'function'
                    name = m.group(2)
                    if ctype == 'function' and class_stack:
                        name = f"{class_stack[-1][1]}.{name}"
            elif ext in ('.r', '.R'):
                m = re.match(r'^(\w+)\s*<-\s*function', lstripped)
                if m:
                    is_definition = True
                    ctype = 'function'
                    name = m.group(1)

            if is_definition and current_chunk:
                flush_chunk(i - 1)
                chunk_start = i

            current_chunk.append(stripped)
            if is_definition:
                current_name = name
                current_type = ctype
                if ctype == 'class' and ext == '.py':
                    class_stack.append((indent, m.group(2)))

        # Save last chunk
        flush_chunk(len(lines) - 1)

        return chunks

    def _chunk_by_braces(self, lines: list, doc_id: str, ext: str) -> list:
        """Chunk JS/TS files by top-level declarations."""
        chunks = []
        current_chunk = []
        current_name = None
        current_type = None
        chunk_start = 0
        brace_depth = 0

        for i, line in enumerate(lines):
            stripped = line.rstrip()
            current_chunk.append(stripped)

            # Track brace depth
            brace_depth += stripped.count('{') - stripped.count('}')

            # Detect top-level declarations at depth 0
            if brace_depth <= 0:
                m = re.match(
                    r'^(?:export\s+)?(?:default\s+)?(?:async\s+)?'
                    r'(?:function|class|const|let|var|interface|type|enum)\s+(\w+)',
                    stripped
                )
                if m and len(current_chunk) > 1:
                    name = m.group(1)
                    # Save accumulated chunk
                    prev_lines = current_chunk[:-1]
                    if prev_lines:
                        chunks.append({
                            "id": f"{doc_id}::{current_name or f'block_{chunk_start}'}",
                            "doc_id": doc_id,
                            "content": '\n'.join(prev_lines),
                            "tokens": count_tokens('\n'.join(prev_lines)),
                            "type": current_type or "block",
                            "name": current_name,
                            "line_start": chunk_start,
                            "line_end": i - 1,
                        })
                    current_chunk = [stripped]
                    current_name = name
                    current_type = "declaration"
                    chunk_start = i
                    brace_depth = stripped.count('{') - stripped.count('}')

        if current_chunk:
            chunks.append({
                "id": f"{doc_id}::{current_name or f'block_{chunk_start}'}",
                "doc_id": doc_id,
                "content": '\n'.join(current_chunk),
                "tokens": count_tokens('\n'.join(current_chunk)),
                "type": current_type or "block",
                "name": current_name,
                "line_start": chunk_start,
                "line_end": len(lines) - 1,
            })

        return chunks

    def _chunk_fixed(self, lines: list, doc_id: str,
                     chunk_size: int = 40, overlap: int = 10) -> list:
        """Fixed-size chunking with overlap."""
        chunks = []
        for i in range(0, len(lines), chunk_size - overlap):
            chunk_lines = lines[i:i + chunk_size]
            if not any(l.strip() for l in chunk_lines):
                continue
            chunks.append({
                "id": f"{doc_id}::chunk_{i}",
                "doc_id": doc_id,
                "content": '\n'.join(chunk_lines),
                "tokens": count_tokens('\n'.join(chunk_lines)),
                "type": "block",
                "name": None,
                "line_start": i,
                "line_end": min(i + chunk_size, len(lines)) - 1,
            })
        return chunks

    def _tokenize(self, text: str) -> list:
        """Simple tokenization for TF-IDF."""
        # Split camelCase and snake_case
        text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
        text = text.replace('_', ' ').replace('-', ' ')
        tokens = re.findall(r'[a-zA-Z]{2,}', text.lower())
        return tokens

    def _expand_query_tokens(self, query: str) -> tuple:
        """Expand query with synonyms + return phrase bigrams.

        Returns (expanded_tokens, bigrams). Memoized per query string —
        repeat searches (agentic flows) skip re-expansion.
        Bigrams are returned (not stored on self) so concurrent searches
        don't race on shared state.
        """
        cached = self._expand_cache.get(query)
        if cached is not None:
            return cached

        base_tokens = self._tokenize(query)
        if not base_tokens:
            self._expand_cache[query] = ([], [])
            if len(self._expand_cache) > 256:
                self._expand_cache.pop(next(iter(self._expand_cache)))
            return [], []

        synonyms = {
            "endpoint": ["route", "handler", "api"],
            "api": ["endpoint", "route", "handler"],
            "helper": ["util", "utils", "common"],
            "registry": ["profile", "profiles", "config"],
            "profile": ["registry", "config", "ide"],
            "compress": ["compression", "compressor"],
            "metrics": ["stats", "summary"],
            "summary": ["metrics", "stats"],
            "delegate": ["ollama", "model"],
            "search": ["index", "retrieval"],
            "file": ["path", "filepath"],
            "path": ["file", "filepath"],
            "mcp": ["server", "tool"],
        }

        expanded = list(base_tokens)
        seen = set(base_tokens)
        for token in base_tokens:
            # Hardcoded synonyms
            for related in synonyms.get(token, []):
                if related not in seen:
                    expanded.append(related)
                    seen.add(related)
            # Co-occurrence synonyms (learned from index)
            if token in self._cooccurrence:
                for related in list(self._cooccurrence[token].keys())[:3]:
                    if related not in seen:
                        expanded.append(related)
                        seen.add(related)

        # Bigrams for phrase matching — returned to caller, never stored on self
        # (thread-safety: concurrent searches would race on a shared attribute).
        bigrams = [
            (base_tokens[i], base_tokens[i + 1])
            for i in range(len(base_tokens) - 1)
        ]
        self._expand_cache[query] = (expanded, bigrams)
        if len(self._expand_cache) > 256:
            self._expand_cache.pop(next(iter(self._expand_cache)))
        return expanded, bigrams

    def _score_chunk(self, chunk_id: str, query: str, query_tokens: list,
                     query_bigrams: list = None) -> float:
        """Combine TF-IDF relevance with path/name heuristics, bigrams, and recency."""
        tfidf = self.chunk_tfidf.get(chunk_id, {})
        chunk = self.chunks[chunk_id]
        doc_id = chunk["doc_id"]
        path_lower = doc_id.lower()
        name_lower = (chunk.get("name") or "").lower()
        path_parts = [part for part in re.split(r'[/\\._-]+', path_lower) if part]
        chunk_tokens = chunk.get("tokens")
        if not chunk_tokens:
            chunk_tokens = count_tokens(chunk["content"])
            chunk["tokens"] = chunk_tokens

        score = sum(tfidf.get(qt, 0) for qt in query_tokens)
        if score <= 0:
            return 0.0

        for qt in query_tokens:
            if chunk.get("name"):
                if qt == name_lower:
                    score *= 3.2
                elif qt in name_lower:
                    score *= 1.7

            if qt in path_parts:
                score += 1.5
            elif qt in path_lower:
                score += 0.75

        # Cache lowercased content + tokens per-chunk. Before this cache,
        # the tokenizer ran for every chunk on every search — dominant hot-path cost.
        content_lower = chunk.get("_content_lower")
        if content_lower is None:
            content_lower = (chunk.get("content") or "").lower()
            chunk["_content_lower"] = content_lower

        query_lower = query.lower()
        if query_lower and query_lower in content_lower:
            score *= 2.0

        query_joined = query_lower.replace(" ", "_")
        if len(query_joined) > 3 and query_joined in content_lower:
            score *= 1.8

        if any(part in query_lower for part in path_parts[:2]):
            score += 0.4

        # Bigram scoring — bigrams are passed in (not read from self) so the
        # search path is thread-safe. Chunk tokens are cached after first use.
        if query_bigrams:
            chunk_content_tokens = chunk.get("_content_tokens")
            if chunk_content_tokens is None:
                chunk_content_tokens = self._tokenize(content_lower)
                chunk["_content_tokens"] = chunk_content_tokens
            for t1, t2 in query_bigrams:
                for j in range(len(chunk_content_tokens) - 1):
                    if chunk_content_tokens[j] == t1 and chunk_content_tokens[j + 1] == t2:
                        score *= 1.5
                        break

        # Recency bias — recently modified files get a small boost (1d)
        mtime = self._file_mtimes.get(doc_id, 0)
        if mtime > 0 and self._file_mtimes:
            max_mtime = max(self._file_mtimes.values())
            if max_mtime > 0:
                age_ratio = mtime / max_mtime  # 1.0 for newest, lower for older
                score *= (0.9 + 0.2 * age_ratio)  # up to 1.1x for newest files

        score += min(len(path_parts), 6) * 0.02
        size_penalty = 1.0 + max(0.0, chunk_tokens - 450) / 1200.0
        return score / size_penalty

    def _build_tfidf(self):
        """Build TF-IDF scores for all chunks."""
        N = len(self.chunks)
        if N == 0:
            return

        # Document frequency
        df = Counter()
        chunk_tf = {}

        for chunk_id, chunk in self.chunks.items():
            tokens = self._tokenize(chunk["content"])
            # Include file path tokens
            tokens += self._tokenize(chunk["doc_id"])
            if chunk.get("name"):
                tokens += self._tokenize(chunk["name"]) * 3  # Boost symbol names

            tf = Counter(tokens)
            chunk_tf[chunk_id] = tf
            for term in set(tokens):
                df[term] += 1

        # IDF
        self.idf = {term: math.log(N / (1 + freq)) for term, freq in df.items()}

        # TF-IDF per chunk
        self.chunk_tfidf = {}
        for chunk_id, tf in chunk_tf.items():
            self.chunk_tfidf[chunk_id] = {}
            max_tf = max(tf.values()) if tf else 1
            for term, freq in tf.items():
                normalized_tf = 0.5 + 0.5 * (freq / max_tf)
                self.chunk_tfidf[chunk_id][term] = normalized_tf * self.idf.get(term, 0)

    def _build_cooccurrence(self):
        """Build lightweight co-occurrence map from indexed chunks for auto-synonyms.

        Bounded on purpose: the pair loop is quadratic in a chunk's unique
        token count, so a single minified bundle (798 unique tokens x 4091
        chunks = 1.9e9 pair updates, measured on a real project) can hang a
        build that has nothing else wrong with it. Skipped chunks and an
        exhausted budget are recorded in ``self._cooccurrence_stats`` and
        returned by :meth:`build_index`.
        """
        self._cooccurrence = {}
        skipped = 0
        updates = 0
        budget_hit = False
        for chunk in self.chunks.values():
            tokens = set(self._tokenize(chunk["content"]))
            if len(tokens) > _COOC_MAX_CHUNK_TOKENS:
                skipped += 1
                continue
            if updates + len(tokens) * (len(tokens) - 1) > _COOC_MAX_PAIR_UPDATES:
                budget_hit = True
                break
            for t in tokens:
                if t not in self._cooccurrence:
                    self._cooccurrence[t] = Counter()
                for t2 in tokens:
                    if t != t2:
                        self._cooccurrence[t][t2] += 1
            updates += len(tokens) * (len(tokens) - 1)
        self._cooccurrence_stats = {
            "chunks_skipped": skipped,
            "pair_updates": updates,
            "budget_exhausted": budget_hit,
        }
        # Prune: keep only top-5 co-occurring terms per token (minimum 3 co-occurrences)
        pruned = {}
        for term, counts in self._cooccurrence.items():
            top = [(t, c) for t, c in counts.most_common(5) if c >= 3]
            if top:
                pruned[term] = dict(top)
        self._cooccurrence = pruned

    def search(self, query: str, top_k: int = 5, max_tokens: int = 4000,
               include_content: bool = True) -> list:
        """Search the index and return most relevant chunks.

        Set include_content=False to get metadata only (saves ~70% tokens).
        """
        if not self.chunks:
            self._load_index()
            if not self.chunks:
                return []

        cache_key = (query, int(top_k), int(max_tokens), bool(include_content))
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            self._search_cache.move_to_end(cache_key)
            return [dict(item) for item in cached]

        query_tokens, query_bigrams = self._expand_query_tokens(query)
        if not query_tokens:
            return []

        # Score each chunk
        scores = {}
        for chunk_id in self.chunk_tfidf:
            score = self._score_chunk(chunk_id, query, query_tokens, query_bigrams)
            if score > 0:
                scores[chunk_id] = score

        # Sort by score, then prefer named/structural chunks and shorter paths on ties.
        ranked = sorted(
            scores.items(),
            key=lambda item: (
                item[1],
                1 if self.chunks[item[0]].get("name") else 0,
                1 if self.chunks[item[0]].get("type") in {"function", "class", "method", "declaration"} else 0,
                -len(self.chunks[item[0]]["doc_id"]),
            ),
            reverse=True,
        )

        # Collect results up to token budget
        results = []
        token_budget = max_tokens
        seen_docs = set()

        for chunk_id, score in ranked[:top_k * 4]:
            chunk = self.chunks[chunk_id]
            chunk_tokens = chunk.get("tokens") or count_tokens(chunk["content"])

            if chunk_tokens > token_budget:
                continue

            if chunk["doc_id"] in seen_docs and len(results) >= max(2, top_k // 2):
                continue

            doc = self.documents.get(chunk["doc_id"], {})
            result = {
                "chunk_id": chunk_id,
                "file": chunk["doc_id"],
                "name": chunk.get("name"),
                "type": chunk["type"],
                "lines": f"{chunk['line_start']}-{chunk['line_end']}",
                "tokens": chunk_tokens,
                "file_tokens": doc.get("tokens", chunk_tokens),
                "score": round(score, 3),
            }
            if include_content:
                result["content"] = chunk["content"]

            results.append(result)

            token_budget -= chunk_tokens
            seen_docs.add(chunk["doc_id"])

            if len(results) >= top_k or token_budget <= 0:
                break

        self._search_cache[cache_key] = [dict(item) for item in results]
        if len(self._search_cache) > self._search_cache_max:
            self._search_cache.popitem(last=False)
        return results

    def get_context(self, query: str, top_k: int = 5, max_tokens: int = 4000) -> str:
        """Get a formatted context string ready to pass to Claude."""
        results = self.search(query, top_k, max_tokens)

        if not results:
            return "No relevant code found in index."

        sections = []
        total_tokens = 0

        for r in results:
            section = f"## {r['file']} (L{r['lines']})"
            if r['name']:
                section += f" — {r['name']}"
            section += f"\n```\n{r['content']}\n```"
            sections.append(section)
            total_tokens += r['tokens']

        header = f"# Relevant Code Context ({total_tokens} tokens, {len(results)} chunks)\n"
        return header + '\n\n'.join(sections)

    def _save_index(self):
        """Save index to disk."""
        # Strip in-memory caches (fields prefixed with _) before persisting —
        # they're regenerated on demand and would bloat index.json.
        data = {
            "documents": self.documents,
            "chunks": {
                k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                for k, v in self.chunks.items()
            },
            "symbols": self.symbols,
            "idf": self.idf,
            "chunk_tfidf": self.chunk_tfidf,
        }
        index_file = self.index_dir / "index.json"
        with open(index_file, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def _load_index(self) -> bool:
        """Load index from disk."""
        index_file = self.index_dir / "index.json"
        if not index_file.exists():
            return False

        try:
            with open(index_file, encoding='utf-8') as f:
                data = json.load(f)
            self.documents = data["documents"]
            self.chunks = data["chunks"]
            self.symbols = data.get("symbols", {})
            self.idf = data.get("idf", {})
            self.chunk_tfidf = data.get("chunk_tfidf", {})
            mutated = False
            for chunk in self.chunks.values():
                if "tokens" not in chunk:
                    chunk["tokens"] = count_tokens(chunk.get("content", ""))
                    mutated = True
            self._search_cache = OrderedDict()
            if mutated:
                self._save_index()
            return True
        except Exception:
            return False

    def get_stats(self) -> dict:
        """Get index statistics."""
        if not self.documents:
            self._load_index()

        total_tokens = sum(d.get("tokens", 0) for d in self.documents.values())
        return {
            "files_indexed": len(self.documents),
            "total_chunks": len(self.chunks),
            "total_tokens_in_codebase": total_tokens,
            "unique_symbols": len(self.symbols),
            "index_size_kb": round(
                (self.index_dir / "index.json").stat().st_size / 1024, 1
            ) if (self.index_dir / "index.json").exists() else 0
        }
