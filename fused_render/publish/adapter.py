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

Three vocabularies meet here.

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
    """


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
