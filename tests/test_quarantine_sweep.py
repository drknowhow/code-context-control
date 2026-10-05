"""Sweep of leftover ``quarantine_corrupt_*`` dirs: the service function, ``c3 prune-quarantine``, ``c3 init``."""
from __future__ import annotations

import json
import os
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

import pytest

from cli import c3 as c3_cli
from cli.commands.parser import build_parser
from services import embedding_index as ei_mod
from services.embedding_index import prune_quarantines

STAMPS = ("20260930_073930", "20260930_074018", "20261001_000000", "20261002_120000")
PAYLOAD = 1000


def _quarantine(embeddings, stamp):
    store = embeddings / f"quarantine_corrupt_{stamp}" / "chromadb"
    store.mkdir(parents=True)
    (store / "data_level0.bin").write_bytes(b"x" * PAYLOAD)
    return store.parent


def _names(embeddings):
    return sorted(p.name for p in embeddings.glob("quarantine_corrupt_*"))


def _run(argv):
    c3_cli.cmd_prune_quarantine(build_parser("0.0-test", lambda v: v).parse_args(argv))


@pytest.fixture
def embeddings(tmp_path):
    d = tmp_path / ".c3" / "embeddings"
    d.mkdir(parents=True)
    for stamp in STAMPS:
        _quarantine(d, stamp)
    return d


def test_prune_keeps_the_newest_two_by_default(embeddings):
    res = prune_quarantines(embeddings)

    assert res["pruned"] == [f"quarantine_corrupt_{s}" for s in STAMPS[:2]]
    assert res["kept"] == [f"quarantine_corrupt_{s}" for s in STAMPS[2:]]
    assert res["failed"] == []
    assert res["bytes"] == 2 * PAYLOAD
    assert _names(embeddings) == res["kept"]


def test_prune_dry_run_reports_without_deleting(embeddings):
    res = prune_quarantines(embeddings, dry_run=True)

    assert len(res["pruned"]) == 2 and res["bytes"] == 2 * PAYLOAD
    assert len(_names(embeddings)) == 4


def test_prune_keep_zero_removes_every_copy(embeddings):
    res = prune_quarantines(embeddings, keep=0)

    assert len(res["pruned"]) == 4 and res["kept"] == []
    assert _names(embeddings) == []


def test_prune_leaves_files_and_symlinks_alone(embeddings, tmp_path):
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("mine", encoding="utf-8")
    (embeddings / "quarantine_corrupt_20250101_000000").write_text("a file", encoding="utf-8")
    try:
        os.symlink(precious, embeddings / "quarantine_corrupt_20250102_000000",
                   target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    prune_quarantines(embeddings, keep=0)

    assert (precious / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert (embeddings / "quarantine_corrupt_20250101_000000").is_file()
    assert (embeddings / "quarantine_corrupt_20250102_000000").is_symlink()


def test_prune_of_a_missing_dir_is_empty(tmp_path):
    assert prune_quarantines(tmp_path / "nope") == {
        "kept": [], "pruned": [], "failed": [], "bytes": 0}


def test_prune_reports_a_dir_that_would_not_go(embeddings, monkeypatch):
    monkeypatch.setattr(ei_mod.shutil, "rmtree", lambda *a, **k: None)

    res = prune_quarantines(embeddings)

    assert res["pruned"] == [] and len(res["failed"]) == 2 and res["bytes"] == 0
    assert len(_names(embeddings)) == 4


def test_prune_reports_progress_per_dir(embeddings):
    seen = []

    prune_quarantines(embeddings, on_progress=lambda done, total: seen.append((done, total)))

    assert seen == [(1, 2), (2, 2)]


def test_command_prunes_and_reports(tmp_path, embeddings, capsys):
    _run(["prune-quarantine", str(tmp_path)])

    out = capsys.readouterr().out
    assert "removed 2 quarantine dir(s)" in out
    assert len(_names(embeddings)) == 2


def test_command_dry_run_deletes_nothing(tmp_path, embeddings, capsys):
    _run(["prune-quarantine", str(tmp_path), "--dry-run"])

    assert "would remove 2 quarantine dir(s)" in capsys.readouterr().out
    assert len(_names(embeddings)) == 4


def test_command_keep_overrides_the_default(tmp_path, embeddings):
    _run(["prune-quarantine", str(tmp_path), "--keep", "1"])

    assert _names(embeddings) == [f"quarantine_corrupt_{STAMPS[-1]}"]


def test_command_says_so_when_there_is_nothing_to_prune(tmp_path, capsys):
    _run(["prune-quarantine", str(tmp_path)])

    assert "Nothing to prune in 1 project(s)." in capsys.readouterr().out


def test_command_all_walks_every_registered_project(tmp_path, capsys):
    paths = []
    for name in ("one", "two"):
        project = tmp_path / name
        emb = project / ".c3" / "embeddings"
        emb.mkdir(parents=True)
        for stamp in STAMPS:
            _quarantine(emb, stamp)
        paths.append(project)
    with mock.patch("services.project_manager.ProjectManager") as pm:
        pm.return_value.list_registered.return_value = [{"path": str(p)} for p in paths]
        _run(["prune-quarantine", "--all"])

    assert [len(_names(p / ".c3" / "embeddings")) for p in paths] == [2, 2]
    assert "Removed 4 quarantine dir(s)" in capsys.readouterr().out


def test_command_exits_nonzero_when_a_dir_survives(tmp_path, embeddings, monkeypatch):
    monkeypatch.setattr(ei_mod.shutil, "rmtree", lambda *a, **k: None)

    with pytest.raises(SystemExit) as exc:
        _run(["prune-quarantine", str(tmp_path)])

    assert exc.value.code == 1


def test_init_on_an_existing_install_sweeps_quarantines(tmp_path, embeddings, capsys):
    (tmp_path / ".c3" / "config.json").write_text(json.dumps({"version": "2.153.1"}),
                                                  encoding="utf-8")
    health = {"sessions": 0, "facts": 0, "instructions_file": "CLAUDE.md",
              "issues": [], "healthy": True}
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(c3_cli, "_check_c3_health", return_value=health))
        stack.enter_context(mock.patch.object(c3_cli, "_prompt_choice", return_value="Cancel"))
        stack.enter_context(mock.patch("services.ollama_client.OllamaClient"))
        stack.enter_context(mock.patch("cli.tools.delegate.check_codex",
                                       return_value={"status": "missing"}))
        stack.enter_context(mock.patch("cli.tools.delegate.check_gemini",
                                       return_value={"status": "missing"}))
        c3_cli.cmd_init(SimpleNamespace(project_path=str(tmp_path), ide="auto"))

    assert "freed" in capsys.readouterr().out
    assert len(_names(embeddings)) == 2
