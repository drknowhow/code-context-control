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

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._payload


def _ok(json_obj):
    return _Resp(json.dumps(json_obj).encode("utf-8"))


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
            captured["method"] = req.get_method()
            return _ok({"version": "8.5.0", "displayName": "Bitbucket"})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = self.client.application_properties()

        self.assertEqual(res["version"], "8.5.0")
        self.assertEqual(captured["auth"], "Bearer t0k3n")
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

    def test_get_pr_diff_returns_text(self):
        diff_bytes = b"diff --git a/x b/x\n+y\n"

        def fake_urlopen(req, timeout=None, context=None):
            return _Resp(diff_bytes)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            text = self.client.get_pr_diff("PROJ", "repo", 1)
        self.assertIn("+y", text)

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


if __name__ == "__main__":
    unittest.main()
