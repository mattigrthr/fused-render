"""Turn an export bundle into a static site a reader's browser can run.

The bundle (SPEC §18, format v2) already mirrors the author's folder — *"bundle
layout = author's folder = served tree"* — so most of this is a copy. What it is
missing is the floor the local server used to be: the page's ``window.fused``,
the Python engine behind ``runPython``, and somewhere for the reader's own state
to live. This module adds those as files.

The tree it writes::

    index.html                the page, with one <script> and the install tags injected
    <the app's own files>     verbatim, at their real page-relative paths
    _fused/
      runtime.js              window.fused, implemented in the browser
      boot.py                 the harness that calls main()
      _binding.py             a verbatim copy of the app's own param binder
      site.json               what the runtime needs to know about this app
      manifest.webmanifest    name, icon, display mode
      icon.svg                the author's, or a generated lettermark
      pyodide/                the pinned runtime + only the wheels this app imports
    fused-sw.js               offline, so Add to Home Screen means something
    _headers                  cache policy (Cloudflare Pages reads this)

Three decisions worth knowing:

**Everything ships at its real path.** The hosted runtime serves assets through an
``_asset`` allow-list; a static host has no route layer to enforce one, so every
bundled file is a URL. That is not a leak to paper over — it is the honest shape
of the target, and it has a real consequence the eligibility scan reports out
loud: an app whose Python runs in the reader's browser has published that
Python's source, because the browser has to be handed it either way.

**The page is copied to ``index.html``.** A static host resolves ``/`` to
``index.html`` and nothing else, and the canonical share URL has to be the bare
origin — it is what a QR code encodes and what someone types from memory. An app
whose entry page is ``dashboard.html`` keeps that name too, so an existing link
still works.

**``_fused/`` is ours and must be empty of theirs.** A collision is a hard error
rather than a silent overwrite: an app with its own ``_fused/`` folder would
otherwise have files replaced under it and only find out from a reader. The
service worker is the one exception to living under it: a worker's default scope
is its own directory, so one at ``_fused/sw.js`` could not control the page. It
sits at the root as ``fused-sw.js``, which needs no host-specific
``Service-Worker-Allowed`` header to work.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil

from fused_render.publish.adapter import PublishError
from fused_render.publish.eligibility import Eligibility
from fused_render.publish.icon import resolve as resolve_icon
from fused_render.publish import pyodide_dist

#: Our directory inside the published site. Underscore-prefixed to stay out of
#: the author's namespace, and safe on Pages: only specific underscore-prefixed
#: FILES at the root (``_headers``, ``_redirects``, ``_routes.json``,
#: ``_worker.js``) are reserved there, never directories.
RUNTIME_DIR = "_fused"

#: The service worker, at the site ROOT — see the module docstring on scope.
SW_NAME = "fused-sw.js"

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

#: Where to inject the runtime tag: immediately after ``<head>``, so it is
#: parser-blocking and ``window.fused`` exists before the page's own first inline
#: script runs. Case-insensitive because HTML is.
_HEAD_OPEN = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_HTML_OPEN = re.compile(r"<html\b[^>]*>", re.IGNORECASE)

#: A page's encoding declaration, which the HTML spec requires inside the first
#: 1024 bytes. Our tags go AFTER it when there is one — pushing a charset past
#: that limit would make the browser guess the encoding, and this app's pages are
#: full of characters (Chinese, em dashes) that guess wrong.
_CHARSET = re.compile(
    r"<meta\b[^>]*\b(?:charset\s*=|http-equiv\s*=\s*[\"\']content-type)[^>]*>",
    re.IGNORECASE,
)

#: Pulled out of the page's <title> for the install prompt's app name. A page
#: with no title falls back to the folder name, which is what the sidebar shows.
_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)

#: Files a reader needs before the app can run at all, so the service worker
#: precaches them. Everything else is cached on first use — precaching the whole
#: Pyodide distribution would make the install a 30 MB stall on a phone.
_PRECACHE_EXTRA = ("./", "index.html")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _inject(html: str, head_tags: str) -> str:
    """Put ``head_tags`` at the start of the page's ``<head>``, after its charset.

    "After the charset" is the one ordering constraint that outranks ours: the
    encoding declaration has to stay inside the document's first 1024 bytes or
    the browser guesses, and these pages are full of characters that guess wrong.
    Everything else in a head is metadata, so going in front of it costs nothing —
    what matters is being ahead of the page's own SCRIPTS, which this is.

    Falls back to just after ``<html>``, then to the front of the document, for a
    fragment with no head. A browser hoists a stray ``<script>`` into the head it
    synthesizes, so the ordering guarantee holds in all three cases.
    """
    m = _HEAD_OPEN.search(html)
    if m:
        at = m.end()
        charset = _CHARSET.search(html, at)
        if charset and charset.start() - at < 512:
            at = charset.end()
        return html[:at] + head_tags + html[at:]
    m = _HTML_OPEN.search(html)
    if m:
        return html[: m.end()] + "<head>" + head_tags + "</head>" + html[m.end() :]
    return head_tags + html


def _head_tags() -> str:
    """The tags every published page gets, in the order they must appear.

    The runtime first and parser-blocking; then the install metadata a browser
    reads to offer Add to Home Screen. ``apple-touch-icon`` is not redundant with
    the manifest: iOS ignores the manifest's icons for the home screen and uses
    this link, which is precisely the platform the storage-lifetime problem lives
    on.
    """
    return (
        f'\n<script src="{RUNTIME_DIR}/runtime.js"></script>'
        f'\n<link rel="manifest" href="{RUNTIME_DIR}/manifest.webmanifest" />'
        f'\n<link rel="apple-touch-icon" href="{RUNTIME_DIR}/icon.svg" />'
        f'\n<link rel="icon" type="image/svg+xml" href="{RUNTIME_DIR}/icon.svg" />'
        '\n<meta name="mobile-web-app-capable" content="yes" />'
        '\n<meta name="apple-mobile-web-app-capable" content="yes" />'
        '\n<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />\n'
    )


def _manifest(name: str, title: str) -> dict:
    """The web app manifest.

    ``display: standalone`` and ``scope: "./"`` are what turn the installed app
    into its own window rather than a browser tab pointed at a URL — and, on iOS,
    what puts it in the storage-exempt bucket the parent issue's open question is
    about. The icon is declared ``any maskable`` so an Android launcher may crop
    it to its own shape without letterboxing it (the generated lettermark is
    full-bleed for exactly this).
    """
    return {
        "name": title,
        "short_name": title[:12],
        "id": f"/{name}",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0f1216",
        "theme_color": "#0f1216",
        "icons": [
            {"src": "icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}
        ],
    }


#: Cloudflare Pages reads this file (and other static hosts ignore it harmlessly).
#:
#: The app's own files are `no-cache`: they must be REVALIDATED on every load, or
#: a re-publish would not reach a reader who already has the page — the exact
#: failure "the author pushed a fix and nobody sees it" is made of. The runtime
#: and the Pyodide distribution are immutable-by-content (the distribution is
#: version-pinned, and the runtime is revalidated with the page) so they get a
#: long max-age: that is the ~7 MB a reader must not re-download on every visit.
_HEADERS = """# Written by fused-render's publish adapter.
/*
  Cache-Control: no-cache
/_fused/pyodide/*
  Cache-Control: public, max-age=31536000, immutable
"""


def _build_id(site_dir: str) -> str:
    """A short digest of everything published, for the service worker's cache name.

    Content-derived rather than a timestamp: two publishes of an unchanged app
    keep the same id, so readers are not made to re-download an identical site
    because the author clicked Publish twice.
    """
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(site_dir):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            digest.update(os.path.relpath(path, site_dir).encode("utf-8"))
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    digest.update(chunk)
    return digest.hexdigest()[:16]


def build(
    bundle_dir: str,
    site_dir: str,
    *,
    app_dir: str,
    name: str,
    elig: Eligibility,
) -> dict:
    """Write the static site for ``bundle_dir`` into ``site_dir``.

    ``site_dir`` must be empty or absent — same non-destructive stance as
    ``export_page``: this writes a self-contained tree and must never be able to
    delete an author's files because a path was mistyped.

    Returns the ``site.json`` it wrote, which is also the summary the Publish page
    shows (what shipped, which packages, how big).
    """
    if os.path.isdir(site_dir) and os.listdir(site_dir):
        raise PublishError(
            f"cannot build the site into {site_dir}: the directory must be empty."
        )
    with open(os.path.join(bundle_dir, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    payload = os.path.join(bundle_dir, manifest.get("root", "files"))
    page_key = manifest["page"]

    for taken, what in ((RUNTIME_DIR, "folder"), (SW_NAME, "file")):
        if os.path.exists(os.path.join(payload, taken)):
            raise PublishError(
                f"this app has its own {taken} {what}, which is the name a published site "
                f"uses for its runtime. Rename it and publish again."
            )

    os.makedirs(site_dir, exist_ok=True)
    data_keys: list[str] = []
    for root, _dirs, files in os.walk(payload):
        for fn in files:
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, payload).replace(os.sep, "/")
            dest = os.path.join(site_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(src, dest)
            if rel != page_key and not rel.endswith(".py"):
                data_keys.append(rel)

    # The page: inject the runtime, then place it at index.html so the bare
    # origin resolves. Its original name is kept too, so a link someone already
    # has does not break.
    page_html = _read(os.path.join(payload, page_key))
    title = (_TITLE.search(page_html).group(1).strip() if _TITLE.search(page_html) else "") or name
    injected = _inject(page_html, _head_tags())
    _write(os.path.join(site_dir, page_key), injected)
    if page_key != "index.html":
        _write(os.path.join(site_dir, "index.html"), injected)

    runtime = os.path.join(site_dir, RUNTIME_DIR)
    os.makedirs(runtime, exist_ok=True)
    shutil.copyfile(os.path.join(_STATIC, "runtime.js"), os.path.join(runtime, "runtime.js"))
    shutil.copyfile(os.path.join(_STATIC, "boot.py"), os.path.join(runtime, "boot.py"))
    # The param binder, byte for byte. Copied rather than re-implemented so the
    # browser binds `main()`'s arguments under the same rules as both local
    # engines — the same reason the fused engine reads this file's source (D167).
    from fused_render import _binding

    shutil.copyfile(_binding.__file__, os.path.join(runtime, "_binding.py"))

    icon_svg, icon_is_authors = resolve_icon(app_dir, name)
    _write(os.path.join(runtime, "icon.svg"), icon_svg)
    _write(
        os.path.join(runtime, "manifest.webmanifest"),
        json.dumps(_manifest(name, title), indent=2) + "\n",
    )

    packages = _vendor_pyodide(runtime, elig.pyodide_packages, needed=bool(elig.python_files))

    site = {
        "fused_render_site": 1,
        "page": page_key,
        "app": {"name": name, "title": title},
        "python": list(elig.python_files),
        "data": sorted(data_keys),
        "packages": packages,
        "pyodide": {
            "indexURL": f"{RUNTIME_DIR}/pyodide/",
            "version": pyodide_dist.PYODIDE_VERSION,
        },
        "icon": {"source": "app" if icon_is_authors else "generated"},
    }
    _write(os.path.join(runtime, "site.json"), json.dumps(site, indent=2, sort_keys=True) + "\n")
    _write(os.path.join(site_dir, "_headers"), _HEADERS)

    # The service worker last: its cache name is a digest of everything above, so
    # it has to be written after the rest exists. Its own bytes are therefore not
    # in the digest, which is correct — the worker is a function OF the site, and
    # including it would make the id unstable for an unchanged app.
    _write(
        os.path.join(site_dir, SW_NAME),
        _read(os.path.join(_STATIC, "sw.js"))
        .replace("__BUILD_ID__", _build_id(site_dir))
        .replace(
            "__PRECACHE__",
            json.dumps(
                list(_PRECACHE_EXTRA)
                + [f"{RUNTIME_DIR}/{n}" for n in ("runtime.js", "site.json", "icon.svg")]
            ),
        ),
    )
    site["bytes"] = _total_bytes(site_dir)
    return site


def _vendor_pyodide(runtime_dir: str, packages: list[str], *, needed: bool) -> list[str]:
    """Copy the pinned Pyodide core, and the app's wheels, into the site.

    Returns the resolved package NAMES (the dependency closure), which is what
    ``site.json`` carries and what the runtime hands ``loadPackage``. Names, not
    URLs, because every wheel lands **beside** ``pyodide-lock.json`` — the layout
    the full distribution uses — so Pyodide resolves each ``file_name`` against
    our own directory and walks the dependency graph itself, without a single
    request leaving the site.

    Skipped entirely for an app with no Python: a pure HTML/JS app has no reason
    to carry a 7 MB interpreter, and its publish is then a plain file copy with no
    network step at all.
    """
    if not needed:
        return []
    core = pyodide_dist.ensure_core()
    resolved = pyodide_dist.resolve_packages(packages)
    wheels = pyodide_dist.ensure_wheels(packages)
    dest = os.path.join(runtime_dir, "pyodide")
    os.makedirs(dest, exist_ok=True)
    for filename in pyodide_dist.CORE_FILES:
        src = os.path.join(core, filename)
        # CORE_OPTIONAL names move between releases (see pyodide_dist); the
        # required ones were checked when the distribution was cached.
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(dest, filename))
    for wheel in wheels:
        shutil.copyfile(wheel, os.path.join(dest, os.path.basename(wheel)))
    return resolved


def _total_bytes(site_dir: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(site_dir):
        for fn in files:
            total += os.path.getsize(os.path.join(root, fn))
    return total
