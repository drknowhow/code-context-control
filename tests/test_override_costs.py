"""services/override_costs — the "this rule is costing you" fold (§11 T5).

Pure aggregation over a temp request store with a FIXED clock, so every
window edge is exact. The load-bearing assertions:

- the window is `[now - days, now]` on `created_at`, both edges inclusive;
- `withdrawn` is in `count` and in no bucket; a pending row past its
  `expires_at` counts as expired against the supplied clock, on disk or not;
- the three suggestion strings are exact (the desktop prints them);
- the order is count desc, then last_at desc;
- a missing, corrupt or non-list store is an empty list, never a raise —
  this feeds a route and a tray, and a nudge that crashes is worse than no
  nudge.
"""
from __future__ import annotations

import atexit
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import override_costs as oc  # noqa: E402
from services import override_requests as orq  # noqa: E402

NOW = datetime(2026, 9, 6, 21, 0, 0, tzinfo=timezone.utc)

# Real directories, so canonicalisation behaves the same on every OS (a
# drive-letter literal is relative on posix and absolute on Windows).
_MODULE_TMP = tempfile.TemporaryDirectory()
atexit.register(_MODULE_TMP.cleanup)
PROJ_A = str(Path(_MODULE_TMP.name) / "CardMaker").replace("\\", "/")
PROJ_B = str(Path(_MODULE_TMP.name) / "c3-mobile").replace("\\", "/")
for _d in (PROJ_A, PROJ_B):
    Path(_d).mkdir(parents=True, exist_ok=True)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _row(*, project=PROJ_A, rule="**/.claude/skills/**", status="approved",
         age=timedelta(hours=1), ttl=timedelta(minutes=10), n=0,
         rule_class="access_confirm", layer="access") -> dict:
    created = NOW - age
    return {
        "id": f"ovr_{n:06x}", "project_path": project, "session_id": "s1",
        "created_at": _iso(created), "expires_at": _iso(created + ttl),
        "status": status, "layer": layer, "rule": rule,
        "rule_class": rule_class, "scope": "project", "tool": "Edit",
        "op": "write", "path": f"{project}/x{n}.md", "path_key": "",
        "refusal": "", "justification": "", "resolved_at": None,
        "decided_by": None, "decision_note": None,
    }


class CostsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name) / "override_requests.json"
        self._patch = mock.patch.object(orq, "store_path",
                                        return_value=self.store)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def write(self, rows):
        self.store.write_text(json.dumps(rows), encoding="utf-8")

    def costs(self, **kw):
        kw.setdefault("now", NOW)
        return oc.rule_costs(**kw)


class TestShape(CostsBase):
    def test_fields_exactly_in_order(self):
        self.write([_row(n=1)])
        rows = self.costs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0].keys()), oc.FIELDS)
        self.assertEqual(oc.FIELDS, (
            "project_path", "rule", "rule_class", "layer",
            "count", "approved", "denied", "expired", "pending",
            "last_at", "suggestion"))

    def test_labels_come_from_the_rows(self):
        self.write([_row(n=1, rule_class="access_deny", layer="access")])
        row = self.costs()[0]
        self.assertEqual(row["project_path"], PROJ_A)
        self.assertEqual(row["rule"], "**/.claude/skills/**")
        self.assertEqual(row["rule_class"], "access_deny")
        self.assertEqual(row["layer"], "access")

    def test_last_at_is_the_newest_created_at_verbatim(self):
        newest = _row(n=1, age=timedelta(minutes=5))
        self.write([_row(n=2, age=timedelta(hours=3)), newest,
                    _row(n=3, age=timedelta(hours=1))])
        self.assertEqual(self.costs()[0]["last_at"], newest["created_at"])


class TestWindow(CostsBase):
    def test_default_window_is_seven_days(self):
        self.write([_row(n=1, age=timedelta(days=6, hours=23)),
                    _row(n=2, age=timedelta(days=7, hours=1))])
        rows = self.costs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 1)

    def test_edges_are_inclusive(self):
        self.write([_row(n=1, age=timedelta(days=7)),      # exactly now-7d
                    _row(n=2, age=timedelta(0))])          # exactly now
        self.assertEqual(self.costs()[0]["count"], 2)

    def test_days_widens_the_window(self):
        self.write([_row(n=1, age=timedelta(days=20))])
        self.assertEqual(self.costs(), [])
        self.assertEqual(self.costs(days=30)[0]["count"], 1)

    def test_rows_after_now_are_outside_the_window(self):
        self.write([_row(n=1, age=timedelta(hours=-2))])
        self.assertEqual(self.costs(), [])

    def test_days_floor_is_one(self):
        self.write([_row(n=1, age=timedelta(hours=20))])
        self.assertEqual(self.costs(days=0)[0]["count"], 1)
        self.assertEqual(self.costs(days=-5)[0]["count"], 1)
        self.assertEqual(self.costs(days="junk")[0]["count"], 1)

    def test_unparseable_created_at_is_skipped(self):
        bad = _row(n=1)
        bad["created_at"] = "not a date"
        self.write([bad, _row(n=2)])
        self.assertEqual(self.costs()[0]["count"], 1)

    def test_now_defaults_to_the_wall_clock(self):
        live = _row(n=1)
        live["created_at"] = (datetime.now(timezone.utc)
                              - timedelta(minutes=1)).isoformat()
        self.write([live])
        self.assertEqual(oc.rule_costs()[0]["count"], 1)


class TestBuckets(CostsBase):
    def test_each_status_lands_in_its_bucket(self):
        self.write([
            _row(n=1, status="approved"), _row(n=2, status="approved"),
            _row(n=3, status="denied"),
            _row(n=4, status="expired"),
            _row(n=5, status="pending", age=timedelta(minutes=2)),
        ])
        row = self.costs()[0]
        self.assertEqual((row["count"], row["approved"], row["denied"],
                          row["expired"], row["pending"]), (5, 2, 1, 1, 1))

    def test_withdrawn_is_counted_but_in_no_bucket(self):
        self.write([_row(n=1, status="withdrawn"), _row(n=2, status="approved")])
        row = self.costs()[0]
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["approved"] + row["denied"] + row["expired"]
                         + row["pending"], 1)

    def test_lapsed_pending_counts_as_expired_without_touching_the_store(self):
        # Status on disk still says pending; the TTL ran out an hour ago.
        self.write([_row(n=1, status="pending", age=timedelta(hours=2),
                         ttl=timedelta(minutes=10))])
        before = self.store.read_text(encoding="utf-8")
        row = self.costs()[0]
        self.assertEqual((row["pending"], row["expired"]), (0, 1))
        self.assertEqual(self.store.read_text(encoding="utf-8"), before,
                         "the cost fold must never write the store")

    def test_pending_with_no_expires_at_counts_as_expired(self):
        r = _row(n=1, status="pending", age=timedelta(minutes=1))
        r["expires_at"] = None
        self.write([r])
        row = self.costs()[0]
        self.assertEqual((row["pending"], row["expired"]), (0, 1))

    def test_unknown_status_counts_but_buckets_nowhere(self):
        self.write([_row(n=1, status="weird")])
        row = self.costs()[0]
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["approved"] + row["denied"] + row["expired"]
                         + row["pending"], 0)


class TestSuggestion(CostsBase):
    def test_strings_are_the_contract(self):
        self.assertEqual(oc.SUGGEST_ALLOW, "convert to allow")
        self.assertEqual(oc.SUGGEST_TIGHTEN, "tighten or deny")
        self.assertEqual(oc.SUGGEST_REVIEW, "review")

    def test_three_approvals_and_no_denial_is_convert_to_allow(self):
        self.assertEqual(oc.suggestion(3, 0), "convert to allow")
        self.assertEqual(oc.suggestion(7, 0), "convert to allow")

    def test_two_denials_is_tighten_or_deny(self):
        self.assertEqual(oc.suggestion(0, 2), "tighten or deny")
        self.assertEqual(oc.suggestion(5, 2), "tighten or deny")

    def test_everything_else_is_review(self):
        self.assertEqual(oc.suggestion(0, 0), "review")
        self.assertEqual(oc.suggestion(2, 0), "review")
        self.assertEqual(oc.suggestion(3, 1), "review")
        self.assertEqual(oc.suggestion(0, 1), "review")

    def test_suggestion_rides_on_the_row(self):
        self.write([_row(n=i, status="approved") for i in range(3)]
                   + [_row(n=9, rule="secrets/**", status="denied"),
                      _row(n=10, rule="secrets/**", status="denied")])
        by_rule = {r["rule"]: r for r in self.costs()}
        self.assertEqual(by_rule["**/.claude/skills/**"]["suggestion"],
                         "convert to allow")
        self.assertEqual(by_rule["secrets/**"]["suggestion"], "tighten or deny")


class TestGroupingAndOrder(CostsBase):
    def test_grouped_by_project_and_rule(self):
        self.write([
            _row(n=1, project=PROJ_A, rule="a/**"),
            _row(n=2, project=PROJ_A, rule="a/**"),
            _row(n=3, project=PROJ_A, rule="b/**"),
            _row(n=4, project=PROJ_B, rule="a/**"),
        ])
        keys = {(r["project_path"], r["rule"]): r["count"] for r in self.costs()}
        self.assertEqual(keys, {(PROJ_A, "a/**"): 2, (PROJ_A, "b/**"): 1,
                                (PROJ_B, "a/**"): 1})

    def test_project_spelling_is_canonicalised_for_grouping(self):
        # Same directory, two spellings — one group, not two.
        self.write([_row(n=1, project="Y:/Projects/CardMaker"),
                    _row(n=2, project="y:\\projects\\cardmaker")])
        rows = self.costs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 2)

    def test_sorted_by_count_desc_then_last_at_desc(self):
        self.write([
            _row(n=1, rule="one", age=timedelta(minutes=1)),
            _row(n=2, rule="two", age=timedelta(hours=5)),
            _row(n=3, rule="two", age=timedelta(hours=4)),
            _row(n=4, rule="three", age=timedelta(minutes=30)),
            _row(n=5, rule="three", age=timedelta(minutes=20)),
            _row(n=6, rule="three", age=timedelta(minutes=10)),
            _row(n=7, rule="four", age=timedelta(hours=2)),
            _row(n=8, rule="four", age=timedelta(minutes=5)),
        ])
        self.assertEqual([r["rule"] for r in self.costs()],
                         ["three", "four", "two", "one"])

    def test_project_filter_any_spelling(self):
        self.write([_row(n=1, project=PROJ_A), _row(n=2, project=PROJ_B)])
        spellings = [PROJ_A, PROJ_A.upper()]
        if os.name == "nt":
            spellings.append(PROJ_A.replace("/", "\\"))
        for spelling in spellings:
            rows = self.costs(project_path=spelling)
            self.assertEqual([r["project_path"] for r in rows], [PROJ_A],
                             spelling)

    def test_project_filter_unknown_is_empty(self):
        self.write([_row(n=1, project=PROJ_A)])
        self.assertEqual(self.costs(project_path="Q:/nowhere/at/all"), [])

    def test_empty_project_path_means_everything(self):
        self.write([_row(n=1, project=PROJ_A), _row(n=2, project=PROJ_B)])
        self.assertEqual(len(self.costs(project_path="")), 2)
        self.assertEqual(len(self.costs(project_path=None)), 2)

    def test_rows_without_a_project_never_adopt_the_cwd(self):
        orphan = _row(n=1)
        orphan["project_path"] = ""
        self.write([orphan, _row(n=2, project=PROJ_A)])
        self.assertEqual(len(self.costs()), 2)
        self.assertEqual(len(self.costs(project_path=PROJ_A)), 1)


class TestStoreTrouble(CostsBase):
    def test_missing_store_is_empty(self):
        self.assertFalse(self.store.exists())
        self.assertEqual(self.costs(), [])

    def test_corrupt_store_is_empty_not_an_exception(self):
        self.store.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.costs(), [])

    def test_non_list_store_is_empty(self):
        self.store.write_text(json.dumps({"rows": []}), encoding="utf-8")
        self.assertEqual(self.costs(), [])

    def test_non_dict_entries_are_skipped(self):
        self.write([42, "x", None, _row(n=1)])
        self.assertEqual(self.costs()[0]["count"], 1)

    def test_loader_exception_is_empty(self):
        with mock.patch.object(orq, "load", side_effect=OSError("boom")):
            self.assertEqual(self.costs(), [])


class TestCosting(unittest.TestCase):
    def test_threshold_is_three(self):
        self.assertEqual(oc.NUDGE_THRESHOLD, 3)

    def test_filters_below_threshold(self):
        rules = [{"count": 3}, {"count": 2}, {"count": 9}]
        self.assertEqual(oc.costing(rules), [{"count": 3}, {"count": 9}])
        self.assertEqual(oc.costing(rules, threshold=9), [{"count": 9}])


if __name__ == "__main__":
    unittest.main()
