"""Cloudflare Pages, through the author's own ``wrangler``.

The first adapter, and the one that covers the whole JS/Pyodide + client-local
region of the grid on a free tier that needs no card. What a publish actually
does is a **direct upload**: the static site built by ``site.py`` is handed to
``wrangler pages deploy`` as a directory. No git repository, no registry, no
build step on Cloudflare's side — the tree we built is the tree that is served.

## Authentication

``wrangler login`` — a browser OAuth approval, run as a subprocess here.

Deliberately **not** an API-token paste field. A token box is worse UX, a worse
security posture, and it would hand FusedRender a long-lived credential to hold,
which is the one thing this whole feature is built not to do. The token
``wrangler login`` mints lives in wrangler's own config and we never read it: we
ask wrangler *whether* we are authenticated, and it does the rest.

## The origin is the contract

A Pages project gets one canonical ``*.pages.dev`` hostname, stable for the
project's life. That is what makes re-publishing in place possible, and
re-publishing in place is not a nicety — a rung-1 app's state lives in
``localStorage``, which is origin-scoped, so a publish that minted a new project
would silently orphan every reader's progress. So the project name is recorded on
the first publish (``record.py``) and every later publish deploys into that same
project, never creating a second one.

That hostname is **read back from Cloudflare, never predicted**. It is tempting
to assume ``<project>.pages.dev``, and for most projects that is what it is — but
the ``pages.dev`` subdomain is unique across all of Cloudflare, not within one
account. Ask for a name a stranger already took and Cloudflare still creates
*your* project under the name you asked for, then quietly assigns it a suffixed
subdomain (``pushup-tracker`` -> ``pushup-tracker-dqd.pages.dev``). Guessing there
does not produce a broken link, which would at least be visible; it produces a
working link to someone else's site, handed to the author as their own.

The per-deployment ``<hash>.<subdomain>.pages.dev`` alias Cloudflare also mints is
recorded but never handed to the author: it pins a reader to one snapshot and
would split their saved state across origins.

## Why shelling out rather than the REST API

Because the credential is the point. Talking to the API ourselves would mean
holding a token; talking to wrangler means the author's browser approval stays
between them and Cloudflare. It is the same division as ``mounts``, where rclone
holds the remote's credentials and we only drive it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from fused_render.publish.adapter import (
    AuthState,
    Capability,
    PublishError,
    PublishRecord,
    PublishResult,
)

#: A Pages project name: lowercase alphanumerics and hyphens, starting and ending
#: alphanumeric, at most 58 characters. It is not cosmetic — it *is* the hostname.
_NAME_OK = re.compile(r"^[a-z0-9]([a-z0-9-]{0,56}[a-z0-9])?$")

#: How long to wait for the browser approval in ``wrangler login``. Long, because
#: the author has to switch to a browser, possibly sign in, and read a consent
#: screen — and a timeout here reads to them as "publishing is broken".
LOGIN_TIMEOUT_S = 300

#: Everything else. Generous for a deploy (a first publish uploads ~14 MB of
#: Pyodide over whatever connection the author has) and modest for a query.
DEPLOY_TIMEOUT_S = 900
QUERY_TIMEOUT_S = 60


def project_name(app_name: str) -> str:
    """A valid Pages project name derived from an app folder name.

    ``Chinese HSK Cards`` -> ``chinese-hsk-cards``. A name that survives this
    unchanged is what an author would have typed anyway, which matters because
    they will read it back as a hostname.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", app_name.lower()).strip("-")[:58].rstrip("-")
    if not slug:
        slug = "fused-app"
    if slug[0].isdigit():
        # A hostname label may start with a digit, but Pages rejects a project
        # name that does — prefix rather than fail on a folder called "2048".
        slug = f"app-{slug}"[:58].rstrip("-")
    return slug


#: Where a project's name lives in ``wrangler pages project list --json``.
#:
#: ``--json`` on this command does NOT emit the API shape — it serialises the
#: human-readable TABLE, so the key is the column heading ``"Project Name"``.
#: ``"name"`` is what the REST API and every other wrangler ``--json`` command
#: use, and is accepted too: reading both costs one tuple and means a wrangler
#: that switches to the API shape does not silently break re-publishing. The
#: failure mode this guards is quiet and bad — an existing project reads as
#: missing, so a re-publish refuses on "that project no longer exists".
_PROJECT_NAME_KEYS = ("Project Name", "name")


#: Where a project's hostnames live in the same ``--json`` table. ``"Project
#: Domains"`` is the column heading; ``"domains"`` is the API shape, accepted for
#: the same reason ``"name"`` is above.
_PROJECT_DOMAIN_KEYS = ("Project Domains", "domains")


def _project_key(entry: object) -> str | None:
    if not isinstance(entry, dict):
        return None
    for key in _PROJECT_NAME_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _pages_dev_host(entry: object) -> str | None:
    """The ``*.pages.dev`` hostname a project-list entry carries, if any.

    The column is a *joined string* of every domain on the project (the API shape
    is a list; both are read), so a project with a custom domain attached looks
    like ``"myapp.com, myapp-dqd.pages.dev"``. The ``pages.dev`` one is the one we
    want, and not merely because it is always present: a custom domain can be
    detached at Cloudflare, and if the author's share link followed it, every
    reader's ``localStorage`` progress would be stranded on an origin nothing
    points at any more. The ``pages.dev`` host lives as long as the project.
    """
    if not isinstance(entry, dict):
        return None
    for key in _PROJECT_DOMAIN_KEYS:
        value = entry.get(key)
        parts = value if isinstance(value, list) else str(value or "").split(",")
        for part in parts:
            host = part.strip().strip("/") if isinstance(part, str) else ""
            host = host.split("://")[-1]
            if host.endswith(".pages.dev"):
                return host
    return None


class CloudflarePages:
    """The Cloudflare Pages adapter (``adapter.PublishAdapter``)."""

    id = "cloudflare-pages"
    label = "Cloudflare Pages"
    blurb = (
        "Static hosting on Cloudflare's network, from your own free account. "
        "The app's Python runs in each reader's browser, and their progress is "
        "saved in it."
    )
    capabilities = frozenset(
        {
            Capability.RUNTIME_JS,
            Capability.RUNTIME_PYODIDE,
            Capability.STATE_NONE,
            Capability.STATE_CLIENT_LOCAL,
        }
    )

    # ---- the wrangler seam --------------------------------------------------
    # Every subprocess goes through _run, which is the one thing tests replace.

    def wrangler(self) -> list[str] | None:
        """The command that runs wrangler, or ``None`` when there is none.

        Prefers a real ``wrangler`` on ``PATH`` — the author installed it, it
        starts instantly. Falls back to ``npx``, which fetches it on first use:
        slower and a surprise download, but the difference between "publish
        works" and "install this first, then come back", and the Publish page
        says which one it found.

        ``FUSED_RENDER_WRANGLER`` overrides both (a pinned version, a wrapper).
        """
        override = os.environ.get("FUSED_RENDER_WRANGLER")
        if override:
            return override.split()
        found = shutil.which("wrangler")
        if found:
            return [found]
        npx = shutil.which("npx")
        if npx:
            return [npx, "--yes", "wrangler@4"]
        return None

    def _run(self, args: list[str], *, timeout: int, cwd: str | None = None) -> subprocess.CompletedProcess:
        """Run wrangler with ``args``.

        ``CI=1`` and ``WRANGLER_SEND_METRICS=false`` between them stop wrangler
        asking anything: a subprocess with no terminal that stops on a prompt
        looks, from the Publish page, exactly like one that hung.
        """
        cmd = self.wrangler()
        if cmd is None:
            raise PublishError(
                "wrangler is not installed. Cloudflare Pages is published through your own "
                "wrangler, so install it with `npm install -g wrangler` (or install Node, "
                "which gives fused-render an `npx` to use) and try again."
            )
        env = dict(os.environ, CI="1", WRANGLER_SEND_METRICS="false")
        try:
            return subprocess.run(
                cmd + args,
                capture_output=True,
                text=True,
                # Pinned, not left to the locale: wrangler prints a project name
                # the author chose, and a machine running under LANG=C would
                # otherwise turn one accented character into a decode crash.
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise PublishError(
                f"wrangler {' '.join(args)} did not finish within {timeout}s. Nothing was "
                "changed at Cloudflare that a re-run will not fix."
            ) from exc
        except OSError as exc:
            raise PublishError(f"could not run wrangler: {exc}") from exc

    @staticmethod
    def _json(proc: subprocess.CompletedProcess):
        """The JSON object wrangler printed, or ``None``.

        Scans for the last balanced JSON value in stdout rather than parsing the
        whole stream: wrangler prefixes its output with a banner and, in some
        environments, a proxy warning, and those are not going to stop.
        """
        text = proc.stdout or ""
        for start in range(len(text)):
            if text[start] in "[{":
                try:
                    return json.loads(text[start:])
                except ValueError:
                    continue
        return None

    @staticmethod
    def _failure(proc: subprocess.CompletedProcess) -> str:
        """What wrangler said went wrong, in one readable blob.

        Wrangler's own error text is better than anything we could paraphrase —
        it names the account, the permission, the conflicting project — so it is
        passed through rather than replaced.
        """
        parts = [(proc.stderr or "").strip(), (proc.stdout or "").strip()]
        return "\n".join(p for p in parts if p) or f"wrangler exited with status {proc.returncode}"

    # ---- the adapter interface ---------------------------------------------

    def auth(self) -> AuthState:
        if self.wrangler() is None:
            return AuthState(
                status="unavailable",
                detail=(
                    "wrangler is not installed. Publishing to Cloudflare goes through your "
                    "own wrangler, so fused-render never holds a Cloudflare credential."
                ),
                help_url="https://developers.cloudflare.com/workers/wrangler/install-and-update/",
            )
        proc = self._run(["whoami", "--json"], timeout=QUERY_TIMEOUT_S)
        info = self._json(proc)
        if not isinstance(info, dict) or not info.get("loggedIn"):
            return AuthState(
                status="needs-login",
                detail="Log in to Cloudflare in your browser. fused-render never sees the token.",
            )
        account = info.get("email") or self._account_name(info) or "your Cloudflare account"
        return AuthState(status="ready", account=account)

    @staticmethod
    def _account_name(info: dict) -> str | None:
        accounts = info.get("accounts")
        if isinstance(accounts, list) and accounts and isinstance(accounts[0], dict):
            name = accounts[0].get("name") or accounts[0].get("account_name")
            return name if isinstance(name, str) else None
        return None

    def login(self) -> AuthState:
        """Run ``wrangler login`` and report what it left behind.

        Blocking, and it opens a browser: the caller runs it off the event loop.
        The result is read back with :meth:`auth` rather than trusted from the
        exit code — a login that "succeeded" but left no usable session should
        not present itself as ready.
        """
        proc = self._run(["login"], timeout=LOGIN_TIMEOUT_S)
        state = self.auth()
        if state.ready:
            return state
        return AuthState(
            status=state.status,
            detail=(self._failure(proc) if proc.returncode else state.detail)
            or "the browser approval did not complete",
        )

    def publish(self, site_dir: str, *, name: str, record: PublishRecord | None) -> PublishResult:
        state = self.auth()
        if not state.ready:
            raise PublishError(
                "not signed in to Cloudflare: " + (state.detail or "run wrangler login first")
            )

        project = record.project if record else project_name(name)
        if not _NAME_OK.match(project):
            raise PublishError(
                f"{project!r} is not a valid Cloudflare Pages project name. It becomes the "
                "hostname, so it must be lowercase letters, digits and hyphens, start and "
                "end with a letter or digit, and be at most 58 characters."
            )

        existed = self._project_exists(project)
        if record and not existed:
            # The record points at a project that is gone — deleted at Cloudflare,
            # or the record was copied in from another account. Creating a
            # replacement silently would be the WRONG kind of helpful: it gets the
            # same name and therefore the same origin only if the name is still
            # free, and if it is not, every reader is stranded with no warning.
            raise PublishError(
                f"this app was published to a Cloudflare Pages project called {project!r}, "
                "but that project no longer exists in this account. Publishing again would "
                "create a new one — and anyone using the old address would lose the progress "
                "saved in their browser. Check you are signed in to the right account; if the "
                "project really is gone, choose Forget this deployment and publish fresh."
            )
        if not existed:
            self._create_project(project)

        proc = self._run(
            [
                "pages", "deploy", site_dir,
                "--project-name", project,
                "--branch", "main",
                "--commit-dirty=true",
            ],
            timeout=DEPLOY_TIMEOUT_S,
        )
        if proc.returncode != 0:
            raise PublishError(f"the Cloudflare upload failed:\n{self._failure(proc)}")

        host = self._canonical_host(project, proc)
        notes: list[str] = []
        if not existed:
            notes.append(
                "This is the app's first publish. The address can take a minute to start "
                "resolving worldwide — if it does not load straight away, wait and retry."
            )
        if host != f"{project}.pages.dev":
            # Otherwise the address just looks wrong, and the obvious guess — that
            # fused-render mangled it — is the one explanation that isn't true.
            notes.append(
                f"{project}.pages.dev was already taken by someone else's site, so Cloudflare "
                f"gave this project {host} instead. That is your app's real address; the "
                "shorter one is not."
            )
        return PublishResult(
            url=f"https://{host}",
            project=project,
            updated_in_place=existed,
            notes=notes,
            # The per-deployment alias, kept for support ("which upload is live?")
            # and never handed to the author — see the module docstring.
            extra={"deployment_url": self._deployment_url(proc) or ""},
        )

    # ---- Pages plumbing -----------------------------------------------------

    def _projects(self) -> list:
        """Every Pages project in this account, as wrangler's table-shaped JSON."""
        proc = self._run(["pages", "project", "list", "--json"], timeout=QUERY_TIMEOUT_S)
        if proc.returncode != 0:
            raise PublishError(
                f"could not list your Cloudflare Pages projects:\n{self._failure(proc)}"
            )
        projects = self._json(proc)
        if not isinstance(projects, list):
            raise PublishError(
                "could not read the list of Cloudflare Pages projects. Nothing was published."
            )
        return projects

    def _project_exists(self, project: str) -> bool:
        return any(_project_key(p) == project for p in self._projects())

    def _canonical_host(self, project: str, deploy: subprocess.CompletedProcess) -> str:
        """The hostname Cloudflare actually assigned ``project``.

        Asked *after* the deploy, because on a first publish the project — and so
        the subdomain — did not exist before it. Three sources, degrading rather
        than failing, because a deploy that already succeeded must not be reported
        as an error over a hostname lookup:

        1. The project's own ``pages.dev`` domain from the project list. The
           authoritative answer, and the only one that shows a suffix Cloudflare
           added because the name was taken elsewhere.
        2. The per-deployment ``<hash>.<subdomain>.pages.dev`` alias this deploy
           printed, with the hash label dropped. Carries the same suffix, so it
           survives a project list that failed or changed shape.
        3. ``<project>.pages.dev`` — the old guess. Right for most projects, and
           no worse than what we did before when everything else is unavailable.
        """
        try:
            for entry in self._projects():
                if _project_key(entry) == project:
                    host = _pages_dev_host(entry)
                    if host:
                        return host
                    break
        except PublishError:
            pass  # the upload landed; a lookup failure must not undo that

        alias = self._deployment_url(deploy)
        if alias:
            labels = alias.split("://")[-1].split("/")[0].split(".")
            # <hash>.<subdomain>.pages.dev is 4 labels; anything shorter is
            # already the canonical host and has no hash to strip.
            if len(labels) >= 4:
                return ".".join(labels[1:])

        return f"{project}.pages.dev"

    def _create_project(self, project: str) -> None:
        proc = self._run(
            ["pages", "project", "create", project, "--production-branch", "main"],
            timeout=QUERY_TIMEOUT_S,
        )
        if proc.returncode != 0:
            raise PublishError(
                f"could not create the Cloudflare Pages project {project!r}:\n"
                f"{self._failure(proc)}\n\n"
                f"If the name is already taken, publish under a different one — it becomes "
                "the app's web address."
            )

    _DEPLOY_URL = re.compile(r"https://[a-z0-9.-]+\.pages\.dev\b")

    def _deployment_url(self, proc: subprocess.CompletedProcess) -> str | None:
        match = self._DEPLOY_URL.search((proc.stdout or "") + (proc.stderr or ""))
        return match.group(0) if match else None
