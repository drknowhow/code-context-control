"""Tests for services/bitbucket_client.py.

Mocks ``urllib.request.urlopen`` with a context-manager stub so we can
exercise the request/response paths without touching the network.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.bitbucket_client import (
    BitbucketDataCenterClient,
    BitbucketError,
)


class _Resp:
    """Tiny stand-in for the urllib response context manager."""

    def __init__(self, payload: bytes, headers: dict | None = None):
        self._payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._payload


def _ok(json_obj, headers: dict | None = None):
    return _Resp(json.dumps(json_obj).encode("utf-8"), headers=headers)


class TestBitbucketClient(unittest.TestCase):
    def setUp(self):
        self.client = BitbucketDataCenterClient(
            base_url="https://bb.example.com/", token="t0k3n"
        )

    def test_strips_trailing_slash(self):
        self.assertEqual(self.client.base_url, "https://bb.example.com")

    def test_required_args_validated(self):
        with self.assertRaises(ValueError):
            BitbucketDataCenterClient(base_url="", token="t")
        with self.assertRaises(ValueError):
            BitbucketDataCenterClient(base_url="https://x", token="")

    def test_application_properties_uses_bearer_auth(self):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            captured["accept"] = req.get_header("Accept")
            captured["method"] = req.get_method()
            return _ok({"version": "8.5.0", "displayName": "Bitbucket"})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = self.client.application_properties()

        self.assertEqual(res["version"], "8.5.0")
        self.assertEqual(captured["auth"], "Bearer t0k3n")
        self.assertEqual(captured["accept"], "application/json")
        self.assertEqual(captured["method"], "GET")
        self.assertIn("/rest/api/1.0/application-properties", captured["url"])

    def test_list_pull_requests_includes_state_and_paging(self):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["url"] = req.full_url
            return _ok({
                "values": [{"id": 7, "title": "test", "state": "OPEN"}],
                "isLastPage": True,
            })

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            prs = self.client.list_pull_requests("PROJ", "repo", state="OPEN")

        self.assertEqual(prs[0]["id"], 7)
        self.assertIn("state=OPEN", captured["url"])
        self.assertIn("limit=50", captured["url"])

    def test_paged_iterates_until_last_page(self):
        pages = [
            {"values": [{"id": 1}], "isLastPage": False, "nextPageStart": 1},
            {"values": [{"id": 2}, {"id": 3}], "isLastPage": True},
        ]
        idx = {"i": 0}

        def fake_urlopen(req, timeout=None, context=None):
            i = idx["i"]
            idx["i"] += 1
            return _ok(pages[i])

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            repos = self.client.list_repos("PROJ")

        self.assertEqual([r["id"] for r in repos], [1, 2, 3])

    def test_create_pull_request_posts_json(self):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["data"] = req.data
            captured["method"] = req.get_method()
            captured["ctype"] = req.get_header("Content-type")
            return _ok({"id": 99, "title": "hi"})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = self.client.create_pull_request(
                "PROJ", "repo",
                title="hi", from_branch="feat", to_branch="main",
                description="d", reviewers=["alice"],
            )

        self.assertEqual(res["id"], 99)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["ctype"], "application/json")
        body = json.loads(captured["data"])
        self.assertEqual(body["title"], "hi")
        self.assertEqual(body["fromRef"]["id"], "refs/heads/feat")
        self.assertEqual(body["toRef"]["id"], "refs/heads/main")
        self.assertEqual(body["reviewers"], [{"user": {"name": "alice"}}])

    def test_merge_pr_passes_version(self):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["url"] = req.full_url
            return _ok({"state": "MERGED"})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = self.client.merge_pr("PROJ", "repo", 5, version=4, message="ship it")
        self.assertEqual(res["state"], "MERGED")
        self.assertIn("version=4", captured["url"])

    def test_get_pr_diff_requests_text_plain(self):
        diff_bytes = b"diff --git a/x b/x\n+y\n"
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["accept"] = req.get_header("Accept")
            captured["url"] = req.full_url
            return _Resp(diff_bytes)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            text = self.client.get_pr_diff("PROJ", "repo", 1)
        self.assertIn("+y", text)
        # Issue 1: the diff must be content-negotiated as text/plain, not JSON.
        self.assertEqual(captured["accept"], "text/plain")
        self.assertIn("/pull-requests/1/diff", captured["url"])

    def test_http_error_translated_to_bitbucket_error(self):
        import urllib.error

        body = json.dumps({"errors": [{"message": "permission denied"}]}).encode("utf-8")

        class _ErrResp(io.BytesIO):
            def read(self, n=-1):
                return super().read(n) if n >= 0 else self.getvalue()

        def fake_urlopen(req, timeout=None, context=None):
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {}, _ErrResp(body)
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(BitbucketError) as cm:
                self.client.application_properties()
        self.assertEqual(cm.exception.status, 403)
        self.assertIn("permission denied", str(cm.exception))

    def test_url_error_translated_to_bitbucket_error(self):
        import urllib.error

        def fake_urlopen(req, timeout=None, context=None):
            raise urllib.error.URLError("name resolution failed")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(BitbucketError) as cm:
                self.client.application_properties()
        self.assertEqual(cm.exception.status, 0)
        self.assertIn("Transport failure", str(cm.exception))

    def test_decline_pr_carries_version_query(self):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["url"] = req.full_url
            return _ok({"state": "DECLINED"})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.decline_pr("PROJ", "repo", 3, version=2)
        self.assertIn("version=2", captured["url"])
        self.assertIn("/decline", captured["url"])

    def test_whoami_resolves_via_xausername_header(self):
        # Issue 2: Data Center has no /users/me; whoami must read X-AUSERNAME
        # from an authenticated response and enrich via /users/{slug}.
        calls = []

        def fake_urlopen(req, timeout=None, context=None):
            calls.append(req.full_url)
            if req.full_url.endswith("/application-properties"):
                return _Resp(
                    json.dumps({"version": "9.4.0"}).encode("utf-8"),
                    headers={"X-AUSERNAME": "jdoe"},
                )
            if "/users/" in req.full_url:
                return _ok(
                    {
                        "name": "jdoe",
                        "displayName": "Jane Doe",
                        "emailAddress": "jdoe@example.com",
                    }
                )
            return _ok({})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            user = self.client.whoami()

        self.assertEqual(user.get("displayName"), "Jane Doe")
        self.assertTrue(any(u.endswith("/application-properties") for u in calls))
        self.assertTrue(any("/users/jdoe" in u for u in calls))
        self.assertFalse(any("/users/me" in u for u in calls))

    def test_whoami_without_header_returns_empty(self):
        def fake_urlopen(req, timeout=None, context=None):
            return _ok({"version": "9.4.0"})  # no X-AUSERNAME header

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(self.client.whoami(), {})

    def test_user_agent_reflects_package_version(self):
        # Issue 5: User-Agent must not be the stale hardcoded 2.30.0 literal.
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["ua"] = req.get_header("User-agent")
            return _ok({"version": "9.4.0"})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.application_properties()
        self.assertTrue(captured["ua"].startswith("c3-bitbucket/"))
        self.assertNotEqual(captured["ua"], "c3-bitbucket/2.30.0")

    def test_update_pull_request_puts_full_state(self):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _ok({"id": 42, "version": 8, "title": "New title"})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.update_pull_request(
                "PROJ", "repo", 42,
                version=7, title="New title", description="Body",
                to_branch="develop", reviewers=["bob"],
            )

        self.assertEqual(captured["method"], "PUT")
        self.assertIn("/pull-requests/42", captured["url"])
        body = captured["body"]
        self.assertEqual(body["version"], 7)
        self.assertEqual(body["title"], "New title")
        self.assertEqual(body["toRef"]["id"], "refs/heads/develop")
        self.assertEqual(body["reviewers"], [{"user": {"name": "bob"}}])

    def test_update_pull_request_omits_absent_toref_and_reviewers(self):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _ok({"id": 42})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.update_pull_request(
                "PROJ", "repo", 42, version=7, title="T", reviewers=None
            )
        self.assertNotIn("toRef", captured["body"])
        self.assertNotIn("reviewers", captured["body"])

    def test_get_pr_commits_pages_the_commits_endpoint(self):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["url"] = req.full_url
            return _ok({"values": [{"id": "abc", "displayId": "abc"}],
                        "isLastPage": True})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            commits = self.client.get_pr_commits("PROJ", "repo", 42)
        self.assertEqual(len(commits), 1)
        self.assertIn("/pull-requests/42/commits", captured["url"])

    def test_update_pr_comment_puts_text_and_version(self):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _ok({"id": 17, "version": 4})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.update_pr_comment(
                "PROJ", "repo", 42, 17, version=3, text="edited"
            )
        self.assertEqual(captured["method"], "PUT")
        self.assertIn("/pull-requests/42/comments/17", captured["url"])
        self.assertEqual(captured["body"], {"text": "edited", "version": 3})

    def test_delete_pr_comment_sends_version_param(self):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            return _ok({})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.delete_pr_comment("PROJ", "repo", 42, 17, version=3)
        self.assertEqual(captured["method"], "DELETE")
        self.assertIn("/comments/17", captured["url"])
        self.assertIn("version=3", captured["url"])

    def test_create_pr_task_posts_blocker_comment(self):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _ok({"id": 92})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.create_pr_task("PROJ", "repo", 42, text="Fix it")
        self.assertIn("/pull-requests/42/comments", captured["url"])
        self.assertEqual(captured["body"],
                         {"text": "Fix it", "severity": "BLOCKER"})

    def test_list_pr_tasks_uses_blocker_comments_endpoint(self):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["url"] = req.full_url
            return _ok({"values": [{"id": 91, "state": "OPEN"}],
                        "isLastPage": True})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            tasks = self.client.list_pr_tasks("PROJ", "repo", 42, state="OPEN")
        self.assertEqual(len(tasks), 1)
        self.assertIn("/pull-requests/42/blocker-comments", captured["url"])
        self.assertIn("state=OPEN", captured["url"])

    def test_set_pr_task_state_puts_state_and_version(self):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _ok({"id": 91, "state": "RESOLVED"})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.set_pr_task_state(
                "PROJ", "repo", 42, 91, version=3, state="RESOLVED"
            )
        self.assertEqual(captured["body"], {"state": "RESOLVED", "version": 3})

    def test_set_pr_reviewer_status_puts_participant(self):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _ok({"status": "NEEDS_WORK"})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.set_pr_reviewer_status(
                "PROJ", "repo", 42, user_slug="jane doe", status="NEEDS_WORK"
            )

        self.assertEqual(captured["method"], "PUT")
        self.assertIn("/pull-requests/42/participants/jane%20doe", captured["url"])
        self.assertEqual(captured["body"], {"status": "NEEDS_WORK"})


if __name__ == "__main__":
    unittest.main()
