"""Tests for the runtime x state classification (fused_render/publish/eligibility.py).

The scan is the provider-agnostic half of Publish: it decides what an app would
ask of a host, and every target's offer/refuse decision is downstream of it. So
these pin BOTH directions — that a plain app is placed at the weakest cells, and
that each thing which raises a requirement actually raises it.
"""

import pytest

from fused_render.publish.adapter import Capability
from fused_render.publish.eligibility import scan


def _app(tmp_path, html, **files):
    (tmp_path / "index.html").write_text(html, encoding="utf-8")
    for name, content in files.items():
        p = tmp_path / name.replace("__", "/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return str(tmp_path / "index.html")


def test_a_page_with_no_python_is_js_and_stateless(tmp_path):
    page = _app(tmp_path, "<html><body><h1>hi</h1></body></html>")
    elig = scan(page)
    assert elig.runtime is Capability.RUNTIME_JS
    assert elig.state is Capability.STATE_NONE
    assert elig.blockers == []
    assert elig.python_files == []


def test_stdlib_only_python_is_pyodide_and_stateless(tmp_path):
    page = _app(
        tmp_path,
        '<html><script>fused.runPython("./calc.py")</script></html>',
        **{"calc.py": "import json, math\n\ndef main():\n    return {'x': math.pi}\n"},
    )
    elig = scan(page)
    assert elig.runtime is Capability.RUNTIME_PYODIDE
    assert elig.state is Capability.STATE_NONE
    assert elig.pyodide_packages == []
    assert elig.python_files == ["calc.py"]


def test_a_bundled_data_stack_import_becomes_a_vendored_wheel(tmp_path):
    page = _app(
        tmp_path,
        '<html><script>fused.runPython("./calc.py")</script></html>',
        **{"calc.py": "import pandas as pd\nfrom PIL import Image\n\ndef main():\n    return {}\n"},
    )
    elig = scan(page)
    assert elig.runtime is Capability.RUNTIME_PYODIDE
    assert elig.pyodide_packages == ["pandas", "pillow"]
    assert elig.unplaceable_imports == []


def test_an_import_we_cannot_place_forces_cpython(tmp_path):
    page = _app(
        tmp_path,
        '<html><script>fused.runPython("./calc.py")</script></html>',
        **{"calc.py": "import pikepdf\n\ndef main():\n    return {}\n"},
    )
    elig = scan(page)
    assert elig.runtime is Capability.RUNTIME_CPYTHON
    assert elig.unplaceable_imports == ["pikepdf"]
    # Not a blocker: some OTHER target may run CPython. It is the capability
    # comparison, not the scan, that refuses a target.
    assert elig.blockers == []
    assert any("real CPython process" in n for n in elig.notes)


def test_a_sibling_module_import_is_first_party_not_third_party(tmp_path):
    page = _app(
        tmp_path,
        '<html><script>fused.runPython("./calc.py")</script></html>',
        **{
            "calc.py": "import helpers\n\ndef main():\n    return helpers.go()\n",
            "helpers.py": "def go():\n    return {'ok': True}\n",
        },
    )
    elig = scan(page)
    assert elig.runtime is Capability.RUNTIME_PYODIDE
    assert elig.unplaceable_imports == []
    assert set(elig.python_files) == {"calc.py", "helpers.py"}


@pytest.mark.parametrize(
    "body",
    [
        "open('out.json', 'w').write('{}')",
        "import os\nos.replace('a', 'b')",
        "import shutil\nshutil.copyfile('a', 'b')",
        "from pathlib import Path\nPath('x').write_text('y')",
        "import io\nio.open('f', mode='a')",
    ],
)
def test_any_filesystem_mutation_makes_the_app_stateful(tmp_path, body):
    page = _app(
        tmp_path,
        '<html><script>fused.runPython("./calc.py")</script></html>',
        **{"calc.py": f"def main():\n    {body.replace(chr(10), chr(10) + '    ')}\n    return {{}}\n"},
    )
    elig = scan(page)
    assert elig.state is Capability.STATE_CLIENT_LOCAL
    assert any("each reader's own browser" in n for n in elig.notes)


def test_a_read_only_open_is_not_state(tmp_path):
    page = _app(
        tmp_path,
        '<html><script>fused.runPython("./calc.py")</script></html>',
        **{"calc.py": "def main():\n    return {'t': open('data.txt').read()}\n"},
    )
    assert scan(page).state is Capability.STATE_NONE


def test_an_unreadable_mode_counts_as_a_write(tmp_path):
    # Over-counting state costs a target; under-counting ships an app whose saves
    # silently go nowhere. The scan takes the first.
    page = _app(
        tmp_path,
        '<html><script>fused.runPython("./calc.py")</script></html>',
        **{"calc.py": "def main(mode='r'):\n    return {'x': open('f', mode).read()}\n"},
    )
    assert scan(page).state is Capability.STATE_CLIENT_LOCAL


def test_a_write_outside_the_app_folder_blocks_every_target(tmp_path):
    page = _app(
        tmp_path,
        '<html><script>fused.runPython("./calc.py")</script></html>',
        **{"calc.py": "def main():\n    open('/tmp/scratch.json', 'w').write('{}')\n    return {}\n"},
    )
    elig = scan(page)
    assert not elig.publishable
    assert any("outside the app folder" in b for b in elig.blockers)


def test_an_export_error_is_a_publish_blocker_verbatim(tmp_path):
    # The gating socket ExportPlan.errors always described ("Deploy is disabled")
    # finally has a consumer: an export error blocks publishing, unaltered.
    page = _app(tmp_path, '<html><script>fused.writeFile("x", "y")</script></html>')
    elig = scan(page)
    assert not elig.publishable
    assert any("fused.writeFile() is not supported" in b for b in elig.blockers)


def test_an_ungated_local_only_surface_is_a_note_naming_the_gate(tmp_path):
    page = _app(tmp_path, '<html><script>fused.capture.screenshot()</script></html>')
    elig = scan(page)
    assert elig.publishable  # a note, never a refusal
    note = next(n for n in elig.notes if "fused.capture" in n)
    assert "not gated" in note and 'fused.env === "local"' in note


def test_a_page_that_reads_fused_env_gets_the_check_your_gate_wording(tmp_path):
    page = _app(
        tmp_path,
        '<html><script>if (fused.env === "local") fused.capture.screenshot()</script></html>',
    )
    note = next(n for n in scan(page).notes if "fused.capture" in n)
    assert "check that branch covers" in note


def test_a_py_that_does_not_parse_is_assumed_to_write(tmp_path):
    page = _app(
        tmp_path,
        '<html><script>fused.runPython("./calc.py")</script></html>',
        **{"calc.py": "def main(:\n"},
    )
    assert scan(page).state is Capability.STATE_CLIENT_LOCAL


def test_network_python_earns_a_cors_note(tmp_path):
    page = _app(
        tmp_path,
        '<html><script>fused.runPython("./calc.py")</script></html>',
        **{"calc.py": "import requests\n\ndef main():\n    return requests.get('https://x').json()\n"},
    )
    elig = scan(page)
    assert elig.pyodide_packages == ["requests"]
    assert any("CORS" in n for n in elig.notes)


def test_an_unbacked_computed_asset_path_blocks_the_publish(tmp_path):
    # The failure this test exists for: the app works locally, because the dev
    # server hands out the whole folder, and then every deck 404s on the hosted
    # origin. An export only warns — it may be handed to something that fills
    # the gap — but a publish IS the hosting, so there is nothing left to fill it.
    page = _app(
        tmp_path,
        "<html><script>s.src = fused.rawUrl(`data/hsk${n}.js`)</script></html>",
        **{"data__hsk1.js": "window.HSK_DATA = {};"},
    )
    elig = scan(page)
    assert not elig.publishable
    blocker = next(b for b in elig.blockers if "computed path" in b)
    assert "would miss for every reader" in blocker
    # The remedy has to be one this page can actually offer. "Include files" is
    # the export dialog's per-deployment picker, which Publish never sends and
    # which would not travel with the app anyway.
    assert "fused-bundle" in blocker
    assert "Include files" not in blocker
    assert not any("computed path" in n for n in elig.notes)


def test_a_manifest_glob_clears_the_computed_asset_blocker(tmp_path):
    # The fix, and the proof that the blocker is about what SHIPPED rather than
    # about the call being computed: the glob is what puts the files in the site.
    page = _app(
        tmp_path,
        '<html><head><script type="application/fused-bundle">\n'
        '{ "include": ["data/*.js"] }\n'
        "</script></head>"
        "<script>s.src = fused.rawUrl(`data/hsk${n}.js`)</script></html>",
        **{"data__hsk1.js": "window.HSK_DATA = {};"},
    )
    elig = scan(page)
    assert elig.blockers == []
    assert elig.publishable
