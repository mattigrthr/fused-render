"""Tests for the vendored Pyodide distribution (fused_render/publish/pyodide_dist.py).

Two kinds of test, deliberately separated.

The offline ones pin the CONTRACT — an interrupted download must not look
complete, a package the distribution does not carry must be a loud build error
rather than a page that fails in a reader's browser, and the dependency closure
must be complete because a published site has nothing to fall back to.

One networked test holds the checked-in import map to the pinned release. It is
skipped when the network is unreachable, so the suite runs offline; on a
connected machine a stale map is a red test rather than a silently wrong answer
on the Publish page.
"""

import json
import os
import urllib.error
import urllib.request

import pytest

from fused_render.publish.adapter import PublishError
from fused_render.publish import pyodide_dist


@pytest.fixture(autouse=True)
def _own_home(tmp_path, monkeypatch):
    """Never touch the real cache — a test must not delete a 10 MB download."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


def _lock(**packages):
    return {"info": {"python": "3.14.2"}, "packages": packages}


def test_an_interrupted_download_does_not_read_as_cached(tmp_path):
    root = pyodide_dist.cache_root()
    os.makedirs(root)
    # Everything but the wasm — the shape an interrupted extraction leaves.
    for name in ("pyodide.js", "python_stdlib.zip", "pyodide-lock.json"):
        with open(os.path.join(root, name), "w") as f:
            f.write("x")
    assert pyodide_dist.is_cached() is False


def test_a_complete_cache_reads_as_cached(tmp_path):
    root = pyodide_dist.cache_root()
    os.makedirs(root)
    for name in pyodide_dist.CORE_REQUIRED:
        with open(os.path.join(root, name), "w") as f:
            f.write("x")
    assert pyodide_dist.is_cached() is True


def test_the_dependency_closure_is_complete(monkeypatch):
    # loadPackage CAN resolve dependencies itself — by going to the network,
    # which is the one thing bundling exists to avoid. So every wheel in the
    # closure has to be vendored.
    lock = _lock(
        pandas={"file_name": "pandas.whl", "depends": ["numpy", "python-dateutil"], "imports": ["pandas"]},
        numpy={"file_name": "numpy.whl", "depends": [], "imports": ["numpy"]},
        **{"python-dateutil": {"file_name": "dateutil.whl", "depends": ["six"], "imports": ["dateutil"]}},
    )
    lock["packages"]["six"] = {"file_name": "six.whl", "depends": [], "imports": ["six"]}
    assert pyodide_dist.resolve_packages(["pandas"], lock=lock) == [
        "numpy", "pandas", "python-dateutil", "six",
    ]


def test_a_cycle_in_the_dependency_graph_terminates(monkeypatch):
    lock = _lock(
        a={"file_name": "a.whl", "depends": ["b"], "imports": ["a"]},
        b={"file_name": "b.whl", "depends": ["a"], "imports": ["b"]},
    )
    assert pyodide_dist.resolve_packages(["a"], lock=lock) == ["a", "b"]


def test_a_package_the_distribution_lacks_fails_the_build_loudly(monkeypatch):
    # At build time on the author's machine, naming the package — not at runtime
    # in a reader's browser, as an import error nobody will report.
    with pytest.raises(PublishError, match="no package named 'pikepdf'"):
        pyodide_dist.resolve_packages(["pikepdf"], lock=_lock())


def test_the_import_map_is_a_projection_of_the_lock():
    imports = pyodide_dist.import_map()
    # Sanity, offline: the data stack that pyproject's `bundled` extra ships
    # locally is reachable in a browser too, so those apps publish.
    assert imports["numpy"] == "numpy"
    assert imports["pandas"] == "pandas"
    assert imports["PIL"] == "pillow"


def _reachable(url):
    try:
        urllib.request.urlopen(url, timeout=20).close()
        return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


@pytest.mark.skipif(
    not _reachable(pyodide_dist.lock_url()),
    reason="offline: the checked-in import map cannot be compared to the pinned release",
)
def test_the_checked_in_import_map_matches_the_pinned_release():
    # A stale map means the Publish page tells an author their app is
    # CPython-only when it is not, or the reverse. Regenerate with
    # scripts/refresh_pyodide_imports.py in the same commit that moves the pin.
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    from refresh_pyodide_imports import build  # noqa: E402

    with urllib.request.urlopen(pyodide_dist.lock_url(), timeout=60) as r:
        live = json.load(r)
    assert pyodide_dist.import_map() == build(live)
