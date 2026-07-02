"""Shared C3 runtime/bootstrap helpers for UI and MCP entrypoints."""

from __future__ import annotations

import json
import re as _re
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.config import load_delegate_config, load_hybrid_config
from core.ide import get_profile, load_ide_config
from services.activity_log import ActivityLog
from services.agents import create_agents
from services.claude_md import ClaudeMdManager
from services.compressor import CodeCompressor
from services.context_snapshot import ContextSnapshot
from services.conversation_store import ConversationStore
from services.doc_index import DocIndex
from services.edit_ledger import EditLedger
from services.embedding_index import EmbeddingIndex
from services.file_memory import FileMemoryStore
from services.indexer import CodeIndex
from services.memory import MemoryStore
from services.memory_consolidator import MemoryConsolidator
from services.memory_graph import MemoryGraph
from services.memory_grounder import MemoryGrounder
from services.memory_scorer import MemoryScorer
from services.metrics import MetricsCollector
from services.notifications import NotificationStore
from services.ollama_client import OllamaClient
from services.output_filter import OutputFilter
from services.retention import RetentionManager
from services.retrieval_broker import MemoryRetrievalBroker
from services.router import ModelRouter
from services.session_manager import SessionManager
from services.session_preloader import SessionPreloader
from services.task_store import TaskStore
from services.validation_cache import ValidationCache
from services.vector_store import VectorStore
from services.version_tracker import VersionTracker
from services.watcher import CodeWatcher


@dataclass
class C3Runtime:
    """Shared runtime container for C3 services."""

    project_path: str
    ide_name: str
    ide_profile: object
    indexer: CodeIndex
    compressor: CodeCompressor
    session_mgr: SessionManager
    memory: MemoryStore
    claude_md: ClaudeMdManager
    activity_log: ActivityLog
    notifications: NotificationStore
    hybrid_config: dict
    delegate_config: dict
    vector_store: Optional[VectorStore] = None
    output_filter: Optional[OutputFilter] = None
    router: Optional[ModelRouter] = None
    metrics: Optional[MetricsCollector] = None
    watcher: Optional[CodeWatcher] = None
    file_memory: Optional[FileMemoryStore] = None
    version_tracker: Optional[VersionTracker] = None
    ollama_client: Optional[OllamaClient] = None
    ollama_available: bool = False
    agents: list = field(default_factory=list)
    transcript_index: Optional[object] = None
    snapshots: Optional[object] = None
    convo_store: Optional[object] = None
    retrieval: Optional[object] = None
    embedding_index: Optional[EmbeddingIndex] = None
    doc_index: Optional[DocIndex] = None
    preloader: Optional[SessionPreloader] = None
    validation_cache: Optional[ValidationCache] = None
    edit_ledger: Optional[EditLedger] = None
    memory_scorer: Optional[MemoryScorer] = None
    memory_graph: Optional[MemoryGraph] = None
    memory_consolidator: Optional[MemoryConsolidator] = None
    memory_grounder: Optional[MemoryGrounder] = None
    retention: Optional[RetentionManager] = None
    task_store: Optional[TaskStore] = None


def _load_agent_config(project_path: Path) -> dict:
    config_path = project_path / ".c3" / "config.json"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, encoding="utf-8") as f:
            return json.load(f).get("agents", {})
    except Exception:
        return {}


_TMP_PATTERN = _re.compile(r"\.tmp\.\d+\.\d+$")
_TMP_MIN_AGE = 3600  # seconds — only sweep files older than 1h to avoid racing active atomic writes


def _cleanup_stale_tmp_files(project_path: Path) -> int:
    """Remove orphaned .tmp.PID.TIMESTAMP files left by interrupted atomic writes.

    Scoped to known hot directories so the sweep is O(small) on every startup.
    """
    count = 0
    scan_roots = [
        project_path,
        project_path / "cli",
        project_path / "cli" / "tools",
        project_path / "services",
        project_path / ".claude",
    ]
    now = _time.time()
    for root in scan_roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            for p in root.iterdir():
                if not p.is_file() or not _TMP_PATTERN.search(p.name):
                    continue
                try:
                    if now - p.stat().st_mtime > _TMP_MIN_AGE:
                        p.unlink()
                        count += 1
                except OSError:
                    pass
        except OSError:
            continue
    return count


def build_runtime(project_path: str, ide_name: str | None = None) -> C3Runtime:
    """Build the shared C3 runtime without starting background workers."""
    project = Path(project_path).resolve()
    _cleanup_stale_tmp_files(project)
    c3_dir = project / ".c3"
    resolved_ide = ide_name or load_ide_config(str(project))
    ide_profile = get_profile(resolved_ide)
    hybrid_config = load_hybrid_config(str(project))

    indexer = CodeIndex(str(project), str(c3_dir / "index"))
    compressor = CodeCompressor(str(c3_dir / "cache"), project_root=str(project))

    ollama_client = OllamaClient(hybrid_config.get("ollama_base_url", "http://localhost:11434"))
    session_mgr = SessionManager(str(project), str(c3_dir / "sessions"), ollama_client=ollama_client)

    vector_store = None
    if not hybrid_config.get("HYBRID_DISABLE_SLTM"):
        try:
            vector_store = VectorStore(str(project), hybrid_config)
        except Exception:
            pass

    output_filter = None
    if not hybrid_config.get("HYBRID_DISABLE_TIER1"):
        output_filter = OutputFilter(hybrid_config)

    router = None
    if not hybrid_config.get("HYBRID_DISABLE_TIER2"):
        router = ModelRouter(hybrid_config)

    metrics = MetricsCollector(
        output_filter=output_filter,
        router=router,
        vector_store=vector_store,
    )
    memory = MemoryStore(str(project), vector_store=vector_store)
    session_mgr._memory_store = memory
    convo_store = ConversationStore(str(project))
    snapshots = ContextSnapshot(str(project))
    watcher = CodeWatcher(str(project))
    file_memory = FileMemoryStore(str(project))
    validate_cfg = hybrid_config.get("validation_pipeline", {})
    validation_cache = ValidationCache(str(project), validate_cfg) if validate_cfg.get("enabled", True) else None
    watcher.set_backends(file_memory, compressor, validation_cache)
    version_tracker = VersionTracker(str(project), ide_name=resolved_ide)
    notifications = NotificationStore(str(project))
    claude_md = ClaudeMdManager(
        str(project),
        session_mgr,
        indexer,
        memory,
        instructions_file=ide_profile.instructions_file or "CLAUDE.md",
        line_limit=ide_profile.instructions_line_limit or 200,
        supports_hooks=ide_profile.supports_hooks,
        supports_clear=ide_profile.supports_clear,
    )
    activity_log = ActivityLog(str(project))
    delegate_config = load_delegate_config(str(project))
    retrieval = MemoryRetrievalBroker(
        str(project),
        memory_store=memory,
        conversation_store=convo_store,
        file_memory=file_memory,
        snapshots=snapshots,
    )
    memory.set_retrieval_broker(retrieval)

    # Embedding index for semantic code search
    embedding_index = None
    if not hybrid_config.get("disable_embedding_index"):
        try:
            embed_model = hybrid_config.get("embed_model", "nomic-embed-text")
            embedding_index = EmbeddingIndex(
                str(project), ollama_client, embed_model=embed_model,
            )
        except Exception:
            pass

    # Doc index for Local RAG Pipeline
    doc_index = None
    rag_config = hybrid_config.get("rag", {})
    if rag_config.get("enabled", True):
        try:
            doc_index = DocIndex(str(project), str(c3_dir / "doc_index"))
        except Exception:
            pass

    # Session preloader for first-prompt auto-retrieval
    preloader = None
    if doc_index and rag_config.get("enabled", True):
        preloader = SessionPreloader(
            doc_index=doc_index,
            embedding_index=embedding_index,
            session_mgr=session_mgr,
            memory_store=memory,
            config=rag_config,
        )

    # Edit ledger for AI-tracked versioning
    edit_ledger = EditLedger(str(project))

    # Project management store (tasks/milestones/notes) — construction is I/O-free
    task_store = TaskStore(str(project))

    # Memory brain: scorer, graph, consolidator, grounder
    memory_scorer = MemoryScorer()
    memory_graph = MemoryGraph(str(project))
    memory_consolidator = MemoryConsolidator(
        memory_store=memory,
        graph=memory_graph,
        scorer=memory_scorer,
        project_path=str(project),
    )
    memory_grounder = MemoryGrounder(
        project_path=str(project),
        memory_store=memory,
        graph=memory_graph,
        file_memory=file_memory,
    )

    # Storage retention & rotation (P5) — sweeps run via EditLedgerEnricherAgent
    retention = RetentionManager(
        str(project),
        edit_ledger=edit_ledger,
        notifications=notifications,
        file_memory=file_memory,
    )

    # Ollama probe deferred to background — shaves ~2s off startup.
    # Runtime starts with ollama_available=False; background thread updates it.
    import threading as _t

    def _bg_ollama_probe():
        try:
            runtime.ollama_available = ollama_client.is_available(timeout=2)
        except Exception:
            pass

    runtime = C3Runtime(
        project_path=str(project),
        ide_name=resolved_ide,
        ide_profile=ide_profile,
        indexer=indexer,
        compressor=compressor,
        session_mgr=session_mgr,
        memory=memory,
        claude_md=claude_md,
        activity_log=activity_log,
        notifications=notifications,
        hybrid_config=hybrid_config,
        delegate_config=delegate_config,
        vector_store=vector_store,
        output_filter=output_filter,
        router=router,
        metrics=metrics,
        watcher=watcher,
        file_memory=file_memory,
        version_tracker=version_tracker,
        ollama_client=ollama_client,
        ollama_available=False,
        snapshots=snapshots,
        convo_store=convo_store,
        retrieval=retrieval,
        embedding_index=embedding_index,
        doc_index=doc_index,
        preloader=preloader,
        validation_cache=validation_cache,
        edit_ledger=edit_ledger,
        memory_scorer=memory_scorer,
        memory_graph=memory_graph,
        memory_consolidator=memory_consolidator,
        memory_grounder=memory_grounder,
        retention=retention,
        task_store=task_store,
    )
    runtime.agents = create_agents(
        runtime,
        notifications,
        _load_agent_config(project),
        ollama=ollama_client,
    )
    # Start Ollama probe in background after runtime is constructed
    _t.Thread(target=_bg_ollama_probe, daemon=True, name="c3-ollama-probe").start()
    return runtime


def start_runtime(runtime: C3Runtime) -> None:
    """Start watcher and agent background workers."""
    if runtime.watcher:
        runtime.watcher.start()
    for agent in runtime.agents or []:
        agent.start()


def stop_runtime(runtime: Optional[C3Runtime]) -> None:
    """Stop watcher and agents for a runtime."""
    if not runtime:
        return
    for agent in runtime.agents or []:
        try:
            agent.stop()
        except Exception:
            pass
    if runtime.watcher:
        try:
            runtime.watcher.stop()
        except Exception:
            pass
