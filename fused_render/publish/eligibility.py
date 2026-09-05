"""Place an app on the runtime x state grid, and say what that rules out.

The parent issue's load-bearing piece: *before* any provider exists, an app can
be classified by what it actually needs to run, and a target can then be offered
or refused with a reason instead of a shrug. Nothing here knows about Cloudflare,
ICP or Cloud Run — it answers "what would this app require of a host", and
``registry.py`` compares that against what each adapter covers.

It builds on the exporter rather than beside it. :func:`~fused_render.export.plan_export`
already resolves a page's dependency set and records the blocking problems (SPEC
§18.2 EX-3/EX-4: ``fused.writeFile``/``stat``/``ai``, a computed ``runPython``
path, a missing or escaping file), and its ``ExportPlan.errors`` is the gating
socket #582 left dangling — *"errors are blocking (export_page raises; Deploy is
disabled)"*. This module is the consumer that docstring lost: an export error is
a publish blocker, verbatim, on every target.

On top of that it adds the two axes the exporter has no reason to care about.

**Runtime.** Every bundled ``.py`` is parsed and its absolute top-level imports
classified: the standard library and first-party siblings are free, a name the
Pyodide distribution carries costs a vendored wheel, and anything else — a C
extension, a package we cannot place — means the app needs a real CPython
process. The strongest cell wins.

**State.** A static scan for filesystem mutation (``open(..., "w")``,
``os.replace``, ``pathlib.Path.write_text``, ``shutil.*``, …). An app that never
writes needs nothing of a host; one that writes needs somewhere for that state to
live. The scan cannot tell *per-viewer* from *shared* intent — no static analysis
can — so it reports the weakest honest answer, ``STATE_CLIENT_LOCAL``, and leaves
a note saying so. An author who meant one shared document will read that note and
know this target is the wrong shape; nothing else could have told them.

Deliberately conservative in one direction only: when the scan cannot place an
import or resolve a path, it reports the STRONGER requirement (CPython, has
state). A misclassification then costs a target that would have worked — visible,
and fixed by the author telling us otherwise — rather than a publish that looks
fine and 500s in front of the reader.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from dataclasses import dataclass, field

from fused_render.export import ExportPlan, plan_export
from fused_render.publish.adapter import (
    RUNTIME_AXIS,
    STATE_AXIS,
    Capability,
)

#: Top-level import name -> the package name in the Pyodide distribution.
#:
#: NOT a guess and NOT the whole distribution: this is the subset we promise to
#: vendor, which is the app's bundled data stack (``pyproject.toml`` ``bundled``,
#: SPEC DM-2) plus the handful of pure-Python names that ride along with it. The
#: authority at PUBLISH time is the distribution's own ``pyodide-lock.json``
#: (``pyodide_dist.py`` resolves against it and fails loudly on a miss), so a
#: name here that a future Pyodide drops becomes a clear build error, never a
#: page that 500s in the reader's browser.
#:
#: An import missing from this map is not "unavailable in Pyodide" — it is
#: "unplaceable by us", which is the same verdict for a different reason and is
#: worded that way in the report.
PYODIDE_IMPORTS: dict[str, str] = {
    "numpy": "numpy",
    "pandas": "pandas",
    "PIL": "pillow",
    "openpyxl": "openpyxl",
    "requests": "requests",
    "dateutil": "python-dateutil",
    "pytz": "pytz",
    "six": "six",
    "yaml": "pyyaml",
    "et_xmlfile": "et-xmlfile",
}

#: Imports that work under Pyodide but reach the network, which in a browser
#: means the *reader's* browser and therefore CORS. Worth a note: the call that
#: worked on the author's machine fails on a host that sends no
#: ``Access-Control-Allow-Origin``, and the failure looks like a bug in the app.
_NETWORK_IMPORTS = frozenset({"requests", "urllib", "http", "socket", "ftplib"})

#: ``os`` functions that mutate the filesystem.
_OS_WRITES = frozenset(
    {
        "remove", "unlink", "rename", "renames", "replace", "mkdir", "makedirs",
        "rmdir", "removedirs", "truncate", "symlink", "link", "chmod", "utime",
        "write", "mkfifo",
    }
)

#: ``shutil`` functions that mutate the filesystem.
_SHUTIL_WRITES = frozenset(
    {
        "copy", "copy2", "copyfile", "copytree", "copymode", "copystat", "move",
        "rmtree", "make_archive", "unpack_archive", "chown",
    }
)

#: ``pathlib.Path`` methods that mutate the filesystem. Matched by ATTRIBUTE NAME
#: alone (we do not track which objects are Paths), so a same-named method on an
#: unrelated object counts too. That over-counts state, never under-counts it —
#: the conservative direction, per the module docstring.
_PATH_WRITES = frozenset(
    {
        "write_text", "write_bytes", "mkdir", "touch", "unlink", "rmdir",
        "rename", "replace", "symlink_to", "hardlink_to", "chmod",
    }
)

#: An ``open()`` mode that mutates. ``r`` alone (or an absent mode) reads.
_WRITE_MODE = re.compile(r"[wax+]")

#: Local-only ``window.fused`` surfaces that are NOT export errors but cannot work
#: on any host — each maps to what the author has to do about it. EXPORT.md calls
#: these a ``fused.env`` gating obligation rather than a blocker, and that is the
#: right call: a page that reaches for a local model only behind an
#: ``env === "local"`` branch is legitimately publishable, and refusing it would
#: be wrong. So they are NOTES, worded as the obligation they are.
#:
#: Matched dotted (``fused.ai.image(``) as well as bare, which is exactly the gap
#: EXPORT.md documents in the exporter's own check.
_LOCAL_ONLY_SURFACES: dict[str, str] = {
    "ai": "local inference and the claude CLI run on your machine; a reader's browser cannot reach them",
    "capture": "screen and microphone capture writes a file to your disk (SPEC §45); a published page has no disk of yours",
    "fileIndex": "the file index is a store built by scanning your filesystem; there is nothing behind it on a published page",
    "engine": "an engine is a resident process on your machine",
    "daemon": "a background app's daemon is a process on your machine (SPEC §46)",
    "uploadFile": "a published page has no filesystem to upload into",
    "mkdir": "a published page has no filesystem to make a directory in",
}
_LOCAL_ONLY_CALL = {
    name: re.compile(r"fused\.%s\s*[.(]" % name) for name in _LOCAL_ONLY_SURFACES
}
#: Does the page branch on ``fused.env`` at all? Its presence does not prove any
#: particular call is gated, but its ABSENCE proves none of them are — which is
#: the difference between "check your gate covers this" and "this will break".
_ENV_BRANCH = re.compile(r"fused\.env\b")


@dataclass(frozen=True)
class Eligibility:
    """Where an app sits on the grid, and everything a target needs to judge it.

    ``runtime`` and ``state`` are the single strongest cell the app needs on each
    axis (the axes are ordered — see ``adapter.RUNTIME_AXIS``), so a target's
    check is a membership test, not a per-provider rule.

    ``blockers`` are problems no target can host around — the exporter's own
    errors, plus a write to a path outside the app. They are worded for the
    author and shown verbatim. ``notes`` never block: they say what will be
    *different* about the app once it is published.
    """

    page: str
    runtime: Capability
    state: Capability
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Payload-relative paths of every bundled ``.py`` (entrypoints + modules).
    python_files: list[str] = field(default_factory=list)
    #: Pyodide package names the site build must vendor, resolved from imports.
    pyodide_packages: list[str] = field(default_factory=list)
    #: Imports we could place nowhere — the reason ``runtime`` is CPython.
    unplaceable_imports: list[str] = field(default_factory=list)
    #: The export plan the scan ran on, so a caller can build the bundle without
    #: re-scanning. ``None`` only when the page could not be read at all.
    plan: ExportPlan | None = None

    @property
    def publishable(self) -> bool:
        """True when *some* target could take this app. A target may still refuse
        it on capability grounds — that is ``registry.verdict``'s answer, not this."""
        return not self.blockers


def _stronger(current: Capability, candidate: Capability, axis: tuple[Capability, ...]) -> Capability:
    """The later of two cells on ``axis`` — "needs at least this much"."""
    return candidate if axis.index(candidate) > axis.index(current) else current


def _is_write_open(node: ast.Call) -> bool:
    """True when this ``open(...)`` call opens for writing.

    Mode is the second positional argument or the ``mode`` keyword. A **non-literal**
    mode counts as a write: we cannot see it, and over-counting state is the safe
    direction. An absent mode is ``"r"`` and does not.
    """
    mode: ast.expr | None = None
    if len(node.args) >= 2:
        mode = node.args[1]
    for kw in node.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return bool(_WRITE_MODE.search(mode.value))
    return True


def _literal_str(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _scan_python(source: str) -> tuple[list[str], bool, list[str]]:
    """``(top-level absolute imports, writes anything, escaping literal paths)``.

    A file that does not parse yields ``([], True, [])`` — nothing importable to
    place, and "assume it writes", again the conservative direction. Its
    ``SyntaxError`` is the author's to see when the page runs it, not ours to
    re-report here.

    An *escaping literal path* is a string constant that is absolute or starts
    with ``~`` and is passed to something that writes. That is the one filesystem
    escape we can see statically; a computed one (``os.path.join(home, …)``) we
    cannot, which is why the docs say the scan bounds the obvious case rather
    than proving containment.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], True, []

    imports: dict[str, None] = {}
    writes = False
    escapes: list[str] = []

    def _note_path(node: ast.expr | None) -> None:
        text = _literal_str(node)
        if text and (os.path.isabs(text) or text.startswith("~")):
            escapes.append(text)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.setdefault(alias.name.split(".", 1)[0], None)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.setdefault(node.module.split(".", 1)[0], None)
        elif isinstance(node, ast.Call):
            func = node.func
            first = node.args[0] if node.args else None
            if isinstance(func, ast.Name) and func.id == "open" and _is_write_open(node):
                writes = True
                _note_path(first)
            elif isinstance(func, ast.Attribute):
                attr = func.attr
                root = func.value
                mod = root.id if isinstance(root, ast.Name) else None
                if mod == "os" and attr in _OS_WRITES:
                    writes = True
                    _note_path(first)
                elif mod == "shutil" and attr in _SHUTIL_WRITES:
                    writes = True
                    _note_path(first)
                elif attr == "open" and _is_write_open(node):
                    # `io.open(...)`, `Path(...).open("w")`, `gzip.open(...)`.
                    writes = True
                    _note_path(first)
                elif attr in _PATH_WRITES:
                    writes = True
    return list(imports), writes, escapes


def scan(html_path: str, *, include: list[str] | None = None, exclude: list[str] | None = None) -> Eligibility:
    """Classify the app whose entry page is ``html_path``.

    Runs the exporter's own scan first (so the bundle set and its blocking errors
    are the *same* ones a real publish would hit — there is no second, drifting
    notion of what ships), then parses every bundled ``.py`` for imports and
    filesystem mutation.

    ``include``/``exclude`` mirror ``/api/export``: the eligibility of an app
    depends on which files ship, so the selection has to be the same one the
    publish will use.
    """
    page_dir = os.path.dirname(os.path.abspath(html_path))
    try:
        with open(html_path, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()
    except OSError as exc:
        return Eligibility(
            page=html_path,
            runtime=Capability.RUNTIME_CPYTHON,
            state=Capability.STATE_SHARED,
            blockers=[f"cannot read the page: {exc}"],
        )

    plan = plan_export(html, page_dir, include=include, exclude=exclude)
    blockers = list(plan.errors)
    notes = list(plan.warnings)

    # Surfaces the exporter lets through but no host can serve. Notes, not
    # blockers (see _LOCAL_ONLY_SURFACES): the author's `fused.env` branch may
    # already cover them, and refusing a correctly-gated page would be wrong.
    gated = bool(_ENV_BRANCH.search(html))
    for name, why in _LOCAL_ONLY_SURFACES.items():
        if not _LOCAL_ONLY_CALL[name].search(html):
            continue
        if gated:
            notes.append(
                f"the page calls fused.{name} — {why}. It also branches on fused.env, so "
                f"check that branch covers every fused.{name} call; an ungated one fails "
                "in the reader's browser."
            )
        else:
            notes.append(
                f"the page calls fused.{name} — {why}. Nothing here reads fused.env, so "
                f"the call is not gated: guard it with `fused.env === \"local\"` or the "
                "published page breaks where it runs."
            )

    # Every .py that ships: the runPython entrypoints plus the sibling modules
    # they import (the exporter already resolved both, transitively).
    py_files: list[str] = []
    seen: set[str] = set()
    for e in plan.entrypoints:
        key = e.file.split("/", 1)[1] if "/" in e.file else e.file
        if key not in seen:
            seen.add(key)
            py_files.append(key)
    for r in plan.resources:
        if r.key not in seen:
            seen.add(r.key)
            py_files.append(r.key)
    # A bundled ASSET may also be a .py (a module reached by rawUrl, or added by
    # hand). It ships and can be imported, so it is scanned like any other.
    for a in plan.assets:
        if a.name.endswith(".py") and a.name not in seen:
            seen.add(a.name)
            py_files.append(a.name)

    runtime = Capability.RUNTIME_JS if not py_files else Capability.RUNTIME_PYODIDE
    state = Capability.STATE_NONE
    packages: dict[str, None] = {}
    unplaceable: dict[str, None] = {}
    network_seen = False
    local_names = {os.path.splitext(k)[0].rsplit("/", 1)[-1] for k in py_files}

    for key in py_files:
        src_path = os.path.join(page_dir, key)
        try:
            with open(src_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except OSError:
            # The exporter already errored on a missing file; nothing to add.
            continue
        imports, writes, escapes = _scan_python(source)
        for mod in imports:
            if mod in sys.stdlib_module_names or mod in local_names:
                if mod in _NETWORK_IMPORTS:
                    network_seen = True
                continue
            if mod in PYODIDE_IMPORTS:
                packages.setdefault(PYODIDE_IMPORTS[mod], None)
                if mod in _NETWORK_IMPORTS:
                    network_seen = True
                continue
            unplaceable.setdefault(mod, None)
        if writes:
            state = _stronger(state, Capability.STATE_CLIENT_LOCAL, STATE_AXIS)
        for path in escapes:
            blockers.append(
                f"{key} writes to {path!r}, a path outside the app folder — a published "
                "app can only touch its own files, wherever it runs"
            )

    if unplaceable:
        runtime = _stronger(runtime, Capability.RUNTIME_CPYTHON, RUNTIME_AXIS)
        notes.append(
            "these imports could not be placed in a browser Python runtime, so the app "
            "needs a real CPython process: " + ", ".join(sorted(unplaceable))
        )
    if network_seen:
        notes.append(
            "the app's Python reaches the network. In a browser that request comes from "
            "the reader's machine and is subject to CORS — an endpoint that does not send "
            "Access-Control-Allow-Origin will refuse it, however well it worked locally."
        )
    if state is Capability.STATE_CLIENT_LOCAL:
        notes.append(
            "the app's Python writes files, so it has state. On a client-local target "
            "that state lives in each reader's own browser: it survives their reloads and "
            "restarts, but it is theirs alone — it does not follow them to another device "
            "and no reader sees another's. If you meant one shared document, this is the "
            "wrong shape of target."
        )

    return Eligibility(
        page=os.path.abspath(html_path),
        runtime=runtime,
        state=state,
        blockers=blockers,
        notes=notes,
        python_files=py_files,
        pyodide_packages=sorted(packages),
        unplaceable_imports=sorted(unplaceable),
        plan=plan,
    )
