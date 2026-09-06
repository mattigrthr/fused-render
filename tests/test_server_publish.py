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

import logging
import os
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from fused_render.publish import runs
from fused_render.publish.adapter import (
    AuthState,
    Capability,
    CyclesReading,
    FundingState,
    IdentityCreated,
    PublishError,
    PublishRecord,
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
    "route",
    [
        "/api/publish/login",
        "/api/publish/deploy",
        "/api/publish/forget",
        "/api/publish/identity",
    ],
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


# ---- funding: the three routes only a paid-for target answers -----------------
#
# An ICP canister costs cycles the author transfers themselves, from their own
# terminal, with no account anywhere to sign into. That is not authentication and
# is not modelled as an AuthState, so it has its own routes — guarded by the
# `FundedTarget` Protocol rather than by a target id, which is what stops the
# Publish page growing an empty funding panel for a provider with nothing to
# fund.


class _FakeFunded(_Fake):
    """A target that costs money. Same publish, three more questions."""

    id = "icp-canister"
    label = "Internet Computer"
    identity = None
    balance = 0
    created = 0

    def auth(self):
        # No login for this one, ever: there is nobody to log in to.
        return AuthState(status="ready", account=self.identity)

    def funding(self):
        return FundingState(
            funded=self.balance >= 1_000_000_000_000,
            identity=self.identity is not None,
            principal=self.identity,
            balance=self.balance if self.identity else None,
            minimum=1_000_000_000_000,
            transfer_command=(
                f"icp cycles transfer 1T {self.identity} -n ic" if self.identity else None
            ),
            detail="fund me",
        )

    def create_identity(self):
        self.created += 1
        self.identity = "un4fu-tqaaa-aaaab-qadjq-cai"
        return IdentityCreated(principal=self.identity, seed_phrase=SEED)

    def cycles(self, record):
        self.reads = getattr(self, "reads", 0) + 1
        return CyclesReading(
            balance=3_000_000_000_000,
            idle_burned_per_day=100_000_000_000,
            read_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    #: Set to fail the way this target fails: the canister is minted, the upload
    #: then runs out of cycles, and the id is real and paid for either way.
    fail_upload = False

    def publish(self, site_dir, *, name, record):
        if self.fail_upload:
            raise PublishError(
                "not enough cycles to finish the upload",
                salvage=PublishRecord(
                    target=self.id,
                    project=name,
                    url=CANISTER_URL,
                    extra={"canister_id": "aaaaa-bbbbb-ccccc-ddddd-eeeee"},
                ),
            )
        return PublishResult(
            url=CANISTER_URL,
            project=name,
            updated_in_place=record is not None,
            extra={"canister_id": "aaaaa-bbbbb-ccccc-ddddd-eeeee"},
        )


SEED = "silk marble tunnel harvest cobalt errand willow ledger"
CANISTER_URL = "https://aaaaa-bbbbb-ccccc-ddddd-eeeee.icp0.io"


@pytest.fixture
def funded_target(monkeypatch):
    target = _FakeFunded()
    monkeypatch.setattr("fused_render.publish.registry._adapters", lambda: (target,))
    monkeypatch.setattr(
        "fused_render.publish.site._vendor_pyodide",
        lambda runtime_dir, packages, *, needed: [],
    )
    return target


def test_a_target_that_needs_no_funding_says_so_rather_than_faking_a_panel(
    tmp_path, fake_target
):
    body = _client(tmp_path).get("/api/publish/funding", params={"target": "cloudflare-pages"})
    assert body.status_code == 400
    assert "does not need funding" in body.json()["error"]


def test_the_plan_says_which_targets_have_funding_at_all(tmp_path, app_dir, funded_target):
    plan = _client(tmp_path).get("/api/publish/plan", params={"path": str(app_dir)}).json()
    (target,) = plan["targets"]
    assert target["funding"] is True
    # And no reading yet, because nothing has been published or fetched.
    assert target["cycles"] is None


def test_reading_the_funding_state_never_creates_an_identity(tmp_path, funded_target):
    body = _client(tmp_path).get("/api/publish/funding", params={"target": "icp-canister"}).json()
    assert body["identity"] is False and body["funded"] is False
    # A key on the author's disk is not a side effect of drawing a panel.
    assert funded_target.created == 0


def test_creating_the_identity_needs_x_fused(tmp_path, funded_target):
    # It mints a credential in the author's OS keyring. A page in a tab must not
    # be able to do that because it was visited.
    resp = _client(tmp_path).post("/api/publish/identity", json={"target": "icp-canister"})
    assert resp.status_code == 403
    assert funded_target.created == 0


def test_the_seed_phrase_crosses_the_boundary_exactly_once(tmp_path, funded_target):
    client = _client(tmp_path)
    created = client.post(
        "/api/publish/identity", json={"target": "icp-canister"}, headers=HEADERS
    ).json()
    assert created["seed_phrase"] == SEED
    assert created["principal"] == "un4fu-tqaaa-aaaab-qadjq-cai"
    # No later call can recover it: funding reports the principal and the
    # balance and has no way to say the phrase again.
    later = client.get("/api/publish/funding", params={"target": "icp-canister"}).json()
    assert SEED not in str(later)
    assert later["identity"] is True
    assert later["principal"] == created["principal"]


def test_the_access_log_records_the_request_line_and_never_the_body(
    tmp_path, funded_target, caplog
):
    # The phrase is in exactly one response body. This server's access log
    # records method, path, status and duration and no body at all
    # (server/common.no_cache_and_log) — a property to hold on purpose, since
    # the whole point of not storing the phrase is undone by logging it.
    with caplog.at_level(logging.INFO):
        _client(tmp_path).post(
            "/api/publish/identity", json={"target": "icp-canister"}, headers=HEADERS
        )
    assert any("/api/publish/identity" in r.getMessage() for r in caplog.records)
    assert not any(SEED in r.getMessage() for r in caplog.records)


def test_the_cycles_readout_is_cached_and_not_refetched_on_every_load(
    tmp_path, app_dir, funded_target
):
    client = _client(tmp_path)
    client.post(
        "/api/publish/deploy",
        json={"path": str(app_dir), "target": "icp-canister"},
        headers=HEADERS,
    )
    _await_run(client, str(app_dir), target="icp-canister")

    params = {"path": str(app_dir), "target": "icp-canister"}
    first = client.get("/api/publish/cycles", params=params).json()
    assert first["cycles"]["balance"] == 3_000_000_000_000
    # Balance ÷ idle burn: the runway is the number, the balance is the detail.
    assert first["cycles"]["days_left"] == 30
    assert first["cycles"]["fresh"] is True

    client.get("/api/publish/cycles", params=params)
    assert funded_target.reads == 1  # the second load painted the cached one

    client.get("/api/publish/cycles", params={**params, "refresh": True})
    assert funded_target.reads == 2

    # And the plan hands the cached reading to the page on first paint, without
    # running a provider CLI of its own.
    plan = client.get("/api/publish/plan", params={"path": str(app_dir)}).json()
    assert plan["targets"][0]["cycles"]["balance"] == 3_000_000_000_000
    assert funded_target.reads == 2


def test_a_refresh_that_fails_keeps_the_last_reading_rather_than_blanking_it(
    tmp_path, app_dir, funded_target, monkeypatch
):
    client = _client(tmp_path)
    client.post(
        "/api/publish/deploy",
        json={"path": str(app_dir), "target": "icp-canister"},
        headers=HEADERS,
    )
    _await_run(client, str(app_dir), target="icp-canister")
    params = {"path": str(app_dir), "target": "icp-canister"}
    client.get("/api/publish/cycles", params=params)

    def boom(self, record):
        raise PublishError("could not reach the network")

    monkeypatch.setattr(_FakeFunded, "cycles", boom)
    body = client.get("/api/publish/cycles", params={**params, "refresh": True}).json()
    # A runway with an "as of" on it beats an empty panel, and the balance is
    # exactly the number an author should not lose sight of.
    assert body["cycles"]["balance"] == 3_000_000_000_000
    assert "could not reach the network" in body["error"]


def test_an_app_that_was_never_published_has_no_balance_to_read(
    tmp_path, app_dir, funded_target
):
    body = _client(tmp_path).get(
        "/api/publish/cycles", params={"path": str(app_dir), "target": "icp-canister"}
    ).json()
    assert body["cycles"] is None
    assert not hasattr(funded_target, "reads")


def test_a_publish_that_fails_after_minting_the_origin_records_it_anyway(
    tmp_path, app_dir, funded_target
):
    # The whole reason PublishError carries a salvage record. The canister
    # exists and cost real cycles; a retry that forgot it would mint a second
    # one at a second origin and strand every reader's saved progress behind an
    # address nobody will open again.
    funded_target.fail_upload = True
    client = _client(tmp_path)
    client.post(
        "/api/publish/deploy",
        json={"path": str(app_dir), "target": "icp-canister"},
        headers=HEADERS,
    )
    run = _await_run(client, str(app_dir), target="icp-canister")
    assert run["state"] == "error"
    assert "not enough cycles" in run["error"]

    plan = client.get("/api/publish/plan", params={"path": str(app_dir)}).json()
    assert plan["targets"][0]["published"]["url"] == CANISTER_URL

    # …and the retry lands on that same canister rather than minting another.
    funded_target.fail_upload = False
    client.post(
        "/api/publish/deploy",
        json={"path": str(app_dir), "target": "icp-canister"},
        headers=HEADERS,
    )
    run = _await_run(client, str(app_dir), target="icp-canister")
    assert run["state"] == "done"
    assert run["result"]["updated_in_place"] is True
    assert run["result"]["url"] == CANISTER_URL
