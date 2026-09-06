"""Publish: user-owned app hosting behind a provider-adapter seam.

Sharing an app with someone who does not have fused-render used to ask something
of the recipient every way you tried it — the app installed, a URL scheme
registered, the author's laptop awake on the same Wi-Fi. Publish removes all of
that: the app becomes a page at a URL, and the reader needs a browser.

**fused-render hosts nothing.** It ships adapters; the author publishes to their
own infrastructure, with their own provider account, and the credential lives in
the provider's CLI. This satisfies the SPEC §1 non-goal verbatim — *"fused-render
itself still has no accounts, no tokens, and no server-side users"* — and is the
same shape as ``mounts``, where we ship the rclone adapter and never run the
storage.

The pieces, in the order a publish goes through them:

* ``eligibility`` — place the app on the runtime x state grid, and list what no
  target can host around. Provider-agnostic; the load-bearing piece.
* ``registry`` — which adapters exist, and whether each will take this app.
* ``site`` — turn an export bundle (SPEC §18 bundle v2) into a static tree: the
  page, its files, a vendored Pyodide, and the in-browser runtime that gives the
  page back the portable subset of ``window.fused``.
* ``adapter`` — the interface a provider implements.
* ``cloudflare`` — the first one: Cloudflare Pages, via ``wrangler``.
* ``record`` — what the app remembers so the NEXT publish lands on the same
  origin, which is what keeps every reader's saved progress.
"""

from fused_render.publish.adapter import (  # noqa: F401
    AuthState,
    Capability,
    PublishAdapter,
    PublishError,
    PublishRecord,
    PublishResult,
)
from fused_render.publish.eligibility import Eligibility, scan  # noqa: F401
from fused_render.publish.registry import Verdict, get, targets, verdict  # noqa: F401

__all__ = [
    "AuthState",
    "Capability",
    "Eligibility",
    "PublishAdapter",
    "PublishError",
    "PublishRecord",
    "PublishResult",
    "Verdict",
    "get",
    "scan",
    "targets",
    "verdict",
]
