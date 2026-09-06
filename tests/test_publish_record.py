"""Tests for the per-app publish record (fused_render/publish/record.py).

The record is what makes a re-publish land on the SAME origin, and a rung-1 app's
whole state story is origin-scoped `localStorage`. So the interesting assertions
here are the ones about not losing it: a corrupt file reads as absent rather than
as something wrong, a target we do not know survives a rewrite, and a write that
cannot happen is an error rather than a shrug.
"""

import json
import os

import pytest

from fused_render.publish.adapter import PublishError, PublishRecord
from fused_render.publish import record as rec


def _record(target="cloudflare-pages", project="hsk-cards", url="https://hsk-cards.pages.dev"):
    return PublishRecord(target=target, project=project, url=url)


def test_no_record_before_the_first_publish(tmp_path):
    assert rec.load(str(tmp_path), "cloudflare-pages") is None
    assert rec.load_all(str(tmp_path)) == {}


def test_save_then_load_round_trips_and_stamps_the_time(tmp_path):
    saved = rec.save(str(tmp_path), _record())
    assert saved.published_at  # stamped by save, not by the caller
    got = rec.load(str(tmp_path), "cloudflare-pages")
    assert got == saved
    assert os.path.isfile(tmp_path / ".fused" / "data" / "publish.json")


def test_the_record_lives_under_data_not_cache(tmp_path):
    # `cache/` is deletable at any time (SPEC §47). Losing this file strands every
    # reader's saved progress behind an origin nothing will publish to again.
    rec.save(str(tmp_path), _record())
    assert not (tmp_path / ".fused" / "cache").exists()
    assert (tmp_path / ".fused" / "data" / "publish.json").exists()


def test_one_app_can_be_live_on_several_targets_at_once(tmp_path):
    rec.save(str(tmp_path), _record())
    rec.save(str(tmp_path), _record(target="icp", project="abc", url="https://abc.icp0.io"))
    both = rec.load_all(str(tmp_path))
    assert set(both) == {"cloudflare-pages", "icp"}
    # Publishing to the second must not have disturbed the first's URL.
    assert both["cloudflare-pages"].url == "https://hsk-cards.pages.dev"


def test_a_target_this_build_does_not_know_survives_a_rewrite(tmp_path):
    path = rec.path_for(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"version": 1, "targets": {"from-the-future": {"project": "p", "url": "https://u"}}},
            f,
        )
    rec.save(str(tmp_path), _record())
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert set(data["targets"]) == {"from-the-future", "cloudflare-pages"}


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        '["a list"]',
        '{"version": 99, "targets": {"cloudflare-pages": {"project": "p", "url": "https://u"}}}',
        '{"version": 1, "targets": {"cloudflare-pages": {"project": "p"}}}',
    ],
)
def test_an_unusable_file_reads_as_no_record(tmp_path, content):
    path = rec.path_for(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    assert rec.load(str(tmp_path), "cloudflare-pages") is None


def test_a_record_that_cannot_be_written_is_a_hard_error(tmp_path, monkeypatch):
    # Reporting success while failing to remember WHERE we published is the exact
    # shape of the stranded-origin bug, so it must not degrade to a warning.
    def _boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(rec.os, "makedirs", _boom)
    with pytest.raises(PublishError, match="could not record it"):
        rec.save(str(tmp_path), _record())


def test_forget_drops_one_target_and_reports_whether_there_was_one(tmp_path):
    rec.save(str(tmp_path), _record())
    assert rec.forget(str(tmp_path), "cloudflare-pages") is True
    assert rec.load(str(tmp_path), "cloudflare-pages") is None
    assert rec.forget(str(tmp_path), "cloudflare-pages") is False
