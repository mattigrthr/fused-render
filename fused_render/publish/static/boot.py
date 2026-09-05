"""The in-browser half of ``fused.runPython`` — calls a page's ``main()``.

Runs inside Pyodide, in the reader's tab, with the app's tree at ``/app`` as both
the working directory and ``sys.path[0]``. That placement is the whole
compatibility story: a bundled entrypoint's ``open("data.csv")`` and
``import helpers`` resolve here exactly as they do on the author's machine
(EXPORT.md, "Reading a bundled file / importing a module"), so an app is
published without editing a line of its Python.

Two things it deliberately does NOT reimplement:

* **Parameter binding.** ``_binding.py`` is copied in verbatim by the site build
  and imported here, so a URL's ``"7"`` reaches an ``n: int`` parameter as ``7``
  under the same rules as both local engines. Copying the file rather than
  paraphrasing it is the established pattern (D167: the fused engine reads that
  same source and ``exec``s it inside generated code) and exists for the same
  reason — one definition of the coercion rules.
* **The result shape.** ``{"ok", "result", "stdout", "stderr"}`` on success and
  ``{"ok": False, "error": {"type", "message", "traceback"}}`` on failure, the
  same envelope ``executor.py`` returns, so the page's error overlay and any
  ``catch`` the author wrote keep working.

Modules are re-imported per call rather than cached. A page's ``.py`` is
stateless by contract, and a fresh module per call is what the subprocess engine
gives locally — caching would introduce a difference in behaviour that only shows
up once published, which is the worst place to find one.
"""

import importlib
import importlib.util
import io
import json
import os
import sys
import traceback

import _binding


def _module_for(path):
    """Import the ``.py`` at page-relative ``path`` as a throwaway module.

    Built with ``module_from_spec`` + ``exec_module`` and never inserted into
    ``sys.modules``, so repeated calls get independent module objects and a page
    that reloads its own data on import behaves the way it does locally.
    """
    rel = os.path.normpath(path.replace("\\", "/"))
    if rel.startswith(".."):
        raise ValueError(f"{path!r} escapes the app")
    full = os.path.join(os.getcwd(), rel)
    if not os.path.isfile(full):
        raise FileNotFoundError(f"{path} is not part of this app")
    spec = importlib.util.spec_from_file_location("__fused_entry__", full)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def call(payload_json):
    """Run one ``fused.runPython`` call. Takes and returns JSON strings.

    JSON on both sides rather than Pyodide's automatic proxying: the boundary
    then behaves like the HTTP one it replaces — a value that would not have
    survived the wire locally does not survive it here either, and the page gets
    the same "not JSON-serializable" message instead of a JsProxy it cannot use.
    """
    request = json.loads(payload_json)
    path = request["path"]
    params = request.get("params") or {}
    captured = io.StringIO()
    real_stdout, sys.stdout = sys.stdout, captured
    try:
        module = _module_for(path)
        fn = getattr(module, "main", None)
        if not callable(fn):
            raise AttributeError(
                f"{os.path.basename(path)} does not define a callable 'main' function"
            )
        result = fn(**_binding.bind_params(fn, params))
        try:
            json.dumps(result)
        except (TypeError, ValueError):
            raise TypeError(
                f"main() returned {type(result).__name__}, which is not JSON-serializable; "
                "return dict/list/str/number/bool/None"
            ) from None
        return json.dumps(
            {"ok": True, "result": result, "stdout": captured.getvalue(), "stderr": ""}
        )
    except BaseException as exc:  # noqa: BLE001 — mirror executor.py's catch-all
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
                "stdout": captured.getvalue(),
                "stderr": "",
            }
        )
    finally:
        sys.stdout = real_stdout
