"""Regression tests for the startup deadlock in services/embedding_index.py.

Two independent failures used to combine into a dead-but-healthy-looking MCP
server:

1. ``_remove_file_chunks`` called ``collection.delete(where=...)``, which was
   observed never returning inside the chromadb Rust bindings
   (``chromadb/api/rust.py``, ``RustBindingsAPI._delete``). The old
   ``except``-guarded fallback could not help: a hang raises nothing, so the
   ``except`` clause was unreachable by construction.

2. ``build()`` held ``self._lock`` unconditionally while doing that, so the
   wedged thread parked every later caller behind it forever.

These tests pin both contracts. The hang is simulated with a blocking fake
collection so a regression FAILS (in a daemon thread, bounded join) instead of
hanging the suite -- the same convention tests/test_cli_smoke.py uses.
"""
import logging
import threading

import pytest

from services import embedding_index as ei_mod
from services.embedding_index import EmbeddingIndex

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingCollection:
    """Records every delete/get call so tests can assert on their shape."""

    def __init__(self, docs=None):
        # id -> doc_id
        self.docs = dict(docs or {})
        self.delete_calls = []
        self.get_calls = []

    def get(self, where=None, include=None, **kwargs):
        self.get_calls.append({"where": where, "include": include})
        doc_id = (where or {}).get("doc_id")
        ids = [i for i, d in self.docs.items() if d == doc_id]
        return {"ids": ids}

    def delete(self, ids=None, where=None, **kwargs):
        self.delete_calls.append({"ids": ids, "where": where})
        if where is not None and ids is None:
            raise AssertionError(
                "delete(where=...) is the call that hangs; it must never be used"
            )
        for i in ids or []:
            self.docs.pop(i, None)

    def count(self):
        return len(self.docs)

    def upsert(self, **kwargs):
        for i, m in zip(kwargs.get("ids", []), kwargs.get("metadatas", [])):
            self.docs[i] = m.get("doc_id")


class _HangingWhereDeleteCollection(_RecordingCollection):
    """delete(where=...) blocks forever, exactly like chromadb 1.5.6 did."""

    def __init__(self, docs=None):
        super().__init__(docs)
        self.released = threading.Event()
        self.where_delete_entered = threading.Event()

    def delete(self, ids=None, where=None, **kwargs):
        self.delete_calls.append({"ids": ids, "where": where})
        if where is not None and ids is None:
            self.where_delete_entered.set()
            # Never returns unless the test explicitly lets go.
            self.released.wait()
            return
        for i in ids or []:
            self.docs.pop(i, None)


class _FakeCodeIndex:
    def __init__(self, chunks):
        self.chunks = chunks

    def _load_index(self):
        pass


def _make_index(tmp_path, collection) -> EmbeddingIndex:
    """An EmbeddingIndex wired to *collection* with backends stubbed ready."""
    idx = EmbeddingIndex(str(tmp_path), ollama_client=None)
    idx._initialized = True
    idx._available = True
    idx._ollama_up = True
    idx._model_ok = True
    idx._ollama_ok = True
    idx._collection = collection
    return idx


def _chunks_for(doc_id, n=3):
    return {
        f"{doc_id}::c{i}": {
            "doc_id": doc_id,
            "content": f"def sym{i}():\n    return {i}  # padding to clear the 20-char floor",
            "name": f"sym{i}",
            "type": "function",
            "line_start": i * 10,
            "line_end": i * 10 + 5,
        }
        for i in range(n)
    }


# ---------------------------------------------------------------------------
# Bug 1 -- chunk removal must not use the hanging where-delete
# ---------------------------------------------------------------------------


def test_remove_file_chunks_deletes_by_id_never_by_where(tmp_path):
    """The get-ids-then-delete-by-ids path is primary, not a fallback."""
    col = _RecordingCollection({
        "a.py::c0": "a.py", "a.py::c1": "a.py", "b.py::c0": "b.py",
    })
    idx = _make_index(tmp_path, col)

    idx._remove_file_chunks("a.py")

    # The file's chunks are gone, the other file's are untouched.
    assert col.docs == {"b.py::c0": "b.py"}

    # Every delete resolved explicit ids; none passed a where filter.
    assert col.delete_calls, "expected at least one delete call"
    for call in col.delete_calls:
        assert call["where"] is None, f"where-delete reintroduced: {call}"
        assert call["ids"] is not None

    # The metadata filter moved to get(), which does return.
    assert col.get_calls[0]["where"] == {"doc_id": "a.py"}


def test_remove_file_chunks_returns_when_where_delete_would_hang(tmp_path):
    """A regression that reintroduces delete(where=) must fail, not hang.

    The fake blocks forever on where-delete. We join with a bound in a daemon
    thread, so a regression surfaces as a failed assertion rather than a
    wedged pytest run.
    """
    col = _HangingWhereDeleteCollection({"a.py::c0": "a.py", "a.py::c1": "a.py"})
    idx = _make_index(tmp_path, col)

    done = threading.Event()

    def _run():
        idx._remove_file_chunks("a.py")
        done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    finished = done.wait(timeout=10)

    col.released.set()  # let any wedged thread go so it cannot leak

    assert finished, (
        "_remove_file_chunks blocked -- it is calling delete(where=...) again"
    )
    assert not col.where_delete_entered.is_set()
    assert col.docs == {}


def test_remove_file_chunks_survives_include_kwarg_rejection(tmp_path):
    """Older chromadb (>=0.4.24 is supported) may reject include=[]."""

    class _NoIncludeCollection(_RecordingCollection):
        def get(self, where=None, include=None, **kwargs):
            if include is not None:
                raise TypeError("get() got an unexpected keyword argument 'include'")
            return super().get(where=where, **kwargs)

    col = _NoIncludeCollection({"a.py::c0": "a.py"})
    idx = _make_index(tmp_path, col)

    idx._remove_file_chunks("a.py")

    assert col.docs == {}
    assert col.delete_calls == [{"ids": ["a.py::c0"], "where": None}]


def test_remove_file_chunks_noop_when_nothing_matches(tmp_path):
    col = _RecordingCollection({"b.py::c0": "b.py"})
    idx = _make_index(tmp_path, col)

    idx._remove_file_chunks("a.py")

    assert col.delete_calls == []
    assert col.docs == {"b.py::c0": "b.py"}


def test_build_removes_stale_file_chunks_without_where_delete(tmp_path):
    """End-to-end: build() drops a deleted file's chunks by id."""
    col = _RecordingCollection({"gone.py::c0": "gone.py"})
    idx = _make_index(tmp_path, col)
    idx._file_hashes = {"gone.py": "deadbeef"}

    class _Ollama:
        def embed_batch(self, texts, model=None):
            return [[0.1, 0.2, 0.3] for _ in texts]

    idx.ollama = _Ollama()

    result = idx.build(_FakeCodeIndex(_chunks_for("kept.py")))

    assert "gone.py" not in idx._file_hashes
    assert "gone.py::c0" not in col.docs
    assert result.get("chunks_embedded", 0) == 3
    for call in col.delete_calls:
        assert call["where"] is None, f"where-delete reintroduced: {call}"


# ---------------------------------------------------------------------------
# Bug 2 -- a busy build lock degrades, it does not block
# ---------------------------------------------------------------------------


def test_build_degrades_instead_of_blocking_on_busy_lock(tmp_path, monkeypatch):
    """build() must give up on a held lock, not park behind it forever."""
    monkeypatch.setattr(ei_mod, "_BUILD_LOCK_WAIT_SECONDS", 0.2)

    col = _RecordingCollection()
    idx = _make_index(tmp_path, col)

    holder_may_release = threading.Event()
    holder_has_lock = threading.Event()

    def _hold():
        with idx._lock:
            holder_has_lock.set()
            holder_may_release.wait(timeout=30)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    assert holder_has_lock.wait(timeout=5), "holder never acquired the lock"

    result_box = {}
    done = threading.Event()

    def _build():
        result_box["r"] = idx.build(_FakeCodeIndex(_chunks_for("a.py")))
        done.set()

    t = threading.Thread(target=_build, daemon=True)
    t.start()
    finished = done.wait(timeout=10)

    holder_may_release.set()
    holder.join(timeout=5)

    assert finished, "build() blocked on a held lock instead of degrading"
    result = result_box["r"]
    assert result["degraded"] is True
    assert result["available"] is True
    assert result["chunks_embedded"] == 0
    assert "busy" in result["error"].lower()
    # Degraded returns keep the normal stats shape so callers using .get()
    # with defaults (cli/c3.py, cli/hub_server.py) do not KeyError.
    for key in ("files_processed", "files_skipped", "chunks_skipped",
                "errors", "total_embedded"):
        assert key in result


def test_busy_lock_warns_once_per_instance(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(ei_mod, "_BUILD_LOCK_WAIT_SECONDS", 0.05)
    idx = _make_index(tmp_path, _RecordingCollection())

    with caplog.at_level(logging.WARNING, logger="c3.embedding_index"):
        with idx._lock:
            assert idx._acquire_build_lock() is False
            assert idx._acquire_build_lock() is False
            assert idx._acquire_build_lock() is False

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one WARNING, got {len(warnings)}"
    assert "lock" in warnings[0].getMessage().lower()


def test_acquire_build_lock_releases_cleanly(tmp_path):
    """The happy path still takes and gives back the lock."""
    idx = _make_index(tmp_path, _RecordingCollection())

    assert idx._acquire_build_lock(timeout=1.0) is True
    idx._lock.release()
    # Still acquirable afterwards -- no leak.
    assert idx._acquire_build_lock(timeout=1.0) is True
    idx._lock.release()


def test_build_releases_lock_even_when_body_raises(tmp_path):
    """A failure mid-build must not leave the lock held forever.

    This is the property the ``with self._lock`` -> ``try/finally`` rewrite
    has to preserve.
    """
    idx = _make_index(tmp_path, _RecordingCollection())
    idx._file_hashes = {"gone.py": "deadbeef"}

    def _boom(doc_id):
        raise RuntimeError("backend exploded mid-build")

    idx._remove_file_chunks = _boom

    with pytest.raises(RuntimeError):
        idx.build(_FakeCodeIndex(_chunks_for("a.py")))

    assert idx._lock.acquire(timeout=1.0), "build() leaked the lock"
    idx._lock.release()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
