"""Unit tests for services/shell_output.py — the c3_shell spill store (S1).

Covers:
- project_id normalization (case/slash on Windows, case kept on POSIX), opaque ids
- ShellCapture streams a real subprocess (3 MB single line + 200 KB stderr) with
  bounded memory; stats/head/tail/sha256 correct; text() fast path and its ceiling
- redact runs before bytes hit disk, including a secret straddling a read piece
- timeout path calls kill_tree
- promote writes the triplet + meta, discard removes the spool
- resolve(): same wording for unknown / other project / other session ids, guard
  re-check denial, expired, swept; the happy path
- read window + 512-char clip, search with context, tail, delete, list
- sweep: expired first, oldest-first to the byte cap, foreign files untouched,
  stale spool pieces, the 10-minute marker
- paths with spaces and unicode end to end
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from services import shell_output as so  # noqa: E402

PY = sys.executable
_ALLOW = lambda path: None  # guard_check that allows everything  # noqa: E731


def _popen(code: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    return subprocess.Popen(
        [PY, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, env=env,
    )


class _FakeProc:
    """A Popen stand-in with in-memory binary pipes — fast store tests."""

    def __init__(self, out: bytes = b"", err: bytes = b"", rc: int = 0):
        self.stdout = io.BytesIO(out)
        self.stderr = io.BytesIO(err)
        self.returncode = rc

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        pass


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _capture_bytes(spool: Path, out: bytes, err: bytes = b"", rc: int = 0, **kw) -> so.ShellCapture:
    cap = so.ShellCapture(_FakeProc(out, err, rc), spool, **kw)
    cap.wait(5)
    return cap


def _rewrite_meta(meta: so.OutputMeta, **changes) -> None:
    data = json.loads(meta.meta_path.read_text(encoding="utf-8"))
    data.update(changes)
    meta.meta_path.write_text(json.dumps(data), encoding="utf-8")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# ── Identity ────────────────────────────────────────────────────────────────

class TestIdentity(unittest.TestCase):
    def test_windows_normalization_collapses_case_and_slashes(self):
        a = so._norm_project("U:/A/B", windows=True)
        b = so._norm_project("u:\\a\\b\\", windows=True)
        self.assertEqual(a, b)
        self.assertEqual(a, "u:/a/b")

    def test_posix_normalization_keeps_case(self):
        self.assertNotEqual(so._norm_project("/A/B", windows=False),
                            so._norm_project("/a/b", windows=False))
        self.assertEqual(so._norm_project("/a/b/", windows=False), "/a/b")

    @unittest.skipUnless(sys.platform == "win32", "case-insensitive project ids are a Windows rule")
    def test_project_id_stable_across_spellings_on_windows(self):
        self.assertEqual(so.project_id("U:/A/B"), so.project_id("u:\\a\\b\\"))
        self.assertEqual(so.project_id(Path("U:/A/B")), so.project_id("U:/A/B/"))

    def test_project_id_shape_with_spaces_and_unicode(self):
        pid = so.project_id("U:/Мои проекты/c3 v2 (α)")
        self.assertRegex(pid, r"^[0-9a-f]{12}$")
        self.assertEqual(pid, so.project_id("U:/Мои проекты/c3 v2 (α)/"))
        self.assertNotEqual(pid, so.project_id("U:/Мои проекты/c3 v3"))

    def test_output_ids_are_opaque_and_unique(self):
        ids = {so.new_output_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        for oid in ids:
            self.assertRegex(oid, r"^o-[0-9a-f]{12}$")

    def test_session_dir_mapping(self):
        self.assertEqual(so._session_dir(""), "nosession")
        self.assertEqual(so._session_dir("018f2b3c-aaaa-4bbb-8ccc-1234567890ab"), "018f2b3c-aaaa-4bbb-8ccc-1234567890ab")
        hashed = so._session_dir("sess ión/1")
        self.assertRegex(hashed, r"^s-[0-9a-f]{12}$")
        self.assertEqual(hashed, so._session_dir("sess ión/1"))

    def test_env_override_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {so.ENV_ROOT: tmp}):
                self.assertEqual(so.ShellOutputStore().root, Path(tmp))
            with patch.dict(os.environ, {so.ENV_ROOT: ""}):
                self.assertEqual(so.ShellOutputStore().root, so.STORE_ROOT)


# ── Streaming capture ───────────────────────────────────────────────────────

class TestShellCapture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.spool = Path(self._tmp.name) / "spool dir ü"

    def tearDown(self):
        self._tmp.cleanup()

    def test_big_single_line_streams_with_bounded_memory(self):
        code = (
            "import sys\n"
            "out = b''.join(b'%07d,' % i for i in range(375000)) + b'\\n'\n"
            "sys.stdout.buffer.write(out); sys.stdout.buffer.flush()\n"
            "err = b''.join(b'e%02d' % (i % 100) + b'x' * 96 + b'\\n' for i in range(2000))\n"
            "sys.stderr.buffer.write(err); sys.stderr.buffer.flush()\n"
        )
        proc = _popen(code)
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            cap = so.ShellCapture(proc, self.spool)
            timed_out = cap.wait(120)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertFalse(timed_out)
        self.assertEqual(cap.exit_code, 0)
        self.assertEqual(cap.errors, {})
        # 3 MB on one line never sat in RAM whole (head+tail 128 KiB + one 64 KiB piece per stream).
        self.assertLess(peak, 2 * 1024 * 1024, f"peak traced memory {peak} bytes")

        out = cap.stats.stdout
        self.assertEqual(out.bytes, 3_000_001)
        self.assertEqual(out.lines, 1)
        self.assertEqual(out.longest_line, 3_000_000)
        self.assertTrue(out.truncated_middle)
        spool_bytes = cap.stdout_path.read_bytes()
        self.assertEqual(len(spool_bytes), 3_000_001)
        self.assertEqual(out.sha256, hashlib.sha256(spool_bytes).hexdigest())
        self.assertEqual(out.head, spool_bytes[: so.HEAD_BYTES].decode())
        self.assertEqual(out.tail, spool_bytes[-so.TAIL_BYTES:].decode())
        self.assertTrue(out.head.startswith("0000000,0000001,"))
        self.assertTrue(out.tail.endswith("0374999,\n"))

        err = cap.stats.stderr
        self.assertEqual(err.bytes, 200_000)
        self.assertEqual(err.lines, 2000)
        self.assertEqual(err.longest_line, 99)
        self.assertTrue(err.truncated_middle)
        self.assertEqual(err.sha256, _sha(cap.stderr_path))
        self.assertEqual(err.head, cap.stderr_path.read_bytes()[: so.HEAD_BYTES].decode())

        with self.assertRaises(ValueError):
            cap.text("stdout")
        text = cap.text("stderr")
        self.assertEqual(len(text.encode()), 200_000)
        self.assertTrue(text.startswith("e00" + "x" * 96 + "\n"))

    def test_multibyte_and_crlf_accounting(self):
        raw = "h\u00e9llo\r\nw\u00f6rld\n\u20ac".encode("utf-8")
        cap = _capture_bytes(self.spool, raw, b"")
        st = cap.stats.stdout
        self.assertEqual(st.bytes, len(raw))
        self.assertEqual(st.lines, 3)
        self.assertEqual(st.longest_line, 5)
        self.assertEqual(st.head, raw.decode())
        self.assertEqual(st.tail, raw.decode())
        self.assertFalse(st.truncated_middle)
        self.assertEqual(cap.stdout_path.read_bytes(), raw)
        self.assertEqual(cap.text("stdout"), raw.decode())
        self.assertEqual(cap.stats.stderr.bytes, 0)
        self.assertEqual(cap.stats.stderr.sha256, hashlib.sha256(b"").hexdigest())

    def test_long_line_pieces_preserve_bytes_across_chunk_boundaries(self):
        # "€" is 3 bytes; 64 KiB pieces cut mid-character, and the holdback re-joins pieces.
        line = "\u20ac" * 70000
        raw = (line + "\nshort 1\nshort 2\nshort 3\n").encode("utf-8")
        cap = _capture_bytes(self.spool, raw, b"", head_bytes=1000, tail_bytes=1000)
        st = cap.stats.stdout
        self.assertEqual(cap.stdout_path.read_bytes(), raw)
        self.assertEqual(st.bytes, len(raw))
        self.assertEqual(st.lines, 4)
        self.assertEqual(st.longest_line, 70000)
        self.assertEqual(st.sha256, hashlib.sha256(raw).hexdigest())
        self.assertTrue(st.truncated_middle)
        # previews are decoded at character boundaries: no U+FFFD introduced by the byte cut
        self.assertNotIn("\ufffd", st.head)
        self.assertNotIn("\ufffd", st.tail)
        self.assertTrue(raw.decode().startswith(st.head))
        self.assertTrue(raw.decode().endswith(st.tail))
        self.assertGreaterEqual(len(st.head.encode()), 1000 - 3)
        self.assertGreaterEqual(len(st.tail.encode()), 1000 - 3)

    def test_redact_runs_before_disk_even_across_pieces(self):
        secret = "SECRET123"
        # second line: the secret starts 4 bytes before the 64 KiB piece boundary
        raw = (
            f"token={secret} ok\n"
            + "x" * (so._CHUNK - 4) + secret + "y" * 100 + "\n"
            + "tail line\n"
        ).encode("utf-8")
        calls = []

        def redact(piece: str) -> str:
            calls.append(len(piece))
            return piece.replace(secret, "[cred:T]")

        cap = _capture_bytes(self.spool, raw, secret.encode() + b" on stderr\n", redact=redact)
        spool_out = cap.stdout_path.read_bytes()
        self.assertNotIn(secret.encode(), spool_out)
        self.assertEqual(spool_out.count(b"[cred:T]"), 2)
        self.assertNotIn(secret.encode(), cap.stderr_path.read_bytes())
        self.assertEqual(cap.stats.stdout.sha256, hashlib.sha256(spool_out).hexdigest())
        self.assertEqual(cap.stats.stdout.bytes, len(spool_out))
        self.assertEqual(cap.stats.stdout.lines, 3)
        self.assertNotIn(secret, cap.stats.stdout.head)
        self.assertNotIn(secret, cap.stats.stdout.tail)
        self.assertGreater(len(calls), 2)          # the long line arrived in pieces

    def test_timeout_invokes_kill_tree(self):
        proc = _popen("import time; time.sleep(30)")
        killed = []

        def kill_tree(p):
            killed.append(p.pid)
            p.kill()

        cap = so.ShellCapture(proc, self.spool, kill_tree=kill_tree)
        t0 = time.monotonic()
        timed_out = cap.wait(1)
        self.assertTrue(timed_out)
        self.assertTrue(cap.timed_out)
        self.assertEqual(killed, [proc.pid])
        self.assertEqual(cap.exit_code, -1)
        self.assertLess(time.monotonic() - t0, 15)
        self.assertTrue(cap.finished)
        self.assertIsNotNone(proc.poll())

    def test_text_needs_wait_and_discard_removes_spool(self):
        cap = so.ShellCapture(_FakeProc(b"hi\n"), self.spool)
        with self.assertRaises(RuntimeError):
            cap.text("stdout")
        cap.wait(5)
        self.assertEqual(cap.text("stdout"), "hi\n")
        self.assertTrue(cap.stdout_path.exists() and cap.stderr_path.exists())
        cap.discard()
        self.assertFalse(cap.stdout_path.exists())
        self.assertFalse(cap.stderr_path.exists())
        cap.discard()                                    # idempotent
        with self.assertRaises(ValueError):
            cap.text("nope")

    def test_text_pipes_are_rejected(self):
        proc = subprocess.Popen([PY, "-c", "pass"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True)
        try:
            with self.assertRaises(TypeError):
                so.ShellCapture(proc, self.spool)
        finally:
            proc.communicate(timeout=30)

    def test_missing_stream_keeps_triplet_complete(self):
        proc = _FakeProc(b"only out\n")
        proc.stderr = None
        cap = so.ShellCapture(proc, self.spool)
        cap.wait(5)
        self.assertEqual(cap.stats.stderr.bytes, 0)
        self.assertEqual(cap.stderr_path.read_bytes(), b"")
        self.assertEqual(cap.text("stderr"), "")
        self.assertEqual(cap.stats.stdout.lines, 1)

    def test_empty_output(self):
        cap = _capture_bytes(self.spool, b"", b"")
        st = cap.stats.stdout
        self.assertEqual((st.bytes, st.lines, st.longest_line), (0, 0, 0))
        self.assertEqual(st.sha256, hashlib.sha256(b"").hexdigest())
        self.assertEqual(st.head, "")
        self.assertFalse(st.truncated_middle)
        self.assertEqual(cap.text("stdout"), "")


# ── Store ───────────────────────────────────────────────────────────────────

_LINES = [f"line {i:03d} alpha" for i in range(1, 101)]
for _n in (10, 12, 80):
    _LINES[_n - 1] += " needle here"
_LINES[30 - 1] += " NEEDLE loud"
_LINES[50 - 1] = "Z" * 2000
_BODY = ("\n".join(_LINES) + "\n").encode("utf-8")


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "c3 out ünïcode"
        self.store = so.ShellOutputStore(self.root, apply_acl=False)
        self.project = str(base / "proj A ü")
        self.other_project = str(base / "proj B")
        self.session = "018f2b3c-aaaa-4bbb-8ccc-1234567890ab"
        self.cwd = str(Path(self.project) / "sub dir")

    def tearDown(self):
        self._tmp.cleanup()

    def spill(self, out: bytes = _BODY, err: bytes = b"", *, project=None, session="default",
              cmd="grep -rn needle .\nsecond line", rc=0, guard_paths=None, timed_out=False) -> so.OutputMeta:
        cap = _capture_bytes(self.store.spool_dir(), out, err, rc)
        return self.store.promote(
            cap, project_path=project or self.project,
            session_id=self.session if session == "default" else session,
            cmd=cmd, cwd=self.cwd,
            guard_paths=guard_paths if guard_paths is not None else [self.cwd, str(Path(self.project) / "src")],
            exit_code=cap.exit_code, timed_out=timed_out, duration_ms=cap.duration_ms,
        )

    def resolve(self, oid, *, project=None, session="default", guard_check=_ALLOW) -> so.OutputMeta:
        return self.store.resolve(oid, project_path=project or self.project,
                               session_id=self.session if session == "default" else session,
                               guard_check=guard_check)


class TestPromote(_StoreCase):
    def test_promote_writes_triplet_and_meta(self):
        cap = _capture_bytes(self.store.spool_dir(), _BODY, b"warn\n", rc=3)
        spool_out, spool_err = cap.stdout_path, cap.stderr_path
        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        meta = self.store.promote(
            cap, project_path=self.project, session_id=self.session,
            cmd="grep -rn needle .\nsecond line", cwd=self.cwd,
            guard_paths=[self.cwd, Path(self.project) / "src"],
            exit_code=cap.exit_code, timed_out=False, duration_ms=cap.duration_ms,
        )
        self.assertFalse(spool_out.exists())
        self.assertFalse(spool_err.exists())
        d = self.root / so.project_id(self.project) / self.session
        self.assertEqual(Path(meta.dir), d)
        self.assertEqual((d / f"{meta.id}.stdout").read_bytes(), _BODY)
        self.assertEqual((d / f"{meta.id}.stderr").read_bytes(), b"warn\n")
        data = json.loads((d / f"{meta.id}.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(set(data), set(so._META_KEYS))
        self.assertEqual(data["id"], meta.id)
        self.assertEqual(data["project_id"], so.project_id(self.project))
        self.assertEqual(data["project_path"], self.project)
        self.assertEqual(data["session_id"], self.session)
        self.assertEqual(data["cmd_display"], "grep -rn needle .")
        self.assertEqual(data["cmd_sha256"], hashlib.sha256(b"grep -rn needle .\nsecond line").hexdigest())
        self.assertEqual(data["cwd"], self.cwd)
        self.assertEqual(data["exit_code"], 3)
        self.assertFalse(data["timed_out"])
        self.assertIsInstance(data["duration_ms"], int)
        self.assertEqual(data["stdout"], {"bytes": len(_BODY), "lines": 100, "longest_line": 2000,
                                          "sha256": hashlib.sha256(_BODY).hexdigest()})
        self.assertEqual(data["stderr"]["bytes"], 5)
        self.assertEqual(data["guard"], {"cwd": self.cwd, "paths": [self.cwd, str(Path(self.project) / "src")]})
        self.assertFalse(data["acl_applied"])
        created = datetime.fromisoformat(data["created_at"])
        expires = datetime.fromisoformat(data["expires_at"])
        self.assertGreaterEqual(created, before)
        self.assertEqual(expires - created, timedelta(days=so.RETENTION_DAYS))
        self.assertTrue((self.root / so.SWEEP_MARKER).exists())

    def test_cmd_display_is_clipped_to_240(self):
        meta = self.spill(cmd="x" * 500 + "\nrest")
        self.assertEqual(len(meta.cmd_display), 240)
        self.assertTrue(meta.cmd_display.endswith("…"))

    def test_promote_requires_wait(self):
        cap = so.ShellCapture(_FakeProc(b"x\n"), self.store.spool_dir())
        with self.assertRaises(RuntimeError):
            self.store.promote(cap, project_path=self.project, session_id=self.session, cmd="x",
                               cwd=self.cwd, guard_paths=[], exit_code=0, timed_out=False, duration_ms=0)
        cap.wait(5)
        cap.discard()

    def test_no_session_maps_to_nosession_dir(self):
        meta = self.spill(session=None)
        self.assertEqual(Path(meta.dir).name, so.NO_SESSION)
        self.assertEqual(meta.session_id, "")
        self.assertEqual(self.resolve(meta.id, session="").id, meta.id)
        self.assertEqual(self.resolve(meta.id, session=None).id, meta.id)

    def test_permissions_are_user_only_when_acl_enabled(self):
        store = so.ShellOutputStore(self.root / "acl", apply_acl=True)
        cap = _capture_bytes(store.spool_dir(), b"x\n")
        meta = store.promote(cap, project_path=self.project, session_id=self.session, cmd="x",
                             cwd=self.cwd, guard_paths=[], exit_code=0, timed_out=False, duration_ms=1)
        self.assertIsInstance(meta.acl_applied, bool)
        if os.name == "nt":
            self.assertTrue(meta.acl_applied, "icacls on the store root should succeed for the current user")
            self.assertTrue((store.root / so.ACL_MARKER).exists())
        else:
            self.assertTrue(meta.acl_applied)
            self.assertEqual(stat.S_IMODE(store.root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(meta.stream_path("stdout").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(meta.meta_path.stat().st_mode), 0o600)
        # a broken icacls / chmod never fails promotion
        with patch.object(so, "_secure_root", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                so.ShellOutputStore(self.root / "acl2", apply_acl=True)._ensure_root()
        with patch.object(so.subprocess, "run", side_effect=OSError("no icacls")), \
             patch.object(so.os, "chmod", side_effect=OSError("ro fs")):
            store3 = so.ShellOutputStore(self.root / "acl3", apply_acl=True)
            cap3 = _capture_bytes(store3.spool_dir(), b"x\n")
            meta3 = store3.promote(cap3, project_path=self.project, session_id=self.session, cmd="x",
                                   cwd=self.cwd, guard_paths=[], exit_code=0, timed_out=False, duration_ms=1)
            self.assertFalse(meta3.acl_applied)
            self.assertTrue(meta3.stream_path("stdout").exists())


class TestOpenGate(_StoreCase):
    def test_happy_path_returns_meta(self):
        meta = self.spill()
        seen = []

        def guard_check(path):
            seen.append(str(path))
            return None

        got = self.resolve(meta.id, guard_check=guard_check)
        self.assertEqual(got.id, meta.id)
        self.assertEqual(got.to_dict(), meta.to_dict())
        self.assertEqual(seen, [self.cwd, self.cwd, str(Path(self.project) / "src")])

    def test_unknown_other_project_and_other_session_share_one_wording(self):
        meta = self.spill()
        messages = {}
        for label, kwargs in {
            "unknown": dict(oid="o-0123456789ab"),
            "other_project": dict(oid=meta.id, project=self.other_project),
            "other_session": dict(oid=meta.id, session="another-session"),
        }.items():
            with self.assertRaises(so.OutputAccessError) as ctx:
                self.resolve(kwargs["oid"], project=kwargs.get("project"), session=kwargs.get("session", "default"))
            messages[label] = str(ctx.exception).replace(kwargs["oid"], "<id>")
        self.assertEqual(len(set(messages.values())), 1, messages)
        self.assertIn("not found for this project and session", messages["unknown"])
        for bad in ("", None, "o-xyz", "../../etc/passwd", "o-0123456789ab/../x", 42):
            with self.assertRaises(so.OutputAccessError) as ctx:
                self.resolve(bad)
            self.assertIn("not found for this project and session", str(ctx.exception))

    def test_moved_file_from_another_project_still_refused(self):
        # even if a meta lands in the caller's directory, the recorded project/session must match
        meta = self.spill(project=self.other_project)
        mine = self.root / so.project_id(self.project) / self.session
        mine.mkdir(parents=True, exist_ok=True)
        for suffix in ("stdout", "stderr", "meta.json"):
            (mine / f"{meta.id}.{suffix}").write_bytes((Path(meta.dir) / f"{meta.id}.{suffix}").read_bytes())
        with self.assertRaises(so.OutputAccessError) as ctx:
            self.resolve(meta.id)
        self.assertIn("not found", str(ctx.exception))

    def test_guard_denial_refuses_with_path(self):
        meta = self.spill()
        denied = str(Path(self.project) / "src")

        def guard_check(path):
            if str(path) == denied:
                return SimpleNamespace(rule="**/src/**", kind="deny", scope="global", reason="deny rule")
            return None

        with self.assertRaises(so.OutputAccessError) as ctx:
            self.resolve(meta.id, guard_check=guard_check)
        msg = str(ctx.exception)
        self.assertIn(meta.id, msg)
        self.assertIn(denied, msg)
        self.assertIn("deny rule **/src/**", msg)
        self.assertIn("no longer readable", msg)
        # cwd is checked too
        with self.assertRaises(so.OutputAccessError):
            self.resolve(meta.id, guard_check=lambda p: SimpleNamespace(rule="x", kind="read_only", scope="project",
                                                                     reason="r") if str(p) == self.cwd else None)
        # no guard at all: fail closed
        with self.assertRaises(so.OutputAccessError) as ctx:
            self.resolve(meta.id, guard_check=None)
        self.assertIn("no access-guard check", str(ctx.exception))

    def test_expired_and_swept(self):
        meta = self.spill()
        _rewrite_meta(meta, expires_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=1)))
        with self.assertRaises(so.OutputAccessError) as ctx:
            self.resolve(meta.id)
        self.assertIn("expired", str(ctx.exception))
        meta2 = self.spill()
        meta2.stream_path("stdout").unlink()
        with self.assertRaises(so.OutputAccessError) as ctx:
            self.resolve(meta2.id)
        self.assertIn("swept or deleted", str(ctx.exception))
        # a corrupt meta reads as not found, not as a crash
        meta3 = self.spill()
        meta3.meta_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(so.OutputAccessError) as ctx:
            self.resolve(meta3.id)
        self.assertIn("not found", str(ctx.exception))


class TestReadSearchTail(_StoreCase):
    def test_read_window_and_line_clip(self):
        meta = self.spill()
        text = self.store.read(meta, lines=(48, 52))
        lines = text.split("\n")
        self.assertEqual(lines[0], "[stdout L48-52 of 100]")
        self.assertEqual(lines[1], "line 048 alpha")
        self.assertEqual(lines[5], "line 052 alpha")
        clipped = lines[3]
        self.assertTrue(clipped.startswith("Z" * so.LINE_CLIP_PREFIX + "…[+1488 chars]…"))
        self.assertTrue(clipped.endswith("Z" * so.LINE_CLIP_SUFFIX))
        self.assertEqual(clipped.count("Z"), so.LINE_CLIP_PREFIX + so.LINE_CLIP_SUFFIX)
        # whole stream by default, budget-clipped with a resume hint
        text = self.store.read(meta, lines=(1, 10), max_bytes=50)
        self.assertTrue(text.startswith("[stdout L1-3 of 100]\nline 001 alpha\n"))
        self.assertIn("continue with lines=(4, 10)", text)
        text = self.store.read(meta, max_bytes=50)
        self.assertIn("continue with lines=(4, 100)", text)
        # past EOF
        self.assertEqual(self.store.read(meta, lines=(500, 600)), "[stdout: no lines in 500-600; stream has 100 lines]")
        # stderr stream and bad windows
        self.assertIn("no lines", self.store.read(meta, "stderr"))
        for bad in ((0, 5), (5, 2), "1-2"):
            with self.assertRaises(ValueError):
                self.store.read(meta, lines=bad)
        with self.assertRaises(ValueError):
            self.store.read(meta, "nope")

    def test_search_context_groups_and_caps(self):
        meta = self.spill()
        out = self.store.search(meta, "needle", context=2).split("\n")
        self.assertEqual(out, [
            " L8: line 008 alpha", " L9: line 009 alpha", ">L10: line 010 alpha needle here",
            " L11: line 011 alpha", ">L12: line 012 alpha needle here", " L13: line 013 alpha",
            " L14: line 014 alpha", "---", " L78: line 078 alpha", " L79: line 079 alpha",
            ">L80: line 080 alpha needle here", " L81: line 081 alpha", " L82: line 082 alpha",
        ])
        capped = self.store.search(meta, "needle", context=0, max_matches=2)
        self.assertEqual(capped.split("\n"), [
            ">L10: line 010 alpha needle here", "---", ">L12: line 012 alpha needle here",
            "…[1 more matches beyond max_matches=2; narrow the pattern]",
        ])
        loud = self.store.search(meta, "needle", context=0, flags=re.IGNORECASE)
        self.assertIn(">L30: line 030 alpha NEEDLE loud", loud)
        self.assertNotIn("L30", self.store.search(meta, "needle", context=0))
        # a hit on the 2000-char line is clipped the same way
        z = self.store.search(meta, "^Z{10}", context=0)
        self.assertIn("…[+1488 chars]…", z)
        self.assertTrue(z.startswith(">L50: " + "Z" * so.LINE_CLIP_PREFIX))
        # budget
        tight = self.store.search(meta, "alpha", context=0, max_bytes=60)
        self.assertIn("budget reached at L", tight)
        self.assertEqual(self.store.search(meta, "unicorn"), "[stdout: no match for 'unicorn' in 100 lines]")
        with self.assertRaises(ValueError):
            self.store.search(meta, "(")

    def test_tail(self):
        meta = self.spill(err=b"")
        self.assertEqual(self.store.tail(meta, lines=3),
                         "[stdout L98-100 of 100]\nline 098 alpha\nline 099 alpha\nline 100 alpha")
        self.assertEqual(self.store.tail(meta, "stderr"), "[stderr: empty; 0 lines]")
        clipped = self.store.tail(meta, lines=10, max_bytes=50)
        self.assertTrue(clipped.startswith("…[50 B budget: showing the last 3 of 10 requested lines]\n[stdout L98-100 of 100]\n"))
        self.assertTrue(clipped.endswith("line 100 alpha"))
        # the newest line always survives, even when it alone is over budget
        big = self.spill(out=b"a\n" + b"Q" * 2000 + b"\n")
        self.assertIn("…[+1488 chars]…", self.store.tail(big, lines=5, max_bytes=100))

    def test_delete_and_list(self):
        a = self.spill()
        b = self.spill(session="other-session")
        c = self.spill(project=self.other_project)
        mine = self.store.list(project_path=self.project, session_id=self.session)
        self.assertEqual([m.id for m in mine], [a.id])
        self.assertEqual([m.id for m in self.store.list(project_path=self.project, session_id="other-session")], [b.id])
        self.assertEqual([m.id for m in self.store.list(project_path=self.other_project, session_id=self.session)], [c.id])
        self.assertEqual(self.store.list(project_path=self.other_project, session_id="nope"), [])
        _rewrite_meta(b, expires_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=1)))
        self.assertEqual(self.store.list(project_path=self.project, session_id="other-session"), [])
        self.store.delete(a)
        for suffix in ("stdout", "stderr", "meta.json"):
            self.assertFalse((Path(a.dir) / f"{a.id}.{suffix}").exists())
        self.assertFalse(Path(a.dir).exists(), "empty session dir pruned")
        self.assertTrue(Path(b.dir).exists())
        self.store.delete(a)                             # idempotent
        with self.assertRaises(so.OutputAccessError):
            self.resolve(a.id)


class TestSweep(_StoreCase):
    def _size(self, meta: so.OutputMeta) -> int:
        return sum((Path(meta.dir) / f"{meta.id}.{s}").stat().st_size for s in ("stdout", "stderr", "meta.json"))

    def test_expired_then_oldest_first_and_foreign_files_untouched(self):
        now = datetime.now(timezone.utc)
        a = self.spill(out=b"a" * 1000)
        b = self.spill(out=b"b" * 1000)
        c = self.spill(out=b"c" * 1000)
        d = self.spill(out=b"d" * 1000)
        _rewrite_meta(a, created_at=_iso(now - timedelta(days=4)), expires_at=_iso(now - timedelta(days=1)))
        _rewrite_meta(b, created_at=_iso(now - timedelta(hours=3)))
        _rewrite_meta(c, created_at=_iso(now - timedelta(hours=2)))
        _rewrite_meta(d, created_at=_iso(now - timedelta(hours=1)))
        sizes = {m.id: self._size(m) for m in (a, b, c, d)}
        session_dir = Path(a.dir)
        foreign = [self.root / "README.txt", session_dir / "notes.txt", session_dir / "foo.stdout",
                   self.root / "other" / "o-abcdefabcdef.stdout", self.root / "zz" / "x.meta.json"]
        for f in foreign:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("keep me", encoding="utf-8")
        orphan = session_dir / "o-111111111111.stdout"
        orphan.write_bytes(b"orphan" * 10)
        old = time.time() - 2 * 86400
        os.utime(orphan, (old, old))
        spool = self.store.spool_dir()
        stale = spool / "o-222222222222.stdout.part"
        stale.write_bytes(b"stale")
        os.utime(stale, (old, old))
        fresh = spool / "o-333333333333.stderr.part"
        fresh.write_bytes(b"fresh")

        with patch.object(so, "RETENTION_BYTES", sizes[c.id] + sizes[d.id]):
            result = self.store.sweep(now=now)
        self.assertEqual(result["deleted"], 4)          # a (expired), b (evicted), orphan, stale part
        self.assertEqual(result["freed_bytes"], sizes[a.id] + sizes[b.id] + 60 + 5)
        for gone in (a, b):
            self.assertFalse(gone.meta_path.exists())
            self.assertFalse(gone.stream_path("stdout").exists())
        for kept in (c, d):
            self.assertTrue(kept.meta_path.exists())
            self.assertEqual(kept.stream_path("stdout").stat().st_size, 1000)
        self.assertFalse(orphan.exists())
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        for f in foreign:
            self.assertTrue(f.exists(), f)
        self.assertEqual((self.root / so.SWEEP_MARKER).read_text(encoding="utf-8"), _iso(now))
        # a float `now` works too, and a no-op sweep reports zeros
        self.assertEqual(self.store.sweep(now=time.time()), {"deleted": 0, "freed_bytes": 0})

    def test_corrupt_meta_of_ours_is_swept(self):
        meta = self.spill()
        meta.meta_path.write_text("{", encoding="utf-8")
        result = self.store.sweep()
        self.assertEqual(result["deleted"], 1)
        self.assertFalse(meta.stream_path("stdout").exists())

    def test_ten_minute_marker_gates_sweep_on_promote(self):
        expired = self.spill()
        _rewrite_meta(expired, expires_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=1)))
        marker = self.root / so.SWEEP_MARKER
        self.assertTrue(marker.exists())
        now = time.time()
        os.utime(marker, (now, now))
        self.spill()
        self.assertTrue(expired.meta_path.exists(), "a sweep ran inside the 10-minute window")
        os.utime(marker, (now - so.SWEEP_INTERVAL_S - 100, now - so.SWEEP_INTERVAL_S - 100))
        self.spill()
        self.assertFalse(expired.meta_path.exists(), "no sweep ran after the window elapsed")
        self.assertGreater(marker.stat().st_mtime, now - 5)
        marker.unlink()
        self.spill()
        self.assertTrue(marker.exists(), "a missing marker means sweep now")


class TestUnicodePaths(_StoreCase):
    def test_end_to_end_with_spaces_and_unicode(self):
        project = str(Path(self._tmp.name) / "Мои проекты" / "c3 v2 (α)")
        session = "sess ión/β 1"
        body = "первая строка ✓\nsecond line with needle 🧵\nthird\n".encode("utf-8")
        cap = _capture_bytes(self.store.spool_dir(), body, "ошибка\n".encode("utf-8"), rc=1)
        meta = self.store.promote(cap, project_path=project, session_id=session, cmd="echo ✓",
                                  cwd=project + "/тест dir", guard_paths=[project + "/тест dir"],
                                  exit_code=1, timed_out=False, duration_ms=7)
        self.assertRegex(Path(meta.dir).name, r"^s-[0-9a-f]{12}$")
        got = self.store.resolve(meta.id, project_path=project, session_id=session, guard_check=_ALLOW)
        self.assertEqual(got.cmd_display, "echo ✓")
        self.assertEqual(got.stdout["lines"], 3)
        self.assertEqual(got.stdout["longest_line"], len("second line with needle 🧵"))
        self.assertEqual(self.store.read(got, lines=(1, 1)), "[stdout L1-1 of 3]\nпервая строка ✓")
        self.assertEqual(self.store.search(got, "needle"),
                         " L1: первая строка ✓\n>L2: second line with needle 🧵\n L3: third")
        self.assertEqual(self.store.tail(got, "stderr", lines=1), "[stderr L1-1 of 1]\nошибка")
        self.assertEqual([m.id for m in self.store.list(project_path=project, session_id=session)], [meta.id])
        with self.assertRaises(so.OutputAccessError):
            self.store.resolve(meta.id, project_path=project, session_id="sess ión/β 2", guard_check=_ALLOW)
        self.store.delete(got)
        self.assertEqual(self.store.list(project_path=project, session_id=session), [])


class TestAtomicWrite(unittest.TestCase):
    """`_write_json_atomic` — the job store's publish step.

    Both cases below fail against the pre-2.118.1 version, which used a single
    shared `<name>.tmp` and attempted `os.replace` exactly once. That is what
    crashed a supervisor on Windows CI with WinError 5 and reported "failed
    before running" for a command that was fine.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "job.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_temp_name_is_per_process(self):
        """Two writers must not collide on the temp file itself."""
        seen = []
        real = os.replace

        def spy(src, dst):
            seen.append(Path(src).name)
            return real(src, dst)

        with patch.object(so.os, "replace", spy):
            so._write_json_atomic(self.path, {"a": 1})
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], "job.json.tmp",
                            "shared temp name — concurrent writers will race")
        self.assertTrue(seen[0].startswith("job.json.tmp"))
        self.assertIn(str(os.getpid()), seen[0])
        # …and unique per WRITE, not just per process: this store is written
        # from a threaded server, and threads share a pid.
        seen.clear()
        with patch.object(so.os, "replace", spy):
            so._write_json_atomic(self.path, {"a": 2})
            so._write_json_atomic(self.path, {"a": 3})
        self.assertEqual(len(set(seen)), 2, f"temp name reused: {seen}")

    def test_transient_permission_error_is_retried(self):
        calls = {"n": 0}
        real = os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:          # two transient failures, then success
                raise PermissionError(5, "Access is denied")
            return real(src, dst)

        with patch.object(so.os, "replace", flaky):
            so._write_json_atomic(self.path, {"ok": True})
        self.assertEqual(calls["n"], 3)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"ok": True})

    def test_a_persistent_permission_error_still_raises(self):
        """Retrying must not swallow a real, non-transient failure."""
        def always(src, dst):
            raise PermissionError(5, "Access is denied")

        with patch.object(so.os, "replace", always):
            with self.assertRaises(PermissionError):
                so._write_json_atomic(self.path, {"a": 1})

    def test_no_temp_file_is_left_behind_when_replace_fails(self):
        def always(src, dst):
            raise PermissionError(5, "Access is denied")

        with patch.object(so.os, "replace", always):
            with self.assertRaises(PermissionError):
                so._write_json_atomic(self.path, {"a": 1})
        leftovers = list(self.path.parent.glob("job.json.tmp*"))
        self.assertEqual(leftovers, [], f"orphaned temp files: {leftovers}")

    def test_concurrent_writers_all_publish_valid_json(self):
        """The end-to-end property: N threads hammering one path never leave
        the target truncated or half-written."""
        import threading
        errors = []

        def writer(i):
            try:
                for _ in range(20):
                    so._write_json_atomic(self.path, {"writer": i})
            except Exception as exc:      # noqa: BLE001 — surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertIn("writer", json.loads(self.path.read_text(encoding="utf-8")))
        self.assertEqual(list(self.path.parent.glob("job.json.tmp*")), [])


if __name__ == "__main__":
    unittest.main()
