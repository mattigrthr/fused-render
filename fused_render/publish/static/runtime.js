/**
 * `window.fused` for a PUBLISHED page — the portable subset, implemented in the
 * reader's browser.
 *
 * The local runtime (fused_render/static/runtime.js) is injected by `/render`
 * and talks to a server on 127.0.0.1 that has the author's filesystem behind it.
 * A published page has neither. This file gives the SAME page the same API with
 * a different floor under it:
 *
 *   fused.runPython  -> Pyodide, in this tab, over a persistent virtual filesystem
 *   fused.rawUrl     -> a plain relative URL (every bundled file is served)
 *   fused.readFile   -> fetch()
 *   fused.params     -> the page's own URL, same semantics as SPEC §6
 *   fused.env        -> "hosted"
 *
 * It is deliberately standalone: no imports, no build step, ES2020, one IIFE.
 * It is loaded parser-blocking at the very top of <head>, so `window.fused`
 * exists before any of the page's own script runs — a page whose first inline
 * script calls `fused.runPython` must not have to wait for anything.
 *
 * ## Why the Python runs here at all
 *
 * Because the alternative is a server, and the whole point is that the author
 * does not run one. Pyodide costs the reader a one-time download of a runtime
 * that then lives in their HTTP cache; it costs the author nothing per view,
 * forever, on a free tier. That trade is what makes "share it with my class"
 * a thing an individual can do.
 *
 * ## fused.env is "hosted", not a new value
 *
 * A page already branches on `fused.env === "local"` to gate the surfaces that
 * only exist on the author's machine (EXPORT.md). Every one of those gates is
 * correct here for exactly the same reason, so reporting a THIRD value would
 * break working pages to express a distinction they do not act on. The finer
 * one is available as `fused.runtime === "pyodide-static"` for a page that
 * genuinely wants it.
 *
 * ## State: the overlay
 *
 * Python here writes to an in-memory filesystem that would vanish on reload.
 * That is not acceptable — a flashcard app whose progress resets is not an app.
 * So the filesystem is seeded from the published files on every boot, and every
 * DIFFERENCE the reader's own Python then makes to it is kept in `localStorage`
 * as an OVERLAY keyed by path.
 *
 * Seeding-then-overlaying, rather than snapshotting the whole tree, is what
 * makes a re-publish safe. The author ships new code and new data; the reader's
 * overlay is re-applied on top of it. Their progress survives an update to the
 * app, which is the difference between a toy and something you would ask a class
 * to use. (It also keeps the stored bytes to what the reader actually created,
 * instead of a copy of the whole app in every visitor's browser.)
 *
 * `localStorage` is origin-scoped, which is why the publish adapter must update
 * a deployment in place and never mint a new origin — see publish/record.py.
 */
(function () {
  "use strict";
  if (window.fused) return; // already injected (a page included the tag twice)

  var SITE = null; // _fused/site.json, loaded lazily with Pyodide
  var BASE = new URL(".", document.baseURI).href;

  // ---- theme --------------------------------------------------------------
  // Same contract as the local runtime: `data-fused-theme` on <html> opts a page
  // into having `data-theme` kept in step, everything else just gets
  // `color-scheme` so the browser's own defaults match. Read from the same
  // localStorage key so an app that offers its own light/dark switch keeps
  // working when published; with no shell to write it, the OS preference is
  // what normally decides.
  var THEME_KEY = "fused-render:theme";
  var DARK_QUERY = "(prefers-color-scheme: dark)";

  function resolvedTheme() {
    var pref = null;
    try {
      pref = localStorage.getItem(THEME_KEY);
    } catch (e) {
      /* blocked storage — fall through to the OS preference */
    }
    if (pref === "light" || pref === "dark") return pref;
    try {
      return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
    } catch (e) {
      return "dark";
    }
  }

  (function startTheme() {
    var root = document.documentElement;
    if (!root) return;
    var optedIn = root.hasAttribute("data-fused-theme");
    var apply = function () {
      var theme = resolvedTheme();
      root.style.colorScheme = theme;
      if (optedIn) root.setAttribute("data-theme", theme);
    };
    apply();
    window.addEventListener("storage", function (e) {
      if (e.key === null || e.key === THEME_KEY) apply();
    });
    try {
      window.matchMedia(DARK_QUERY).addEventListener("change", apply);
    } catch (e) {
      /* no matchMedia */
    }
  })();

  // ---- fused.params (SPEC §6) ---------------------------------------------
  // A published page is a top-level document: no shell, no ancestor frames, no
  // `_layout` span. So this is the local runtime's contract minus the parts that
  // only exist inside the app — reserved `_`-prefixed keys, string-or-null
  // values, `{history:"replace"}`, `{default:…}`, and change notification on
  // both our writes and the reader's Back button.
  var paramListeners = [];
  var lastSnapshot = null;

  function isReserved(key) {
    return key.charAt(0) === "_";
  }

  function currentParams() {
    return new URLSearchParams(window.location.search);
  }

  function getParam(key) {
    if (isReserved(key)) return undefined;
    var p = currentParams();
    return p.has(key) ? p.get(key) : undefined;
  }

  function getAllParams() {
    var out = {};
    currentParams().forEach(function (value, key) {
      if (!isReserved(key)) out[key] = value;
    });
    return out;
  }

  function snapshotEqual(a, b) {
    if (!a || !b) return false;
    var ka = Object.keys(a);
    if (ka.length !== Object.keys(b).length) return false;
    for (var i = 0; i < ka.length; i++) if (a[ka[i]] !== b[ka[i]]) return false;
    return true;
  }

  function notifyParams() {
    var snap = getAllParams();
    if (snapshotEqual(snap, lastSnapshot)) return;
    lastSnapshot = snap;
    paramListeners.slice().forEach(function (cb) {
      try {
        cb(snap);
      } catch (e) {
        // A listener that throws is the page's bug, not a reason to stop
        // telling every OTHER listener about the change.
        console.error("fused.params.onChange listener threw", e);
      }
    });
  }

  function setParam(key, value, options) {
    if (isReserved(key)) {
      throw new Error(
        "fused.params.set: '" + key + "' is a reserved param name and cannot be set"
      );
    }
    var removing = value === null;
    if (!removing && typeof value !== "string") {
      throw new Error(
        "fused.params.set: value for '" + key + "' must be a string or null, got " +
          typeof value
      );
    }
    var opts = options || {};
    if (opts.history !== undefined && opts.history !== "replace") {
      throw new Error(
        "fused.params.set: unknown history option " + JSON.stringify(opts.history) +
          " (the only value is \"replace\")"
      );
    }
    // `{default: d}` means "d is what an absent key already says", so writing d
    // is a no-op and REMOVES the key rather than spelling it out. Same rule as
    // the local runtime, so a page's URLs look the same published.
    if (!removing && opts.default !== undefined && value === opts.default) {
      removing = true;
    }
    var params = currentParams();
    var before = params.toString();
    if (removing) params.delete(key);
    else params.set(key, value);
    var after = params.toString();
    if (after === before) return;
    var url = window.location.pathname + (after ? "?" + after : "") + window.location.hash;
    if (opts.history === "replace") window.history.replaceState(null, "", url);
    else window.history.pushState(null, "", url);
    notifyParams();
  }

  function onParamsChange(cb) {
    if (typeof cb !== "function") throw new Error("fused.params.onChange needs a function");
    paramListeners.push(cb);
    return function () {
      var i = paramListeners.indexOf(cb);
      if (i >= 0) paramListeners.splice(i, 1);
    };
  }

  window.addEventListener("popstate", notifyParams);
  lastSnapshot = getAllParams();

  // ---- rawUrl / readFile ---------------------------------------------------
  // Every bundled file is served at its real page-relative path (the site tree
  // mirrors the author's folder — SPEC §18.1 EX-1), so resolution is just URL
  // arithmetic and a COMPUTED path works with no lookup table: `fused.rawUrl(
  // "data/" + name)` resolves exactly as it did locally.
  function rawUrl(path) {
    if (typeof path !== "string") throw new Error("fused.rawUrl needs a path string");
    return new URL(path, BASE).href;
  }

  function readFile(path) {
    return fetch(rawUrl(path)).then(function (r) {
      if (!r.ok) throw new Error("readFile " + path + ": HTTP " + r.status);
      return r.text();
    });
  }

  // ---- the local-only surfaces --------------------------------------------
  // Present and throwing, never absent. EXPORT.md is explicit that sniffing for
  // a method is the WRONG way to tell the environments apart — `fused.env` is —
  // and a page that feature-detects `fused.writeFile` would misread a published
  // page as local if we deleted it. The message names the branch to add.
  function unsupported(name, why) {
    return function () {
      throw new Error(
        "fused." + name + "() does not work on a published page: " + why +
          '. Gate it on fused.env === "local".'
      );
    };
  }

  // Progress reporting is decoration, not data (SPEC BG-14): the handle and all
  // its methods exist and resolve, so a page that reports progress runs
  // unchanged with no download manager behind it.
  function jobStub() {
    var handle = {
      update: function () { return Promise.resolve(null); },
      done: function () { return Promise.resolve(null); },
      fail: function () { return Promise.resolve(null); },
      cancel: function () { return Promise.resolve(null); },
      watch: function () { return Promise.resolve(null); },
      id: null,
    };
    return handle;
  }

  // ---- Pyodide ------------------------------------------------------------
  var pyodideReady = null; // the single boot promise

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = function () { reject(new Error("could not load " + src)); };
      document.head.appendChild(s);
    });
  }

  function loadSite() {
    if (SITE) return Promise.resolve(SITE);
    return fetch(new URL("_fused/site.json", BASE).href)
      .then(function (r) { return r.json(); })
      .then(function (site) { SITE = site; return site; });
  }

  /**
   * The reader's own writes, as `{path: contentOrNull}` — null being a delete of
   * a file the app shipped. One localStorage key, so a save is one write.
   *
   * Text only. Every failure mode of storage (a private window, a browser with
   * site data blocked, a full quota) is caught, because a page that throws on
   * boot because it could not READ a preference is worse than one that starts
   * empty. A failure to WRITE is not swallowed — see persistOverlay.
   */
  function overlayKey(site) {
    return "fused-render:app-state:v1:" + (site.app && site.app.name ? site.app.name : "app");
  }

  function readOverlay(site) {
    try {
      var raw = localStorage.getItem(overlayKey(site));
      var parsed = raw ? JSON.parse(raw) : null;
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (e) {
      return {};
    }
  }

  // A 32-bit FNV-1a over the string. Change detection, not security: it decides
  // whether a file in the virtual filesystem still matches the one the site
  // shipped, so a collision costs a save that is not persisted — and the space
  // of "two versions of the same small JSON file" makes that vanishingly
  // unlikely. A real digest would mean SubtleCrypto, which is async, per file,
  // on every call.
  function hash(text) {
    var h = 0x811c9dc5;
    for (var i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return h >>> 0;
  }

  var ROOT = "/app"; // the app's runtime root inside Pyodide: cwd and sys.path[0]
  // Our own harness lives OUTSIDE the app tree, on sys.path but not in the
  // directory the overlay walks. Keeping it out is not tidiness: anything under
  // ROOT that differs from what the site shipped is, by definition, the reader's
  // own state, so a harness file there would be copied into every reader's
  // localStorage and re-applied over the next release of itself.
  var RUNTIME_PY = "/fused-runtime";
  // path -> {hash, mtime} of the bytes the SITE published, recorded per boot.
  // The hash is the authority on "did the reader change this"; the mtime is a
  // cheap gate in front of it, so persisting after a call costs the size of what
  // the reader actually wrote rather than the size of the whole app. An app
  // shipping a large data file is then not paying for it on every single call.
  var shipped = {};

  function bootPyodide() {
    if (pyodideReady) return pyodideReady;
    pyodideReady = loadSite().then(function (site) {
      var indexURL = new URL(site.pyodide.indexURL, BASE).href;
      return loadScript(indexURL + "pyodide.js")
        .then(function () {
          return window.loadPyodide({ indexURL: indexURL });
        })
        .then(function (py) {
          // By NAME, not by URL: every wheel in the dependency closure was
          // vendored beside this site's own pyodide-lock.json, so Pyodide
          // resolves each one against our directory and loads it without a
          // single request leaving the site.
          var names = site.packages || [];
          return (names.length ? py.loadPackage(names) : Promise.resolve()).then(function () {
            return py;
          });
        })
        .then(function (py) {
          return seedFilesystem(py, site).then(function () {
            return bootHarness(py, site);
          });
        });
    });
    return pyodideReady;
  }

  /**
   * Put the app's tree inside Pyodide: the published files first, then the
   * reader's overlay on top.
   *
   * The order is the whole design (see the file header). Published files are
   * authoritative for everything the author ships; the overlay is authoritative
   * for everything the reader's own runs created or changed. A re-publish
   * therefore updates the app WITHOUT touching the reader's progress.
   */
  function seedFilesystem(py, site) {
    var FS = py.FS;
    mkdirp(FS, ROOT);
    var files = (site.python || []).concat(site.data || []);
    return Promise.all(
      files.map(function (key) {
        return fetch(new URL(key, BASE).href).then(function (r) {
          if (!r.ok) throw new Error("could not load " + key + " (HTTP " + r.status + ")");
          return r.text().then(function (text) {
            var path = ROOT + "/" + key;
            mkdirp(FS, path.slice(0, path.lastIndexOf("/")));
            FS.writeFile(path, text, { encoding: "utf8" });
            shipped[key] = { hash: hash(text), mtime: mtimeOf(FS, path) };
          });
        });
      })
    ).then(function () {
      var overlay = readOverlay(site);
      Object.keys(overlay).forEach(function (key) {
        var path = ROOT + "/" + key;
        var value = overlay[key];
        try {
          if (value === null) {
            FS.unlink(path); // the reader deleted a file the app shipped
          } else {
            mkdirp(FS, path.slice(0, path.lastIndexOf("/")));
            FS.writeFile(path, value, { encoding: "utf8" });
          }
        } catch (e) {
          // An overlay entry for a path that no longer exists in a republished
          // app: the author moved or removed it. Dropping it silently is right —
          // there is nowhere for it to go, and failing the boot over stale state
          // would brick the app for exactly the readers who used it most.
        }
      });
    });
  }

  function mtimeOf(FS, path) {
    try {
      var m = FS.stat(path).mtime;
      return m && m.getTime ? m.getTime() : m;
    } catch (e) {
      return null;
    }
  }

  function mkdirp(FS, dir) {
    var parts = dir.split("/").filter(Boolean);
    var path = "";
    for (var i = 0; i < parts.length; i++) {
      path += "/" + parts[i];
      try {
        FS.mkdir(path);
      } catch (e) {
        /* already there */
      }
    }
  }

  /**
   * Install the harness that calls a page's `main()` the way the app does.
   *
   * `_fused/boot.py` carries a verbatim copy of the app's own `_binding.py`, so
   * a URL param binds to an annotated signature identically here and locally.
   * That is the same trick the fused engine uses (D167) for the same reason: one
   * definition of the coercion rules, not a second one to keep in step.
   */
  function bootHarness(py, site) {
    var files = ["boot.py", "_binding.py"];
    return Promise.all(
      files.map(function (name) {
        return fetch(new URL("_fused/" + name, BASE).href).then(function (r) {
          if (!r.ok) throw new Error("could not load _fused/" + name);
          return r.text();
        });
      })
    ).then(function (sources) {
      mkdirp(py.FS, RUNTIME_PY);
      py.FS.writeFile(RUNTIME_PY + "/_fused_boot.py", sources[0], { encoding: "utf8" });
      py.FS.writeFile(RUNTIME_PY + "/_binding.py", sources[1], { encoding: "utf8" });
      // ROOT last, so it ends up at sys.path[0]: an entrypoint's `import
      // helpers` must find the app's own module before anything of ours, exactly
      // as it does locally.
      py.runPython(
        "import sys, os\n" +
          "sys.path.insert(0, " + JSON.stringify(RUNTIME_PY) + ")\n" +
          "sys.path.insert(0, " + JSON.stringify(ROOT) + ")\n" +
          "os.chdir(" + JSON.stringify(ROOT) + ")\n" +
          "import _fused_boot\n"
      );
      return py;
    });
  }

  /**
   * Persist everything the reader's Python changed.
   *
   * Walks the app tree, compares each file against the hash of what the site
   * shipped, and keeps the differences. A file the app shipped and the reader
   * deleted becomes an explicit null tombstone rather than an absence, because
   * an absence is indistinguishable from "never written" and would be undone by
   * the next boot's seeding.
   *
   * A quota failure REJECTS the call. The Python said it saved; if the bytes did
   * not reach storage the page has to hear about it, or the reader loses work
   * while the UI says "saved".
   */
  function persistOverlay(py, site) {
    var FS = py.FS;
    var overlay = {};
    walk(FS, ROOT, "", function (key, path) {
      var origin = shipped[key];
      if (origin && origin.mtime !== null && mtimeOf(FS, path) === origin.mtime) {
        return; // not written since it was seeded — no need to read it at all
      }
      var text;
      try {
        text = FS.readFile(path, { encoding: "utf8" });
      } catch (e) {
        return; // not a readable regular file (a socket, a device) — nothing to keep
      }
      if (origin && origin.hash === hash(text)) {
        return; // rewritten with identical bytes — the site is still its home
      }
      overlay[key] = text;
    });
    Object.keys(shipped).forEach(function (key) {
      try {
        FS.stat(ROOT + "/" + key);
      } catch (e) {
        overlay[key] = null; // shipped, then deleted by the reader's own code
      }
    });
    try {
      localStorage.setItem(overlayKey(site), JSON.stringify(overlay));
    } catch (e) {
      throw new Error(
        "this app's saved data no longer fits in your browser's storage for this site, so " +
          "the last change was not kept. Free some space for this site and try again."
      );
    }
  }

  function walk(FS, dir, prefix, visit) {
    var entries;
    try {
      entries = FS.readdir(dir);
    } catch (e) {
      return;
    }
    for (var i = 0; i < entries.length; i++) {
      var name = entries[i];
      if (name === "." || name === "..") continue;
      var path = dir + "/" + name;
      var key = prefix ? prefix + "/" + name : name;
      var mode;
      try {
        mode = FS.stat(path).mode;
      } catch (e) {
        continue;
      }
      if (FS.isDir(mode)) walk(FS, path, key, visit);
      else if (FS.isFile(mode)) visit(key, path);
    }
  }

  // ---- runPython -----------------------------------------------------------
  // Stale-request cancellation, SPEC RH-9: a second call for the same key while
  // the first is in flight SUPERSEDES it — the earlier promise rejects with an
  // AbortError-shaped reason so a page that renders on every keystroke paints
  // the last answer, not whichever finished last. Keyed by `pyPath` unless
  // `opts.key` says otherwise (and `opts.key === null` opts out entirely), which
  // is the local runtime's contract verbatim.
  //
  // Pyodide is single-threaded in this tab, so "cancellation" cannot interrupt
  // running Python. What it can do — and what the contract actually promises —
  // is make sure the superseded CALLER never sees a result.
  var inflight = {};

  function abortError(pyPath) {
    var err = new Error("fused.runPython(" + pyPath + ") was superseded by a newer call");
    err.name = "AbortError";
    err.superseded = true;
    return err;
  }

  function runPython(pyPath, params, opts) {
    if (typeof pyPath !== "string") {
      return Promise.reject(new Error("fused.runPython needs a literal path string"));
    }
    opts = opts || {};
    var key = opts.key === undefined ? pyPath : opts.key;
    if (key !== null) {
      var prev = inflight[key];
      if (prev) prev.reject(abortError(pyPath));
    }
    var slot = { reject: null, live: true };
    var guard = new Promise(function (_resolve, reject) {
      slot.reject = function (err) {
        slot.live = false;
        reject(err);
      };
    });
    if (key !== null) inflight[key] = slot;
    if (opts.signal) {
      opts.signal.addEventListener("abort", function () {
        if (slot.live) slot.reject(abortError(pyPath));
      });
    }

    var work = bootPyodide().then(function (py) {
      var site = SITE;
      var payload = JSON.stringify({
        path: pyPath,
        params: params && typeof params === "object" ? params : {},
      });
      var out = py.runPython(
        "import _fused_boot; _fused_boot.call(" + JSON.stringify(payload) + ")"
      );
      var result = JSON.parse(out);
      if (!result.ok) {
        var err = new Error(result.error.message);
        err.type = result.error.type;
        err.traceback = result.error.traceback;
        throw err;
      }
      // Persist BEFORE resolving: a page that saves and then immediately tells
      // the reader "saved" must not be able to say so before the bytes are in
      // storage — and a quota failure has to surface as this call failing.
      persistOverlay(py, site);
      if (result.stdout) console.log(result.stdout);
      return result.result;
    });

    var race = Promise.race([guard, work]);
    // Settle the slot no matter which side won, so a key never leaks a stale
    // rejecter that would abort an unrelated later call.
    race.then(
      function () { if (inflight[key] === slot) delete inflight[key]; },
      function () { if (inflight[key] === slot) delete inflight[key]; }
    );
    return race;
  }

  // ---- the surface ---------------------------------------------------------
  var reject = function (name, why) {
    return function () {
      return Promise.reject(
        new Error(
          "fused." + name + " does not work on a published page: " + why +
            '. Gate it on fused.env === "local".'
        )
      );
    };
  };

  var ai = reject("ai", "it runs a model or the claude CLI on the author's machine");
  ai.image = ai;
  ai.video = ai;
  ai.transcribe = ai;
  ai.embed = ai;
  ai.models = { list: ai, get: ai };

  window.fused = {
    // "hosted", not a third value — see the file header. `runtime` carries the
    // finer distinction for a page that wants it.
    env: "hosted",
    runtime: "pyodide-static",
    runPython: runPython,
    rawUrl: rawUrl,
    readFile: readFile,
    writeFile: unsupported("writeFile", "a published page has no filesystem of yours to write to"),
    stat: unsupported("stat", "a published page has no filesystem of yours to stat"),
    uploadFile: unsupported("uploadFile", "a published page has no filesystem of yours"),
    mkdir: unsupported("mkdir", "a published page has no filesystem of yours"),
    ai: ai,
    capture: {
      screen: reject("capture.screen", "capture writes a file to the author's disk (SPEC §45)"),
      audio: reject("capture.audio", "capture writes a file to the author's disk (SPEC §45)"),
      screenshot: reject("capture.screenshot", "capture writes a file to the author's disk (SPEC §45)"),
      list: reject("capture.list", "there is no capture store on a published page"),
      attach: reject("capture.attach", "there is no capture store on a published page"),
      sources: function () {
        // Answers rather than rejects: this is the call a well-written page makes
        // to decide whether to draw a record button at all, and it should get a
        // clean "no" instead of an exception to catch.
        return Promise.resolve({ available: false, reason: "not available on a published page" });
      },
    },
    fileIndex: {
      search: reject("fileIndex.search", "the index is built from the author's own filesystem"),
      query: reject("fileIndex.query", "the index is built from the author's own filesystem"),
    },
    engine: unsupported("engine", "an engine is a process on the author's machine"),
    daemon: unsupported("daemon", "a background app runs on the author's machine (SPEC §46)"),
    trackJob: function () { return jobStub(); },
    watchJob: function () { return jobStub(); },
    autoReload: function () { /* the published artifact does not change under the page */ },
    params: {
      get: getParam,
      getAll: getAllParams,
      set: setParam,
      onChange: onParamsChange,
    },
  };

  // ---- the error overlay ---------------------------------------------------
  // Same contract as the local runtime: an unhandled runPython rejection that
  // carries a `.traceback` is shown, because a reader staring at a page that
  // silently stopped working has nothing else to report to the author.
  window.addEventListener("unhandledrejection", function (event) {
    var err = event.reason;
    if (!err || !err.traceback) return;
    var overlay = document.createElement("div");
    overlay.style.cssText = [
      "position:fixed", "inset:0", "z-index:2147483647",
      "background:rgba(20,0,0,0.92)", "color:#ffdede",
      "font-family:ui-monospace,Menlo,Consolas,monospace",
      "font-size:13px", "padding:24px", "overflow:auto",
      "border:4px solid #c0392b", "box-sizing:border-box", "white-space:pre-wrap",
    ].join(";");
    var title = document.createElement("div");
    title.style.cssText = "font-size:16px;font-weight:bold;margin-bottom:12px;color:#ff6b6b;";
    title.textContent = (err.type || "Error") + ": " + (err.message || "");
    var pre = document.createElement("pre");
    pre.style.cssText = "margin:0;white-space:pre-wrap;word-break:break-word;";
    pre.textContent = err.traceback || "";
    overlay.appendChild(title);
    overlay.appendChild(pre);
    document.body.appendChild(overlay);
  });

  // ---- warming ------------------------------------------------------------
  // Start the interpreter as soon as the page is otherwise idle. A published app
  // with no Python never ships one, so this only ever runs where it is going to
  // be needed — and it turns the first runPython from "wait for a 10 MB download
  // and a boot" into "wait for whatever is left of it". The rejection is
  // swallowed here on purpose: the real call surfaces the same failure with the
  // context of what the page was trying to do.
  window.addEventListener("load", function () {
    var warm = function () {
      loadSite().then(function (site) {
        if ((site.python || []).length) bootPyodide().catch(function () {});
      }).catch(function () {});
    };
    if (window.requestIdleCallback) window.requestIdleCallback(warm, { timeout: 2000 });
    else setTimeout(warm, 200);
  });

  // ---- offline ------------------------------------------------------------
  // The service worker is what makes Add to Home Screen honest: the reader taps
  // an icon and the app opens, on a train, without the tab having been warm.
  // Registered late and failure-tolerantly — a browser that refuses it (or a
  // page served from file://) still works exactly as it does now, just online.
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register(new URL("fused-sw.js", BASE).href).catch(function () {
        /* no offline support here; the app still runs */
      });
    });
  }
})();
