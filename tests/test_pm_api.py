"""PM endpoints: hub board/mutations/global aggregate + audit + inspect wiring."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli import hub_server  # noqa: E402
from services import project_manager as pm_mod  # noqa: E402
from services import project_runtime as pr  # noqa: E402
from services.task_store import TaskStore  # noqa: E402


class PmApiBase(unittest.TestCase):
    def setUp(self):
        self.client = hub_server.app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.proj = self.base / "alpha"
        (self.proj / ".c3").mkdir(parents=True)
        (self.proj / ".c3" / "config.json").write_text("{}", encoding="utf-8")
        self.reg_file = self.base / "projects.json"
        self._write_registry([
            {"name": "alpha", "path": str(self.proj.resolve()), "ide": "claude-code"},
        ])
        self._patches = [
            mock.patch.object(pr, "_PROJECTS_FILE", self.reg_file),
            mock.patch.object(pm_mod, "_PROJECTS_FILE", self.reg_file),
            mock.patch.object(pm_mod, "_REGISTRY_FILE", self.base / "registry.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _write_registry(self, projects):
        self.reg_file.write_text(json.dumps({"projects": projects}), encoding="utf-8")

    def _audit_events(self, project=None):
        log = (project or self.proj) / ".c3" / "activity_log.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in
                log.read_text(encoding="utf-8").strip().splitlines() if line]


class TestPmBoard(PmApiBase):
    def test_requires_path(self):
        self.assertEqual(self.client.get("/api/projects/pm").status_code, 400)

    def test_unknown_project_404(self):
        resp = self.client.get(f"/api/projects/pm?path={self.base / 'nope'}")
        self.assertEqual(resp.status_code, 404)

    def test_uninitialized_409(self):
        bare = self.base / "bare"
        bare.mkdir()
        resp = self.client.get(f"/api/projects/pm?path={bare}")
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.get_json()["needs_init"])

    def test_board_shape(self):
        TaskStore(str(self.proj)).create_task("seeded")
        resp = self.client.get(f"/api/projects/pm?path={self.proj}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data["board"]["columns"]["backlog"]), 1)
        self.assertIn("stats", data["board"])
        self.assertIn("notes", data)


class TestTaskMutations(PmApiBase):
    def test_create_update_move_archive_roundtrip_with_audit(self):
        resp = self.client.post("/api/projects/pm/task", json={
            "path": str(self.proj), "title": "build board", "priority": "p1"})
        self.assertEqual(resp.status_code, 201, resp.get_json())
        task = resp.get_json()["task"]
        self.assertEqual(task["created_by"], "hub")

        resp = self.client.put("/api/projects/pm/task", json={
            "path": str(self.proj), "id": task["id"],
            "fields": {"description": "kanban"},
            "move": {"status": "in_progress"}})
        updated = resp.get_json()["task"]
        self.assertEqual(updated["status"], "in_progress")
        self.assertEqual(updated["description"], "kanban")

        resp = self.client.delete("/api/projects/pm/task", json={
            "path": str(self.proj), "id": task["id"]})
        self.assertTrue(resp.get_json()["archived"])

        resp = self.client.delete("/api/projects/pm/task", json={
            "path": str(self.proj), "purge": True})
        self.assertEqual(resp.get_json()["purged"], 1)

        ops = [(e["entity"], e["op"]) for e in self._audit_events()
               if e.get("type") == "pm_write"]
        self.assertEqual(ops, [("task", "create"), ("task", "update"),
                               ("task", "archive"), ("task", "purge")])

    def test_create_validation_400(self):
        resp = self.client.post("/api/projects/pm/task",
                                json={"path": str(self.proj), "title": ""})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/projects/pm/task", json={
            "path": str(self.proj), "title": "x", "status": "doing"})
        self.assertEqual(resp.status_code, 400)

    def test_update_requires_fields_or_move(self):
        t = TaskStore(str(self.proj)).create_task("x")
        resp = self.client.put("/api/projects/pm/task",
                               json={"path": str(self.proj), "id": t["id"]})
        self.assertEqual(resp.status_code, 400)

    def test_put_rev_conflict_409(self):
        t = TaskStore(str(self.proj)).create_task("x")
        resp = self.client.put("/api/projects/pm/task", json={
            "path": str(self.proj), "id": t["id"],
            "fields": {"priority": "p1"}, "expected_rev": 999})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json().get("code"), "rev_conflict")
        resp = self.client.put("/api/projects/pm/task", json={
            "path": str(self.proj), "id": t["id"],
            "fields": {"priority": "p1"}, "expected_rev": 1})
        self.assertEqual(resp.status_code, 200)

    def test_events_endpoint_records_history(self):
        t = TaskStore(str(self.proj)).create_task("evt", created_by="test")
        self.client.put("/api/projects/pm/task", json={
            "path": str(self.proj), "id": t["id"],
            "fields": {"priority": "p0"}})
        resp = self.client.get(
            f"/api/projects/pm/events?path={self.proj}&limit=10")
        self.assertEqual(resp.status_code, 200)
        events = resp.get_json()["events"]
        self.assertEqual(events[0]["op"], "update")
        self.assertEqual(events[0]["actor"], "hub")
        self.assertEqual(events[0]["patch"]["priority"], ["p2", "p0"])
        self.assertEqual(events[-1]["op"], "create")

    def test_deps_and_report_endpoints(self):
        store = TaskStore(str(self.proj))
        a = store.create_task("a")
        b = store.create_task("b")
        resp = self.client.post("/api/projects/pm/deps", json={
            "path": str(self.proj), "id": a["id"], "blocker": b["id"]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["task"]["blocked_by"], [b["id"]])
        resp = self.client.post("/api/projects/pm/deps", json={
            "path": str(self.proj), "id": b["id"], "blocker": a["id"]})
        self.assertEqual(resp.status_code, 400)  # would cycle
        resp = self.client.get(f"/api/projects/pm/report?path={self.proj}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("throughput", resp.get_json()["report"])

    def test_restore_via_put(self):
        t = TaskStore(str(self.proj)).create_task("arch-me")
        self.client.delete("/api/projects/pm/task",
                           json={"path": str(self.proj), "id": t["id"]})
        resp = self.client.put("/api/projects/pm/task", json={
            "path": str(self.proj), "id": t["id"], "restore": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["task"]["lifecycle"], "active")

    def test_time_endpoints_crud(self):
        resp = self.client.post("/api/projects/time/entry", json={
            "path": str(self.proj), "minutes": 90, "note": "deep work"})
        self.assertEqual(resp.status_code, 201, resp.get_json())
        entry = resp.get_json()["entry"]
        resp = self.client.put("/api/projects/time/entry", json={
            "path": str(self.proj), "id": entry["id"],
            "fields": {"minutes": 45}})
        self.assertEqual(resp.get_json()["entry"]["minutes"], 45)
        resp = self.client.get(f"/api/projects/time?path={self.proj}")
        data = resp.get_json()
        self.assertEqual(data["summary"]["today"]["manual_min"], 45)
        self.assertEqual(len(data["entries"]), 1)
        resp = self.client.delete("/api/projects/time/entry", json={
            "path": str(self.proj), "id": entry["id"]})
        self.assertTrue(resp.get_json()["deleted"])
        resp = self.client.post("/api/projects/time/entry", json={
            "path": str(self.proj), "minutes": 0})
        self.assertEqual(resp.status_code, 400)


class TestMilestoneNoteLink(PmApiBase):
    def test_milestone_lifecycle(self):
        resp = self.client.post("/api/projects/pm/milestone", json={
            "path": str(self.proj), "name": "M1", "target_date": "2026-08-01"})
        ms = resp.get_json()["milestone"]
        self.client.post("/api/projects/pm/task", json={
            "path": str(self.proj), "title": "in ms", "milestone_id": ms["id"]})
        resp = self.client.put("/api/projects/pm/milestone", json={
            "path": str(self.proj), "id": ms["id"], "fields": {"name": "M1b"}})
        self.assertEqual(resp.get_json()["milestone"]["name"], "M1b")
        resp = self.client.delete("/api/projects/pm/milestone",
                                  json={"path": str(self.proj), "id": ms["id"]})
        self.assertEqual(resp.get_json()["milestone"]["detached_tasks"], 1)

    def test_note_and_link(self):
        resp = self.client.post("/api/projects/pm/note", json={
            "path": str(self.proj), "text": "picked floats", "kind": "decision"})
        self.assertEqual(resp.get_json()["note"]["kind"], "decision")
        t = TaskStore(str(self.proj)).create_task("x")
        resp = self.client.post("/api/projects/pm/link", json={
            "path": str(self.proj), "id": t["id"],
            "link": {"type": "file", "ref": "cli/server.py"}})
        self.assertEqual(len(resp.get_json()["task"]["links"]), 1)
        resp = self.client.post("/api/projects/pm/link", json={
            "path": str(self.proj), "id": t["id"], "op": "remove",
            "link": {"type": "file", "ref": "cli/server.py"}})
        self.assertEqual(resp.get_json()["task"]["links"], [])


class TestChildrenRollup(PmApiBase):
    def test_include_children(self):
        child = self.proj / "api"
        (child / ".c3").mkdir(parents=True)
        (child / ".c3" / "config.json").write_text(json.dumps(
            {"parent": {"name": "alpha", "path": str(self.proj.resolve())}}),
            encoding="utf-8")
        (self.proj / ".c3" / "config.json").write_text(json.dumps({
            "subprojects": [{"name": "api", "rel_path": "api", "added_at": "x"}]}),
            encoding="utf-8")
        self._write_registry([
            {"name": "alpha", "path": str(self.proj.resolve())},
            {"name": "api", "path": str(child.resolve()),
             "parent_path": str(self.proj.resolve())},
        ])
        TaskStore(str(child)).create_task("child task")
        resp = self.client.get(
            f"/api/projects/pm?path={self.proj}&include_children=1")
        data = resp.get_json()
        self.assertEqual(len(data["children"]), 1)
        self.assertEqual(data["children"][0]["name"], "api")
        self.assertEqual(data["children"][0]["tasks"][0]["title"], "child task")


class TestGlobal(PmApiBase):
    def setUp(self):
        super().setUp()
        self.beta = self.base / "beta"
        (self.beta / ".c3").mkdir(parents=True)
        self._write_registry([
            {"name": "alpha", "path": str(self.proj.resolve())},
            {"name": "beta", "path": str(self.beta.resolve()),
             "parent_path": str(self.proj.resolve())},
            {"name": "ghost", "path": str(self.base / "missing")},
        ])
        TaskStore(str(self.proj)).create_task("a-open", priority="p0")
        done = TaskStore(str(self.proj)).create_task("a-done")
        TaskStore(str(self.proj)).update_task(done["id"], status="done")
        TaskStore(str(self.beta)).create_task("b-open")

    def test_aggregate_open_tasks(self):
        resp = self.client.get("/api/pm/global")
        data = resp.get_json()
        self.assertEqual(data["projects_scanned"], 2)
        self.assertEqual(len(data["skipped"]), 1)
        titles = [t["title"] for t in data["tasks"]]
        self.assertIn("a-open", titles)
        self.assertIn("b-open", titles)
        self.assertNotIn("a-done", titles)
        self.assertEqual(titles[0], "a-open")  # p0 sorts first
        beta_row = next(t for t in data["tasks"] if t["title"] == "b-open")
        self.assertEqual(beta_row["project"]["name"], "beta")
        self.assertIn("parent_path", beta_row["project"])
        self.assertEqual(data["by_project"][str(self.proj.resolve())]["open"], 1)

    def test_cap_and_status_all(self):
        resp = self.client.get("/api/pm/global?limit=1")
        data = resp.get_json()
        self.assertTrue(data["capped"])
        self.assertEqual(len(data["tasks"]), 1)
        resp = self.client.get("/api/pm/global?status=all")
        titles = [t["title"] for t in resp.get_json()["tasks"]]
        self.assertIn("a-done", titles)

    def test_status_all_open_count_excludes_done(self):
        resp = self.client.get("/api/pm/global?status=all")
        bp = resp.get_json()["by_project"][str(self.proj.resolve())]
        self.assertEqual(bp["open"], 1)   # a-done is not open
        self.assertEqual(bp["shown"], 2)  # but both rows ship to the client

    def test_global_blocker_enrichment(self):
        store = TaskStore(str(self.proj))
        blocker = store.create_task("dep-blocker")
        blocked = store.create_task("dep-blocked")
        store.add_dependency(blocked["id"], blocker["id"])
        store.update_task(blocked["id"], status="blocked")
        resp = self.client.get("/api/pm/global")
        row = next(t for t in resp.get_json()["tasks"] if t["id"] == blocked["id"])
        self.assertEqual(row["blockers_open"], 1)
        self.assertEqual(row["blocker_titles"], ["dep-blocker"])
        store.update_task(blocker["id"], status="done")
        resp = self.client.get("/api/pm/global")
        row = next(t for t in resp.get_json()["tasks"] if t["id"] == blocked["id"])
        self.assertEqual(row["blockers_open"], 0)  # done blocker resolved server-side


class TestInspectWiring(PmApiBase):
    def test_inspect_tasks_view_and_overview_count(self):
        TaskStore(str(self.proj)).create_task("x")

        class _Stub:
            memory = type("M", (), {"facts": []})()
            edit_ledger = None
            session_mgr = type("S", (), {"list_sessions": staticmethod(lambda n: [])})()
            task_store = TaskStore(str(self.proj))

        with mock.patch.object(hub_server, "_get_runtime", lambda p: _Stub()):
            resp = self.client.post("/api/projects/inspect", json={
                "path": str(self.proj), "view": "tasks"})
            self.assertEqual(len(resp.get_json()["board"]["columns"]["backlog"]), 1)
            resp = self.client.post("/api/projects/inspect", json={
                "path": str(self.proj), "view": "overview"})
            self.assertEqual(resp.get_json()["counts"]["tasks_open"], 1)

    def test_projects_list_open_task_count(self):
        TaskStore(str(self.proj)).create_task("x")
        resp = self.client.get("/api/projects")
        row = next(p for p in resp.get_json() if p["name"] == "alpha")
        self.assertEqual(row["open_task_count"], 1)


if __name__ == "__main__":
    unittest.main()
