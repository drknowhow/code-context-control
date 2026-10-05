"""Caller labels must survive the real cross-project edit dispatch."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cli.tools import project
from services import agent_locks


def test_project_edit_uses_caller_provider_without_mutating_cached_runtime(tmp_path):
    (tmp_path / ".c3").mkdir()
    target = tmp_path / "router.py"
    target.write_text("alpha\n", encoding="utf-8")
    cached = MagicMock()
    cached.project_path = str(tmp_path)
    cached.ide_name = "claude-code"
    cached.edit_ledger = None
    cached.activity_log = None
    target_session = {"id": "target-session-123", "source_ide": "claude-code"}
    cached.session_mgr.current_session = target_session
    caller = SimpleNamespace(
        project_path=str(tmp_path), ide_name="codex",
        session_mgr=SimpleNamespace(current_session={"id": "caller-session-456"}),
    )

    def finalize(_name, _args, response, _summary, **_kwargs):
        return response

    with (
        patch.object(project, "resolve_project", return_value={
            "path": str(tmp_path), "name": "target",
        }),
        patch.object(project, "_is_registered", return_value=True),
        patch.object(project, "_runtime_for", return_value=cached),
    ):
        output = project.handle_project(
            "edit", caller, finalize, project=str(tmp_path),
            file_path=str(target), old_string="alpha", new_string="beta",
            allow_write=True,
        )

    assert "[error]" not in output
    assert target.read_text(encoding="utf-8") == "beta\n"
    lease = agent_locks.LockStore(tmp_path).snapshot()["locks"][0]
    assert lease["agent_id"] == "codex:target-s"
    assert lease["session_id"] == "target-session-123"
    assert cached.ide_name == "claude-code"
    assert cached.session_mgr.current_session is target_session
    assert target_session == {"id": "target-session-123", "source_ide": "claude-code"}
