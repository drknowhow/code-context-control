"""Regression tests for corrupt-store recovery in services/embedding_index.py.

A damaged HNSW segment does not announce itself at open time. chromadb hands
back a client and a collection handle, then faults on the first real read --
and when that read lands on the background index thread, the fault is a native
access violation that kills the whole MCP process. The host sees only a server
that connected, listed tools and died (observed 2026-07-17 and 2026-09-06).

The fix probes the store on the init path, where the failure is still a
catchable Python exception, and treats a failed probe as corruption: close the
client, move the store aside, reopen empty. These tests pin that contract.
"""
import json

import pytest

from services import embedding_index as ei_mod
from services.embedding_index import COLLECTION_NAME, EmbeddingIndex


class _FakeCollection:
    """A collection whose health probe can be made to fail."""

    def __init__(self, count_error=None):
        self._count_error = count_error
        self.count_calls = 0

    def count(self):
        self.count_calls += 1
        if self._count_error is not None:
            raise self._count_error


class _FakeClient:
    def __init__(self, path, collection, on_close=None):
        self.path = path
        self._collection = collection
        self._on_close = on_close
        self.closed = False

    def get_or_create_collection(self, name=None, metadata=None, **kwargs):
        assert name == COLLECTION_NAME
        return self._collection

    def close(self):
        self.closed = True
        if self._on_close is not None:
            self._on_close()


def _install_fake_chromadb(monkeypatch, collections):
    """Patch chromadb.PersistentClient to hand out *collections* in order."""
    made = []
    remaining = list(collections)

    class _FakeChromadb:
        @staticmethod
        def PersistentClient(path=None, settings=None):
            client = _FakeClient(path, remaining.pop(0))
            made.append(client)
            return client

    class _FakeConfig:
        @staticmethod
        def Settings(**kwargs):
            return object()

    import sys
    import types

    mod = types.ModuleType("chromadb")
    mod.PersistentClient = _FakeChromadb.PersistentClient
    cfg = types.ModuleType("chromadb.config")
    cfg.Settings = _FakeConfig.Settings
    mod.config = cfg
    monkeypatch.setitem(sys.modules, "chromadb", mod)
    monkeypatch.setitem(sys.modules, "chromadb.config", cfg)
    return made


def _make_index(tmp_path) -> EmbeddingIndex:
    return EmbeddingIndex(str(tmp_path), ollama_client=None)


def test_healthy_store_is_not_quarantined(tmp_path, monkeypatch):
    """A store that answers count() is left exactly where it is."""
    healthy = _FakeCollection()
    made = _install_fake_chromadb(monkeypatch, [healthy])
    idx = _make_index(tmp_path)

    idx._open_chroma()

    assert healthy.count_calls == 1, "the health probe must actually run"
    assert len(made) == 1, "a healthy store must not be reopened"
    assert not made[0].closed
    assert (tmp_path / ".c3" / "embeddings" / "chromadb").is_dir()
    assert not list((tmp_path / ".c3" / "embeddings").glob("quarantine_corrupt_*"))


def test_corrupt_store_is_quarantined_and_reopened_empty(tmp_path, monkeypatch):
    """A failed probe moves the store aside and reopens, rather than raising."""
    store = tmp_path / ".c3" / "embeddings" / "chromadb"
    store.mkdir(parents=True, exist_ok=True)
    (store / "chroma.sqlite3").write_text("damaged", encoding="utf-8")

    boom = RuntimeError(
        "Error executing plan: Error sending backfill request to compactor: "
        "Failed to apply logs to the hnsw segment writer"
    )
    corrupt, fresh = _FakeCollection(count_error=boom), _FakeCollection()
    made = _install_fake_chromadb(monkeypatch, [corrupt, fresh])
    idx = _make_index(tmp_path)

    idx._open_chroma()

    # It recovered onto the second client rather than propagating the error.
    assert len(made) == 2
    assert made[0].closed, "the corrupt client must be closed before the move"
    assert idx._collection is fresh
    assert fresh.count_calls == 1, "the replacement store is probed too"

    # The damaged bytes were preserved for post-mortem, not deleted.
    quarantines = list((tmp_path / ".c3" / "embeddings").glob("quarantine_corrupt_*"))
    assert len(quarantines) == 1
    moved = quarantines[0] / "chromadb" / "chroma.sqlite3"
    assert moved.read_text(encoding="utf-8") == "damaged"


def test_quarantine_takes_the_hash_file_with_it(tmp_path, monkeypatch):
    """Stale hashes would suppress the rebuild the quarantine exists to force."""
    embeddings = tmp_path / ".c3" / "embeddings"
    embeddings.mkdir(parents=True, exist_ok=True)
    (embeddings / "chromadb").mkdir(exist_ok=True)
    hash_file = embeddings / "file_hashes_v2.json"
    hash_file.write_text(json.dumps({"cli/mcp_server.py": "deadbeef"}), encoding="utf-8")

    boom = RuntimeError("Failed to apply logs to the hnsw segment writer")
    _install_fake_chromadb(monkeypatch, [_FakeCollection(count_error=boom), _FakeCollection()])
    idx = _make_index(tmp_path)
    idx._file_hashes = {"cli/mcp_server.py": "deadbeef"}

    idx._open_chroma()

    assert not hash_file.exists(), "stale hashes must not survive the quarantine"
    assert idx._file_hashes == {}, "in-memory hashes must be dropped too"
    quarantined = next(embeddings.glob("quarantine_corrupt_*")) / "file_hashes_v2.json"
    assert json.loads(quarantined.read_text(encoding="utf-8")) == {
        "cli/mcp_server.py": "deadbeef"
    }


def test_second_failure_raises_instead_of_looping(tmp_path, monkeypatch):
    """One quarantine, then give up: a rebuild loop is worse than degrading."""
    boom = RuntimeError("Failed to apply logs to the hnsw segment writer")
    made = _install_fake_chromadb(
        monkeypatch,
        [_FakeCollection(count_error=boom), _FakeCollection(count_error=boom)],
    )
    idx = _make_index(tmp_path)

    with pytest.raises(RuntimeError):
        idx._open_chroma()

    assert len(made) == 2, "exactly one retry, not an unbounded loop"


def test_init_backends_degrades_instead_of_killing_the_server(tmp_path, monkeypatch):
    """Unrecoverable corruption marks the index unavailable; it never raises."""
    boom = RuntimeError("Failed to apply logs to the hnsw segment writer")
    _install_fake_chromadb(
        monkeypatch,
        [_FakeCollection(count_error=boom), _FakeCollection(count_error=boom)],
    )
    idx = _make_index(tmp_path)

    class _DeadOllama:
        def is_available(self, timeout=None):
            return False

        def has_model(self, model):
            return False

    idx.ollama = _DeadOllama()

    idx._init_backends()  # must not raise

    assert idx._available is False
