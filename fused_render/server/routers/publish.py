"""Routes behind Publish — the app's own hosting, on the author's infrastructure.

Nine endpoints, and they map one-to-one onto what the Publish page can do:

  GET  /api/publish/plan     what this app needs, which targets take it, what
                             it is already published to
  GET  /api/publish/auth     one provider's sign-in state (its own request, so
                             the plan does not wait on a CLI to start)
  POST /api/publish/login    run that provider's browser approval
  POST /api/publish/deploy   start a publish; returns immediately with a run
  GET  /api/publish/run      poll one
  POST /api/publish/forget   stop treating a provider project as this app's
  GET  /api/publish/funding  whether a target that costs money is paid for
  POST /api/publish/identity mint the publishing identity — the one response
                             that carries a seed phrase, once
  GET  /api/publish/cycles   the app's balance and burn, cached for a day

The last three exist only for targets that implement ``adapter.FundedTarget``
(an ICP canister is paid for in cycles the author transfers themselves, with no
account anywhere to sign into). They are guarded by that Protocol rather than by
a target id, so a provider without funding answers 400 instead of pretending.

The server does not host, authenticate or bill anything here; it drives the
author's own provider CLI on the author's own machine. Same posture as
``mounts``: local orchestration of a tool that holds its own credentials.

``X-Fused`` is required on the mutating four, like every other mutating
endpoint. ``login``, ``deploy`` and ``identity`` are also the ones with a cost
outside this process, so they carry it for the reason the header exists: a page
in a browser tab must not be able to make this app spend the author's bandwidth,
open an OAuth window, or mint a key in their OS keyring, because it was
visited.

Every route hands ``PublishError``'s message back verbatim as a 400. Those
messages are written for the author and shown unmodified in the UI — see
``publish/adapter.py``.
"""

import os

from fastapi import APIRouter, Body, Header

from fused_render.publish.adapter import FundedTarget, PublishError
from fused_render.server.common import _error, _require_fused

router = APIRouter()


def _abs(path: object, field: str):
    """Validate an absolute-path field, returning it or an error response.

    Absolute paths on every endpoint, the same convention the rest of the API
    uses: a relative path would resolve against the SERVER's working directory,
    which is not anywhere the author is thinking about.
    """
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return None, _error(f"'{field}' must be an absolute path to the app folder")
    return path, None


@router.get("/api/publish/plan")
def api_publish_plan(path: str = ""):
    """What could happen if this app were published, and what already has.

    Deliberately excludes each provider's sign-in state: probing it runs that
    provider's CLI, which costs seconds, and the page's first job is to render
    the eligibility report — which needs no network at all.
    """
    app_dir, bad = _abs(path, "path")
    if bad is not None:
        return bad
    from fused_render.publish import runs

    try:
        return runs.plan(app_dir)
    except PublishError as exc:
        return _error(str(exc))


@router.get("/api/publish/auth")
def api_publish_auth(target: str = ""):
    """One provider's sign-in state. Read-only and side-effect-free: it must
    never be the thing that opens a browser window."""
    from fused_render.publish import registry

    try:
        adapter = registry.get(target)
    except KeyError:
        return _error(f"no publish target named {target!r}")
    try:
        state = adapter.auth()
    except PublishError as exc:
        return _error(str(exc))
    return {
        "target": target,
        "status": state.status,
        "account": state.account,
        "detail": state.detail,
        "help_url": state.help_url,
    }


@router.post("/api/publish/login")
async def api_publish_login(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Run the provider's own browser approval and report the result.

    Blocking and slow — it waits for a human to click Approve — so it runs on the
    threadpool rather than the event loop, which would otherwise stall every
    other request in the app for as long as the author takes.

    Takes no credential, by design. The author approves in their browser and the
    token lands in the provider CLI's config; this app never sees it.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from starlette.concurrency import run_in_threadpool

    from fused_render.publish import registry

    try:
        adapter = registry.get(body.get("target") or "")
    except KeyError:
        return _error(f"no publish target named {body.get('target')!r}")
    try:
        state = await run_in_threadpool(adapter.login)
    except PublishError as exc:
        return _error(str(exc))
    return {
        "target": adapter.id,
        "status": state.status,
        "account": state.account,
        "detail": state.detail,
        "help_url": state.help_url,
    }


@router.post("/api/publish/deploy")
def api_publish_deploy(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Start a publish. Returns as soon as the run exists, not when it finishes.

    A first publish fetches a Python runtime and uploads ~14 MB; holding the
    request open for that would be a request that times out in the middle of
    something irreversible. The run is polled at ``/api/publish/run`` instead, so
    the page can name the phase and survive being navigated away from.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    app_dir, bad = _abs(body.get("path"), "path")
    if bad is not None:
        return bad
    include = body.get("include") or []
    exclude = body.get("exclude") or []
    for name, value in (("include", include), ("exclude", exclude)):
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            return _error(f"'{name}' must be an array of relative file paths")
    project = body.get("project") or None
    if project is not None and not isinstance(project, str):
        return _error("'project' must be a string")

    from fused_render.publish import runs

    try:
        run = runs.start(
            app_dir, body.get("target") or "", include=include, exclude=exclude, project=project
        )
    except KeyError:
        return _error(f"no publish target named {body.get('target')!r}")
    except PublishError as exc:
        return _error(str(exc))
    return run.public()


@router.get("/api/publish/run")
def api_publish_run(path: str = "", target: str = ""):
    """Poll one publish. ``null`` when there is none — a fresh page load for an
    app nobody has published in this session, which is not an error."""
    app_dir, bad = _abs(path, "path")
    if bad is not None:
        return bad
    from fused_render.publish import runs

    run = runs.get(app_dir, target)
    return {"run": run.public() if run else None}


@router.post("/api/publish/forget")
def api_publish_forget(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Stop treating a provider project as this app's.

    Deletes NOTHING at the provider: the deployment stays up and the URL keeps
    working. This is the escape hatch for a record pointing at a project the
    author removed by hand, where every publish would otherwise refuse to touch
    a project that is not there.

    The consequence is spelled out by the caller, not here, because it is
    severe: the next publish mints a new project, and anyone still using the old
    address keeps a copy of the app whose saved progress the new one cannot see.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    app_dir, bad = _abs(body.get("path"), "path")
    if bad is not None:
        return bad
    from fused_render.publish import record

    return {"forgotten": record.forget(app_dir, body.get("target") or "")}


def _funded(target: object):
    """The adapter for ``target``, if it is one that costs money.

    Two ways to fail and they are different: a target id nobody has heard of is
    the same 404-shaped mistake every other route reports, while a real target
    that simply has nothing to fund is a page asking a question that does not
    apply to it. Both are 400s with their own sentence rather than a shrug.
    """
    from fused_render.publish import registry

    try:
        adapter = registry.get(target or "")
    except KeyError:
        return None, _error(f"no publish target named {target!r}")
    if not isinstance(adapter, FundedTarget):
        return None, _error(f"{adapter.label} does not need funding")
    return adapter, None


@router.get("/api/publish/funding")
def api_publish_funding(target: str = ""):
    """Whether this target is paid for, and what the author does if not.

    Read-only and side-effect-free, like ``auth``: it is called to draw a panel.
    In particular it must never create an identity — that is a key on the
    author's disk, and it happens on an explicit press and nowhere else.
    """
    adapter, bad = _funded(target)
    if bad is not None:
        return bad
    try:
        state = adapter.funding()
    except PublishError as exc:
        return _error(str(exc))
    return {
        "target": adapter.id,
        "funded": state.funded,
        "identity": state.identity,
        "principal": state.principal,
        "balance": state.balance,
        "minimum": state.minimum,
        "transfer_command": state.transfer_command,
        "detail": state.detail,
        "help_url": state.help_url,
    }


@router.post("/api/publish/identity")
async def api_publish_identity(
    body: dict = Body(...), x_fused: str | None = Header(default=None)
):
    """Create the publishing identity. **The one response that carries a seed
    phrase**, and the only time it exists.

    The phrase is printed by the provider CLI at creation and never again.
    fused-render shows it once and forgets it: it is not written to the record,
    not put in a keyring of ours, and not stored by the client. The response
    body is not logged — this server's access log records method, path, status
    and duration and never a body (``server/common.no_cache_and_log``), which is
    a property to keep rather than a coincidence to rely on quietly.

    ``X-Fused`` because this mints a credential on the author's machine and
    touches their OS keyring. A page in a tab must not be able to do that
    because it was visited.

    Runs on the threadpool: the keyring can put a system prompt in front of the
    author, and waiting for them on the event loop stalls the whole app.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    adapter, bad = _funded(body.get("target"))
    if bad is not None:
        return bad
    from starlette.concurrency import run_in_threadpool

    try:
        created = await run_in_threadpool(adapter.create_identity)
    except PublishError as exc:
        return _error(str(exc))
    return {
        "target": adapter.id,
        "principal": created.principal,
        # Shown once, then dropped by both sides. No other route returns it and
        # no later call can recover it.
        "seed_phrase": created.seed_phrase,
    }


@router.get("/api/publish/cycles")
async def api_publish_cycles(path: str = "", target: str = "", refresh: bool = False):
    """This app's balance and idle burn, cached for a day.

    Serves the cached reading unless it is stale or ``refresh`` is set, because
    the number is a network round trip that does not move meaningfully between
    two page loads. A refresh that fails returns the OLD reading with the error
    beside it rather than an empty panel: a runway with an "as of" on it is
    worth more than nothing, and the balance is exactly the number an author
    should not lose sight of.
    """
    app_dir, bad = _abs(path, "path")
    if bad is not None:
        return bad
    adapter, bad = _funded(target)
    if bad is not None:
        return bad
    from starlette.concurrency import run_in_threadpool

    from fused_render.publish import cycles as cycles_cache
    from fused_render.publish import record, runs

    cached = cycles_cache.load(app_dir, adapter.id)
    if not refresh and cycles_cache.is_fresh(cached):
        return {"target": adapter.id, "cycles": runs.cycles_reading(app_dir, adapter.id)}
    rec = record.load(app_dir, adapter.id)
    if rec is None:
        return {"target": adapter.id, "cycles": None}
    try:
        reading = await run_in_threadpool(adapter.cycles, rec)
    except PublishError as exc:
        return {
            "target": adapter.id,
            "cycles": runs.cycles_reading(app_dir, adapter.id),
            "error": str(exc),
        }
    cycles_cache.save(app_dir, adapter.id, reading)
    return {"target": adapter.id, "cycles": runs.cycles_reading(app_dir, adapter.id)}
