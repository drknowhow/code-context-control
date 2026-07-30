"""Oracle single-use session bootstrap (#31).

Plain ``GET /`` no longer issues the dashboard session cookie: a local process
running as a *different* OS user could otherwise fetch the page and obtain
one. The cookie is now exchanged for a single-use, short-lived code minted by
the server and handed to the same-OS-user CLI via an owner-only key file.

These pin: no cookie without a code, codes are genuinely single-use and
expiring, the mint endpoint is not itself gated behind the cookie it issues
(the chicken-and-egg), and the redeem path does not leave the code in the URL.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from oracle.services import local_session as ls  # noqa: E402


class TestBootstrapCodes(unittest.TestCase):
    def setUp(self):
        ls._codes.clear()

    def test_code_is_single_use(self):
        code = ls.mint_code()
        self.assertTrue(ls.consume_code(code))
        self.assertFalse(ls.consume_code(code), "a code must not be replayable")

    def test_unknown_code_rejected(self):
        ls.mint_code()
        self.assertFalse(ls.consume_code("not-a-real-code"))

    def test_empty_code_rejected(self):
        self.assertFalse(ls.consume_code(""))
        self.assertFalse(ls.consume_code(None))

    def test_codes_are_distinct(self):
        self.assertNotEqual(ls.mint_code(), ls.mint_code())

    def test_expired_code_rejected(self):
        code = ls.mint_code()
        ls._codes[code] = 0.0  # force expiry
        self.assertFalse(ls.consume_code(code))

    def test_outstanding_codes_are_capped(self):
        for _ in range(ls._MAX_OUTSTANDING_CODES + 10):
            ls.mint_code()
        self.assertLessEqual(len(ls._codes), ls._MAX_OUTSTANDING_CODES)

    def test_consuming_one_code_leaves_others_valid(self):
        a, b = ls.mint_code(), ls.mint_code()
        self.assertTrue(ls.consume_code(a))
        self.assertTrue(ls.consume_code(b))


class TestBootstrapKeyFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read_roundtrip(self):
        ls.write_bootstrap_key(self.dir)
        self.assertTrue(ls.verify_bootstrap_key(ls.read_bootstrap_key(self.dir)))

    def test_missing_key_file_reads_empty(self):
        self.assertEqual(ls.read_bootstrap_key(self.dir), "")

    def test_wrong_key_rejected(self):
        self.assertFalse(ls.verify_bootstrap_key("nope"))
        self.assertFalse(ls.verify_bootstrap_key(""))
        self.assertFalse(ls.verify_bootstrap_key(None))

    def test_key_is_not_the_cookie_secret(self):
        """Possessing the mint key must not be the same as holding a session."""
        ls.write_bootstrap_key(self.dir)
        self.assertFalse(ls.verify(ls.read_bootstrap_key(self.dir)))


class TestServerRoutes(unittest.TestCase):
    """End-to-end through the real Flask app."""

    @classmethod
    def setUpClass(cls):
        from oracle import oracle_server
        cls.mod = oracle_server
        oracle_server.app.config["TESTING"] = True
        cls.client = oracle_server.app.test_client()

    def setUp(self):
        ls._codes.clear()
        self.client.delete_cookie(ls.COOKIE_NAME)

    def _cookie(self, resp):
        return "\n".join(resp.headers.getlist("Set-Cookie"))

    def test_plain_get_root_issues_no_cookie(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(ls.COOKIE_NAME, self._cookie(resp))

    def test_valid_code_sets_cookie_and_redirects_clean(self):
        code = ls.mint_code()
        resp = self.client.get(f"/?{ls.BOOTSTRAP_PARAM}={code}")
        self.assertEqual(resp.status_code, 302)
        self.assertIn(ls.COOKIE_NAME, self._cookie(resp))
        self.assertNotIn(code, resp.headers.get("Location", ""))

    def test_replayed_code_does_not_set_cookie(self):
        code = ls.mint_code()
        self.client.get(f"/?{ls.BOOTSTRAP_PARAM}={code}")
        self.client.delete_cookie(ls.COOKIE_NAME)
        resp = self.client.get(f"/?{ls.BOOTSTRAP_PARAM}={code}")
        self.assertNotIn(ls.COOKIE_NAME, self._cookie(resp))

    def test_bogus_code_does_not_set_cookie(self):
        resp = self.client.get(f"/?{ls.BOOTSTRAP_PARAM}=garbage")
        self.assertNotIn(ls.COOKIE_NAME, self._cookie(resp))

    def test_mint_requires_authorization(self):
        resp = self.client.post("/api/session/bootstrap", json={"key": "wrong"})
        self.assertEqual(resp.status_code, 401)

    def test_mint_with_key_returns_usable_url(self):
        resp = self.client.post("/api/session/bootstrap",
                                json={"key": ls._BOOTSTRAP_KEY})
        self.assertEqual(resp.status_code, 200)
        url = resp.get_json()["url"]
        self.assertIn(f"{ls.BOOTSTRAP_PARAM}=", url)
        code = url.split(f"{ls.BOOTSTRAP_PARAM}=", 1)[1]
        redeemed = self.client.get(f"/?{ls.BOOTSTRAP_PARAM}={code}")
        self.assertIn(ls.COOKIE_NAME, self._cookie(redeemed))

    def test_mint_is_exempt_from_the_cookie_it_issues(self):
        """The chicken-and-egg: the write gate must not guard the mint."""
        resp = self.client.post("/api/session/bootstrap",
                                json={"key": ls._BOOTSTRAP_KEY})
        self.assertNotEqual(resp.status_code, 401,
                            "mint must not require the session cookie")

    def test_other_mutating_api_still_requires_a_session(self):
        resp = self.client.post("/api/apikey/rotate")
        self.assertEqual(resp.status_code, 401)

    def test_cookie_from_bootstrap_unlocks_mutating_api(self):
        code = ls.mint_code()
        self.client.get(f"/?{ls.BOOTSTRAP_PARAM}={code}")  # client keeps cookie
        resp = self.client.post("/api/apikey/rotate")
        self.assertNotEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
