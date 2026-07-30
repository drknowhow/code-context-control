"""Tests for services/jira_client.py + jira_cloud.py + jira_data_center.py.

All transport goes through ``services.jira_client.urllib.request.urlopen`` —
patched here with canned responses (payload shapes pinned from Atlassian's
documented examples), so no live Jira instance is ever needed.
"""
from __future__ import annotations

import base64
import email.message
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import jira_client
from services.jira_client import JiraClient, JiraError
from services.jira_cloud import adf_from_text, text_from_adf

_URLOPEN = "services.jira_client.urllib.request.urlopen"
_SLEEP = "services.jira_client.time.sleep"


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


def _ok(json_obj) -> _Resp:
    return _Resp(json.dumps(json_obj).encode("utf-8"))


def _http_error(code: int, body: dict | None = None,
                headers: dict | None = None) -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    payload = json.dumps(body or {}).encode("utf-8")
    return urllib.error.HTTPError("https://x", code, "err", hdrs, io.BytesIO(payload))


def _cloud() -> JiraClient:
    return JiraClient(
        "https://x.atlassian.net/", "a@x.com", "tok", deployment="cloud"
    )


def _dc() -> JiraClient:
    return JiraClient(
        "https://jira.corp.example", "alice", "pat", deployment="data_center"
    )


_ISSUE = {
    "key": "PROJ-1",
    "id": "10001",
    "fields": {
        "summary": "Fix the flux capacitor",
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "issuetype": {"name": "Bug"},
        "priority": {"name": "High"},
        "assignee": {"displayName": "Alice", "accountId": "acc-1"},
        "reporter": {"displayName": "Bob"},
        "project": {"key": "PROJ"},
        "created": "2026-07-01T10:00:00.000+0000",
        "updated": "2026-07-02T10:00:00.000+0000",
        "labels": ["infra"],
    },
}


class TestJiraClientConstruction(unittest.TestCase):
    def test_required_args_validated(self):
        with self.assertRaises(ValueError):
            JiraClient("", "u", "t", deployment="cloud")
        with self.assertRaises(ValueError):
            JiraClient("https://x", "u", "", deployment="cloud")
        with self.assertRaises(ValueError):
            JiraClient("https://x", "u", "t", deployment="server")

    def test_trailing_slash_stripped(self):
        self.assertEqual(_cloud().base_url, "https://x.atlassian.net")


class TestAuthHeaders(unittest.TestCase):
    def test_cloud_auth_is_basic_email_token(self):
        with mock.patch(_URLOPEN, return_value=_ok({})) as m:
            _cloud().myself()
        req = m.call_args[0][0]
        auth = req.get_header("Authorization")
        self.assertTrue(auth.startswith("Basic "))
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
        self.assertEqual(decoded, "a@x.com:tok")
        self.assertIn("/rest/api/3/myself", req.get_full_url())

    def test_dc_auth_is_bearer_pat(self):
        with mock.patch(_URLOPEN, return_value=_ok({})) as m:
            _dc().myself()
        req = m.call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), "Bearer pat")
        self.assertIn("/rest/api/2/myself", req.get_full_url())

    def test_user_agent_reflects_package(self):
        with mock.patch(_URLOPEN, return_value=_ok({})) as m:
            _cloud().server_info()
        req = m.call_args[0][0]
        self.assertIn("c3-jira/", req.get_header("User-agent"))


class TestSearchPagination(unittest.TestCase):
    def test_cloud_search_uses_v3_jql_and_next_token(self):
        page = {"issues": [_ISSUE], "nextPageToken": "tok-2"}
        with mock.patch(_URLOPEN, return_value=_ok(page)) as m:
            result = _cloud().search("assignee = currentUser()")
        url = m.call_args[0][0].get_full_url()
        self.assertIn("/rest/api/3/search/jql?", url)
        self.assertNotIn("nextPageToken", url)
        self.assertEqual(result["next_cursor"], "tok-2")
        dto = result["issues"][0]
        self.assertEqual(dto["key"], "PROJ-1")
        self.assertEqual(dto["status_category"], "indeterminate")
        self.assertEqual(dto["assignee_id"], "acc-1")

    def test_cloud_search_cursor_passthrough(self):
        with mock.patch(_URLOPEN, return_value=_ok({"issues": []})) as m:
            result = _cloud().search("project = PROJ", cursor="tok-2")
        self.assertIn("nextPageToken=tok-2", m.call_args[0][0].get_full_url())
        self.assertEqual(result["next_cursor"], "")

    def test_dc_search_offset_cursor_round_trip(self):
        first = {"issues": [_ISSUE, _ISSUE], "startAt": 0, "total": 3}
        with mock.patch(_URLOPEN, return_value=_ok(first)) as m:
            result = _dc().search("project = PROJ")
        self.assertIn("/rest/api/2/search?", m.call_args[0][0].get_full_url())
        self.assertIn("startAt=0", m.call_args[0][0].get_full_url())
        self.assertEqual(result["next_cursor"], "2")

        second = {"issues": [_ISSUE], "startAt": 2, "total": 3}
        with mock.patch(_URLOPEN, return_value=_ok(second)) as m:
            result = _dc().search("project = PROJ", cursor="2")
        self.assertIn("startAt=2", m.call_args[0][0].get_full_url())
        self.assertEqual(result["next_cursor"], "")


class TestBodyFormats(unittest.TestCase):
    def _sent_body(self, m) -> dict:
        return json.loads(m.call_args[0][0].data.decode("utf-8"))

    def test_cloud_create_issue_wraps_description_in_adf(self):
        with mock.patch(_URLOPEN, return_value=_ok({"key": "PROJ-2"})) as m:
            _cloud().create_issue("PROJ", "Bug", "Boom", description="a\nb")
        fields = self._sent_body(m)["fields"]
        self.assertEqual(fields["description"]["type"], "doc")
        self.assertEqual(fields["description"]["version"], 1)
        self.assertEqual(fields["project"], {"key": "PROJ"})

    def test_dc_create_issue_keeps_plain_description(self):
        with mock.patch(_URLOPEN, return_value=_ok({"key": "PROJ-2"})) as m:
            _dc().create_issue("PROJ", "Bug", "Boom", description="plain text")
        self.assertEqual(self._sent_body(m)["fields"]["description"], "plain text")

    def test_cloud_comment_wraps_text_in_adf(self):
        raw = {"id": "1", "author": {"displayName": "A"}, "created": "", "body": {}}
        with mock.patch(_URLOPEN, return_value=_ok(raw)) as m:
            _cloud().add_comment("PROJ-1", "hello")
        self.assertEqual(self._sent_body(m)["body"]["type"], "doc")

    def test_cloud_comment_adf_escape_hatch_passes_through(self):
        doc = {"type": "doc", "version": 1, "content": [{"type": "rule"}]}
        raw = {"id": "1", "author": {}, "created": "", "body": doc}
        with mock.patch(_URLOPEN, return_value=_ok(raw)) as m:
            _cloud().add_comment("PROJ-1", doc, body_format="adf")
        self.assertEqual(self._sent_body(m)["body"], doc)

    def test_dc_comment_plain_string(self):
        raw = {"id": "1", "author": {}, "created": "", "body": "hello"}
        with mock.patch(_URLOPEN, return_value=_ok(raw)) as m:
            _dc().add_comment("PROJ-1", "hello")
        self.assertEqual(self._sent_body(m)["body"], "hello")

    def test_adf_round_trip(self):
        self.assertEqual(text_from_adf(adf_from_text("line1\nline2")), "line1\nline2")

    def test_text_from_adf_tolerates_strings_and_none(self):
        self.assertEqual(text_from_adf("already text"), "already text")
        self.assertEqual(text_from_adf(None), "")


class TestRetryPolicy(unittest.TestCase):
    def test_read_429_retried_once_honoring_retry_after(self):
        responses = [
            _http_error(429, headers={"Retry-After": "2"}),
            _ok({"issues": []}),
        ]
        with mock.patch(_URLOPEN, side_effect=responses) as m, \
                mock.patch(_SLEEP) as slept:
            result = _cloud().search("project = PROJ")
        self.assertEqual(m.call_count, 2)
        slept.assert_called_once_with(2.0)
        self.assertEqual(result["issues"], [])

    def test_read_429_gives_up_after_one_retry(self):
        responses = [
            _http_error(429, headers={"Retry-After": "1"}),
            _http_error(429, headers={"Retry-After": "1"}),
        ]
        with mock.patch(_URLOPEN, side_effect=responses), mock.patch(_SLEEP):
            with self.assertRaises(JiraError) as ctx:
                _cloud().search("project = PROJ")
        self.assertEqual(ctx.exception.status, 429)

    def test_mutation_429_never_retried(self):
        with mock.patch(_URLOPEN, side_effect=[_http_error(429)]) as m, \
                mock.patch(_SLEEP) as slept:
            with self.assertRaises(JiraError):
                _cloud().create_issue("PROJ", "Bug", "Boom")
        self.assertEqual(m.call_count, 1)
        slept.assert_not_called()


class TestErrorTranslation(unittest.TestCase):
    def test_http_error_surfaces_jira_messages(self):
        err = _http_error(404, body={"errorMessages": ["Issue does not exist"]})
        with mock.patch(_URLOPEN, side_effect=[err]):
            with self.assertRaises(JiraError) as ctx:
                _cloud().get_issue("PROJ-404")
        self.assertEqual(ctx.exception.status, 404)
        self.assertIn("Issue does not exist", str(ctx.exception))

    def test_http_error_surfaces_field_errors(self):
        err = _http_error(400, body={"errors": {"summary": "Summary is required"}})
        with mock.patch(_URLOPEN, side_effect=[err]):
            with self.assertRaises(JiraError) as ctx:
                _cloud().create_issue("PROJ", "Bug", "x")
        self.assertIn("summary: Summary is required", str(ctx.exception))

    def test_url_error_translated(self):
        with mock.patch(_URLOPEN, side_effect=urllib.error.URLError("boom")):
            with self.assertRaises(JiraError) as ctx:
                _cloud().myself()
        self.assertEqual(ctx.exception.status, 0)


class TestActions(unittest.TestCase):
    def test_assign_cloud_uses_account_id(self):
        with mock.patch(_URLOPEN, return_value=_ok({})) as m:
            _cloud().assign_issue("PROJ-1", "acc-42")
        req = m.call_args[0][0]
        self.assertEqual(req.get_method(), "PUT")
        self.assertEqual(json.loads(req.data.decode()), {"accountId": "acc-42"})

    def test_assign_dc_uses_name(self):
        with mock.patch(_URLOPEN, return_value=_ok({})) as m:
            _dc().assign_issue("PROJ-1", "alice")
        self.assertEqual(json.loads(m.call_args[0][0].data.decode()), {"name": "alice"})

    def test_list_transitions_normalized(self):
        raw = {"transitions": [
            {"id": "31", "name": "Done", "to": {"name": "Done"}},
            {"id": "21", "name": "Start", "to": {"name": "In Progress"}},
        ]}
        with mock.patch(_URLOPEN, return_value=_ok(raw)):
            transitions = _cloud().list_transitions("PROJ-1")
        self.assertEqual(
            transitions[0], {"id": "31", "name": "Done", "to_status": "Done"}
        )

    def test_get_issue_flattens_adf_description_and_comments(self):
        issue = json.loads(json.dumps(_ISSUE))
        issue["fields"]["description"] = adf_from_text("desc text")
        issue["fields"]["comment"] = {"comments": [{
            "id": "9", "author": {"displayName": "Carol"},
            "created": "2026-07-03", "body": adf_from_text("first comment"),
        }]}
        with mock.patch(_URLOPEN, return_value=_ok(issue)):
            dto = _cloud().get_issue("PROJ-1")
        self.assertEqual(dto["description"], "desc text")
        self.assertEqual(dto["comments"][0]["body"], "first comment")
        self.assertEqual(dto["comments"][0]["author"], "Carol")

    def test_transition_with_comment_cloud(self):
        with mock.patch(_URLOPEN, return_value=_ok({})) as m:
            _cloud().transition_issue("PROJ-1", "31", comment="done!")
        body = json.loads(m.call_args[0][0].data.decode())
        self.assertEqual(body["transition"], {"id": "31"})
        added = body["update"]["comment"][0]["add"]["body"]
        self.assertEqual(added["type"], "doc")


class TestDataCenterCreateMeta(unittest.TestCase):
    """Jira DC 9.0 split createmeta in two; 11.x removed the original, which
    404s as 'Issue Does Not Exist' on every project."""

    _TYPES = {"values": [{"id": "10004", "name": "Task"},
                         {"id": "10005", "name": "Bug"}]}
    _FIELDS = {"isLast": True, "total": 2, "values": [
        {"fieldId": "summary", "name": "Summary", "required": True,
         "schema": {"type": "string"}},
        {"fieldId": "customfield_1", "name": "Squad", "required": False,
         "schema": {"type": "option"}},
    ]}

    def test_uses_split_endpoints_first(self):
        with mock.patch(_URLOPEN, side_effect=[_ok(self._TYPES),
                                               _ok(self._FIELDS)]) as m:
            meta = _dc().get_create_metadata("RNDA", "Task")
        urls = [c[0][0].get_full_url() for c in m.call_args_list]
        self.assertIn("/issue/createmeta/RNDA/issuetypes", urls[0])
        self.assertIn("/issue/createmeta/RNDA/issuetypes/10004", urls[1])
        self.assertEqual([f["id"] for f in meta["required_fields"]], ["summary"])
        self.assertEqual(meta["optional_fields"], ["Squad"])

    def test_falls_back_to_legacy_on_404(self):
        legacy = {"projects": [{"issuetypes": [{"fields": {
            "summary": {"name": "Summary", "required": True,
                        "schema": {"type": "string"}},
            "labels": {"name": "Labels", "required": False,
                       "schema": {"type": "array"}},
        }}]}]}
        with mock.patch(_URLOPEN, side_effect=[_http_error(404),
                                               _ok(legacy)]) as m:
            meta = _dc().get_create_metadata("RNDA", "Task")
        self.assertIn("projectKeys=RNDA", m.call_args_list[1][0][0].get_full_url())
        self.assertEqual([f["id"] for f in meta["required_fields"]], ["summary"])

    def test_both_endpoints_404_raises_explanatory_error(self):
        with mock.patch(_URLOPEN, side_effect=[_http_error(404),
                                               _http_error(404)]):
            with self.assertRaises(JiraError) as ctx:
                _dc().get_create_metadata("NOPE", "Task")
        self.assertEqual(ctx.exception.status, 404)
        self.assertIn("removed on Jira 9.0+", str(ctx.exception))

    def test_non_404_is_not_masked_by_fallback(self):
        with mock.patch(_URLOPEN, side_effect=[_http_error(401)]):
            with self.assertRaises(JiraError) as ctx:
                _dc().get_create_metadata("RNDA", "Task")
        self.assertEqual(ctx.exception.status, 401)

    def test_unknown_issue_type_lists_known_ones(self):
        with mock.patch(_URLOPEN, return_value=_ok(self._TYPES)):
            meta = _dc().get_create_metadata("RNDA", "Epic")
        self.assertIn("Task, Bug", meta["error"])

    def test_fields_pagination_is_drained(self):
        page1 = {"total": 3, "values": [
            {"fieldId": "a", "name": "A", "required": True},
            {"fieldId": "b", "name": "B", "required": True},
        ]}
        page2 = {"total": 3, "isLast": True, "values": [
            {"fieldId": "c", "name": "C", "required": True},
        ]}
        with mock.patch(_URLOPEN, side_effect=[_ok(self._TYPES), _ok(page1),
                                               _ok(page2)]) as m:
            meta = _dc().get_create_metadata("RNDA", "Task")
        self.assertEqual([f["id"] for f in meta["required_fields"]],
                         ["a", "b", "c"])
        self.assertIn("startAt=2", m.call_args_list[2][0][0].get_full_url())

    def test_dict_keyed_fields_on_split_route_still_parse(self):
        odd = {"fields": {"summary": {"name": "Summary", "required": True,
                                      "schema": {"type": "string"}}}}
        with mock.patch(_URLOPEN, side_effect=[_ok(self._TYPES), _ok(odd)]):
            meta = _dc().get_create_metadata("RNDA", "Task")
        self.assertEqual([f["id"] for f in meta["required_fields"]], ["summary"])


if __name__ == "__main__":
    unittest.main()
