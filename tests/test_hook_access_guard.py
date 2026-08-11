"""T2b tests — the access-guard pretool sub-hook + fail-closed dispatch.

Proves: native tool verdicts fire BEFORE unlock logic (sticky-unlock
immunity), the dispatcher denies write-class tools when the guard itself
breaks, explicit-path search denial vs rootless advisory, the best-effort
shell scan, Bash matcher registration, and canonical-key parity between the
unlock map and the evaluator.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

WIN = os.name == "nt"

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli import _hook_utils  # noqa: E402

sys.modules.setdefault("_hook_utils", _hook_utils)

import cli.hook_access_guard as hag  # noqa: E402
import cli.hook_dispatch as hd  # noqa: E402
from services import access_guard as ag  # noqa: E402


class HookGuardBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3").mkdir()
        self._write_access({"deny": ["secrets/**"], "read_only": ["docs/**"]})
        (self.proj / "secrets").mkdir()
        (self.proj / "secrets" / "key.txt").write_text("k", encoding="utf-8")
        _hook_utils.drain_state_warnings()

    def tearDown(self):
        _hook_utils.drain_state_warnings()
        self._tmp.cleanup()

    def _write_access(self, section):
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps({"access": section}), encoding="utf-8")

    def _run(self, tool, tool_input):
        return hag.run({"tool_name": tool, "tool_input": tool_input},
                       project_path=self.proj)

    def _reason(self, out):
        return (out or {}).get("hookSpecificOutput", {}).get(
            "permissionDecisionReason", "")


class TestNativeVerdicts(HookGuardBase):
    def test_write_tools_denied_on_deny_rule(self):
        for tool in ("Edit", "Write", "MultiEdit"):
            out = self._run(tool, {"file_path": str(self.proj / "secrets/key.txt")})
            self.assertIn("[c3-access:denied]", self._reason(out), tool)

    def test_notebook_edit_uses_notebook_path(self):
        out = self._run("NotebookEdit",
                        {"notebook_path": str(self.proj / "secrets/nb.ipynb")})
        self.assertIn("[c3-access:denied]", self._reason(out))

    def test_read_denied_on_deny_rule(self):
        out = self._run("Read", {"file_path": str(self.proj / "secrets/key.txt")})
        self.assertIn("[c3-access:denied]", self._reason(out))

    def test_read_only_rule_allows_read_denies_write(self):
        (self.proj / "docs").mkdir()
        target = str(self.proj / "docs" / "a.md")
        self.assertIsNone(self._run("Read", {"file_path": target}))
        out = self._run("Edit", {"file_path": target})
        self.assertIn("[c3-access:read_only]", self._reason(out))

    def test_clean_file_passes(self):
        self.assertIsNone(self._run("Edit",
                                    {"file_path": str(self.proj / "src.py")}))


class TestSearchTools(HookGuardBase):
    def test_explicit_denied_root_hard_denied(self):
        out = self._run("Grep", {"pattern": "x",
                                 "path": str(self.proj / "secrets")})
        self.assertIn("[c3-access:denied]", self._reason(out))

    def test_rootless_search_gets_advisory_footer(self):
        out = self._run("Grep", {"pattern": "x"})
        self.assertIsNotNone(out)
        self.assertIn("[c3-access:limited]", out.get("additionalContext", ""))
        self.assertNotIn("hookSpecificOutput", out)


class TestShellScan(HookGuardBase):
    def test_existing_denied_path_in_command_denied(self):
        target = str(self.proj / "secrets" / "key.txt")
        out = self._run("Bash", {"command": f"cat {target}"})
        self.assertIn("[c3-access:denied]", self._reason(out))
        self.assertIn("best-effort shell scan", self._reason(out))

    def test_regex_lookalike_token_not_denied(self):
        # '\.env' is a grep pattern, not a file — existence gate must pass it.
        out = self._run("Bash", {"command": r'grep -r "\.env" docs/'})
        self.assertIsNone(out)

    def test_benign_command_passes(self):
        self.assertIsNone(self._run("Bash", {"command": "git status"}))


class TestDispatchFailClosed(HookGuardBase):
    def _dispatch(self, tool, tool_input):
        return hd.dispatch("pretool",
                           {"tool_name": tool, "tool_input": tool_input},
                           project_path=self.proj)

    def test_import_failure_denies_write_class(self):
        with mock.patch.object(hd, "_load_run",
                               side_effect=lambda m: (None, "ImportError: x")):
            out = self._dispatch("Edit", {"file_path": str(self.proj / "a.py")})
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("[c3-access:error]", reason)
        self.assertIn("fail-closed", reason)

    def test_runtime_exception_denies_write_class(self):
        real_load = hd._load_run

        def loader(mod):
            if mod == "hook_access_guard":
                return (lambda p, pp=None: 1 / 0), ""
            return real_load(mod)

        with mock.patch.object(hd, "_load_run", side_effect=loader):
            out = self._dispatch("Write", {"file_path": str(self.proj / "a.py")})
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("[c3-access:error]",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_read_class_fails_open_with_warning(self):
        with mock.patch.object(hd, "_load_run",
                               side_effect=lambda m: (None, "ImportError: x")):
            out = self._dispatch("Read", {"file_path": str(self.proj / "a.py")})
        self.assertNotIn("hookSpecificOutput", out or {})
        self.assertIn("[c3:hook-error]", (out or {}).get("additionalContext", ""))


class TestStickyUnlockImmunity(HookGuardBase):
    def test_unlocked_denied_file_still_denied(self):
        target = str(self.proj / "secrets" / "key.txt")
        _hook_utils.record_unlocked_files(
            [target], {"edit", "read"}, session_id="s1",
            project_path=self.proj)
        out = hd.dispatch(
            "pretool",
            {"tool_name": "Edit", "session_id": "s1",
             "tool_input": {"file_path": target}},
            project_path=self.proj)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("[c3-access:denied]", reason)
        self.assertNotIn("[c3:enforce]", reason)


class TestCanonicalParity(unittest.TestCase):
    def test_unlock_key_matches_evaluator_canon(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "A File.TXT"
            f.write_text("x", encoding="utf-8")
            self.assertEqual(_hook_utils.canonical_key(f),
                             ag.canonicalize(str(f), tmp)[0])


class TestShellScanNetworkTokens(HookGuardBase):
    """#50 — the ADS spelling check is exempt from existence-gating, so a URL
    or IPv6 literal in a command's TEXT used to hard-deny on Windows."""

    _ALLOWED = (
        'git commit -m "block the fc00::/7 range"',
        "echo see https://example.com",
        "curl https://api.github.com/rate_limit",
        'gh pr create --body "docs at http://x.io/a"',
        "ping 2001:db8::1",
        "curl http://[::1]:8080/health",
        "pip install git+https://github.com/o/r.git",
    )

    def test_urls_and_ipv6_do_not_deny(self):
        for cmd in self._ALLOWED:
            with self.subTest(cmd=cmd):
                denial, tok = hag._scan_shell(cmd, str(self.proj))
                self.assertIsNone(denial, f"{cmd!r} denied on token {tok!r}")

    @unittest.skipUnless(WIN, "NTFS ADS semantics")
    def test_real_ads_spelling_still_denies(self):
        for tok in ("./notes.txt:$DATA", "./a/b.txt:hidden"):
            with self.subTest(tok=tok):
                denial, _ = hag._scan_shell(f"type {tok}", str(self.proj))
                self.assertIsNotNone(denial)
                self.assertEqual(denial.rule, "<ads>")

    def test_denied_path_still_caught_alongside_a_url(self):
        denial, tok = hag._scan_shell(
            "curl https://example.com -o ./secrets/key.txt", str(self.proj)
        )
        self.assertIsNotNone(denial)
        self.assertEqual(tok, "./secrets/key.txt")

    def test_classifier_rejects_ads_and_plain_paths(self):
        for tok in ("C:/x/f.txt:stream", "./f.txt:$DATA", "c:/temp", "./a/b"):
            self.assertFalse(hag._is_network_token(tok), tok)
        for tok in ("https://x.com", "fc00::/7", "2001:db8::1", "[::1]"):
            self.assertTrue(hag._is_network_token(tok), tok)


class TestShellScanSyntaxTokens(HookGuardBase):
    """#50 follow-up — the first fix whitelisted two SHAPES, not the class.

    ``_is_network_token`` matched a scheme only at position 0, plus a bare IPv6.
    So a URL that was not the whole token still hard-denied, and syntax carrying
    no URL at all was never covered at all. Every command below names no file on
    disk; each one was a hard deny on v2.74.0, observed in a real session.
    """

    _ALLOWED = (
        # a URL inside markdown link syntax — the scheme is not at position 0
        'echo "see [#1](https://example.com/a/b)"',
        'gh pr create --body "fixes [#7](https://github.com/o/r/pull/7)"',
        # jq filters: '/' in the text, ':' in the object syntax, no URL at all
        """echo '{a:.x,b:("/"+.y)}'""",
        'gh pr view 1 --json a -q \'{repo:(.owner.login+"/"+.name)}\'',
        # a python expression naming paths, which is itself not a path
        """python -c "print(['src/a.py','src/b.py'])\"""",
        """python -c "p=pathlib.Path('.claude/x.json')\"""",
        # brace expansion
        "cp {src/a,src/b}/x.txt .",
        # 2026-08-11, third iteration of this class and the one the
        # _SYNTAX_CHARS generalisation could not reach: a single-quoted printf
        # payload that is escape sequences plus a URL. After the tokenizer
        # strips the quotes it carries a separator (`\` and `/`), NO syntax
        # character at all, no `::`, and no scheme at position 0 — so every
        # existing gate passed it through to a residual-colon `<ads>` deny on
        # a command whose only file argument is an ordinary markdown path.
        r"printf '\n---\n\nhttps://claude.ai/code/session_01Hs28\n' >> body.md",
        # The same shape without the escapes, and with the URL last.
        'echo see-https://example.com/a/b >> notes.md',
        # A scheme mid-token inside a quoted commit message.
        'git commit -m "closes https://github.com/o/r/issues/9"',
    )

    def test_syntax_tokens_do_not_deny(self):
        for cmd in self._ALLOWED:
            with self.subTest(cmd=cmd):
                denial, tok = hag._scan_shell(cmd, str(self.proj))
                self.assertIsNone(denial, f"{cmd!r} denied on token {tok!r}")

    def test_a_plain_denied_path_is_still_caught(self):
        """The half that matters: loosening the gate must not blind the scan."""
        for cmd, want in (
            ("cat ./secrets/key.txt", "./secrets/key.txt"),
            ("cp ./secrets/key.txt /tmp/x", "./secrets/key.txt"),
        ):
            with self.subTest(cmd=cmd):
                denial, tok = hag._scan_shell(cmd, str(self.proj))
                self.assertIsNotNone(denial, cmd)
                self.assertEqual(tok, want)

    def test_a_quoted_denied_path_is_still_caught(self):
        """Quotes are stripped at the ends, so quoting is not an escape hatch."""
        denial, _ = hag._scan_shell('cat "./secrets/key.txt"', str(self.proj))
        self.assertIsNotNone(denial)

    @unittest.skipUnless(WIN, "NTFS ADS semantics")
    def test_a_real_ads_spelling_is_still_caught(self):
        """The rule keeps its teeth on a token that IS a path spelling."""
        denial, _ = hag._scan_shell(
            "type ./secrets/key.txt:hidden", str(self.proj))
        self.assertIsNotNone(denial)
        self.assertEqual(denial.rule, "<ads>")

    def test_the_scheme_search_does_not_swallow_a_stream_spelling(self):
        """The cost of unanchoring, priced.

        ``search`` means a token is excused for containing `://` ANYWHERE, so
        the question is whether a real ADS spelling can carry one. It cannot:
        NTFS spells a stream ``file:name:$TYPE`` and no spelling of it
        contains ``//``. Pinned so a future 'tighten the regex' change has to
        argue with a test rather than with a comment."""
        for tok in ("./secrets/key.txt:hidden", "C:/x/f.txt:stream",
                    "notes.txt::$DATA", "file:name:$DATA"):
            with self.subTest(tok=tok):
                self.assertFalse(hag._is_network_token(tok), tok)
        for tok in ("https://example.com/a", "[#1](https://example.com/a)",
                    r"\n---\n\nhttps://claude.ai/code/x",
                    "see-git+ssh://host/repo.git"):
            with self.subTest(tok=tok):
                self.assertTrue(hag._is_network_token(tok), tok)

    def test_a_pytest_node_id_is_not_a_stream_spelling(self):
        """`a/b.py::Thing` — one of two path-SHAPED idioms #73 did not cover."""
        for cmd in (
            "python -m pytest tests/test_chat_poll.py::TestAbort",
            "pytest tests/a.py::TestX::test_y -q",
        ):
            with self.subTest(cmd=cmd):
                denial, tok = hag._scan_shell(cmd, str(self.proj))
                self.assertIsNone(denial, f"{cmd!r} denied on {tok!r}")

    @unittest.skipUnless(WIN, "NTFS ADS semantics")
    def test_the_doubled_colon_default_stream_form_still_denies(self):
        """`file::$DATA` is real ADS syntax, so `::` alone cannot be a pass.

        This is the control that stops the node-id fix from being a hole. An
        earlier draft skipped every `::` token and would have opened it.
        """
        # Deliberately a path NO deny glob covers, so `<ads>` is the only rule
        # that can catch it. Pointing this at `secrets/**` would have "passed"
        # on the glob and proven nothing about the spelling check.
        denial, _ = hag._scan_shell("type ./notes.txt::$DATA", str(self.proj))
        self.assertIsNotNone(denial)
        self.assertEqual(denial.rule, "<ads>")

    def test_a_git_revspec_is_judged_on_its_path_half(self):
        """`git show <rev>:<path>` — the second idiom, and a tightening.

        Every revspec is denied today by accident: the residual colon trips
        `<ads>`. Whitelisting the shape would let `git show HEAD:.env` print a
        denied file, so the path half is what gets checked.
        """
        denial, _ = hag._scan_shell(
            "git show origin/main:pyproject.toml", str(self.proj))
        self.assertIsNone(denial)

        denial, tok = hag._scan_shell(
            "git show HEAD:secrets/key.txt", str(self.proj))
        self.assertIsNotNone(denial, "a revspec naming a denied path must deny")
        self.assertEqual(tok, "secrets/key.txt")

    def test_a_revspec_denies_a_path_that_only_exists_in_history(self):
        """The gap the rewrite would otherwise open.

        `git show HEAD~5:secrets/gone.txt` reads a denied file whether or not it
        is in the working tree, so the existence gate — right for an ordinary
        token — is the wrong question here.
        """
        denial, tok = hag._scan_shell(
            "git show HEAD~5:secrets/gone.txt", str(self.proj))
        self.assertIsNotNone(denial)
        self.assertEqual(tok, "secrets/gone.txt")

    def test_a_git_command_is_recognised_after_a_shell_separator(self):
        """`cd /repo && git show <rev>:<path>` — the shape people actually type.

        The first cut anchored "is this git?" to the start of the whole command
        string, so a leading `cd` disabled the rewrite entirely. Every unit test
        passed, because a test author writes the command as `git show …`. A live
        probe after release is what caught it.
        """
        for cmd in (
            "cd /tmp/repo && git show origin/main:pyproject.toml",
            "git fetch -q; git show origin/main:pyproject.toml",
            "cd x || git show HEAD:setup.py",
        ):
            with self.subTest(cmd=cmd):
                denial, tok = hag._scan_shell(cmd, str(self.proj))
                self.assertIsNone(denial, f"{cmd!r} denied on {tok!r}")

    def test_a_denied_revspec_is_still_caught_after_a_separator(self):
        denial, tok = hag._scan_shell(
            f'cd "{self.proj}" && git show HEAD:secrets/key.txt', str(self.proj))
        self.assertIsNotNone(denial)
        self.assertEqual(tok, "secrets/key.txt")

    def test_a_revspec_path_without_a_separator_now_reaches_the_rules(self):
        """`HEAD:.env` — no slash, so the token gate used to drop it (#82).

        It also never reached the ADS check, which is the claim #81's body got
        wrong. It reaches the rules now, and the revspec bypass of the existence
        gate is what makes the verdict right: a blob in history is denied
        whether or not that file is in the working tree today.
        """
        denial, tok = hag._scan_shell(
            "git show HEAD:.env", str(self.proj))
        self.assertIsNotNone(denial)
        self.assertEqual(tok, ".env")


class TestShellScanFollowsCd(HookGuardBase):
    """#82 — the scan judged paths against the session root, not the cwd.

    `cd <elsewhere> && cat .env` computed the denial correctly and then threw it
    away, because the existence gate looked for `.env` under the project root
    where it does not live. The rule was never wrong; the question was asked
    about the wrong directory.
    """

    def test_a_denied_file_read_after_cd_is_caught(self):
        """The reported case, reproduced with the rule that made it reachable.

        Session rooted in one directory, command `cd`-ing to another, and a
        builtin `**/.env*` deny — builtin because it binds on any path, which is
        why the live failure was a secret file rather than a project-scoped one.
        Before this fix the denial was computed and then discarded, because the
        existence gate looked under the session root.
        """
        (self.proj / ".env").write_text("SECRET=x", encoding="utf-8")
        denial, _ = hag._scan_shell(
            f'cd "{self.proj}" && cat .env', str(self.proj.parent))
        self.assertIsNotNone(denial)
        self.assertEqual(denial.rule, "**/.env*")

    def test_cd_out_then_relative_back_in_is_caught(self):
        denial, _ = hag._scan_shell(
            f'cd "{self.proj.parent}" && cat {self.proj.name}/secrets/key.txt',
            str(self.proj))
        self.assertIsNotNone(denial)

    def test_cd_out_then_absolute_back_in_is_caught(self):
        denial, _ = hag._scan_shell(
            f'cd "{self.proj.parent}" && cat "{self.proj / "secrets/key.txt"}"',
            str(self.proj))
        self.assertIsNotNone(denial)

    def test_the_existence_gate_still_applies_at_the_new_cwd(self):
        """Following the shell must not mean flagging files that are not there.

        A `cd` into a directory without the file leaves nothing to read, and a
        deny on nothing is the false positive this scanner keeps being fixed for.
        """
        denial, _ = hag._scan_shell(
            f'cd "{self.proj.parent}" && cat secrets/key.txt', str(self.proj))
        self.assertIsNone(denial)

    def test_cd_moves_only_the_segments_after_it(self):
        """`cat secrets/key.txt && cd /elsewhere` reads before it moves."""
        denial, _ = hag._scan_shell(
            "cat secrets/key.txt && cd /tmp", str(self.proj))
        self.assertIsNotNone(denial)

    def test_a_quoted_cd_target_with_spaces_is_parsed(self):
        spaced = self.proj / "a dir"
        spaced.mkdir()
        denial, _ = hag._scan_shell(
            f'cd "{spaced}" && cat ../secrets/key.txt', str(self.proj))
        self.assertIsNotNone(denial)

    def test_the_cd_target_itself_is_still_scanned(self):
        """`cd` into a denied directory is itself a token worth refusing."""
        denial, tok = hag._scan_shell(
            f'cd "{self.proj / "secrets"}"', str(self.proj))
        self.assertIsNotNone(denial)

    def test_a_bare_filename_is_still_out_of_scope(self):
        """Stated rather than implied: the token gate is unchanged.

        `cd <denied dir> && type key.txt` is caught only incidentally, by the
        `cd` argument. A separator-free filename still never reaches the rules,
        which is a known limit of the token gate and not what #82 was about.
        """
        self.assertIsNone(hag._cd_target("type key.txt", str(self.proj)))
        denial, tok = hag._scan_shell(
            f'cd "{self.proj / "secrets"}" && type key.txt', str(self.proj))
        self.assertIsNotNone(denial)
        self.assertNotIn("key.txt", tok)

    def test_a_git_segment_does_not_license_rewrites_in_other_segments(self):
        """Per-segment, not per-string: `cat a:b && git status` is two commands.

        Attributing the whole string to git would let a non-git token be
        reinterpreted — the scan would check `hidden` instead of the real
        spelling, which is a hole rather than a false positive.
        """
        self.assertIsNone(
            hag._git_revspec_path("cat ./notes.txt:hidden", "./notes.txt:hidden"))

    @unittest.skipUnless(WIN, "NTFS ADS semantics")
    def test_a_non_git_segment_beside_a_git_one_still_denies(self):
        """The end-to-end half of the above, and Windows-only for a reason.

        `<ads>` is an NTFS spelling rule, so on POSIX this command denies
        nothing and the assertion would be about the platform rather than the
        code. Shipping it undecorated turned six of nine CI jobs red — the
        matrix caught in a minute what a Windows-only local run could not.
        """
        denial, _ = hag._scan_shell(
            "cat ./secrets/key.txt:hidden && git status", str(self.proj))
        self.assertIsNotNone(denial)

    def test_revspec_rewriting_only_applies_to_git(self):
        """`<word>:<word>` means something else in every other command."""
        path = hag._git_revspec_path("cat HEAD:secrets/key.txt", "HEAD:secrets/key.txt")
        self.assertIsNone(path)
        path = hag._git_revspec_path("git show HEAD:a.py", "HEAD:a.py")
        self.assertEqual(path, "a.py")

    def test_a_drive_letter_is_not_a_revspec_separator(self):
        self.assertIsNone(
            hag._git_revspec_path("git show C:/x/f.txt", "C:/x/f.txt"))

    def test_a_stream_type_suffix_is_not_treated_as_a_path(self):
        """`git show notes.txt:$DATA` must not be rewritten into `$DATA`."""
        self.assertIsNone(
            hag._git_revspec_path("git show notes.txt:$DATA", "notes.txt:$DATA"))

    def test_the_classifier_splits_paths_from_syntax(self):
        for tok in ("./a/b", "C:/x/f.txt", "./f.txt:stream", "src/a.py",
                    r"\\server\share\x"):
            self.assertTrue(hag._looks_like_a_path(tok), tok)
        for tok in ("[#1](https://x.com/a)", '{a:.x,b:("/"+.y)}',
                    "['src/a.py','src/b.py']", "{src/a,src/b}/x.txt"):
            self.assertFalse(hag._looks_like_a_path(tok), tok)


class TestSyntheticRulesDiscoverable(unittest.TestCase):
    """#50 — a refusal cites a rule name; `c3 access list` must show it."""

    def test_ads_is_listed(self):
        names = [n for n, _ in ag.SYNTHETIC_RULES]
        self.assertIn("<ads>", names)

    def test_every_synthetic_denial_is_documented(self):
        source = (REPO_ROOT / "services" / "access_guard.py").read_text(
            encoding="utf-8"
        )
        raised = set(re.findall(r'Denial\("(<[a-z0-9.\-]+>)"', source))
        self.assertTrue(raised)
        self.assertEqual(raised - {n for n, _ in ag.SYNTHETIC_RULES}, set())

    def test_access_list_prints_them(self):
        source = (REPO_ROOT / "cli" / "c3.py").read_text(encoding="utf-8")
        self.assertIn("SYNTHETIC_RULES", source)


class TestInstallMatchers(unittest.TestCase):
    def test_pre_matchers_include_shell(self):
        source = (REPO_ROOT / "cli" / "c3.py").read_text(encoding="utf-8")
        block = source.split("_pre_matcher_names = [", 1)[1].split("]", 1)[0]
        self.assertIn("shell_matcher", block)
        self.assertIn("run_shell_command", block)


if __name__ == "__main__":
    unittest.main()
