"""Tests for the static-site build (fused_render/publish/site.py).

The site is what a reader's browser actually gets, so these pin the properties a
published app depends on and that nothing else would catch: the runtime script is
ahead of the page's own scripts but behind its charset, the bare origin resolves,
our directory cannot silently overwrite the author's, and the service worker's
cache id is derived from content rather than from the clock.

Nothing here downloads Pyodide. The distribution fetch is its own seam
(`pyodide_dist`) and is faked, so this file runs offline and in CI.
"""

import json
import os

import pytest

from fused_render.export import export_page
from fused_render.publish.adapter import PublishError
from fused_render.publish.eligibility import scan
from fused_render.publish import site as site_mod


@pytest.fixture
def no_download(monkeypatch, tmp_path):
    """Stand in for the Pyodide distribution with three empty files.

    The build's contract with `pyodide_dist` is "core files land in
    _fused/pyodide/, wheels land beside them, names go in site.json" — which is
    exactly what this fakes, and exactly what a test of the SITE should assert.
    Whether the real distribution has those files is `pyodide_dist`'s own test.
    """
    fake = tmp_path / "pyodide-cache"
    (fake / "packages").mkdir(parents=True)
    for name in ("pyodide.js", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json"):
        (fake / name).write_text("x", encoding="utf-8")
    wheel = fake / "packages" / "numpy-1.0-py3-none-any.whl"
    wheel.write_text("wheel", encoding="utf-8")
    monkeypatch.setattr(site_mod.pyodide_dist, "ensure_core", lambda *a, **k: str(fake))
    monkeypatch.setattr(site_mod.pyodide_dist, "ensure_wheels", lambda names, **k: [str(wheel)] if names else [])
    monkeypatch.setattr(site_mod.pyodide_dist, "resolve_packages", lambda names, **k: sorted(names))
    return fake


def _build(tmp_path, files, *, page="index.html", name="demo", include=None):
    app = tmp_path / "app"
    app.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = app / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    elig = scan(str(app / page), include=include)
    assert not elig.blockers, elig.blockers
    export_page(str(app / page), str(tmp_path / "bundle"), include=include)
    result = site_mod.build(
        str(tmp_path / "bundle"),
        str(tmp_path / "site"),
        app_dir=str(app),
        name=name,
        elig=elig,
    )
    return tmp_path / "site", result


def test_a_plain_page_ships_with_no_python_runtime_at_all(tmp_path, no_download):
    out, site = _build(tmp_path, {"index.html": "<html><head></head><body>hi</body></html>"})
    # A pure HTML/JS app has no reason to carry a 7 MB interpreter.
    assert not (out / "_fused" / "pyodide").exists()
    assert site["python"] == [] and site["packages"] == []


def test_the_runtime_script_precedes_the_pages_own_scripts(tmp_path, no_download):
    html = '<html><head><title>T</title></head><body><script>fused.rawUrl("x")</script></body></html>'
    out, _ = _build(tmp_path, {"index.html": html, "x": "data"})
    text = (out / "index.html").read_text(encoding="utf-8")
    assert text.index("_fused/runtime.js") < text.index('fused.rawUrl("x")')


def test_the_charset_declaration_stays_ahead_of_our_tags(tmp_path, no_download):
    # It has to remain inside the document's first 1024 bytes, or the browser
    # guesses the encoding — and these pages are full of characters that guess
    # wrong. Our block is ~400 bytes, so going first would eat the budget.
    html = '<html><head>\n<meta charset="utf-8" />\n<title>T</title></head><body>汉字</body></html>'
    out, _ = _build(tmp_path, {"index.html": html})
    text = (out / "index.html").read_text(encoding="utf-8")
    assert text.index("charset") < text.index("_fused/runtime.js")


def test_a_page_that_is_not_index_html_is_also_served_at_the_bare_origin(tmp_path, no_download):
    # The canonical share URL is the origin — it is what a QR code encodes and
    # what someone types from memory — and a static host resolves "/" to
    # index.html and nothing else.
    out, site = _build(
        tmp_path, {"dashboard.html": "<html><head></head><body>d</body></html>"}, page="dashboard.html"
    )
    assert (out / "index.html").read_text() == (out / "dashboard.html").read_text()
    assert site["page"] == "dashboard.html"


def test_our_directory_never_silently_overwrites_the_authors(tmp_path, no_download):
    html = '<html><head></head><body><script>fused.rawUrl("_fused/mine.txt")</script></body></html>'
    with pytest.raises(PublishError, match="_fused"):
        _build(tmp_path, {"index.html": html, "_fused/mine.txt": "theirs"})


def test_the_service_worker_name_collision_is_caught_too(tmp_path, no_download):
    html = '<html><head></head><body><script>fused.rawUrl("fused-sw.js")</script></body></html>'
    with pytest.raises(PublishError, match="fused-sw.js"):
        _build(tmp_path, {"index.html": html, "fused-sw.js": "theirs"})


def test_python_ships_with_the_binder_the_local_engines_use(tmp_path, no_download):
    # Copied verbatim, not paraphrased: a URL's "7" must bind to an `n: int`
    # parameter in the browser exactly as it does locally (D167's pattern).
    from fused_render import _binding

    out, site = _build(
        tmp_path,
        {
            "index.html": '<html><head></head><body><script>fused.runPython("./calc.py")</script></body></html>',
            "calc.py": "def main(n: int = 1):\n    return {'n': n}\n",
        },
    )
    assert site["python"] == ["calc.py"]
    shipped = (out / "_fused" / "_binding.py").read_text(encoding="utf-8")
    with open(_binding.__file__, encoding="utf-8") as f:
        assert shipped == f.read()


def test_wheels_land_beside_the_lock_so_pyodide_resolves_them_itself(tmp_path, no_download):
    out, site = _build(
        tmp_path,
        {
            "index.html": '<html><head></head><body><script>fused.runPython("./calc.py")</script></body></html>',
            "calc.py": "import numpy\n\ndef main():\n    return {}\n",
        },
    )
    # Beside pyodide-lock.json — the layout the full distribution uses — so
    # loadPackage("numpy") resolves the wheel out of our own directory and never
    # reaches the network.
    assert (out / "_fused" / "pyodide" / "numpy-1.0-py3-none-any.whl").is_file()
    assert (out / "_fused" / "pyodide" / "pyodide-lock.json").is_file()
    assert site["packages"] == ["numpy"]


def test_an_app_without_an_icon_gets_a_generated_one(tmp_path, no_download):
    out, site = _build(tmp_path, {"index.html": "<html><head></head><body>x</body></html>"}, name="hsk-cards")
    assert site["icon"]["source"] == "generated"
    svg = (out / "_fused" / "icon.svg").read_text(encoding="utf-8")
    assert ">HC<" in svg  # the initials, so two apps are told apart on a home screen


def test_the_authors_icon_wins_when_there_is_one(tmp_path, no_download):
    out, site = _build(
        tmp_path,
        {"index.html": "<html><head></head><body>x</body></html>", "icon.svg": "<svg id='mine'/>"},
    )
    assert site["icon"]["source"] == "app"
    assert "mine" in (out / "_fused" / "icon.svg").read_text(encoding="utf-8")


def test_the_manifest_makes_the_app_installable_and_scoped_to_itself(tmp_path, no_download):
    out, _ = _build(tmp_path, {"index.html": "<html><head><title>HSK Cards</title></head><body>x</body></html>"})
    manifest = json.loads((out / "_fused" / "manifest.webmanifest").read_text(encoding="utf-8"))
    # standalone + a relative scope is what makes an installed app its own window
    # — and, on iOS, what puts it in the storage bucket ITP does not clear.
    assert manifest["display"] == "standalone"
    assert manifest["scope"] == "./" and manifest["start_url"] == "./"
    assert manifest["name"] == "HSK Cards"  # from <title>, not the folder name
    assert manifest["icons"][0]["purpose"] == "any maskable"


def test_the_worker_lives_at_the_root_so_its_scope_covers_the_page(tmp_path, no_download):
    out, _ = _build(tmp_path, {"index.html": "<html><head></head><body>x</body></html>"})
    # A worker's default scope is its own directory: one under _fused/ could not
    # control the page, and widening it needs a host-specific header.
    assert (out / "fused-sw.js").is_file()
    assert not (out / "_fused" / "sw.js").exists()


def test_the_workers_cache_id_comes_from_content_not_the_clock(tmp_path, no_download):
    files = {"index.html": "<html><head></head><body>x</body></html>"}
    out_a, _ = _build(tmp_path / "a", files)
    out_b, _ = _build(tmp_path / "b", files)
    changed, _ = _build(tmp_path / "c", {"index.html": "<html><head></head><body>y</body></html>"})

    def cache_id(root):
        line = next(l for l in (root / "fused-sw.js").read_text().splitlines() if "const CACHE" in l)
        return line

    # Publishing twice without changing anything must not make every reader
    # re-download the site.
    assert cache_id(out_a) == cache_id(out_b)
    assert cache_id(out_a) != cache_id(changed)


def test_the_apps_own_files_are_revalidated_on_every_load(tmp_path, no_download):
    out, _ = _build(tmp_path, {"index.html": "<html><head></head><body>x</body></html>"})
    headers = (out / "_headers").read_text(encoding="utf-8")
    # Otherwise the author pushes a fix and the people already using the app
    # never see it. The version-pinned runtime is the only thing cached hard.
    assert "/*\n  Cache-Control: no-cache" in headers
    assert "immutable" in headers.split("/_fused/pyodide/*")[1]


def test_building_into_a_non_empty_directory_is_refused(tmp_path, no_download):
    out, _ = _build(tmp_path, {"index.html": "<html><head></head><body>x</body></html>"})
    with pytest.raises(PublishError, match="must be empty"):
        site_mod.build(
            str(tmp_path / "bundle"), str(out), app_dir=str(tmp_path / "app"),
            name="demo", elig=scan(str(tmp_path / "app" / "index.html")),
        )
