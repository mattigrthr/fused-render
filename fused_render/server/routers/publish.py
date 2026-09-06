"""Routes behind Publish — the app's own hosting, on the author's infrastructure.

Five endpoints, and they map one-to-one onto what the Publish page can do:

  GET  /api/publish/plan     what this app needs, which targets take it, what
                             it is already published to
  GET  /api/publish/auth     one provider's sign-in state (its own request, so
                             the plan does not wait on a CLI to start)
  POST /api/publish/login    run that provider's browser approval
  POST /api/publish/deploy   start a publish; returns immediately with a run
  GET  /api/publish/run      poll one
  POST /api/publish/forget   stop treating a provider project as this app's

The server does not host, authenticate or bill anything here; it drives the
author's own provider CLI on the author's own machine. Same posture as
``mounts``: local orchestration of a tool that holds its own credentials.

``X-Fused`` is required on the mutating three, like every other mutating
endpoint. ``login`` and ``deploy`` are also the two that reach the network, so
they carry it for the reason the header exists: a page in a browser tab must not
be able to make this app spend the author's bandwidth, or open an OAuth window,
because it was visited.

Every route hands ``PublishError``'s message back verbatim as a 400. Those
messages are written for the author and shown unmodified in the UI — see
``publish/adapter.py``.
"""

import os

from fastapi import APIRouter, Body, Header

from fused_render.publish.adapter import PublishError
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
