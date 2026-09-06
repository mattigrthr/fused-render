"""Tests for /api/publish/* (fused_render/server/routers/publish.py).

The routes are thin, so what is worth pinning here is the posture rather than the
plumbing: nothing that reaches the network or opens a browser window happens
without ``X-Fused``, a deploy hands back a run instead of holding the request
open for minutes, and the eligibility report the page renders on load costs no
provider CLI at all.

The provider is faked. What Cloudflare's adapter does with wrangler has its own
tests (``test_publish_cloudflare.py``); what these tests need is a target that
finishes instantly, so the contract under test is the HTTP one.
"""

import os
import time

import pytest
from fastapi.testclient import TestClient

from fused_render.publish import runs
from fused_render.publish.adapter import (
    AuthState,
    Capability,
    PublishResult,
)
from fused_render.server import create_app

HEADERS = {"X-Fused": "1"}


@pytest.fixture(autouse=True)
def _clean_runs():
    runs.reset()
    yield
    runs.reset()


@pytest.fixture
def app_dir(tmp_path):
    """An app that needs Pyodide and saves state — the shape issue #2 is about."""
    d = tmp_path / "hsk-cards"
    d.mkdir()
    (d / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><body>'
        '<script>fused.runPython("./store.py", {})</script></body></html>',
        encoding="utf-8",
    )
    (d / "store.py").write_text(
        "import json\n\n\ndef main():\n"
        "    open('progress.json', 'w').write(json.dumps({}))\n"
        "    return {}\n",
        encoding="utf-8",
    )
    return d


class _Fake:
    """A publish target that succeeds immediately."""

    id = "cloudflare-pages"
    label = "Cloudflare Pages"
    blurb = "fake"
    capabilities = frozenset(
        {
            Capability.RUNTIME_JS,
            Capability.RUNTIME_PYODIDE,
            Capability.STATE_NONE,
            Capability.STATE_CLIENT_LOCAL,
        }
    )
    logged_in = False

    def auth(self):
        if self.logged_in:
            return AuthState(status="ready", account="author@example.com")
        return AuthState(status="needs-login", detail="log in first")

    def login(self):
        self.logged_in = True
        return self.auth()

    def publish(self, site_dir, *, name, record):
        assert os.path.isfile(os.path.join(site_dir, "index.html"))
        return PublishResult(
            url=f"https://{name}.pages.dev", project=name, updated_in_place=record is not None
        )


@pytest.fixture
def fake_target(monkeypatch):
    target = _Fake()
    monkeypatch.setattr("fused_render.publish.registry._adapters", lambda: (target,))
    # The site build's one slow step, and the one thing here that would touch the
    # network. Vendoring has its own tests in test_publish_site.py.
    monkeypatch.setattr(
        "fused_render.publish.site._vendor_pyodide",
        lambda runtime_dir, packages, *, needed: [],
    )
    return target


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _await_run(client, path, target="cloudflare-pages", timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get("/api/publish/run", params={"path": path, "target": target}).json()
        run = body["run"]
        if run and run["state"] != "running":
            return run
        time.sleep(0.05)
    raise AssertionError("the publish never finished")


@pytest.mark.parametrize(
    "route", ["/api/publish/login", "/api/publish/deploy", "/api/publish/forget"]
)
def test_the_mutating_routes_need_x_fused(tmp_path, route):
    # login opens an OAuth window and deploy spends the author's bandwidth:
    # neither may happen because a page in a browser tab was visited.
    assert _client(tmp_path).post(route, json={}).status_code == 403


def test_the_plan_reports_the_grid_and_costs_no_provider_cli(
    tmp_path, app_dir, fake_target, monkeypatch
):
    def _no(self):
        raise AssertionError("the plan must not probe a provider's sign-in state")

    monkeypatch.setattr(_Fake, "auth", _no)
    plan = _client(tmp_path).get("/api/publish/plan", params={"path": str(app_dir)}).json()
    assert plan["runtime"] == Capability.RUNTIME_PYODIDE.value
    assert plan["state"] == Capability.STATE_CLIENT_LOCAL.value
    assert plan["python_files"] == ["store.py"]
    assert plan["capability_labels"][Capability.RUNTIME_PYODIDE.value]
    (target,) = plan["targets"]
    assert target["eligible"] is True
    assert target["reasons"] == []
    assert target["auth"] is None
    assert target["published"] is None
    assert target["run"] is None


def test_a_folder_with_no_app_page_says_what_to_add(tmp_path):
    plain = tmp_path / "notes"
    plain.mkdir()
    (plain / "readme.html").write_text("<html></html>", encoding="utf-8")
    resp = _client(tmp_path).get("/api/publish/plan", params={"path": str(plain)})
    assert resp.status_code == 400
    assert 'meta name="fused-app"' in resp.json()["error"]


def test_relative_paths_are_refused(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/publish/plan", params={"path": "hsk-cards"})
    assert resp.status_code == 400 and "absolute" in resp.json()["error"]
    resp = client.post(
        "/api/publish/deploy", json={"path": "hsk-cards", "target": "x"}, headers=HEADERS
    )
    assert resp.status_code == 400 and "absolute" in resp.json()["error"]


def test_auth_is_its_own_request(tmp_path, fake_target):
    body = (
        _client(tmp_path)
        .get("/api/publish/auth", params={"target": "cloudflare-pages"})
        .json()
    )
    assert body["status"] == "needs-login"
    assert body["detail"] == "log in first"
    assert body["account"] is None


def test_login_takes_no_credential_and_reports_who_we_became(tmp_path, fake_target):
    body = (
        _client(tmp_path)
        .post("/api/publish/login", json={"target": "cloudflare-pages"}, headers=HEADERS)
        .json()
    )
    assert body["status"] == "ready"
    assert body["account"] == "author@example.com"


def test_an_unknown_target_is_never_silently_swapped(tmp_path, fake_target):
    client = _client(tmp_path)
    resp = client.get("/api/publish/auth", params={"target": "icp"})
    assert resp.status_code == 400 and "no publish target" in resp.json()["error"]
    resp = client.post("/api/publish/login", json={"target": "icp"}, headers=HEADERS)
    assert resp.status_code == 400 and "no publish target" in resp.json()["error"]


def test_deploy_returns_a_run_and_then_the_url(tmp_path, app_dir, fake_target):
    fake_target.logged_in = True
    client = _client(tmp_path)
    started = client.post(
        "/api/publish/deploy",
        json={"path": str(app_dir), "target": "cloudflare-pages"},
        headers=HEADERS,
    )
    assert started.status_code == 200, started.text
    # Returns as soon as the run exists: holding the request open for a first
    # publish is a request that times out mid-upload.
    assert started.json()["state"] in ("running", "done")
    assert started.json()["phase_label"]

    run = _await_run(client, str(app_dir))
    assert run["state"] == "done", run["error"]
    assert run["result"]["url"] == "https://hsk-cards.pages.dev"
    assert run["result"]["bytes"] > 0
    # No icon.svg in the app, so the adapter's lettermark stands in — and the run
    # says so, because the Publish page has to disclose it.
    assert run["result"]["icon"] == "generated"


def test_a_completed_publish_is_recorded_in_the_app_and_shown_next_time(
    tmp_path, app_dir, fake_target
):
    fake_target.logged_in = True
    client = _client(tmp_path)
    client.post(
        "/api/publish/deploy",
        json={"path": str(app_dir), "target": "cloudflare-pages"},
        headers=HEADERS,
    )
    _await_run(client, str(app_dir))
    # The record lives with the app, not with the server, because it is what
    # makes the NEXT publish land on the same origin.
    assert (app_dir / ".fused" / "data" / "publish.json").is_file()
    plan = client.get("/api/publish/plan", params={"path": str(app_dir)}).json()
    published = plan["targets"][0]["published"]
    assert published["url"] == "https://hsk-cards.pages.dev"
    assert published["project"] == "hsk-cards"


def test_a_second_publish_updates_the_same_project(tmp_path, app_dir, fake_target):
    fake_target.logged_in = True
    client = _client(tmp_path)
    body = {"path": str(app_dir), "target": "cloudflare-pages"}
    client.post("/api/publish/deploy", json=body, headers=HEADERS)
    _await_run(client, str(app_dir))
    client.post("/api/publish/deploy", json=body, headers=HEADERS)
    run = _await_run(client, str(app_dir))
    assert run["state"] == "done", run["error"]
    assert run["result"]["updated_in_place"] is True
    assert run["result"]["url"] == "https://hsk-cards.pages.dev"


def test_renaming_a_published_app_is_refused_with_the_reason(tmp_path, app_dir, fake_target):
    fake_target.logged_in = True
    client = _client(tmp_path)
    body = {"path": str(app_dir), "target": "cloudflare-pages"}
    client.post("/api/publish/deploy", json=body, headers=HEADERS)
    _await_run(client, str(app_dir))
    client.post("/api/publish/deploy", json={**body, "project": "somewhere-else"}, headers=HEADERS)
    run = _await_run(client, str(app_dir))
    assert run["state"] == "error"
    assert "progress" in run["error"]


def test_an_ineligible_app_is_refused_before_a_run_exists(tmp_path, fake_target):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><body>'
        '<script>fused.writeFile("x.txt", "y")</script></body></html>',
        encoding="utf-8",
    )
    resp = _client(tmp_path).post(
        "/api/publish/deploy",
        json={"path": str(bad), "target": "cloudflare-pages"},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "writeFile" in resp.json()["error"]
    # A refusal is not a failed run: nothing was started, so nothing is polling.
    assert runs.get(str(bad), "cloudflare-pages") is None


def test_bad_include_is_refused(tmp_path, app_dir, fake_target):
    resp = _client(tmp_path).post(
        "/api/publish/deploy",
        json={"path": str(app_dir), "target": "cloudflare-pages", "include": "data.csv"},
        headers=HEADERS,
    )
    assert resp.status_code == 400 and "include" in resp.json()["error"]


def test_forget_drops_the_record_and_touches_nothing_at_the_provider(
    tmp_path, app_dir, fake_target
):
    fake_target.logged_in = True
    client = _client(tmp_path)
    client.post(
        "/api/publish/deploy",
        json={"path": str(app_dir), "target": "cloudflare-pages"},
        headers=HEADERS,
    )
    _await_run(client, str(app_dir))
    body = client.post(
        "/api/publish/forget",
        json={"path": str(app_dir), "target": "cloudflare-pages"},
        headers=HEADERS,
    ).json()
    assert body["forgotten"] is True
    plan = client.get("/api/publish/plan", params={"path": str(app_dir)}).json()
    assert plan["targets"][0]["published"] is None
    # Forgetting twice is not an error; it is just already forgotten.
    again = client.post(
        "/api/publish/forget",
        json={"path": str(app_dir), "target": "cloudflare-pages"},
        headers=HEADERS,
    ).json()
    assert again["forgotten"] is False


def test_polling_an_app_nobody_published_is_not_an_error(tmp_path, app_dir, fake_target):
    body = (
        _client(tmp_path)
        .get("/api/publish/run", params={"path": str(app_dir), "target": "cloudflare-pages"})
        .json()
    )
    assert body["run"] is None
