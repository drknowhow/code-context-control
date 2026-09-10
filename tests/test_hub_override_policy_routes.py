"""The hub can now turn override requests on — and cannot do it silently.

WHAT WAS MISSING. The hub could already LIST override requests
(`GET /api/hub/overrides`) and DECIDE them
(`POST /api/hub/overrides/<id>`) — but nothing exposed `override.enabled`,
which decides whether a request may exist at all. So the hub answered a
question the agent was never allowed to ask, and the only way to change that
was the phone API or hand-editing `.c3/config.json`.

WHAT THESE PIN.
1. Widening is refused without a typed confirmation, and tightening is not.
   That asymmetry is the security property: making the guard stricter should
   always be one click; loosening what a single approval can allow must not be.
2. The hub and the phone share ONE implementation of "what counts as widening"
   (`services.override_policy`). Two copies would be two chances for one
   surface to allow what the other refuses.
3. `GET .../policy` does not fall through to the POST-only `<request_id>`
   route — the same ordering trap `costs` already carries a comment about.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "cli")):
    if p not in sys.path:
        sys.path.insert(0, p)

from services import override_policy as opol  # noqa: E402


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".c3").mkdir()
    (tmp_path / ".c3" / "config.json").write_text(
        json.dumps({"project_path": str(tmp_path)}), encoding="utf-8")
    return tmp_path


def _read_override(project) -> dict:
    cfg = json.loads((project / ".c3" / "config.json").read_text(encoding="utf-8"))
    return cfg.get("override") or {}


# ── the shared service: the security rule itself ──────────────────────


def test_enabling_is_a_widening_and_needs_confirmation(project):
    with pytest.raises(opol.PolicyEditError) as ei:
        opol.apply_section(project, {"enabled": True}, confirmed=False)
    assert "enabled" in ei.value.widens
    assert ei.value.payload.get("needs_confirmation") is True
    assert _read_override(project) == {}, "it wrote despite refusing"


def test_confirmed_widening_is_written(project):
    written, widens = opol.apply_section(
        project, {"enabled": True}, confirmed=True)
    assert widens == ["enabled"]
    assert written["enabled"] is True
    assert _read_override(project)["enabled"] is True


def test_turning_a_layer_on_is_a_widening(project):
    opol.apply_section(project, {"enabled": True}, confirmed=True)
    with pytest.raises(opol.PolicyEditError) as ei:
        opol.apply_section(project, {"layers": {"access_readonly": True}},
                           confirmed=False)
    assert "layers.access_readonly" in ei.value.widens


def test_tightening_never_needs_confirmation(project):
    """The asymmetry that matters: stricter is always one click."""
    opol.apply_section(project, {"enabled": True}, confirmed=True)
    written, widens = opol.apply_section(
        project, {"enabled": False}, confirmed=False)
    assert widens == []
    assert written["enabled"] is False


def test_unknown_keys_are_a_hard_error_not_a_silent_noop(project):
    with pytest.raises(opol.PolicyEditError) as ei:
        opol.apply_section(project, {"nonsense": 1}, confirmed=True)
    assert "unknown override key" in str(ei.value)
    assert _read_override(project) == {}


def test_unknown_layers_are_rejected(project):
    with pytest.raises(opol.PolicyEditError) as ei:
        opol.apply_section(project, {"layers": {"not_a_layer": True}},
                           confirmed=True)
    assert "unknown layer" in str(ei.value)


def test_wake_is_refused_on_remote_surfaces_and_allowed_on_the_desktop(project):
    """`wake` names an argv this machine runs. A bearer token from a phone is
    authentication, not physical presence."""
    with pytest.raises(opol.PolicyEditError) as ei:
        opol.apply_section(project, {"wake": ["notify-send", "hi"]},
                           confirmed=True, allow_wake=False)
    assert ei.value.status == 403
    written, _ = opol.apply_section(project, {"wake": ["notify-send", "hi"]},
                                    confirmed=True, allow_wake=True)
    assert written["wake"] == ["notify-send", "hi"]


def test_layers_merge_rather_than_replace(project):
    opol.apply_section(project, {"enabled": True,
                                 "layers": {"access_confirm": True}},
                       confirmed=True)
    opol.apply_section(project, {"layers": {"mask": False}}, confirmed=True)
    block = _read_override(project)
    assert block["layers"]["access_confirm"] is True, "an unrelated layer was dropped"
    assert block["layers"]["mask"] is False


# ── the extraction preserved the phone's behaviour ────────────────────


@pytest.mark.parametrize("section", [
    {"enabled": True},
    {"layers": {"access_readonly": True, "mask": False}},
    {"max_ttl_s": 899},
    {"allow_session_grants": True},
    {"enabled": False},
])
def test_shared_widenings_agree_with_the_retired_inline_copy(project, section):
    """The mobile route's inline implementation is kept as
    ``_override_widenings_legacy`` precisely so the move can be shown to have
    preserved behaviour rather than asserted to have."""
    from oracle.services import mobile_api as mapi

    current = opol.resolve(str(project))
    assert opol.widenings(current, section) == \
        mapi._override_widenings_legacy(current, section)


# ── the routes ────────────────────────────────────────────────────────


def test_policy_route_is_registered_before_the_request_id_catchall():
    import cli.hub_server as hub

    rules = [str(r) for r in hub.app.url_map.iter_rules()
             if "/api/hub/overrides" in str(r)]
    assert "/api/hub/overrides/policy" in rules
    # A GET on `policy` must not fall through to the POST-only converter.
    match = hub.app.url_map.bind("localhost").match(
        "/api/hub/overrides/policy", method="GET")
    assert match[0] == "api_hub_override_policy_get", match
