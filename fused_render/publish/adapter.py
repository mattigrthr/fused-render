"""The publish-adapter seam: what a target is, and what publishing to one means.

fused-render publishes an app to infrastructure the **author** owns. This module
holds the interface every provider adapter implements and nothing provider-
specific: the shell's Publish page, the ``/api/publish`` routes and the
eligibility scan all speak to targets through the types here, so adding a
provider is one new module plus one registry line (see ``registry.py``).

The seam deliberately carries no credential of ours and mints no URL of ours —
it satisfies SPEC §1's non-goal verbatim (*"fused-render itself still has no
accounts, no tokens, and no server-side users"*). Authentication is the
provider's own browser flow, run as a subprocess of the author's machine
(:meth:`PublishAdapter.login`), and whatever token it leaves behind lives in the
provider CLI's own config, never in ours. The in-repo precedent is ``mounts``:
we ship the rclone adapter, rclone holds the remote's credentials, and Fused
never runs the storage.

Four vocabularies meet here.

**Capability** — where a target sits on the runtime × state grid (parent issue).
An adapter declares the cells it covers as :class:`Capability`; the eligibility
scan (``eligibility.py``) places the *app* on the same grid, and a target is
offered iff every cell the app needs is one the target covers. This is why a
disabled target can always say *which* requirement it fails — the comparison is
between two sets of the same enum, not a per-provider special case.

**Auth state** — :class:`AuthState`, a three-way answer (``"ready"`` /
``"needs-login"`` / ``"unavailable"``) rather than a bool, because "you are not
logged in" and "the provider's CLI is not installed" need different buttons and
only the second is not fixable by clicking Log in.

**Publish** — :meth:`PublishAdapter.publish` takes a *site directory* (a static
tree built by ``site.py`` from an export bundle) plus the app's
:class:`PublishRecord`, and returns a :class:`PublishResult` naming the canonical
URL. Re-publishing is the same call: the record carries the provider's project
identity, so the adapter updates that project in place. **Origin stability is
the contract, not an optimization** — a rung-1 app's whole state story is
``localStorage``, which is origin-scoped, so a publish that minted a new origin
would silently wipe every viewer's progress.

**Funding** — :class:`FundedTarget`, a second Protocol for the targets that cost
the author money directly rather than through a plan. An ICP canister is paid
for in cycles, transferred by the author from their own terminal, with no
account anywhere to sign into. That is not authentication and must not be
squeezed into :class:`AuthState`: it is a publish precondition with its own
button, asked only of adapters that implement it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class Capability(str, Enum):
    """One cell of the runtime × state grid a target may or may not cover.

    Two independent axes, not a single complexity scale (parent issue): what
    kind of code has to execute, and where per-viewer state has to live. A
    target declares the cells it covers; an app declares the cells it needs.

    ``str``-valued so a capability crosses the API boundary as its own name —
    the Publish page renders these strings directly and no separate wire enum
    has to be kept in step.
    """

    #: HTML/CSS/JS only — no Python executes anywhere.
    RUNTIME_JS = "runtime:js"
    #: Python that Pyodide can run in the viewer's browser: the stdlib plus
    #: packages the Pyodide distribution carries (numpy, pandas, pillow, …).
    RUNTIME_PYODIDE = "runtime:pyodide"
    #: Python that needs a real CPython process — a C extension with no Pyodide
    #: wheel (PyMuPDF, pikepdf), or a third-party import we cannot place.
    RUNTIME_CPYTHON = "runtime:cpython"

    #: The app never writes. Every viewer sees the same thing forever.
    STATE_NONE = "state:none"
    #: Per-viewer state that lives in the viewer's own browser and never leaves
    #: it. Survives a reload and a browser restart; does not cross devices.
    STATE_CLIENT_LOCAL = "state:client-local"
    #: Per-viewer state held by a backend — an account, a row keyed by identity.
    STATE_SERVER_BACKED = "state:server-backed"
    #: State every viewer sees and can change — one document, many readers.
    STATE_SHARED = "state:shared"


#: The runtime axis, weakest requirement first. An app needing an earlier cell is
#: satisfied by a target covering a later one, so the scan reports the single
#: strongest cell the app needs and the offer check is a plain membership test
#: against the target's covered set (which lists every cell it covers, not just
#: its ceiling).
RUNTIME_AXIS: tuple[Capability, ...] = (
    Capability.RUNTIME_JS,
    Capability.RUNTIME_PYODIDE,
    Capability.RUNTIME_CPYTHON,
)

#: The state axis, weakest requirement first — same reading as RUNTIME_AXIS.
STATE_AXIS: tuple[Capability, ...] = (
    Capability.STATE_NONE,
    Capability.STATE_CLIENT_LOCAL,
    Capability.STATE_SERVER_BACKED,
    Capability.STATE_SHARED,
)

#: Human wording for each cell, used verbatim in the Publish page's disabled
#: reason. Kept here rather than in the frontend so the API and the UI can never
#: describe the same cell differently.
CAPABILITY_LABELS: dict[Capability, str] = {
    Capability.RUNTIME_JS: "HTML, CSS and JavaScript only",
    Capability.RUNTIME_PYODIDE: "Python in the browser (Pyodide)",
    Capability.RUNTIME_CPYTHON: "Python on a real CPython process",
    Capability.STATE_NONE: "no saved state",
    Capability.STATE_CLIENT_LOCAL: "state in each viewer's own browser",
    Capability.STATE_SERVER_BACKED: "per-viewer state on a backend",
    Capability.STATE_SHARED: "state shared between viewers",
}


class PublishError(Exception):
    """A user-correctable failure while publishing.

    Carries a message meant to be READ by the author — the Publish page shows it
    verbatim and ``/api/publish/*`` returns it as a ``400 {"error": …}``, exactly
    as ``ExportError`` does for the build step. Anything the author cannot act on
    (a bug here, a broken install) should raise its own exception type instead
    and surface as a 500 with a traceback in the log.

    ``salvage`` is the exception to "a failure changed nothing". Some providers
    mint the deployment's identity *before* the upload that fails — an ICP
    canister id is created, paid for with real cycles, and only then loaded with
    assets. That id is the app's origin. If it is dropped on the way out, the
    retry mints a SECOND canister at a SECOND origin and every reader's saved
    progress is orphaned by way of an error message. An adapter that gets that
    far therefore raises with the record it created, and ``runs.py`` writes it
    before reporting the failure.
    """

    def __init__(self, message: str, *, salvage: "PublishRecord | None" = None):
        super().__init__(message)
        #: Provider identity that came into existence before this failure and
        #: must be remembered anyway. ``None`` for every failure that left
        #: nothing behind, which is most of them.
        self.salvage = salvage


@dataclass(frozen=True)
class AuthState:
    """Whether this target can be published to right now, and what to do if not.

    Three states, not a bool, because they need three different buttons:

      * ``"ready"`` — authenticated; ``account`` names who, so the author can
        see they are about to publish to the right Cloudflare account.
      * ``"needs-login"`` — the provider CLI is present but nobody is signed in.
        The Publish page offers Log in, which runs :meth:`PublishAdapter.login`.
      * ``"unavailable"`` — the provider CLI is missing or unusable. Clicking Log
        in cannot fix this, so the page must not offer it; ``detail`` says what
        to install and ``help_url`` where from.
    """

    status: str  # "ready" | "needs-login" | "unavailable"
    #: Who we are signed in as (an email, an account name) — ``None`` unless ready.
    account: str | None = None
    #: One sentence naming the obstacle, shown under the target's row.
    detail: str = ""
    #: Where the author goes to fix an ``unavailable`` (install docs), if anywhere.
    help_url: str | None = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"


@dataclass(frozen=True)
class PublishRecord:
    """The identity of an app's existing deployment on one target.

    Persisted per app under ``.fused/data/publish.json`` (``record.py``) because
    it is state the app **cannot rebuild**: it is the only thing that makes a
    re-publish land on the same origin, and a lost record means every viewer's
    ``localStorage`` progress is orphaned behind a URL nobody will visit again.

    ``project`` is the provider's own name for the deployment (a Cloudflare Pages
    project name); ``url`` is the canonical share URL that name resolves to.
    ``extra`` carries provider-specific identity an adapter needs to find its own
    deployment again, and is round-tripped untouched by everything else.
    """

    target: str
    project: str
    url: str
    #: ISO-8601 UTC, when this app was last published to this target.
    published_at: str = ""
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PublishResult:
    """What a successful publish produced.

    ``url`` is the canonical share URL to hand the author — the one that stays
    the same across re-publishes, never the per-deployment preview URL a provider
    also mints (Cloudflare Pages gives every deployment a
    ``<hash>.<project>.pages.dev`` alias; handing that out would pin readers to a
    snapshot and split their ``localStorage`` across origins).
    """

    url: str
    project: str
    #: True when this publish updated a deployment that already existed.
    updated_in_place: bool
    #: Free-form provider notes worth showing the author (a first-publish DNS
    #: propagation delay, say). Never an error — a failure raises.
    notes: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@runtime_checkable
class PublishAdapter(Protocol):
    """What every provider adapter implements.

    A :class:`~typing.Protocol` rather than a base class: an adapter is a plain
    object with these five members and no inherited machinery, which keeps the
    fake used by the tests as cheap as the real thing.
    """

    #: Stable wire id — the string the API and the URL use (``"cloudflare-pages"``).
    #: Never change one: it is persisted in every app's publish record.
    id: str
    #: What the Publish page calls this target ("Cloudflare Pages").
    label: str
    #: One sentence under the label, saying what the author gets.
    blurb: str
    #: Every grid cell this target covers.
    capabilities: frozenset[Capability]

    def auth(self) -> AuthState:
        """Ask, without side effects and without opening a browser, whether we
        could publish right now. Called on every Publish-page load, so it must be
        fast and must never block on the network for long."""
        ...

    def login(self) -> AuthState:
        """Run the provider's own interactive auth (a browser OAuth approval) and
        return the resulting state. Blocking — the caller runs it off the event
        loop. Never accepts a credential as an argument: FusedRender must not be
        handed a long-lived token, and a paste field is a worse security posture
        than a browser approval."""
        ...

    def publish(
        self, site_dir: str, *, name: str, record: PublishRecord | None
    ) -> PublishResult:
        """Upload the static tree at ``site_dir`` and return its canonical URL.

        ``name`` is the app's folder name, the seed for a first deployment's
        project name. ``record`` is the app's existing deployment on this target,
        or ``None`` for a first publish — when it is present the adapter MUST
        update that same project rather than mint a new one (see
        :class:`PublishRecord` on why the origin is load-bearing).
        """
        ...


# ---- funding: a publish precondition that is not an auth state ---------------
#
# Some targets have nothing to log in to and still cannot be published to yet.
# An ICP canister is paid for in cycles the author transfers themselves, out of
# band, from their own terminal; there is no account to sign into and no
# credential we could hold even if we wanted to. Modelling that as a fourth
# `AuthState` would be wrong twice over — it is not authentication, and a
# disabled row saying "not signed in" gives the author no way to act.
#
# So funding is its own question, asked of the targets that have it, with its
# own affordance on the Publish page. A target that needs no funding simply does
# not implement `FundedTarget` and nothing about it changes.


@dataclass(frozen=True)
class FundingState:
    """Whether this target has been paid for, and what the author does about it.

    ``identity`` is separate from ``funded`` because they fail in different
    places: with no identity there is nowhere to send money, and the first press
    of Fund cycles is what creates one. That press is also the only moment the
    seed phrase exists (:class:`IdentityCreated`), which is why the identity is
    minted on demand rather than at install time — nobody who never publishes to
    this target should have a key on their disk.
    """

    #: True once there is enough to publish with. The Publish button is gated on
    #: this in addition to :class:`AuthState`.
    funded: bool
    #: Whether an identity exists at all. False means Fund cycles will make one.
    identity: bool
    #: The identity's principal — where the author sends cycles. Shown with a
    #: copy button, because it is a 60-character string nobody retypes.
    principal: str | None = None
    #: What the provider says is there now, in the provider's own smallest unit.
    balance: int | None = None
    #: What it takes to publish once. Shown so the author is not guessing.
    minimum: int = 0
    #: The exact command to run, principal already substituted. The transfer
    #: happens in the author's own terminal — we never move their money.
    transfer_command: str | None = None
    #: One sentence for the panel, in the author's terms.
    detail: str = ""
    #: Where the provider documents funding.
    help_url: str | None = None


@dataclass(frozen=True)
class IdentityCreated:
    """The one moment a seed phrase exists, and the response that carries it.

    Printed by the provider CLI at creation and never again. fused-render shows
    it once and forgets it: no keyring item of ours, no file that outlives the
    call, nothing in the record. Storing it would make us the custodian of a
    portable, unrevokable credential that moves real money, which is the one
    thing the publish seam is built not to be.

    Losing it is not losing the funds — the signing key stays in the provider
    CLI's own keyring and can be exported from there — so the warning around it
    should be serious without being apocalyptic.
    """

    principal: str
    #: Shown once, then dropped. Never persisted, never logged, never returned
    #: by any other call.
    seed_phrase: str


@dataclass(frozen=True)
class CyclesReading:
    """What an app's deployment holds, and how fast it is draining.

    Two numbers rather than one because the pair is a *runway*: a balance alone
    says nothing about how long it lasts, and the failure this readout exists to
    prevent is a canister that quietly runs out, freezes, and is deleted with
    all its state. That is a dead man's switch on the author's app, so the page
    states it rather than showing a bare number.

    Rebuildable from the provider at any time, so it is cache and not data (SPEC
    §47) — see ``publish/cycles.py``, which keeps one reading per app per target
    for a day.
    """

    #: The provider's own smallest unit.
    balance: int
    #: What the deployment burns per day just by existing.
    idle_burned_per_day: int
    #: ISO-8601 UTC. Shown with the number: a reading with an "as of" beats a
    #: spinner, so a stale one is labelled rather than hidden.
    read_at: str

    @property
    def days_left(self) -> float | None:
        """Balance ÷ idle burn. ``None`` when nothing is being burned, which is
        not "forever" so much as "the provider did not tell us"."""
        if self.idle_burned_per_day <= 0:
            return None
        return self.balance / self.idle_burned_per_day


@runtime_checkable
class FundedTarget(Protocol):
    """The extra three questions a target that costs money answers.

    Deliberately a SECOND protocol rather than three more methods on
    :class:`PublishAdapter` with no-op defaults. Cloudflare Pages has no
    identity, no balance and nothing to fund; giving it stubs would put an empty
    Fund cycles panel one bug away from its Publish page. ``isinstance`` against
    this protocol is what the registry and the routes branch on, so the answer
    to "does this target need funding" is the adapter's own shape.
    """

    def funding(self) -> FundingState:
        """Whether this target can be paid for a publish right now.

        Read-only and cheap enough for a page load. Must not create an identity:
        that is :meth:`create_identity`, which happens on an explicit press.
        """
        ...

    def create_identity(self) -> IdentityCreated:
        """Mint the publishing identity and return it, seed phrase included.

        Called once, from the first Fund cycles press. The phrase crosses the
        API boundary exactly here and is never retained on either side.
        """
        ...

    def cycles(self, record: PublishRecord) -> CyclesReading:
        """What this app's deployment holds and burns. One network round trip."""
        ...
