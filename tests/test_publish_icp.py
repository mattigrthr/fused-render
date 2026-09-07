"""Tests for the ICP asset-canister adapter (fused_render/publish/icp.py).

Driven through a FAKE ``icp`` on ``FUSED_RENDER_ICP`` — a real subprocess with a
scripted binary, rather than a patched method — for the reason
``test_publish_cloudflare.py`` gives: the contract being tested IS the subprocess
one. Which flags we pass, that the canister id comes out of icp-cli's own
mapping file instead of a scrape, and that a failure becomes something the author
can act on.

Two assertions matter more than the rest.

**The canister id survives a failed upload.** ``icp deploy`` creates the canister
and only then uploads into it, so an upload that runs out of cycles leaves a real
canister that real cycles paid for. If that id is dropped, the retry mints a
second one at a second origin and every reader's saved progress is orphaned —
the silent-data-loss failure the publish record exists to prevent, reached by way
of an error message.

**The seed phrase is shown once and retained nowhere.** Not in a file that
outlives the call, not in a keyring of ours, not in the publish record.
"""

import json
import os
import stat

import pytest

from fused_render.publish.adapter import PublishError, PublishRecord
from fused_render.publish.icp import (
    MINIMUM_CYCLES,
    Icp,
    canister_name,
    format_cycles,
)

FAKE = r'''#!/usr/bin/env python3
"""An icp stand-in scripted by $FAKE_ICP_STATE (a JSON file)."""
import json, os, sys

state = json.load(open(os.environ["FAKE_ICP_STATE"]))
argv = sys.argv[1:]
state.setdefault("calls", []).append(argv)

def finish(out="", code=0, err=""):
    json.dump(state, open(os.environ["FAKE_ICP_STATE"], "w"))
    if out: sys.stdout.write(out)
    if err: sys.stderr.write(err)
    sys.exit(code)

PRINCIPAL = "un4fu-tqaaa-aaaab-qadjq-cai"
IDS = os.path.join(".icp", "data", "mappings", "ic.ids.json")

if argv[:2] == ["identity", "principal"]:
    if not state.get("identity"): finish("", 1, "identity 'fused-render' does not exist")
    finish(PRINCIPAL + "\n")
if argv[:2] == ["identity", "new"]:
    if state.get("keyring_locked"):
        finish("", 1, "failed to store the key: the keyring is locked")
    state["identity"] = True
    # The real CLI writes the phrase to --output-seed and prints nothing of it.
    seed = argv[argv.index("--output-seed") + 1]
    # From the environment, never from the state file: the leak test scans every
    # file this fixture leaves on disk, and a phrase the fake itself persisted
    # would be indistinguishable from one the adapter failed to forget.
    open(seed, "w").write(os.environ.get("FAKE_ICP_SEED", "abandon ability able about"))
    finish("Created identity 'fused-render'.\n")
if argv[:2] == ["cycles", "balance"]:
    if state.get("balance") is None: finish("", 1, "could not reach the network")
    finish("%s cycles.\n" % state["balance"])
if argv[:2] == ["canister", "status"]:
    if state.get("status_fails"): finish("", 1, "canister not found")
    # Both shapes are icp 1.4's, verbatim, on a canister the caller controls:
    # underscore-separated STRINGS in --json, and a table whose balance field is
    # called "Cycles" while two other lines also contain that word.
    def sep(n): return "{:_}".format(int(n)).replace(",", "_")
    cyc, idle = state.get("canister_cycles", 0), state.get("idle", 0)
    if state.get("status_json", True):
        finish(json.dumps({"id": "aaaaa-bbbbb-ccccc-ddddd-eeeee", "status": "Running",
                           "settings": {"reserved_cycles_limit": sep(5_000_000_000_000)},
                           "cycles": sep(cyc), "reserved_cycles": "0",
                           "idle_cycles_burned_per_day": sep(idle)}))
    # Older builds have no --json and print the table instead.
    finish("Canister Status Report:\n  Status: Running\n"
           "  Reserved cycles limit: %s\n  Cycles: %s\n  Reserved cycles: 0\n"
           "  Idle cycles burned per day: %s\n" % (sep(5_000_000_000_000), sep(cyc), sep(idle)))
if argv[:1] == ["deploy"]:
    # The canister is created BEFORE the upload: the mapping lands either way.
    if not os.path.exists(IDS):
        os.makedirs(os.path.dirname(IDS), exist_ok=True)
        manifest = open("icp.yaml").read()
        name = [l.split("name:")[1].strip() for l in manifest.splitlines() if "name:" in l][0]
        json.dump({name: state.get("mint", "aaaaa-bbbbb-ccccc-ddddd-eeeee")}, open(IDS, "w"))
        state["created"] = True
    else:
        state["reused"] = json.load(open(IDS))
    if state.get("upload_fails"):
        finish("", 1, "Error: insufficient cycles to install the asset canister")
    if state.get("deploy_fails"):
        finish("", 1, "Error: the replica rejected the request")
    state["deployed_manifest"] = open("icp.yaml").read()
    finish("Deployed.\n")
finish("", 1, "unexpected: %s" % argv)
'''


@pytest.fixture
def icp(tmp_path, monkeypatch):
    """A scripted fake icp. Returns the mutable state dict-on-disk."""
    script = tmp_path / "fake-icp"
    script.write_text(FAKE, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    state_path = tmp_path / "icp-state.json"

    class State:
        def __init__(self):
            self.write({"identity": False, "balance": None})

        def write(self, data):
            state_path.write_text(json.dumps(data), encoding="utf-8")

        def read(self):
            return json.loads(state_path.read_text(encoding="utf-8"))

        def update(self, **kw):
            data = self.read()
            data.update(kw)
            self.write(data)

    state = State()
    monkeypatch.setenv("FAKE_ICP_STATE", str(state_path))
    monkeypatch.setenv("FUSED_RENDER_ICP", f"{os.sys.executable} {script}")
    return state


@pytest.fixture
def funded(icp):
    icp.update(identity=True, balance=2_000_000_000_000)
    return icp


@pytest.fixture
def site(tmp_path):
    d = tmp_path / "site"
    d.mkdir()
    (d / "index.html").write_text("<html></html>", encoding="utf-8")
    return str(d)


# ---- availability ------------------------------------------------------------


def test_no_icp_anywhere_is_unavailable_with_somewhere_to_go(monkeypatch):
    # The npm fallback is what keeps this target's "no signup, no card" pitch
    # from being followed by "first install a Rust CLI" — but a machine with
    # neither icp nor Node still has to be told, not left to fail mid-publish.
    monkeypatch.delenv("FUSED_RENDER_ICP", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    state = Icp().auth()
    assert state.status == "unavailable"
    assert state.help_url


def test_node_alone_is_enough_because_icp_cli_publishes_to_npm(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_ICP", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npx" if name == "npx" else None)
    cmd = Icp().icp()
    assert cmd[:2] == ["/usr/bin/npx", "--yes"]
    # Pinned to a major, exactly as the Cloudflare adapter pins wrangler@4.
    assert cmd[2].startswith("@icp-sdk/icp-cli@")


def test_a_real_icp_on_path_wins_over_npx(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_ICP", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")
    assert Icp().icp() == ["/usr/local/bin/icp"]


# ---- auth is not the question here -------------------------------------------


def test_there_is_no_needs_login_state_because_there_is_nobody_to_log_in_to(icp):
    # An author with an icp and no identity is READY: the identity is created on
    # the first Fund cycles press, so its absence is a funding question, not an
    # auth one. A "needs-login" here would put a Log in button on a target with
    # no account to log into.
    assert Icp().auth().status == "ready"
    icp.update(identity=True)
    assert Icp().auth().status == "ready"


def test_a_ready_state_names_the_principal_it_will_publish_as(funded):
    assert Icp().auth().account == "un4fu-tqaaa-aaaab-qadjq-cai"


def test_login_is_a_no_op_that_reports_the_real_state(funded):
    # The Protocol requires the method and the page never offers the button;
    # answering with the truth beats refusing.
    assert Icp().login().ready
    assert not any(c[:1] == ["login"] for c in funded.read()["calls"])


# ---- funding -----------------------------------------------------------------


def test_funding_does_not_create_an_identity_just_because_a_page_was_drawn(icp):
    # It is called to draw a panel. Creating a key on the author's disk as a
    # side effect of rendering would be the worst possible reading of "read-only".
    state = Icp().funding()
    assert state.identity is False and state.funded is False
    assert not any(c[:2] == ["identity", "new"] for c in icp.read()["calls"])


def test_funding_reports_the_principal_the_balance_and_the_command_to_run(funded):
    state = Icp().funding()
    assert state.identity and state.funded
    assert state.principal == "un4fu-tqaaa-aaaab-qadjq-cai"
    assert state.balance == 2_000_000_000_000
    # The transfer happens in the author's own terminal. fused-render has no
    # key and no wallet and never moves their money.
    assert state.transfer_command == "icp cycles transfer 1T un4fu-tqaaa-aaaab-qadjq-cai -n ic"


def test_an_identity_with_nothing_in_it_is_not_funded(icp):
    icp.update(identity=True, balance=1_000_000)
    state = Icp().funding()
    assert state.identity is True and state.funded is False
    assert state.minimum == MINIMUM_CYCLES


def test_creating_the_identity_returns_the_phrase_and_leaves_no_copy(icp, tmp_path, monkeypatch):
    import tempfile

    phrase = "silk marble tunnel harvest cobalt errand willow ledger"
    monkeypatch.setenv("FAKE_ICP_SEED", phrase)
    made: list[str] = []
    real_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(
        tempfile, "mkdtemp", lambda **kw: made.append(real_mkdtemp(**kw)) or made[-1]
    )

    created = Icp().create_identity()
    assert created.principal == "un4fu-tqaaa-aaaab-qadjq-cai"
    assert created.seed_phrase == phrase

    # The one moment the phrase exists. Nothing may hold it afterwards — not a
    # file that outlives the call, not a keyring item of ours, not the record.
    assert made and not any(os.path.exists(d) for d in made)
    leaked = [
        f
        for f in tmp_path.rglob("*")
        if f.is_file() and phrase in f.read_text(errors="replace")
    ]
    assert leaked == []


def test_the_phrase_is_read_from_a_file_and_never_off_stdout(icp):
    Icp().create_identity()
    new = [c for c in icp.read()["calls"] if c[:2] == ["identity", "new"]][0]
    # Every command here goes through a formatter that hands the author the whole
    # captured blob on a non-zero exit. A phrase on stdout is one bad exit away
    # from an error message, a log, and a pasted issue report.
    assert "--output-seed" in new
    assert "-q" not in new
    # Keyring, not password-encrypted (a prompt on every publish) and not
    # plaintext (a key that moves real money in a readable file). The flag is
    # `--storage`; icp 1.4 rejects `--storage-mode` outright, which is how this
    # shipped broken — every Fund cycles press failed on an argument error.
    assert new[new.index("--storage") + 1] == "keyring"


def test_a_locked_keyring_says_so_and_does_not_fall_back_to_plaintext(icp):
    icp.update(keyring_locked=True)
    with pytest.raises(PublishError) as excinfo:
        Icp().create_identity()
    assert "keyring" in str(excinfo.value)
    assert "readable file" in str(excinfo.value)  # and why we will not do that


def test_creating_a_second_identity_is_refused_rather_than_shadowing_the_first(funded):
    # One identity, Fused-wide: one principal, one balance, funded once. A second
    # would strand the cycles the author already transferred to the first.
    with pytest.raises(PublishError, match="already exists"):
        Icp().create_identity()


# ---- publishing --------------------------------------------------------------


def test_a_first_publish_returns_the_gateway_url_for_the_minted_canister(funded, site):
    result = Icp().publish(site, name="Chinese HSK Cards", record=None)
    assert result.url == "https://aaaaa-bbbbb-ccccc-ddddd-eeeee.icp0.io"
    assert result.extra["canister_id"] == "aaaaa-bbbbb-ccccc-ddddd-eeeee"
    assert result.project == "chinese-hsk-cards"
    assert result.updated_in_place is False
    assert any("frozen" in n for n in result.notes)  # the dead man's switch, said once
    assert funded.read()["deployed_manifest"].count(site) == 1


def test_the_deploy_targets_mainnet_and_never_a_local_replica(funded, site):
    Icp().publish(site, name="demo", record=None)
    deploy = [c for c in funded.read()["calls"] if c[:1] == ["deploy"]][0]
    assert deploy[:3] == ["deploy", "--environment", "ic"]


def test_every_command_that_spends_or_reads_runs_as_our_own_identity(funded, site):
    # icp acts as the DEFAULT identity unless told otherwise, and an author who
    # publishes from this machine almost certainly has one of their own. Without
    # --identity the pre-flight check would read someone else's wallet, the
    # panel would print a principal the balance beside it does not belong to,
    # and the deploy would mint the canister under a key fused-render cannot
    # reach again.
    icp = Icp()
    icp.publish(site, name="demo", record=None)
    icp.balance()
    icp.cycles(PublishRecord(target=icp.id, project="demo", url="",
                             extra={"canister_id": "aaaaa-bbbbb-ccccc-ddddd-eeeee"}))
    for call in funded.read()["calls"]:
        assert call[call.index("--identity") + 1] == "fused-render", call


def test_the_deploy_never_waits_on_a_prompt(funded, site):
    # stdin is closed, so a confirmation prompt is not a question — it is a
    # subprocess that sits there until the 30-minute timeout, which the Publish
    # page renders as "Uploading to the provider" for half an hour.
    Icp().publish(site, name="demo", record=None)
    deploy = [c for c in funded.read()["calls"] if c[:1] == ["deploy"]][0]
    assert "--yes" in deploy


def test_the_principal_is_asked_for_by_flag_because_there_is_no_positional_form(icp):
    icp.update(identity=True)
    Icp().funding()
    ask = [c for c in icp.read()["calls"] if c[:2] == ["identity", "principal"]][0]
    # `icp identity principal fused-render` is an argument error in 1.4, and a
    # bare `icp identity principal` answers for whichever identity is default.
    assert ask == ["identity", "principal", "--identity", "fused-render"]


def test_a_re_publish_upgrades_the_same_canister_rather_than_minting_a_second(funded, site):
    # The single most important correctness requirement on this target. Our
    # project directory is synthesized per publish and thrown away, so the
    # record is the ONLY thing that remembers the id — a re-publish that forgot
    # it would mint a new canister, a new origin, and silently wipe every
    # reader's progress.
    record = PublishRecord(
        target="icp-canister",
        project="demo",
        url="https://zzzzz-yyyyy-xxxxx-wwwww-vvvvv.icp0.io",
        extra={"canister_id": "zzzzz-yyyyy-xxxxx-wwwww-vvvvv"},
    )
    funded.update(mint="never-should-be-minted")
    result = Icp().publish(site, name="demo", record=record)
    assert result.url == record.url
    assert result.updated_in_place is True
    assert funded.read()["reused"] == {"demo": "zzzzz-yyyyy-xxxxx-wwwww-vvvvv"}
    assert "created" not in funded.read()


def test_an_upload_that_runs_out_of_cycles_keeps_the_canister_it_already_paid_for(funded, site):
    # The canister exists and cost real money by the time the upload fails.
    # Losing its id here would cost the author their origin by way of an error
    # message, so it comes out on the exception for runs.py to record.
    funded.update(upload_fails=True)
    with pytest.raises(PublishError) as excinfo:
        Icp().publish(site, name="demo", record=None)
    salvage = excinfo.value.salvage
    assert salvage is not None
    assert salvage.extra["canister_id"] == "aaaaa-bbbbb-ccccc-ddddd-eeeee"
    assert salvage.url == "https://aaaaa-bbbbb-ccccc-ddddd-eeeee.icp0.io"


def test_running_out_of_cycles_is_surfaced_as_itself_not_as_cli_stderr(funded, site):
    funded.update(upload_fails=True)
    with pytest.raises(PublishError) as excinfo:
        Icp().publish(site, name="demo", record=None)
    message = str(excinfo.value)
    # Their principal and the exact command, not the replica's word for it.
    assert "un4fu-tqaaa-aaaab-qadjq-cai" in message
    assert "icp cycles transfer" in message
    # And that the retry finishes the canister they already have.
    assert "same canister" in message


def test_any_other_deploy_failure_still_passes_icps_own_words_through(funded, site):
    funded.update(deploy_fails=True)
    with pytest.raises(PublishError, match="replica rejected the request"):
        Icp().publish(site, name="demo", record=None)


def test_an_unfunded_first_publish_stops_before_spending_anything(icp, site):
    icp.update(identity=True, balance=1_000_000)
    with pytest.raises(PublishError) as excinfo:
        Icp().publish(site, name="demo", record=None)
    assert "icp cycles transfer" in str(excinfo.value)
    assert not any(c[:1] == ["deploy"] for c in icp.read()["calls"])


def test_a_balance_we_cannot_read_lets_the_deploy_try_rather_than_refusing(icp, site):
    # None is not zero. Refusing a publish the author has already paid for
    # because the network hiccuped is worse than letting the deploy report its
    # own failure, which it does in the author's terms either way.
    icp.update(identity=True, balance=None)
    result = Icp().publish(site, name="demo", record=None)
    assert result.url.endswith(".icp0.io")


def test_a_re_publish_is_not_blocked_by_a_low_balance(funded, site):
    # The pre-flight minimum is about CREATING a canister. An app that already
    # has one should reach the deploy, which knows what an upgrade actually
    # costs — refusing here would strand a published app behind our estimate.
    funded.update(balance=1_000)
    record = PublishRecord(
        target="icp-canister",
        project="demo",
        url="https://zzzzz-yyyyy-xxxxx-wwwww-vvvvv.icp0.io",
        extra={"canister_id": "zzzzz-yyyyy-xxxxx-wwwww-vvvvv"},
    )
    assert Icp().publish(site, name="demo", record=record).updated_in_place


def test_publishing_without_an_icp_at_all_refuses_before_building_anything(monkeypatch, site):
    monkeypatch.delenv("FUSED_RENDER_ICP", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(PublishError, match="not installed"):
        Icp().publish(site, name="demo", record=None)


# ---- the cycles readout ------------------------------------------------------


def test_the_readout_is_a_runway_not_just_a_balance(funded):
    funded.update(canister_cycles=3_000_000_000_000, idle=100_000_000_000)
    record = PublishRecord(
        target="icp-canister", project="demo", url="https://x.icp0.io",
        extra={"canister_id": "aaaaa-bbbbb-ccccc-ddddd-eeeee"},
    )
    reading = Icp().cycles(record)
    assert reading.balance == 3_000_000_000_000
    assert reading.idle_burned_per_day == 100_000_000_000
    assert reading.days_left == 30


def test_a_status_without_json_is_read_off_the_table_instead(funded):
    # --json is not on every command in every build, so nothing here depends on
    # it existing: the fallback hunts for the field by name.
    funded.update(status_json=False, canister_cycles=2_500_000_000_000, idle=50_000_000_000)
    record = PublishRecord(
        target="icp-canister", project="demo", url="https://x.icp0.io",
        extra={"canister_id": "aaaaa-bbbbb-ccccc-ddddd-eeeee"},
    )
    reading = Icp().cycles(record)
    assert reading.balance == 2_500_000_000_000
    assert reading.idle_burned_per_day == 50_000_000_000


def test_nothing_burned_is_an_unknown_runway_rather_than_forever(funded):
    funded.update(canister_cycles=1_000_000_000_000, idle=0)
    record = PublishRecord(
        target="icp-canister", project="demo", url="https://x.icp0.io",
        extra={"canister_id": "aaaaa-bbbbb-ccccc-ddddd-eeeee"},
    )
    assert Icp().cycles(record).days_left is None


def test_a_record_with_no_canister_has_no_balance_to_read(funded):
    record = PublishRecord(target="icp-canister", project="demo", url="https://x.icp0.io")
    with pytest.raises(PublishError, match="no canister"):
        Icp().cycles(record)


# ---- the small pure things ---------------------------------------------------


@pytest.mark.parametrize(
    "folder,expected",
    [
        ("chinese-hsk-cards", "chinese-hsk-cards"),
        ("Chinese HSK Cards", "chinese-hsk-cards"),
        ("my_app.v2", "my-app-v2"),
        ("2048", "app-2048"),
        ("---", "fused-app"),
    ],
)
def test_the_canister_name_is_the_app_name_the_author_will_recognise(folder, expected):
    # Not the hostname — that is the canister id, which the network assigns —
    # but it is what they will see in `icp canister status`.
    assert canister_name(folder) == expected


@pytest.mark.parametrize(
    "amount,expected",
    [
        (1_000_000_000_000, "1T"),
        (2_500_000_000_000, "2.5T"),
        (850_000_000_000, "850B"),
        (12_000_000, "12M"),
        (999, "999"),
    ],
)
def test_cycles_are_formatted_in_the_units_the_transfer_command_accepts(amount, expected):
    # Fifteen-digit numbers do not compare. The suffixes are icp-cli's own, so a
    # figure read on the page can be typed back into the terminal.
    assert format_cycles(amount) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # What icp 1.4 actually prints, in the human line and inside --json
        # alike — checked against the installed binary.
        ("Balance: 2_000_602_400_000 cycles", 2_000_602_400_000),
        ('{"balance":"2_000_602_400_000 cycles"}', 2_000_602_400_000),
        # A funded-looking zero. Reading this as "could not read" would skip the
        # pre-flight check on the one principal that most needs it, and send the
        # author into a deploy that fails after minting the canister.
        ('{"balance":"0 cycles"}', 0),
        ("3_100_000_000_000 cycles", 3_100_000_000_000),
        ("Balance: 2.5 TC", 2_500_000_000_000),
        ("1T", 1_000_000_000_000),
        ("500 M", 500_000_000),
        ("nothing here", None),
    ],
)
def test_a_cycles_figure_is_read_out_of_whatever_icp_printed(text, expected):
    from fused_render.publish.icp import _cycles_from_text

    assert _cycles_from_text(text) == expected


# The two shapes icp 1.4 actually prints for a canister its caller CONTROLS,
# copied from a real report. Worth pinning verbatim: a caller who is not a
# controller gets the public state tree instead, which carries no cycles at all,
# so this is the one output shape that cannot be checked without owning one.
_REAL_STATUS_JSON = (
    '{"id":"7m5ru-hiaaa-aaaab-qe6hq-cai","status":"Running","settings":{"controllers":'
    '["kl6gl-oaqam-gwxjv-nig2x-lfoba-lbgn3-k3ku6-osrqf-de6xi-4rctk-vae"],'
    '"freezing_threshold":"2_592_000","reserved_cycles_limit":"5_000_000_000_000",'
    '"wasm_memory_limit":"3_221_225_472"},"memory_size":"2_349_181_748",'
    '"cycles":"3_835_149_822_819","reserved_cycles":"0",'
    '"idle_cycles_burned_per_day":"60_880_991_301"}'
)

_REAL_STATUS_TABLE = """Canister Id: 7m5ru-hiaaa-aaaab-qe6hq-cai
Canister Status Report:
  Status: Running
  Freezing threshold: 2_592_000
  Reserved cycles limit: 5_000_000_000_000
  Memory size: 2_349_181_748
  Cycles: 3_835_260_886_361
  Reserved cycles: 0
  Idle cycles burned per day: 60_880_991_301
"""


def test_the_json_report_is_read_as_the_strings_icp_actually_emits():
    # Every figure is a STRING with underscore separators, not a number. Reading
    # it as JSON and expecting an int gets nothing.
    from fused_render.publish.icp import _first_number

    assert _first_number(json.loads(_REAL_STATUS_JSON), "cycles") == 3_835_149_822_819
    assert (
        _first_number(json.loads(_REAL_STATUS_JSON), "idle_cycles_burned_per_day")
        == 60_880_991_301
    )


def test_the_table_balance_is_the_cycles_line_and_not_the_two_that_rhyme_with_it():
    # "Reserved cycles limit" is 5T on a canister holding 3.8T, and "Idle cycles
    # burned per day" is a rate. An unanchored search for the word would return
    # one of them — a number that is not wrong so much as about something else,
    # which is the kind of readout an author acts on.
    reading = Icp()._labelled(_REAL_STATUS_TABLE, r"^[ \t]*(?:cycles|(?:cycle[s]? )?balance)")
    assert reading == 3_835_260_886_361


def test_a_canister_with_nothing_left_reads_as_zero_not_as_unreadable():
    # The difference between "0 cycles, this is about to be deleted with every
    # reader's progress in it" and a blank panel.
    from fused_render.publish.icp import _field_number

    assert _field_number("0") == 0
    assert _field_number("0 cycles") == 0
    assert _field_number("3_835_149_822_819") == 3_835_149_822_819
    assert _field_number("not a number") is None


@pytest.mark.parametrize(
    "entry,expected",
    [
        ({"demo": "aaaaa-bbbbb-ccccc-ddddd-eeeee"}, "aaaaa-bbbbb-ccccc-ddddd-eeeee"),
        # The mapping file gains fields; an id that moved inside an object is
        # still the id, and missing it costs a second canister on the next try.
        ({"demo": {"id": "aaaaa-bbbbb-ccccc-ddddd-eeeee"}}, "aaaaa-bbbbb-ccccc-ddddd-eeeee"),
        ({"demo": "not-a-canister-id"}, None),
        ({}, None),
    ],
)
def test_the_canister_id_is_found_by_shape_not_by_a_fixed_path(tmp_path, entry, expected):
    from fused_render.publish.icp import _IDS_FILE

    ids = tmp_path / _IDS_FILE
    ids.parent.mkdir(parents=True)
    ids.write_text(json.dumps(entry), encoding="utf-8")
    assert Icp._read_id(str(tmp_path), "demo") == expected


@pytest.mark.parametrize("value", ["not-an-id", "", None, 12, {"canister_id": 1}])
def test_a_record_extra_we_cannot_read_is_no_canister_at_all(value):
    # Never guess. A malformed id read as real would deploy at an address that
    # does not exist; read as absent it mints one and records it, which is
    # recoverable.
    record = PublishRecord(
        target="icp-canister", project="demo", url="https://x.icp0.io",
        extra={"canister_id": value},
    )
    assert Icp._recorded_id(record) is None


def test_a_record_that_names_no_canister_stops_rather_than_minting_a_new_origin(funded, site):
    # The ICP twin of Cloudflare's "that project no longer exists". Here the id
    # is the origin and nothing derives it from the app's name, so a record we
    # cannot read means we do not know which canister is this app's — and
    # deploying anyway would silently give every reader a new address whose
    # saved progress the old one cannot see.
    record = PublishRecord(target="icp-canister", project="demo", url="https://x.icp0.io")
    with pytest.raises(PublishError, match="does not name a canister"):
        Icp().publish(site, name="demo", record=record)
    assert not any(c[:1] == ["deploy"] for c in funded.read()["calls"])
