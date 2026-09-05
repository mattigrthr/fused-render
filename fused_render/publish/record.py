"""Where an app remembers what it has already published, and to what URL.

One file, ``<app>/.fused/data/publish.json``. Under ``data/`` and never
``cache/`` because it is the definition of state an app **cannot rebuild** (SPEC
§47): nothing on this machine or the provider's can reconstruct which Pages
project belongs to which folder once the mapping is gone.

Losing it is not a small thing. A rung-1 app's entire state story is
``localStorage``, which is scoped to an origin — so a re-publish that cannot find
the previous project mints a new one at a new URL, and every reader's progress is
stranded behind an address nobody will open again. That failure is silent from
the author's side (the new URL works, and looks right) and total from the
reader's. This file is what prevents it, which is why it is written *before* the
result is reported and why a write failure is a hard error rather than a warning.

The record is keyed by target, so one app can be live on several providers at
once — publishing to ICP later must not disturb the Cloudflare URL people
already have.

Deliberately NOT git-ignored by us and deliberately NOT secret: it holds a
project name and a public URL, nothing a reader of the page could not already
see. An author who commits it and clones elsewhere keeps the ability to
re-publish in place, which is the behaviour they want.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from fused_render.app_fused_dir import data_dir
from fused_render.publish.adapter import PublishError, PublishRecord

FILENAME = "publish.json"

#: Bumped only for a change a reader must branch on — same contract as
#: ``.fused/meta.json`` (``app_fused_dir.META_VERSION``): a file that carries a
#: number a reader does not know is declined, not misread.
VERSION = 1


def path_for(app_dir: str) -> str:
    return os.path.join(data_dir(app_dir), FILENAME)


def _read(app_dir: str) -> dict:
    """The parsed file, or ``{}`` for every way there is nothing usable to read.

    Absent, unreadable, not JSON, not an object, or a version from the future all
    collapse to "no record" — a caller can do nothing different with them, and
    this file is user-writable so all five are reachable without a bug. The cost
    of misreading is the stranded-origin failure above, so an unrecognised
    version declines rather than guesses.
    """
    try:
        with open(path_for(app_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return {}
    targets = data.get("targets")
    return targets if isinstance(targets, dict) else {}


def load(app_dir: str, target: str) -> PublishRecord | None:
    """This app's existing deployment on ``target``, or ``None``."""
    entry = _read(app_dir).get(target)
    if not isinstance(entry, dict):
        return None
    project, url = entry.get("project"), entry.get("url")
    if not isinstance(project, str) or not isinstance(url, str) or not project or not url:
        return None  # a half-written record is no record: re-publishing fresh is safer
    extra = entry.get("extra")
    return PublishRecord(
        target=target,
        project=project,
        url=url,
        published_at=entry.get("published_at") or "",
        extra=extra if isinstance(extra, dict) else {},
    )


def load_all(app_dir: str) -> dict[str, PublishRecord]:
    """Every target this app is live on, keyed by target id."""
    out: dict[str, PublishRecord] = {}
    for target in _read(app_dir):
        rec = load(app_dir, target)
        if rec is not None:
            out[target] = rec
    return out


def save(app_dir: str, record: PublishRecord) -> PublishRecord:
    """Write ``record`` into the app's publish file, replacing that target's entry.

    Stamps ``published_at`` with the current UTC time. Rewrites the whole file
    from the parsed old one, so a target we do not know about (a newer build's,
    or a future provider's) survives the round trip instead of being dropped by
    an older reader.

    Atomic (temp file + :func:`os.replace`): the author may be publishing while
    the app itself is running, and a half-written record reads as no record,
    which is the stranded-origin failure this module exists to prevent.
    """
    targets = dict(_read(app_dir))
    stamped = PublishRecord(
        target=record.target,
        project=record.project,
        url=record.url,
        published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        extra=dict(record.extra),
    )
    targets[record.target] = {
        "project": stamped.project,
        "url": stamped.url,
        "published_at": stamped.published_at,
        "extra": stamped.extra,
    }
    dest = path_for(app_dir)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = f"{dest}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, "targets": targets}, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, dest)
    except OSError as exc:
        raise PublishError(
            f"published, but could not record it in {dest}: {exc}. Re-publishing would "
            "create a SECOND deployment at a new address instead of updating this one, so "
            "fix the permissions on that folder before publishing again."
        ) from exc
    return stamped


def forget(app_dir: str, target: str) -> bool:
    """Drop this app's record for one target. True if there was one to drop.

    Does NOT delete anything at the provider — the deployment stays up and the
    URL keeps working. This is only "stop treating that project as mine", the
    escape hatch for a record pointing at a project the author deleted by hand at
    the provider, where every re-publish would otherwise fail trying to update
    something that is not there.
    """
    targets = dict(_read(app_dir))
    if target not in targets:
        return False
    del targets[target]
    dest = path_for(app_dir)
    tmp = f"{dest}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": VERSION, "targets": targets}, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, dest)
    return True
