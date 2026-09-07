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
    # The REAL shape. `--json` on this one command serialises the human-readable
    # table, so the key is the column heading — not "name", which is what the API
    # and every other wrangler --json command use. A fake that emitted "name"
    # made every test here pass against an adapter that could not recognise a
    # single existing project.
    # "Project Domains" is a joined string of EVERY domain on the project, so a
    # project with a custom domain has two. $subdomains maps a project to the
    # pages.dev host Cloudflare actually gave it, which is not always <name>.
    subs, customs = state.get("subdomains", {}), state.get("custom_domains", {})
    finish(BANNER + json.dumps([
        {"Project Name": n,
         "Project Domains": ", ".join(
             customs.get(n, []) + [subs.get(n, n) + ".pages.dev"])}
        for n in state.get("projects", [])]))
if argv[:3] == ["pages", "project", "create"]:
    name = argv[3]
    if name in state.get("taken", []):
        finish("", 1, "A project with this name already exists.")
    state.setdefault("projects", []).append(name)
    # The real behaviour when the pages.dev subdomain is taken by a STRANGER:
    # the project is still created under the requested name, and quietly gets a
    # suffixed subdomain. Nothing about the exit code says so.
    if name in state.get("subdomain_taken", []):
        state.setdefault("subdomains", {})[name] = name + "-dqd"
    finish(BANNER + "created")
if argv[:2] == ["pages", "deploy"]:
    if state.get("deploy_fails"): finish("", 1, "Upload failed: 413 Payload Too Large")
    state["deployed"] = argv[2]
    proj = argv[argv.index("--project-name") + 1]
    sub = state.get("subdomains", {}).get(proj, proj)
    finish(BANNER + "Deployment complete! https://abc123.%s.pages.dev\n" % sub)
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


@pytest.mark.parametrize("key", ["Project Name", "name"])
def test_an_existing_project_is_recognised_under_either_json_key(key):
    # `wrangler pages project list --json` serialises the human-readable TABLE,
    # so the name arrives under the column heading. Reading only "name" — the
    # API's key, and every other wrangler --json command's — made every existing
    # project look missing, which turned the second publish of an app into
    # "that project no longer exists in this account".
    from fused_render.publish.cloudflare import _project_key

    assert _project_key({key: "chinese-hsk-cards"}) == "chinese-hsk-cards"


@pytest.mark.parametrize("entry", ["chinese-hsk-cards", None, {}, {"Project Name": ""}])
def test_a_project_entry_we_cannot_read_is_not_a_match(entry):
    # Never guess: a shape we do not recognise must read as "no project", which
    # refuses the re-publish loudly, rather than as a match, which would deploy
    # over whatever project the name happened to resolve to.
    from fused_render.publish.cloudflare import _project_key

    assert _project_key(entry) is None


# ---- the address is read back, never predicted ------------------------------
#
# A pages.dev subdomain is unique across ALL of Cloudflare, not within one
# account. Asking for a name a stranger already registered still creates the
# project under that name — with a different subdomain. Guessing the host there
# does not produce a broken link; it produces a WORKING link to someone else's
# site, handed to the author as their own. These are the tests for that.


def test_a_name_taken_elsewhere_returns_the_suffixed_host_cloudflare_assigned(wrangler, site):
    wrangler.update(logged_in=True, subdomain_taken=["pushup-tracker"])
    result = CloudflarePages().publish(site, name="pushup-tracker", record=None)
    # The project keeps the requested name; only the hostname is suffixed.
    assert result.project == "pushup-tracker"
    assert result.url == "https://pushup-tracker-dqd.pages.dev"


def test_a_suffixed_host_is_explained_rather_than_left_looking_like_a_bug(wrangler, site):
    wrangler.update(logged_in=True, subdomain_taken=["pushup-tracker"])
    result = CloudflarePages().publish(site, name="pushup-tracker", record=None)
    note = "\n".join(result.notes)
    assert "pushup-tracker.pages.dev" in note and "pushup-tracker-dqd.pages.dev" in note


def test_an_unsuffixed_host_says_nothing_extra(wrangler, site):
    wrangler.update(logged_in=True)
    result = CloudflarePages().publish(site, name="chinese-hsk-cards", record=None)
    assert not any("already taken" in n for n in result.notes)


def test_a_re_publish_corrects_a_url_that_was_recorded_wrong(wrangler, site):
    # How an app published before this fix heals: the record's stale URL is never
    # trusted, so the next Publish update writes the real one (runs.py rewrites
    # the record from result.url on every publish).
    wrangler.update(
        logged_in=True, projects=["pushup-tracker"], subdomains={"pushup-tracker": "pushup-tracker-dqd"}
    )
    record = PublishRecord(
        target="cloudflare-pages",
        project="pushup-tracker",
        url="https://pushup-tracker.pages.dev",  # the stranger's site
    )
    result = CloudflarePages().publish(site, name="pushup-tracker", record=record)
    assert result.url == "https://pushup-tracker-dqd.pages.dev"
    assert result.updated_in_place is True


def test_a_custom_domain_does_not_displace_the_pages_dev_address(wrangler, site):
    # A custom domain can be detached at Cloudflare. If the share link followed
    # it, every reader's localStorage progress would be stranded on an origin
    # nothing points at any more.
    wrangler.update(
        logged_in=True,
        projects=["chinese-hsk-cards"],
        custom_domains={"chinese-hsk-cards": ["cards.example.com"]},
    )
    record = PublishRecord(
        target="cloudflare-pages",
        project="chinese-hsk-cards",
        url="https://chinese-hsk-cards.pages.dev",
    )
    result = CloudflarePages().publish(site, name="chinese-hsk-cards", record=record)
    assert result.url == "https://chinese-hsk-cards.pages.dev"


def test_the_deploy_alias_carries_the_host_when_the_project_list_is_unreadable(wrangler, site):
    # A deploy that SUCCEEDED must not be reported as a failure because a
    # follow-up lookup did not work. The per-deployment alias has the same
    # subdomain in it, one label along.
    adapter = CloudflarePages()
    wrangler.update(logged_in=True, subdomain_taken=["pushup-tracker"])
    real_projects = adapter._projects
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 2:  # the pre-deploy existence checks still work
            raise PublishError("could not list your Cloudflare Pages projects")
        return real_projects()

    adapter._projects = flaky
    result = adapter.publish(site, name="pushup-tracker", record=None)
    assert result.url == "https://pushup-tracker-dqd.pages.dev"


def test_a_host_with_no_pages_dev_domain_at_all_falls_back_to_the_project_name(wrangler, site):
    # wrangler changing its output shape should degrade to the old guess, not
    # crash a publish that already uploaded.
    adapter = CloudflarePages()
    wrangler.update(logged_in=True)
    adapter._projects = lambda: [{"Project Name": "demo", "Project Domains": ""}]
    adapter._deployment_url = lambda proc: None
    result = adapter.publish(site, name="demo", record=None)
    assert result.url == "https://demo.pages.dev"
