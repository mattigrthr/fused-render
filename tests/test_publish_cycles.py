"""Tests for the cycles cache (fused_render/publish/cycles.py).

The rule this file exists to hold is the SPEC §47 one: a cycles reading is
rebuildable by asking the provider again, so it is cache and not data, and every
way of failing to read it must collapse to "no reading" rather than to a wrong
number. The cost of the first is one network round trip; the cost of the second
is an author who believes their canister has a year of runway when it has a week.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from fused_render.publish import cycles
from fused_render.publish.adapter import CyclesReading

TARGET = "icp-canister"


def reading(**kw) -> CyclesReading:
    return CyclesReading(
        balance=kw.get("balance", 3_000_000_000_000),
        idle_burned_per_day=kw.get("idle", 100_000_000_000),
        read_at=kw.get(
            "read_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
        ),
    )


def test_a_reading_round_trips_through_the_app_folder(tmp_path):
    cycles.save(str(tmp_path), TARGET, reading())
    back = cycles.load(str(tmp_path), TARGET)
    assert back.balance == 3_000_000_000_000
    assert back.idle_burned_per_day == 100_000_000_000


def test_it_lands_under_cache_because_asking_again_rebuilds_it(tmp_path):
    # Not data/: nothing here is lost by a cache sweep, unlike the publish
    # record, which is the only thing that knows which canister is this app's.
    cycles.save(str(tmp_path), TARGET, reading())
    assert ".fused/cache/" in cycles.path_for(str(tmp_path)).replace(os.sep, "/")


def test_one_target_does_not_clobber_another(tmp_path):
    cycles.save(str(tmp_path), TARGET, reading(balance=1))
    cycles.save(str(tmp_path), "another", reading(balance=2))
    assert cycles.load(str(tmp_path), TARGET).balance == 1
    assert cycles.load(str(tmp_path), "another").balance == 2


def test_no_file_is_no_reading_rather_than_an_error(tmp_path):
    assert cycles.load(str(tmp_path), TARGET) is None


@pytest.mark.parametrize("body", ["not json", "[]", '{"version": 99, "targets": {}}'])
def test_anything_unreadable_is_no_reading(tmp_path, body):
    cycles.save(str(tmp_path), TARGET, reading())
    with open(cycles.path_for(str(tmp_path)), "w", encoding="utf-8") as f:
        f.write(body)
    assert cycles.load(str(tmp_path), TARGET) is None


@pytest.mark.parametrize(
    "entry",
    [
        {"balance": "lots", "idle_burned_per_day": 1, "read_at": "2026-01-01T00:00:00+00:00"},
        {"balance": 1, "read_at": "2026-01-01T00:00:00+00:00"},
        {"balance": 1, "idle_burned_per_day": 1},
    ],
)
def test_a_half_written_entry_is_no_reading(tmp_path, entry):
    path = cycles.path_for(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": cycles.VERSION, "targets": {TARGET: entry}}, f)
    assert cycles.load(str(tmp_path), TARGET) is None


def test_a_reading_taken_now_is_fresh_and_one_from_last_week_is_not():
    assert cycles.is_fresh(reading())
    old = reading(
        read_at=(datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    )
    assert not cycles.is_fresh(old)
    # Stale is not gone: the page still paints it, with its timestamp. A runway
    # with an "as of" beats a spinner.
    assert cycles.age_seconds(old) > cycles.MAX_AGE_S


def test_a_stamp_we_cannot_parse_reads_as_stale_not_as_current():
    # The other way round would pin a wrong number on screen forever; this way
    # costs one refresh.
    assert not cycles.is_fresh(reading(read_at="last tuesday"))
    assert cycles.age_seconds(reading(read_at="last tuesday")) is None


def test_a_reading_stamped_in_the_future_is_not_fresh():
    ahead = reading(
        read_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec="seconds")
    )
    assert not cycles.is_fresh(ahead)


def test_a_cache_we_cannot_write_does_not_fail_the_page_that_asked(tmp_path, monkeypatch):
    # Unlike record.save. This is a number we can fetch again, so an app folder
    # on read-only media should still show the reading it just took.
    def boom(*a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr("os.makedirs", boom)
    assert cycles.save(str(tmp_path), TARGET, reading()).balance == 3_000_000_000_000
