"""An ICP asset canister, through the author's own ``icp``.

The second adapter, and the reason the first one's design counts as a seam. An
adapter interface validated against exactly one backend is not a seam, it is
that backend's assumptions with indirection in front — so this target was built
alongside Cloudflare rather than after it.

What it publishes to is the only target on the grid with **no provider account
at all**. No signup, no card, no plan, and no company that can close the
author's account or delete their app. The bundle is served over HTTPS by the
Internet Computer's HTTP gateway at ``https://<canister-id>.icp0.io``, and the
reader needs a stock browser — no wallet, no extension, nothing installed.

Each canister gets its own subdomain, therefore its own origin, therefore its
own ``localStorage``. That works in our favour: a rung-1 app's whole state story
is origin-scoped, and here the origin belongs to one app and nothing else.

**The two WebAssembly layers never meet**, and they invite confusion. Pyodide's
WebAssembly runs in the *reader's browser*; the canister's WebAssembly runs
on-chain. At rung 1 the canister is purely a file server and knows nothing about
Python.

## ``icp``, not ``dfx``

Everything here goes through icp-cli, which supersedes ``dfx``. Same division as
``cloudflare.py`` and as ``mounts``/rclone: we drive the *provider's own* CLI as
a subprocess, the CLI holds the key material, and fused-render never touches a
credential. Resolution order mirrors the wrangler resolver exactly — a real
``icp`` on ``PATH`` first, then ``npx --yes @icp-sdk/icp-cli@1``, then nothing.

Deliberately NOT bundled into the payload the way rclone is (D103). D459 already
drew this line for ffmpeg: a tool one capability's subprocess needs does not go
into the app-wide payload, where every install would carry a Rust binary it will
never run. The npm fallback is the scoped version of D103's "remove the
prerequisite", and a machine with neither ``icp`` nor Node reports
``unavailable`` with the install guide rather than failing mid-publish.

## Authentication is not the question here

There is nobody to sign in to, so :meth:`Icp.auth` answers ``ready`` or
``unavailable`` and nothing else. What actually gates a publish is *funding* —
cycles, transferred by the author, out of band — and that is
:class:`~fused_render.publish.adapter.FundedTarget`, a separate question with a
separate button.

The publishing identity is a machine-level key for putting canisters on-chain,
created once, Fused-wide. One principal and one balance, not one per app: an
author who had to fund a fresh principal for every app they published would
stop after the first. It is not an app login, and no app's own sign-in gates it.

The key lives in the OS keyring — the only one of icp-cli's three storage modes
that is both non-interactive and not embarrassing. Password-encrypted would mean
prompting on every publish or holding the author's password; plaintext would put
a key that controls real money in a readable file. A keyring we cannot reach is
an ``unavailable`` with a reason, never a silent fall back to plaintext.

## The canister id is the origin, and it is minted before the upload

This is the failure mode the module is shaped around. ``icp deploy`` creates the
canister, then installs the asset code, then uploads the tree — and the id
exists, paid for with real cycles, from the first of those. If the upload then
fails and the id is dropped, the retry mints a **second** canister at a second
origin, which is the silent-data-loss failure the publish record exists to
prevent, reached by way of an error message.

So every failure after the id exists carries it out on
``PublishError.salvage``, and ``runs.py`` records it before reporting the
failure. The id itself is read from icp-cli's own mapping file
(``.icp/data/mappings/ic.ids.json``) rather than scraped from output, and
written back into that file before a re-publish — our project directory is
synthesized per publish, so the record is the only thing that remembers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

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

#: The identity fused-render creates and reuses. One, Fused-wide — see the
#: module docstring on why it is not per-app. The name is also what the author
#: will see in ``icp identity list``, so it says where it came from.
IDENTITY = "fused-render"

#: Mainnet, always. We never start a local replica, which is the only thing the
#: install guide's Docker/WSL note is about.
ENVIRONMENT = "ic"
NETWORK = "ic"

#: The recipe that turns a directory of files into a served canister. Pinned:
#: an unpinned recipe would change what our publishes deploy without a commit
#: here, and this one decides what the reader's browser is handed.
STATIC_SITE_RECIPE = "@dfinity/static-site@v0.3.3"

#: 1T. The mainnet deployment guide budgets 1–2T cycles per canister for an
#: initial deploy, so this is the floor at which we let a first publish start —
#: high enough that the common "never funded" case never reaches a failed
#: deploy, low enough that it is not our opinion about how much they should
#: hold. The failure path (``_deploy_failure``) handles the rest, because a
#: pre-flight check can pass and the install still fail.
MINIMUM_CYCLES = 1_000_000_000_000

#: Below this many days of runway the readout is a warning rather than a number.
#: A canister that runs out of cycles is frozen and eventually deleted WITH ALL
#: ITS STATE, so the interesting threshold is "enough time to notice and act",
#: not "nearly empty".
LOW_RUNWAY_DAYS = 30

#: Deploys upload the same tens of megabytes the Cloudflare adapter does, and to
#: a chain that batches. Queries are quick but cross the network.
DEPLOY_TIMEOUT_S = 1800
QUERY_TIMEOUT_S = 120
#: Identity creation touches the OS keyring, which on a locked keychain shows
#: the author a system prompt. Long enough for them to answer it.
IDENTITY_TIMEOUT_S = 120

INSTALL_URL = "https://cli.internetcomputer.org/1.4/guides/installation/"
FUNDING_URL = "https://cli.internetcomputer.org/1.4/guides/tokens-and-cycles/"

#: Where icp-cli records which canister id belongs to which canister name, per
#: environment. Committed by icp-cli's own convention; synthesized by us.
_IDS_FILE = os.path.join(".icp", "data", "mappings", f"{ENVIRONMENT}.ids.json")

#: A canister id: five groups of five lowercase base32 characters. Matching it
#: is how we recognise one in a mapping file whose shape may grow fields.
_CANISTER_ID = re.compile(r"^[a-z0-9]{5}(-[a-z0-9]{5}){4}$")

#: A principal, which is the same alphabet in a variable number of groups.
_PRINCIPAL = re.compile(r"\b[a-z0-9]{5}(?:-[a-z0-9]{3,5}){3,10}\b")

#: How icp-cli says there is not enough money. Matched loosely and in several
#: shapes on purpose: this is the one failure the author must never see as raw
#: CLI stderr, and a phrasing change should degrade to a generic error rather
#: than to a wrong one.
_OUT_OF_CYCLES = re.compile(
    r"insufficient (?:cycles|funds)|not enough cycles|out of cycles"
    r"|cycles balance is too low|InsufficientCycles",
    re.I,
)


def canister_name(app_name: str) -> str:
    """A canister name derived from an app folder name.

    Unlike a Pages project name this is **not** the hostname — the hostname is
    the canister id, which the network assigns — so it only has to be a valid
    identifier in ``icp.yaml``. It still gets the app's name rather than a
    constant, because it is what the author sees in ``icp canister status``.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", app_name.lower()).strip("-")[:48].rstrip("-")
    if not slug:
        slug = "fused-app"
    if slug[0].isdigit():
        slug = f"app-{slug}"[:48].rstrip("-")
    return slug


def format_cycles(amount: int) -> str:
    """Cycles as an author reads them: ``2.4T``, ``850B``, ``12M``.

    Raw cycles are a fifteen-digit number and nobody compares two of those. The
    unit suffixes are the ones icp-cli's own transfer command accepts, so a
    figure read here can be typed back there.
    """
    for size, suffix in ((10**12, "T"), (10**9, "B"), (10**6, "M"), (10**3, "K")):
        if abs(amount) >= size:
            value = amount / size
            text = f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return str(amount)


def _cycles_from_text(text: str) -> int | None:
    """A cycles figure out of whatever icp-cli printed.

    icp 1.4 prints ``2_000_602_400_000 cycles`` from ``cycles balance``, in the
    human line and inside ``--json`` alike, so that shape is tried FIRST and is
    the only one that can read a small number: it is the one match where the
    unit is spelled out, which is what makes a bare ``0`` safe to believe. A
    zero read as "could not read" would skip the pre-flight check on exactly the
    principal that most needs it.

    Then the other shapes the CLIs in this family use: a suffixed figure
    (``3.1 TC``, ``2T``) and a bare integer with separators. Returns ``None``
    rather than guessing when none of them match.
    """
    united = re.search(r"([0-9][0-9_,]*(?:\.[0-9]+)?)\s*cycles?\b", text, re.I)
    if united:
        return int(float(united.group(1).replace("_", "").replace(",", "")))
    suffixed = re.search(
        r"([0-9][0-9_,]*(?:\.[0-9]+)?)\s*(TC|T|BC|B|MC|M|KC|K)\b", text
    )
    if suffixed:
        value = float(suffixed.group(1).replace("_", "").replace(",", ""))
        scale = {"T": 10**12, "B": 10**9, "M": 10**6, "K": 10**3}[suffixed.group(2)[0]]
        return int(value * scale)
    bare = re.search(r"\b([0-9][0-9_,]{3,})\b", text)
    if bare:
        return int(bare.group(1).replace("_", "").replace(",", ""))
    return None


def _first_number(payload: object, *keys: str) -> int | None:
    """The first of ``keys`` present anywhere in a decoded JSON document.

    ``icp canister status --json`` nests its report differently from the
    management canister's own reply, and both are moving targets. Walking for
    the field by name survives a wrapper object appearing above it, which a
    fixed path does not.
    """
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return int(value)
            if isinstance(value, str):
                parsed = _cycles_from_text(value)
                if parsed is not None:
                    return parsed
        for value in payload.values():
            found = _first_number(value, *keys)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _first_number(item, *keys)
            if found is not None:
                return found
    return None


class Icp:
    """The ICP asset-canister adapter.

    Implements ``adapter.PublishAdapter`` and ``adapter.FundedTarget``.
    """

    id = "icp-canister"
    label = "Internet Computer"
    blurb = (
        "A canister you own on the Internet Computer, with no provider account "
        "anywhere — no signup, no card. The app's Python runs in each reader's "
        "browser, and their progress is saved in it."
    )
    capabilities = frozenset(
        {
            Capability.RUNTIME_JS,
            Capability.RUNTIME_PYODIDE,
            Capability.STATE_NONE,
            Capability.STATE_CLIENT_LOCAL,
        }
    )

    # ---- the icp seam -------------------------------------------------------
    # Every subprocess goes through _run, which is the one thing tests replace.

    def icp(self) -> list[str] | None:
        """The command that runs icp-cli, or ``None`` when there is none.

        Prefers a real ``icp`` on ``PATH``: the author installed it, it is
        theirs, and it starts instantly. Falls back to ``npx``, which fetches
        icp-cli on first use — a surprise download, but the difference between
        "publishing works" and "install a Rust CLI first", on the one target
        whose pitch is that it asks nothing of you.

        ``FUSED_RENDER_ICP`` overrides both (a pinned version, a wrapper).
        """
        override = os.environ.get("FUSED_RENDER_ICP")
        if override:
            return override.split()
        found = shutil.which("icp")
        if found:
            return [found]
        npx = shutil.which("npx")
        if npx:
            return [npx, "--yes", "@icp-sdk/icp-cli@1"]
        return None

    def _run(
        self, args: list[str], *, timeout: int, cwd: str | None = None
    ) -> subprocess.CompletedProcess:
        """Run icp-cli with ``args``.

        ``CI=1`` and stdin closed for the reason ``cloudflare.py`` gives: a
        subprocess with no terminal that stops on a prompt looks, from the
        Publish page, exactly like one that hung.
        """
        cmd = self.icp()
        if cmd is None:
            raise PublishError(
                "icp is not installed. Publishing to the Internet Computer goes through "
                "your own icp CLI, so install it from "
                f"{INSTALL_URL} (or install Node, which gives fused-render an `npx` to "
                "use) and try again."
            )
        env = dict(os.environ, CI="1")
        try:
            return subprocess.run(
                cmd + args,
                capture_output=True,
                text=True,
                # Pinned rather than left to the locale, as in cloudflare.py: a
                # machine under LANG=C must not turn one accented character in
                # an app name into a decode crash.
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise PublishError(
                f"icp {' '.join(args)} did not finish within {timeout}s."
            ) from exc
        except OSError as exc:
            raise PublishError(f"could not run icp: {exc}") from exc

    @staticmethod
    def _failure(proc: subprocess.CompletedProcess) -> str:
        """What icp-cli said went wrong, in one readable blob."""
        parts = [(proc.stderr or "").strip(), (proc.stdout or "").strip()]
        return "\n".join(p for p in parts if p) or f"icp exited with status {proc.returncode}"

    @staticmethod
    def _json(proc: subprocess.CompletedProcess):
        """The JSON value icp-cli printed, or ``None`` when it printed prose.

        Every ``--json`` here is optimistic: the flag is documented on some
        commands and not on others, and a build that lacks it prints a table and
        exits non-zero or ignores the flag. Both collapse to ``None`` and the
        caller falls back to reading the text, which is why nothing in this
        module depends on the flag existing.
        """
        text = proc.stdout or ""
        for start in range(len(text)):
            if text[start] in "[{":
                try:
                    return json.loads(text[start:])
                except ValueError:
                    continue
        return None

    # ---- the adapter interface ----------------------------------------------

    def auth(self) -> AuthState:
        """``ready`` or ``unavailable``, never ``needs-login``.

        There is nothing to sign in to. The identity is created on the first
        Fund cycles press, so its absence is not a failure of this call — an
        author with an ``icp`` and no identity yet is ready in the only sense
        this state models, and the Publish page's Fund cycles panel is what has
        something to say about the rest.
        """
        if self.icp() is None:
            return AuthState(
                status="unavailable",
                detail=(
                    "icp is not installed. Publishing to the Internet Computer goes "
                    "through your own icp CLI, so fused-render never holds a key. "
                    "Installing Node is enough — fused-render will use npx."
                ),
                help_url=INSTALL_URL,
            )
        principal = self._principal()
        if principal is None:
            return AuthState(
                status="ready",
                detail=(
                    "No publishing identity yet. Fund cycles creates one on this machine, "
                    "in your OS keyring — fused-render never sees the key."
                ),
            )
        return AuthState(status="ready", account=principal)

    def login(self) -> AuthState:
        """Nothing to do, and deliberately not an error.

        The Protocol requires the method; this target has no browser approval
        because it has no account. The Publish page never offers Log in for a
        ``ready`` or ``unavailable`` state, so this is only reachable by a
        direct API call, and answering it with the real state is more useful
        than refusing.
        """
        return self.auth()

    def publish(
        self, site_dir: str, *, name: str, record: PublishRecord | None
    ) -> PublishResult:
        state = self.auth()
        if not state.ready:
            raise PublishError("cannot publish to the Internet Computer: " + state.detail)

        canister = record.project if record else canister_name(name)
        existing_id = self._recorded_id(record)
        if record is not None and existing_id is None:
            # The app is recorded as published here, but the record does not name
            # a canister we can read. Deploying anyway would mint a SECOND one —
            # a new id, a new origin, and every reader's saved progress stranded
            # behind an address nobody will open again, with nothing on the
            # author's side looking wrong. Cloudflare refuses the same way when
            # its recorded project is gone: loudly, with the consequence named.
            raise PublishError(
                f"this app is recorded as published to the Internet Computer as "
                f"{record.project!r}, but the record does not name a canister. Publishing "
                "again would create a new one at a new address — and anyone using the old "
                "address would lose the progress saved in their browser. Find the canister "
                "with `icp canister status`, or choose Forget this deployment and publish "
                "fresh if it really is gone."
            )

        # Pre-flight. The common case — an author who never funded, or who has
        # drifted to zero since last time — should never reach a failed deploy
        # at all, because a deploy that fails halfway is the one that can cost
        # them an origin.
        balance = self.balance()
        if balance is not None and balance < MINIMUM_CYCLES and existing_id is None:
            raise PublishError(self._fund_message(balance))

        project = self._project_dir(site_dir, canister, existing_id)
        try:
            try:
                proc = self._run(
                    ["deploy", "--environment", ENVIRONMENT, "--identity", IDENTITY, "--yes"],
                    timeout=DEPLOY_TIMEOUT_S,
                    cwd=project,
                )
            except PublishError as exc:
                # A timeout or a CLI we could not start. The canister may still
                # have been created before we gave up, so the id is salvaged
                # here too — this branch and the non-zero-exit one below are the
                # same hazard reached two ways.
                exc.salvage = self._salvage(canister, self._read_id(project, canister))
                raise
            # Read the id back BEFORE looking at the exit code. icp-cli writes
            # the mapping when the canister is created, which is well before the
            # upload that may be what failed — and an id that exists is an
            # origin the author has already paid for.
            canister_id = self._read_id(project, canister) or existing_id
            if proc.returncode != 0:
                raise self._deploy_failure(proc, canister, canister_id)
        finally:
            shutil.rmtree(project, ignore_errors=True)

        if canister_id is None:
            raise PublishError(
                "the Internet Computer deploy reported success but named no canister, so "
                "fused-render cannot tell you the app's address or publish to it again. "
                "Nothing was recorded; check `icp canister status` before retrying."
            )

        notes: list[str] = []
        if existing_id is None:
            notes.append(
                "This is the app's first publish. The canister id in the address is "
                "permanent — re-publishing updates this same canister, so every reader "
                "keeps the progress saved in their browser."
            )
            notes.append(
                "A canister that runs out of cycles is frozen and eventually deleted, "
                "with everything in it. Keep an eye on the balance below."
            )
        return PublishResult(
            url=f"https://{canister_id}.icp0.io",
            project=canister,
            updated_in_place=existing_id is not None,
            notes=notes,
            extra={"canister_id": canister_id},
        )

    # ---- funding (adapter.FundedTarget) -------------------------------------

    def funding(self) -> FundingState:
        if self.icp() is None:
            return FundingState(
                funded=False,
                identity=False,
                minimum=MINIMUM_CYCLES,
                detail=(
                    "icp is not installed, so there is nothing to fund yet. "
                    "Installing Node is enough — fused-render will use npx."
                ),
                help_url=INSTALL_URL,
            )
        principal = self._principal()
        if principal is None:
            return FundingState(
                funded=False,
                identity=False,
                minimum=MINIMUM_CYCLES,
                detail=(
                    "Publishing to the Internet Computer is paid for in cycles, which you "
                    "transfer yourself — there is no account here and no card. The first "
                    "step is a publishing identity on this machine."
                ),
                help_url=FUNDING_URL,
            )
        balance = self.balance()
        return FundingState(
            funded=balance is not None and balance >= MINIMUM_CYCLES,
            identity=True,
            principal=principal,
            balance=balance,
            minimum=MINIMUM_CYCLES,
            transfer_command=self.transfer_command(principal),
            detail=(
                f"{format_cycles(balance)} cycles on this principal."
                if balance is not None
                else "Could not read the balance on this principal."
            ),
            help_url=FUNDING_URL,
        )

    @staticmethod
    def transfer_command(principal: str) -> str:
        """The command the author runs, in their own terminal, with their own
        money. We never move it: fused-render has no key and no wallet."""
        return f"icp cycles transfer {format_cycles(MINIMUM_CYCLES)} {principal} -n {NETWORK}"

    def create_identity(self) -> IdentityCreated:
        """Mint the publishing identity and hand back its seed phrase, once.

        The phrase is read through ``--output-seed`` into a ``0600`` file in a
        private temp dir, which is unlinked before this returns.
        **Deliberately not ``-q`` on stdout**: every command in this module goes
        through :meth:`_failure`, which hands the whole captured blob to the
        author on a non-zero exit. A phrase on stdout is one bad exit away from
        an error message, a log file, and a pasted issue report.
        """
        if self._principal() is not None:
            raise PublishError(
                "a fused-render publishing identity already exists on this machine. Its "
                "seed phrase was shown once, when it was created, and is not stored "
                "anywhere fused-render can read — export the key with "
                "`icp identity export fused-render` if you need it elsewhere."
            )
        # 0700 on the DIRECTORY rather than 0600 on the file: the protection is
        # the same (nobody else can traverse in to reach it), and pre-creating
        # the file would bet on `--output-seed` being willing to overwrite one,
        # which is not something to discover at the moment an author is minting
        # their only copy of a seed phrase. The whole dir goes in `finally`.
        work = tempfile.mkdtemp(prefix="fused-icp-")
        os.chmod(work, 0o700)
        seed_path = os.path.join(work, "seed")
        try:
            proc = self._run(
                [
                    "identity", "new", IDENTITY,
                    "--storage", "keyring",
                    "--output-seed", seed_path,
                ],
                timeout=IDENTITY_TIMEOUT_S,
            )
            if proc.returncode != 0:
                raise PublishError(self._identity_failure(proc))
            try:
                with open(seed_path, encoding="utf-8") as f:
                    phrase = f.read().strip()
            except OSError as exc:
                raise PublishError(
                    f"the identity was created but its seed phrase could not be read: {exc}. "
                    "The key itself is in your OS keyring and the identity works; "
                    "`icp identity export fused-render` gets it out in PEM form."
                ) from exc
        finally:
            shutil.rmtree(work, ignore_errors=True)

        principal = self._principal()
        if principal is None:
            raise PublishError(
                "icp reported creating the identity, but `icp identity principal` does not "
                "see it. Nothing was funded. Check `icp identity list`."
            )
        return IdentityCreated(principal=principal, seed_phrase=phrase)

    def cycles(self, record: PublishRecord) -> CyclesReading:
        """The balance and idle burn of this app's canister, as one round trip.

        Prefers ``--json`` and falls back to the human table, because the flag
        is not on every command in every build. The fallback is where a parser
        like this rots, so both paths hunt for the field by name rather than by
        position.
        """
        canister_id = self._recorded_id(record)
        if canister_id is None:
            raise PublishError(
                "this app's publish record names no canister, so there is no balance to "
                "read. Publish it first."
            )
        proc = self._run(
            [
                "canister", "status", canister_id,
                "--network", NETWORK, "--identity", IDENTITY, "--json",
            ],
            timeout=QUERY_TIMEOUT_S,
        )
        if proc.returncode != 0:
            raise PublishError(
                f"could not read the status of canister {canister_id}:\n{self._failure(proc)}"
            )
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        payload = self._json(proc)
        balance = _first_number(payload, "cycles", "balance", "cycle_balance")
        idle = _first_number(payload, "idle_cycles_burned_per_day", "idle_cycles_burned")
        if balance is None:
            balance = self._labelled(text, r"(?:cycle[s]?\s*)?balance")
        if idle is None:
            idle = self._labelled(text, r"idle[ _]cycles[ _]burned[ _](?:per[ _]day|/day)")
        if balance is None:
            raise PublishError(
                f"could not find a cycles balance in what icp reported for {canister_id}. "
                "The canister is fine; fused-render just cannot show the number."
            )
        return CyclesReading(
            balance=balance,
            idle_burned_per_day=idle or 0,
            read_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    @staticmethod
    def _labelled(text: str, label: str) -> int | None:
        """A number printed after ``label:`` in a human-formatted table."""
        match = re.search(label + r"\s*[:=]\s*([^\n]+)", text, re.I)
        return _cycles_from_text(match.group(1)) if match else None

    def balance(self) -> int | None:
        """The identity's cycles, or ``None`` when we could not read them.

        ``None`` is not zero and must not be treated as it: a balance we failed
        to read is a reason to let the deploy try and report its own failure,
        not a reason to refuse a publish the author has already paid for.
        """
        proc = self._run(
            ["cycles", "balance", "--network", NETWORK, "--identity", IDENTITY, "--json"],
            timeout=QUERY_TIMEOUT_S,
        )
        if proc.returncode != 0:
            return None
        payload = self._json(proc)
        found = _first_number(payload, "cycles", "balance")
        if found is not None:
            return found
        return _cycles_from_text((proc.stdout or "") + " " + (proc.stderr or ""))

    def _principal(self) -> str | None:
        """The principal of our identity, or ``None`` when there is not one yet.

        ``icp identity principal`` reports the SELECTED identity and takes no
        positional name, so ``--identity`` is not decoration: without it this
        would answer with whichever identity the author happens to have made
        default, and the Publish page would print a principal that our deploys
        never use and our balance is not on.
        """
        proc = self._run(
            ["identity", "principal", "--identity", IDENTITY], timeout=QUERY_TIMEOUT_S
        )
        if proc.returncode != 0:
            # "no identity found with name `fused-render`" — the pre-creation
            # state, not a failure worth surfacing.
            return None
        match = _PRINCIPAL.search(proc.stdout or "")
        return match.group(0) if match else None

    # ---- the project icp deploy expects -------------------------------------

    def _project_dir(self, site_dir: str, canister: str, canister_id: str | None) -> str:
        """A minimal icp-cli project pointing at the site we just built.

        ``icp deploy`` wants a project; ``site.py`` produces a bare directory.
        Rather than write a manifest into the author's app folder — which is
        their content, and which would then need ignoring, explaining and
        cleaning up — we synthesize one per publish in a temp dir and delete it
        after.

        Which makes the id mapping our job. icp-cli remembers a canister id in
        ``.icp/data/mappings/ic.ids.json`` inside the project, and our project
        does not survive the publish, so a re-publish would look like a first
        one and mint a new canister at a new origin. Seeding that file from the
        publish record is what makes re-publishing land on the same canister.
        """
        project = tempfile.mkdtemp(prefix="fused-icp-project-")
        manifest = (
            "# Written by fused-render for one publish, then deleted.\n"
            "canisters:\n"
            f"  - name: {canister}\n"
            "    recipe:\n"
            f'      type: "{STATIC_SITE_RECIPE}"\n'
            "      configuration:\n"
            f"        dir: {json.dumps(site_dir)}\n"
        )
        with open(os.path.join(project, "icp.yaml"), "w", encoding="utf-8") as f:
            f.write(manifest)
        if canister_id:
            ids_path = os.path.join(project, _IDS_FILE)
            os.makedirs(os.path.dirname(ids_path), exist_ok=True)
            with open(ids_path, "w", encoding="utf-8") as f:
                json.dump({canister: canister_id}, f, indent=2)
                f.write("\n")
        return project

    @staticmethod
    def _read_id(project: str, canister: str) -> str | None:
        """The canister id icp-cli recorded for ``canister``, if it recorded one.

        Read by name first and then by shape, because this file gains fields:
        an entry that grew from a bare string to an object still carries the id,
        and finding it is the difference between a recorded origin and a second
        canister on the next attempt.
        """
        try:
            with open(os.path.join(project, _IDS_FILE), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        entry = data.get(canister) if isinstance(data, dict) else None
        for value in (entry, data):
            found = Icp._find_id(value)
            if found:
                return found
        return None

    @staticmethod
    def _find_id(value: object) -> str | None:
        if isinstance(value, str):
            return value if _CANISTER_ID.match(value) else None
        if isinstance(value, dict):
            for item in value.values():
                found = Icp._find_id(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = Icp._find_id(item)
                if found:
                    return found
        return None

    def _salvage(self, canister: str, canister_id: str | None) -> PublishRecord | None:
        """The record a failed publish must leave behind, or ``None``.

        Only the id matters — it is the origin, and it was paid for. The URL is
        derived from it so a retry that never succeeds still leaves the author
        an address they can check.
        """
        if not canister_id:
            return None
        return PublishRecord(
            target=self.id,
            project=canister,
            url=f"https://{canister_id}.icp0.io",
            extra={"canister_id": canister_id},
        )

    @staticmethod
    def _recorded_id(record: PublishRecord | None) -> str | None:
        if record is None:
            return None
        value = record.extra.get("canister_id")
        return value if isinstance(value, str) and _CANISTER_ID.match(value) else None

    # ---- failures the author can act on --------------------------------------

    def _deploy_failure(
        self, proc: subprocess.CompletedProcess, canister: str, canister_id: str | None
    ) -> PublishError:
        """The deploy failed. Say what happened in the author's terms, and keep
        the canister id if one was minted on the way.

        Insufficient cycles is recognised specifically rather than passed
        through: it is the one failure with an obvious remedy, and handing the
        author raw CLI stderr instead of their principal and a transfer command
        is how a fixable problem becomes a dead end.
        """
        blob = self._failure(proc)
        salvage = self._salvage(canister, canister_id)
        if _OUT_OF_CYCLES.search(blob):
            message = self._fund_message(self.balance())
            if canister_id:
                message += (
                    f"\n\nThe canister ({canister_id}) was created before the upload ran out, "
                    "and fused-render has recorded it — topping up and publishing again "
                    "will finish loading this same canister, at the same address."
                )
            return PublishError(message, salvage=salvage)
        return PublishError(
            f"the Internet Computer deploy failed:\n{blob}", salvage=salvage
        )

    def _fund_message(self, balance: int | None) -> str:
        principal = self._principal()
        held = (
            f"This principal holds {format_cycles(balance)} cycles"
            if balance is not None
            else "fused-render could not read this principal's balance"
        )
        lines = [
            "there are not enough cycles to publish to the Internet Computer. "
            f"{held}, and a canister needs about {format_cycles(MINIMUM_CYCLES)} to start.",
        ]
        if principal:
            lines.append(f"\nYour principal:\n  {principal}")
            lines.append(
                f"\nTransfer cycles to it from your own terminal:\n"
                f"  {self.transfer_command(principal)}"
            )
        lines.append(f"\nHow to get cycles: {FUNDING_URL}")
        return "\n".join(lines)

    @staticmethod
    def _identity_failure(proc: subprocess.CompletedProcess) -> str:
        blob = Icp._failure(proc)
        if re.search(r"keyring|keychain|secret service|credential store", blob, re.I):
            return (
                "the publishing identity could not be created because your OS keyring "
                "would not accept it:\n" + blob + "\n\n"
                "fused-render only creates keyring-backed identities. The alternatives "
                "icp offers are a password you would be asked for on every publish, or a "
                "key that controls real money sitting in a readable file — neither is "
                "something to fall back to silently. Unlock your keyring and try again."
            )
        return "the publishing identity could not be created:\n" + blob
