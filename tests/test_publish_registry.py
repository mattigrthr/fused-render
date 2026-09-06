"""Tests for the target registry and the offer rule (fused_render/publish/registry.py).

The parent issue is explicit that "a target is disabled because its adapter is
not written yet, never because the provider is incapable", and that the UI must
say which. These pin that: a refusal always carries a reason, and the reason
names the grid cell it fails.
"""

from dataclasses import replace

import pytest

from fused_render.publish import registry
from fused_render.publish.adapter import Capability
from fused_render.publish.eligibility import Eligibility


class _Target:
    id = "test-target"
    label = "Test Target"
    blurb = "for tests"
    capabilities = frozenset(
        {Capability.RUNTIME_JS, Capability.RUNTIME_PYODIDE, Capability.STATE_NONE,
         Capability.STATE_CLIENT_LOCAL}
    )

    def auth(self): ...
    def login(self): ...
    def publish(self, site_dir, *, name, record): ...


def _elig(runtime=Capability.RUNTIME_PYODIDE, state=Capability.STATE_CLIENT_LOCAL, blockers=()):
    return Eligibility(page="/x/index.html", runtime=runtime, state=state, blockers=list(blockers))


def test_a_rung_one_app_is_offered():
    v = registry.verdict(_elig(), _Target())
    assert v.eligible and v.reasons == []


def test_a_cpython_app_is_refused_naming_the_runtime_it_needs():
    v = registry.verdict(_elig(runtime=Capability.RUNTIME_CPYTHON), _Target())
    assert not v.eligible
    assert "Python on a real CPython process" in v.reasons[0]
    assert "Test Target" in v.reasons[0]


def test_an_app_wanting_shared_state_is_refused_naming_the_state_it_needs():
    v = registry.verdict(_elig(state=Capability.STATE_SHARED), _Target())
    assert "state shared between viewers" in v.reasons[0]


def test_a_blocker_refuses_every_target_verbatim():
    # Repeated per target rather than hoisted: a disabled row that says only
    # "unavailable" while the explanation lives elsewhere is what makes a
    # disabled button feel arbitrary.
    v = registry.verdict(_elig(blockers=["fused.writeFile() is not supported"]), _Target())
    assert v.reasons == ["fused.writeFile() is not supported"]


def test_both_axes_are_reported_at_once():
    v = registry.verdict(
        _elig(runtime=Capability.RUNTIME_CPYTHON, state=Capability.STATE_SHARED), _Target()
    )
    assert len(v.reasons) == 2


def test_a_stronger_runtime_cell_is_not_satisfied_by_a_weaker_one():
    # The axes are ordered "weakest requirement first", but a target declares
    # every cell it covers rather than a ceiling — so this is membership, and a
    # JS-only target must not appear to run Python.
    class JsOnly(_Target):
        capabilities = frozenset({Capability.RUNTIME_JS, Capability.STATE_NONE})

    assert registry.verdict(_elig(state=Capability.STATE_NONE), JsOnly()).eligible is False
    assert registry.verdict(
        _elig(runtime=Capability.RUNTIME_JS, state=Capability.STATE_NONE), JsOnly()
    ).eligible is True


def test_cloudflare_is_registered_and_covers_the_rung_one_region():
    ids = [t.id for t in registry.targets()]
    assert "cloudflare-pages" in ids
    cf = registry.get("cloudflare-pages")
    assert registry.verdict(_elig(), cf).eligible


def test_an_unknown_target_is_a_keyerror_not_a_guess():
    # A stale Publish page asking for a removed provider must not publish
    # somewhere else instead.
    with pytest.raises(KeyError):
        registry.get("icp")


def test_describe_round_trips_every_capability_as_a_wire_string():
    described = registry.describe(_Target())
    labels = registry.capability_labels()
    for value in described["capabilities"]:
        assert registry._capability(value) in _Target.capabilities
        assert labels[value]


def test_describe_omits_auth_unless_it_was_probed():
    # The Publish page wants both; a caller listing targets should not have to
    # shell out to every provider CLI to do it.
    assert registry.describe(_Target())["auth"] is None
