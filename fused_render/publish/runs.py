"""Running a publish, and being able to say how it is going.

A first publish is not quick. It exports the app, fetches a Python runtime the
first time this machine has ever needed one, builds a self-contained site, and
uploads it. That is minutes on a slow connection, and a spinner with nothing
behind it for two minutes is indistinguishable from a hang — which is how a
feature earns a reputation for being broken.

So a publish is a **run**: started, tracked by phase, and readable afterwards.
The Publish page starts one and polls it; navigating away and coming back finds
it still there, still running, still with its phase. When it finishes, the run is
where the URL comes from.

Two rules the store exists to enforce:

**One publish per app per target at a time.** Two concurrent deploys of the same
app race on the same provider project, and the loser overwrites the winner with
older bytes. A second start while one is running is refused, not queued.

**A finished run is kept.** Long enough for the page to read it after the poll
that saw it running — a result that vanished the instant it was ready would make
the last poll before completion the one that decided whether the author ever saw
their URL.

The store is process-memory. That is right for what it holds: a run cannot
survive the process anyway (the subprocess it drives dies with us), so persisting
its status would only preserve a claim about work nobody is doing. What DOES have
to survive — where the app was published to — is the publish record, written to
the app's own ``.fused/data/`` before the run is marked done (``record.py``).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field

from fused_render.app_listing import app_entry
from fused_render.export import ExportError, export_page
from fused_render.publish import cycles as cycles_cache
from fused_render.publish import record as record_store
from fused_render.publish import registry, site as site_builder
from fused_render.publish.adapter import PublishError, PublishRecord
from fused_render.publish.eligibility import Eligibility, scan

#: How long a finished run stays readable. Comfortably longer than any polling
#: interval, short enough that a long-lived server is not a museum of URLs.
KEEP_FINISHED_S = 15 * 60

#: The phases, in order, with the wording the Publish page shows. Named here
#: rather than in the frontend so a phase cannot be added without its label.
PHASES: dict[str, str] = {
    "checking": "Checking the app",
    "exporting": "Collecting the app's files",
    "runtime": "Fetching the Python runtime (first publish only)",
    "building": "Building the site",
    "uploading": "Uploading to the provider",
    "recording": "Saving the address",
}


@dataclass
class Run:
    """One publish, in flight or finished."""

    app_dir: str
    target: str
    state: str = "running"  # "running" | "done" | "error"
    phase: str = "checking"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    #: On success: url, project, updated_in_place, notes, bytes, icon.
    result: dict | None = None
    #: On failure: the message, meant to be read by the author.
    error: str | None = None

    def public(self) -> dict:
        return {
            "app_dir": self.app_dir,
            "target": self.target,
            "state": self.state,
            "phase": self.phase,
            "phase_label": PHASES.get(self.phase, self.phase),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }


_lock = threading.Lock()
_runs: dict[tuple[str, str], Run] = {}


def _sweep(now: float) -> None:
    for key, run in list(_runs.items()):
        if run.finished_at is not None and now - run.finished_at > KEEP_FINISHED_S:
            del _runs[key]


def get(app_dir: str, target: str) -> Run | None:
    with _lock:
        _sweep(time.time())
        return _runs.get((os.path.abspath(app_dir), target))


def reset() -> None:
    """Drop every run. Tests only — a shared module-level store between tests is
    the classic way to make one test's failure depend on another's success."""
    with _lock:
        _runs.clear()


def entry_page(app_dir: str) -> str:
    """The app's entry page, by the same rule every other surface uses.

    :raises PublishError: the folder does not declare one. A folder of untagged
        HTML is not an app (D301), and guessing at ``index.html`` here would
        publish a different page from the one the app's own card opens.
    """
    app_dir = os.path.abspath(app_dir)
    if not os.path.isdir(app_dir):
        raise PublishError(f"{app_dir} is not a folder")
    try:
        entry = app_entry(app_dir)
    except OSError as exc:
        raise PublishError(f"could not read {app_dir}: {exc}") from exc
    if not entry:
        raise PublishError(
            f"no page in {os.path.basename(app_dir)} declares itself an app. Add "
            '<meta name="fused-app" /> to the page you want published.'
        )
    return os.path.join(app_dir, entry)


def plan(app_dir: str, *, include: list[str] | None = None, exclude: list[str] | None = None) -> dict:
    """Everything the Publish page needs on load, minus the per-provider auth probe.

    Auth is left out on purpose: probing it means running each provider's CLI,
    which is seconds of latency on a page whose first job is to render. The page
    asks for auth separately, per target, once it is on screen.
    """
    page = entry_page(app_dir)
    elig = scan(page, include=include, exclude=exclude)
    targets = []
    records = record_store.load_all(app_dir)
    for adapter in registry.targets():
        described = registry.describe(adapter)
        v = registry.verdict(elig, adapter)
        described["eligible"] = v.eligible
        described["reasons"] = v.reasons
        rec = records.get(adapter.id)
        described["published"] = (
            None
            if rec is None
            else {"project": rec.project, "url": rec.url, "published_at": rec.published_at}
        )
        run = get(app_dir, adapter.id)
        described["run"] = run.public() if run else None
        # The LAST reading, not a fresh one: this route runs no provider CLI.
        # A cached number with an "as of" beside it is worth more on first paint
        # than a spinner, and the page refreshes a stale one itself.
        described["cycles"] = cycles_reading(app_dir, adapter.id) if described["funding"] else None
        targets.append(described)
    return {
        "app_dir": os.path.abspath(app_dir),
        "page": page,
        "name": os.path.basename(os.path.abspath(app_dir)),
        "runtime": elig.runtime.value,
        "state": elig.state.value,
        "blockers": elig.blockers,
        "notes": elig.notes,
        "python_files": elig.python_files,
        "packages": elig.pyodide_packages,
        "capability_labels": registry.capability_labels(),
        "targets": targets,
    }


def cycles_reading(app_dir: str, target: str) -> dict | None:
    """One target's cached cycles readout for this app, as the API returns it.

    ``fresh`` is what the page branches on: false means the reading is older
    than a day and worth refreshing, not that it is wrong. ``days_left`` is the
    figure that matters — a canister that runs out is frozen and eventually
    deleted with everything in it, so the runway is the number, and the balance
    is the supporting detail.
    """
    reading = cycles_cache.load(app_dir, target)
    if reading is None:
        return None
    return {
        "balance": reading.balance,
        "idle_burned_per_day": reading.idle_burned_per_day,
        "read_at": reading.read_at,
        "days_left": reading.days_left,
        "fresh": cycles_cache.is_fresh(reading),
    }


def start(
    app_dir: str,
    target: str,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    project: str | None = None,
) -> Run:
    """Begin a publish on a background thread and return its :class:`Run`.

    Validates everything that can be checked cheaply — the target exists, the
    app is eligible, nothing else is publishing it — BEFORE starting the thread,
    so a mistake is a synchronous error the caller can return as a 400 rather
    than a run that exists only to hold a failure.
    """
    app_dir = os.path.abspath(app_dir)
    adapter = registry.get(target)
    page = entry_page(app_dir)
    elig = scan(page, include=include, exclude=exclude)
    v = registry.verdict(elig, adapter)
    if not v.eligible:
        raise PublishError(
            f"this app cannot be published to {adapter.label}:\n  - " + "\n  - ".join(v.reasons)
        )

    key = (app_dir, target)
    with _lock:
        _sweep(time.time())
        existing = _runs.get(key)
        if existing is not None and existing.state == "running":
            raise PublishError(
                f"{os.path.basename(app_dir)} is already being published to {adapter.label}."
            )
        run = Run(app_dir=app_dir, target=target)
        _runs[key] = run

    thread = threading.Thread(
        target=_execute,
        args=(run, adapter, page, elig),
        kwargs={"include": include, "exclude": exclude, "project": project},
        name=f"publish-{target}",
        daemon=True,
    )
    thread.start()
    return run


def _execute(run, adapter, page, elig: Eligibility, *, include, exclude, project) -> None:
    """The publish itself, on its own thread. Never raises — a failure lands on
    the run, which is where the page is looking."""
    work = tempfile.mkdtemp(prefix="fused-publish-")
    try:
        run.phase = "exporting"
        bundle = os.path.join(work, "bundle")
        export_page(page, bundle, include=include, exclude=exclude)

        # Named as its own phase because it is the only step that can take
        # minutes for a reason the author has no other way to know: the runtime
        # is fetched once per machine, and a first publish is the one that pays.
        run.phase = "runtime" if elig.python_files else "building"
        site_dir = os.path.join(work, "site")
        name = os.path.basename(run.app_dir)
        summary = site_builder.build(bundle, site_dir, app_dir=run.app_dir, name=name, elig=elig)

        run.phase = "uploading"
        existing = record_store.load(run.app_dir, run.target)
        if project and (existing is None or existing.project != project):
            # An explicit name only ever applies to a FIRST publish: changing it
            # on an app that is already live would move it to a new origin and
            # strand every reader's saved progress.
            if existing is not None:
                raise PublishError(
                    f"{name} is already published as {existing.project!r}. Renaming it would "
                    "move the app to a new address and lose the progress every reader has "
                    "saved. Forget this deployment first if that is really what you want."
                )
            existing = None
        result = adapter.publish(
            site_dir, name=project or name, record=existing
        )

        run.phase = "recording"
        record_store.save(
            run.app_dir,
            PublishRecord(
                target=run.target,
                project=result.project,
                url=result.url,
                extra=result.extra,
            ),
        )
        run.result = {
            "url": result.url,
            "project": result.project,
            "updated_in_place": result.updated_in_place,
            "notes": result.notes,
            "bytes": summary.get("bytes"),
            "icon": summary.get("icon", {}).get("source"),
        }
        run.state = "done"
    except (PublishError, ExportError) as exc:
        run.state = "error"
        run.error = str(exc)
        # A failure that nonetheless minted the deployment's identity. Some
        # providers create the thing the origin is named after and only then
        # upload into it, so the id can exist — paid for — behind an error. Not
        # recording it makes the retry mint a SECOND one at a second origin,
        # which is the stranded-progress failure record.py exists to prevent,
        # reached the one way the record's own contract does not cover.
        salvage = getattr(exc, "salvage", None)
        if salvage is not None:
            try:
                record_store.save(run.app_dir, salvage)
            except PublishError as save_exc:
                run.error = f"{run.error}\n\n{save_exc}"
    except Exception as exc:  # noqa: BLE001 — a bug here must not lose the run
        run.state = "error"
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        run.finished_at = time.time()
        shutil.rmtree(work, ignore_errors=True)
