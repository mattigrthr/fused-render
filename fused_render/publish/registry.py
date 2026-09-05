"""Which targets exist, and whether a given app may go to each.

The registry is the one place that knows the set of adapters, so "a target is
disabled because **its adapter is not written yet**, never because the provider
is incapable" (parent issue) is a property of this file rather than a claim in a
doc. A provider with no adapter is simply absent from :func:`targets`; a provider
with one is present and answers for itself.

:func:`verdict` is the whole offer rule, and it is deliberately three lines of
set membership: an app needs one cell per axis, a target covers a set of cells,
and the target is offered iff it covers both. Everything that makes a refusal
*legible* — which cell, in what words — comes from the shared enum and its labels
in ``adapter.py``, not from per-provider prose that could contradict the check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from fused_render.publish.adapter import (
    CAPABILITY_LABELS,
    AuthState,
    Capability,
    PublishAdapter,
)
from fused_render.publish.eligibility import Eligibility


@dataclass(frozen=True)
class Verdict:
    """Whether one target will take one app, and why not when it will not.

    ``reasons`` is empty exactly when ``eligible`` is true. Each entry is a full
    sentence for the author: the Publish page shows them under a disabled row and
    adds nothing of its own, so a reason that reads badly here reads badly there.
    """

    target: str
    eligible: bool
    reasons: list[str] = field(default_factory=list)


def verdict(elig: Eligibility, adapter: PublishAdapter) -> Verdict:
    """Can ``adapter``'s target host this app?

    Three ways to be refused, in the order the author can act on them:

    1. A blocker — something wrong with the app itself, true of every target.
       Repeated per target rather than hoisted, because a row that says only
       "unavailable" while the explanation sits elsewhere is the thing that makes
       a disabled button feel arbitrary.
    2. The runtime cell the app needs is not one this target covers.
    3. The state cell it needs is not one this target covers.
    """
    reasons: list[str] = list(elig.blockers)
    if elig.runtime not in adapter.capabilities:
        reasons.append(
            f"this app needs {CAPABILITY_LABELS[elig.runtime]}, which {adapter.label} "
            "does not cover in fused-render yet"
        )
    if elig.state not in adapter.capabilities:
        reasons.append(
            f"this app needs {CAPABILITY_LABELS[elig.state]}, which {adapter.label} "
            "does not cover in fused-render yet"
        )
    return Verdict(target=adapter.id, eligible=not reasons, reasons=reasons)


@lru_cache(maxsize=1)
def _adapters() -> tuple[PublishAdapter, ...]:
    """Every adapter, constructed once.

    Imported inside the function so importing the publish package costs nothing a
    caller that only wants :func:`~fused_render.publish.eligibility.scan` has to
    pay, and so a broken provider module cannot take the eligibility scan down
    with it.

    Adding a provider is one line here.
    """
    from fused_render.publish.cloudflare import CloudflarePages

    return (CloudflarePages(),)


def targets() -> tuple[PublishAdapter, ...]:
    """Every target fused-render can publish to today, in display order."""
    return _adapters()


def get(target_id: str) -> PublishAdapter:
    """The adapter with this id.

    :raises KeyError: no such target — the caller turns that into a 404 rather
        than guessing, since a stale Publish page asking for a removed provider
        must not silently publish somewhere else.
    """
    for adapter in _adapters():
        if adapter.id == target_id:
            return adapter
    raise KeyError(target_id)


def describe(adapter: PublishAdapter, auth: AuthState | None = None) -> dict:
    """One target as the API returns it (``/api/publish/targets``).

    ``auth`` is optional so a caller can describe the target set without probing
    every provider CLI — the Publish page wants both, a test usually wants neither.
    """
    return {
        "id": adapter.id,
        "label": adapter.label,
        "blurb": adapter.blurb,
        "capabilities": sorted(c.value for c in adapter.capabilities),
        "auth": None
        if auth is None
        else {
            "status": auth.status,
            "account": auth.account,
            "detail": auth.detail,
            "help_url": auth.help_url,
        },
    }


def capability_labels() -> dict[str, str]:
    """Every grid cell's wording, keyed by wire value — so the Publish page can
    name a capability it was not compiled against."""
    return {c.value: label for c, label in CAPABILITY_LABELS.items()}


def _capability(value: str) -> Capability:
    """A wire capability string back to its enum member (round-trips ``describe``)."""
    return Capability(value)
