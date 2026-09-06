"""One cached cycles reading per app per target, refreshed at most once a day.

A canister's balance is a network round trip and it does not move meaningfully
between two page loads. Fetching it on every visit to the Publish page would
spend seconds of latency to redraw the same number, so the reading is cached and
the Publish page paints the cached one unless it is older than a day.

**Cache, not data** (SPEC §47). A reading is rebuildable from the provider at any
time by asking again, so it lives under ``<app>/.fused/cache/`` and a sweep that
deletes it costs nothing but one refresh. That is the whole test the split
applies, and it is the reason this file sits beside ``record.py`` rather than
inside it: the record is the canister id, which nothing can reconstruct, and
losing it strands every reader.

**A stale reading is shown with its timestamp, never hidden.** The number is a
runway — balance ÷ idle burn is roughly how many days the app survives untouched
— and a runway with an "as of" on it is more use than a spinner. So this module
always hands back what it has and separately says whether it is fresh; deciding
to refresh is the caller's, and a refresh that fails leaves the old reading in
place rather than blanking the panel.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from fused_render.app_fused_dir import cache_dir
from fused_render.publish.adapter import CyclesReading

FILENAME = "publish-cycles.json"

#: Bumped only for a change a reader must branch on — same contract as
#: ``record.VERSION``. A file from a future version is simply no reading, which
#: costs one refresh.
VERSION = 1

#: How long a reading is treated as current. A day, because that is the scale
#: the number moves on: idle burn is a per-day figure and a canister does not go
#: from comfortable to frozen inside one.
MAX_AGE_S = 24 * 60 * 60


def path_for(app_dir: str) -> str:
    return os.path.join(cache_dir(app_dir), FILENAME)


def _read(app_dir: str) -> dict:
    """The parsed file, or ``{}`` for every way there is nothing to read.

    Absent, unreadable, not JSON, or a version we do not know all collapse to
    "no reading" — every one of them is fixed by asking the provider again,
    which is what makes this cache and not data.
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


def load(app_dir: str, target: str) -> CyclesReading | None:
    """The last reading taken for this app on this target, however old."""
    entry = _read(app_dir).get(target)
    if not isinstance(entry, dict):
        return None
    balance, idle = entry.get("balance"), entry.get("idle_burned_per_day")
    read_at = entry.get("read_at")
    if not isinstance(balance, int) or not isinstance(idle, int):
        return None
    if not isinstance(read_at, str) or not read_at:
        return None
    return CyclesReading(balance=balance, idle_burned_per_day=idle, read_at=read_at)


def save(app_dir: str, target: str, reading: CyclesReading) -> CyclesReading:
    """Write ``reading`` into the app's cache, replacing that target's entry.

    Best-effort, unlike ``record.save``: this is a number we can fetch again, so
    an app folder on read-only media should show the reading it just took rather
    than fail the page that asked for it.
    """
    targets = dict(_read(app_dir))
    targets[target] = {
        "balance": reading.balance,
        "idle_burned_per_day": reading.idle_burned_per_day,
        "read_at": reading.read_at,
    }
    dest = path_for(app_dir)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = f"{dest}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, "targets": targets}, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, dest)
    except OSError:
        pass
    return reading


def age_seconds(reading: CyclesReading, *, now: datetime | None = None) -> float | None:
    """How long ago ``reading`` was taken, or ``None`` if its stamp is unusable.

    An unparseable stamp reads as unknown age and therefore as stale, which
    costs one refresh — the other way round would pin a wrong number on screen
    forever.
    """
    try:
        taken = datetime.fromisoformat(reading.read_at)
    except ValueError:
        return None
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - taken).total_seconds()


def is_fresh(reading: CyclesReading | None, *, now: datetime | None = None) -> bool:
    """Whether the Publish page can paint this without asking the provider."""
    if reading is None:
        return False
    age = age_seconds(reading, now=now)
    return age is not None and 0 <= age < MAX_AGE_S
