"""Tests for the Cloudflare Pages adapter (fused_render/publish/cloudflare.py).

Driven through a FAKE wrangler on FUSED_RENDER_WRANGLER — a real subprocess with
a scripted binary, rather than a patched method — because the contract being
tested IS the subprocess one: which flags we pass, that we read JSON instead of
scraping a table, and that a non-zero exit becomes the author's error message
rather than a shrug.

The assertion that matters most is the one about a missing project. Losing the
origin loses every reader's saved progress, so "the recorded project is gone"
must stop and say so, never quietly create a replacement.
"""

import json
import os
import stat

import pytest

from fused_render.publish.adapter import PublishError, PublishRecord
from fused_render.publish.cloudflare import CloudflarePages, project_name

FAKE = r'''#!/usr/bin/env python3
"""A wrangler stand-in scripted by $FAKE_WRANGLER_STATE (a JSON file)."""
import json, os, sys

state = json.load(open(os.environ["FAKE_WRANGLER_STATE"]))
argv = sys.argv[1:]
state.setdefault("calls", []).append(argv)

def finish(out="", code=0, err=""):
    json.dump(state, open(os.environ["FAKE_WRANGLER_STATE"], "w"))
    if out: sys.stdout.write(out)
    if err: sys.stderr.write(err)
    sys.exit(code)

# Wrangler always prints a banner before its JSON; so does the fake.
BANNER = " ⛅️ wrangler 4.129.0\n----\n"

if argv[:1] == ["whoami"]:
    if state.get("logged_in"):
        finish(BANNER + json.dumps({"loggedIn": True, "email": "author@example.com"}))
    finish(BANNER + json.dumps({"loggedIn": False}))
if argv[:1] == ["login"]:
    state["logged_in"] = state.get("login_succeeds", True)
    finish(BANNER + "logged in", 0 if state["logged_in"] else 1, "" if state["logged_in"] else "browser closed")
if argv[:3] == ["pages", "project", "list"]:
    if state.get("list_fails"): finish("", 1, "Authentication error [code: 10000]")
    finish(BANNER + json.dumps([{"name": n} for n in state.get("projects", [])]))
if argv[:3] == ["pages", "project", "create"]:
    name = argv[3]
    if name in state.get("taken", []):
        finish("", 1, "A project with this name already exists.")
    state.setdefault("projects", []).append(name)
    finish(BANNER + "created")
if argv[:2] == ["pages", "deploy"]:
    if state.get("deploy_fails"): finish("", 1, "Upload failed: 413 Payload Too Large")
    state["deployed"] = argv[2]
    finish(BANNER + "Deployment complete! https://abc123.%s.pages.dev\n" % state["projects"][-1])
finish("", 1, "unexpected: %s" % argv)
'''


@pytest.fixture
def wrangler(tmp_path, monkeypatch):
    """A scripted fake wrangler. Returns the mutable state dict-on-disk."""
    script = tmp_path / "fake-wrangler"
    script.write_text(FAKE, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    state_path = tmp_path / "state.json"

    class State:
        def __init__(self):
            self.write({"logged_in": False, "projects": []})

        def write(self, data):
            state_path.write_text(json.dumps(data), encoding="utf-8")

        def read(self):
            return json.loads(state_path.read_text(encoding="utf-8"))

        def update(self, **kw):
            data = self.read()
            data.update(kw)
            self.write(data)

    state = State()
    monkeypatch.setenv("FAKE_WRANGLER_STATE", str(state_path))
    monkeypatch.setenv("FUSED_RENDER_WRANGLER", f"{os.sys.executable} {script}")
    return state


@pytest.fixture
def site(tmp_path):
    d = tmp_path / "site"
    d.mkdir()
    (d / "index.html").write_text("<html></html>", encoding="utf-8")
    return str(d)


def test_no_wrangler_anywhere_is_unavailable_not_needs_login(monkeypatch):
    # Different states need different buttons: clicking Log in cannot install a CLI.
    monkeypatch.delenv("FUSED_RENDER_WRANGLER", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    state = CloudflarePages().auth()
    assert state.status == "unavailable"
    assert state.help_url


def test_a_signed_out_wrangler_is_needs_login(wrangler):
    state = CloudflarePages().auth()
    assert state.status == "needs-login" and not state.ready


def test_login_reports_who_we_ended_up_as(wrangler):
    state = CloudflarePages().login()
    assert state.ready and state.account == "author@example.com"


def test_a_login_that_does_not_complete_stays_unauthenticated(wrangler):
    wrangler.update(login_succeeds=False)
    state = CloudflarePages().login()
    assert not state.ready
    assert "browser closed" in state.detail


def test_a_first_publish_creates_the_project_and_returns_the_canonical_url(wrangler, site):
    wrangler.update(logged_in=True)
    result = CloudflarePages().publish(site, name="Chinese HSK Cards", record=None)
    assert result.project == "chinese-hsk-cards"
    # The canonical origin, never the per-deployment alias: that alias pins a
    # reader to one snapshot and splits their saved state onto another origin.
    assert result.url == "https://chinese-hsk-cards.pages.dev"
    assert result.extra["deployment_url"].startswith("https://abc123.")
    assert result.updated_in_place is False
    assert result.notes  # first publish: DNS may take a moment
    calls = wrangler.read()["calls"]
    assert ["pages", "project", "create", "chinese-hsk-cards", "--production-branch", "main"] in calls
    assert wrangler.read()["deployed"] == site


def test_a_re_publish_updates_the_same_project_and_creates_nothing(wrangler, site):
    wrangler.update(logged_in=True, projects=["chinese-hsk-cards"])
    record = PublishRecord(
        target="cloudflare-pages",
        project="chinese-hsk-cards",
        url="https://chinese-hsk-cards.pages.dev",
    )
    result = CloudflarePages().publish(site, name="chinese-hsk-cards", record=record)
    assert result.url == record.url and result.updated_in_place is True
    assert not any(c[:3] == ["pages", "project", "create"] for c in wrangler.read()["calls"])


def test_a_recorded_project_that_is_gone_stops_rather_than_minting_a_new_origin(wrangler, site):
    # The whole reason the record exists. Creating a replacement here would
    # either take the same name (fine) or a different one (every reader's
    # progress stranded, with no warning) — and we cannot tell which in advance.
    wrangler.update(logged_in=True, projects=[])
    record = PublishRecord(target="cloudflare-pages", project="gone", url="https://gone.pages.dev")
    with pytest.raises(PublishError, match="no longer exists"):
        CloudflarePages().publish(site, name="gone", record=record)
    assert not any(c[:2] == ["pages", "deploy"] for c in wrangler.read()["calls"])


def test_publishing_signed_out_refuses_before_touching_anything(wrangler, site):
    with pytest.raises(PublishError, match="not signed in"):
        CloudflarePages().publish(site, name="demo", record=None)
    assert not any(c[:2] == ["pages", "deploy"] for c in wrangler.read()["calls"])


def test_a_taken_project_name_says_so_and_says_what_to_do(wrangler, site):
    wrangler.update(logged_in=True, taken=["demo"])
    with pytest.raises(PublishError) as excinfo:
        CloudflarePages().publish(site, name="demo", record=None)
    assert "already exists" in str(excinfo.value)  # wrangler's own words, passed through
    assert "different one" in str(excinfo.value)  # plus what to do about it


def test_a_failed_upload_surfaces_wranglers_own_message(wrangler, site):
    wrangler.update(logged_in=True, deploy_fails=True)
    with pytest.raises(PublishError, match="413 Payload Too Large"):
        CloudflarePages().publish(site, name="demo", record=None)


def test_a_project_list_we_cannot_read_stops_before_deploying(wrangler, site):
    wrangler.update(logged_in=True, list_fails=True)
    with pytest.raises(PublishError, match="Authentication error"):
        CloudflarePages().publish(site, name="demo", record=None)


@pytest.mark.parametrize(
    "folder,expected",
    [
        ("chinese-hsk-cards", "chinese-hsk-cards"),
        ("Chinese HSK Cards", "chinese-hsk-cards"),
        ("my_app.v2", "my-app-v2"),
        ("2048", "app-2048"),          # Pages rejects a name starting with a digit
        ("---", "fused-app"),          # nothing survives: still publishable
        ("x" * 80, "x" * 58),          # the hostname label limit
    ],
)
def test_the_project_name_is_a_hostname_the_author_will_recognise(folder, expected):
    assert project_name(folder) == expected


def test_the_banner_wrangler_prints_before_its_json_is_ignored(wrangler):
    # Wrangler always prefixes a banner, and in some environments a proxy
    # warning. Scraping would break on that; finding the JSON does not.
    wrangler.update(logged_in=True)
    assert CloudflarePages().auth().account == "author@example.com"
