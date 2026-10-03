"""Store-safety guards for services/embedding_index.py (issue #180).

A corrupt HNSW segment made chromadb commit 140-200 GB while it failed to load,
once per C3 server, and the old quarantine copied the store instead of moving it
whenever another process held the files, so each new session left another full
copy behind and reopened the same corrupt store. These tests pin the guards:

- an existing store is opened in a capped child before this process touches it;
- only one process per project owns the store;
- quarantine renames or raises, never copies, and keeps a bounded history.
"""
import os
import sys
import types

import pytest

from services import embedding_index as ei_mod
from services.embedding_index import (
    COLLECTION_NAME,
    EmbeddingIndex,
    StoreBusyError,
    run_isolated_probe,
)


class _Col:
    def __init__(self):
        self.count_calls = 0

    def count(self):
        self.count_calls += 1
        return 0


class _Client:
    def __init__(self, path):
        self.path = path
        self.col = _Col()

    def get_or_create_collection(self, name=None, metadata=None, **kw):
        assert name == COLLECTION_NAME
        return self.col

    def list_collections(self):
        return []

    def close(self):
        pass


def _fake_chromadb(monkeypatch):
    opened = []

    def PersistentClient(path=None, settings=None):
        # Record what was on disk at the moment this process opened the store.
        segs = sorted(p.name for p in __import__("pathlib").Path(path).glob("*/header.bin"))
        c = _Client(path)
        c.segments_at_open = segs
        opened.append(c)
        return c

    mod = types.ModuleType("chromadb")
    mod.PersistentClient = PersistentClient
    cfg = types.ModuleType("chromadb.config")
    cfg.Settings = lambda **kw: object()
    mod.config = cfg
    monkeypatch.setitem(sys.modules, "chromadb", mod)
    monkeypatch.setitem(sys.modules, "chromadb.config", cfg)
    return opened


def _seed_segment(tmp_path):
    seg = tmp_path / ".c3" / "embeddings" / "chromadb" / "seg-1"
    seg.mkdir(parents=True)
    (seg / "header.bin").write_bytes(b"\x01" * 100)
    (seg.parent / "chroma.sqlite3").write_text("rows", encoding="utf-8")
    return seg


@pytest.fixture
def idx(tmp_path):
    made = []

    def make():
        i = EmbeddingIndex(str(tmp_path), ollama_client=None)
        made.append(i)
        return i

    yield make
    for i in made:
        i._owner_lock.release()


def test_failed_isolated_probe_quarantines_before_this_process_opens(tmp_path, monkeypatch, idx):
    _seed_segment(tmp_path)
    opened = _fake_chromadb(monkeypatch)
    monkeypatch.setattr(
        EmbeddingIndex, "_probe_store_isolated",
        lambda self, d: (False, "probe exceeded its memory cap (9.0 GB > 4.0 GB)"))
    i = idx()

    i._open_chroma()

    assert len(opened) == 1
    assert opened[0].segments_at_open == [], "the corrupt segment must never be opened here"
    q = list((tmp_path / ".c3" / "embeddings").glob("quarantine_corrupt_*"))
    assert len(q) == 1 and (q[0] / "chromadb" / "seg-1" / "header.bin").exists()


def test_store_without_segments_skips_the_child_probe(tmp_path, monkeypatch, idx):
    _fake_chromadb(monkeypatch)
    calls = []
    monkeypatch.setattr(EmbeddingIndex, "_probe_store_isolated",
                        lambda self, d: calls.append(d) or (True, ""))
    idx()._open_chroma()
    assert calls == []


def test_passing_probe_is_remembered_until_the_store_changes(tmp_path, monkeypatch, idx):
    seg = _seed_segment(tmp_path)
    runs = []
    monkeypatch.setattr(ei_mod, "run_isolated_probe",
                        lambda args, **kw: runs.append(args) or (True, ""))
    i = idx()
    store = tmp_path / ".c3" / "embeddings" / "chromadb"

    assert i._probe_store_isolated(store) == (True, "")
    assert i._probe_store_isolated(store) == (True, "")
    assert len(runs) == 1, "an unchanged store must not be re-probed"

    (seg / "header.bin").write_bytes(b"\x02" * 101)
    i._probe_store_isolated(store)
    assert len(runs) == 2, "a changed store must be probed again"


def test_second_server_for_the_project_does_not_open_the_store(tmp_path, monkeypatch, idx):
    opened = _fake_chromadb(monkeypatch)
    first, second = idx(), idx()

    first._open_chroma()
    with pytest.raises(StoreBusyError):
        second._open_chroma()
    assert len(opened) == 1

    class _NoOllama:
        def is_available(self, timeout=None):
            return False

        def has_model(self, m):
            return False

    second.ollama = _NoOllama()
    second._init_backends()
    assert second._available is False
    assert "another C3 server" in second.unavailable_reason()

    first._owner_lock.release()
    second._init_backends()
    assert second._available is True, "the lock must free when its holder lets go"


def test_quarantine_that_cannot_rename_raises_and_copies_nothing(tmp_path, monkeypatch, idx):
    _seed_segment(tmp_path)
    i = idx()

    def refuse(src, dst):
        raise PermissionError(32, "being used by another process")

    monkeypatch.setattr(ei_mod.os, "rename", refuse)
    with pytest.raises(RuntimeError, match="could not move"):
        i._quarantine_store(tmp_path / ".c3" / "embeddings" / "chromadb")

    emb = tmp_path / ".c3" / "embeddings"
    assert not list(emb.glob("quarantine_corrupt_*")), "no copy, no empty dir left behind"
    assert (emb / "chromadb" / "seg-1" / "header.bin").exists()


def test_quarantine_history_is_bounded(tmp_path, idx):
    emb = tmp_path / ".c3" / "embeddings"
    for stamp in ("20260930_073930", "20260930_074018", "20261001_000000"):
        (emb / f"quarantine_corrupt_{stamp}").mkdir(parents=True)
    _seed_segment(tmp_path)
    i = idx()

    i._quarantine_store(emb / "chromadb")

    left = sorted(p.name for p in emb.glob("quarantine_corrupt_*"))
    assert len(left) == ei_mod._QUARANTINE_KEEP
    assert left[0] == "quarantine_corrupt_20261001_000000"
    assert left[-1].startswith("quarantine_corrupt_2"), "the new quarantine survives"
    assert left[-1] != "quarantine_corrupt_20261001_000000"


def test_isolated_probe_kills_a_child_that_outgrows_its_cap():
    pytest.importorskip("psutil")
    code = "import time; b = bytearray(768 * 1024 * 1024); time.sleep(60)"
    ok, why = run_isolated_probe(["-c", code], cap_bytes=128 * 1024 * 1024, timeout=60)
    assert ok is False
    assert "memory cap" in why


def test_isolated_probe_needs_the_marker_not_just_exit_zero():
    ok, why = run_isolated_probe(["-c", "pass"], cap_bytes=1 << 30, timeout=60)
    assert ok is False and "probe failed" in why
    ok, why = run_isolated_probe(
        ["-c", f"print({ei_mod._PROBE_OK_MARKER!r})"], cap_bytes=1 << 30, timeout=60)
    assert (ok, why) == (True, "")


def test_isolated_probe_passes_a_real_healthy_store(tmp_path):
    """Negative control: the child code must accept a store chromadb wrote."""
    chromadb = pytest.importorskip("chromadb")
    from chromadb.config import Settings

    store = tmp_path / "chromadb"
    client = chromadb.PersistentClient(path=str(store),
                                       settings=Settings(anonymized_telemetry=False))
    col = client.get_or_create_collection(COLLECTION_NAME, metadata={"hnsw:space": "cosine"})
    col.add(ids=[f"c{n}" for n in range(50)],
            embeddings=[[float(n % 7 == k) + 0.01 for k in range(8)] for n in range(50)])
    assert col.count() == 50
    del col, client

    ok, why = run_isolated_probe(
        ["-c", ei_mod._PROBE_CHILD_CODE, str(store), COLLECTION_NAME],
        cap_bytes=4 << 30, timeout=120)
    assert (ok, why) == (True, "")
