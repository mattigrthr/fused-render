// The app page's Publish tab: what this app needs to run somewhere that is not
// this machine, which providers can give it that, and — once it is up — the
// address, as something a phone can reach in one move.
//
// The page is a report before it is a button. An author who cannot publish
// deserves to know why in the same breath as being told they cannot, so the
// summary at the top says which two cells of the runtime × state grid this app
// occupies, and every disabled row repeats the reason under itself rather than
// pointing elsewhere. All of that prose comes down from the server unmodified
// (publish-lib.ts explains why).
//
// Nothing here holds a credential. Log in runs the provider's own browser
// approval and the token lands in the provider CLI's config; this app never
// sees it, and there is deliberately no field to paste one into.
//
// A publish is a background run, not a request: the first one fetches a Python
// runtime and uploads megabytes. The page starts it, polls it by phase, and
// finds it still going if you leave and come back — the run lives in the
// server, not in this component's state.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  Copy,
  ExternalLink,
  Info,
  Loader2,
  LogIn,
  Rocket,
  TriangleAlert,
  Unlink,
} from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { copyToClipboard } from "@platform/lib/clipboard";
import { pushToast } from "@platform/lib/toast";
import { formatSize } from "@platform/lib/format";
import {
  canPublish,
  deploy,
  displayUrl,
  fetchAuth,
  fetchPlan,
  fetchRun,
  forget,
  login,
  phaseProgress,
  pollDelay,
  requirements,
  type PublishAuth,
  type PublishPlan,
  type PublishRun,
  type PublishTarget,
} from "./publish-lib";
import { qrMatrix, qrPath } from "./qr";

type Load =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ok"; plan: PublishPlan };

export default function AppPublish({ dir }: { dir: string }) {
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [auths, setAuths] = useState<Record<string, PublishAuth>>({});
  const [runs, setRuns] = useState<Record<string, PublishRun>>({});
  const [busy, setBusy] = useState<Record<string, string>>({});

  const reload = useCallback(
    (signal?: AbortSignal) =>
      fetchPlan(dir, signal)
        .then((plan) => {
          setLoad({ kind: "ok", plan });
          // A run that was going when the tab was last open is still going.
          const live: Record<string, PublishRun> = {};
          for (const t of plan.targets) if (t.run) live[t.id] = t.run;
          setRuns((prev) => ({ ...prev, ...live }));
          return plan;
        })
        .catch((e) => {
          if (!signal?.aborted) setLoad({ kind: "error", message: (e as Error).message });
          return null;
        }),
    [dir],
  );

  useEffect(() => {
    const ctrl = new AbortController();
    setLoad({ kind: "loading" });
    setAuths({});
    setRuns({});
    reload(ctrl.signal).then((plan) => {
      // Auth is asked for per target, AFTER the report is on screen: each probe
      // runs a provider CLI, and the eligibility half of this page needs no
      // network at all.
      for (const t of plan?.targets ?? [])
        fetchAuth(t.id, ctrl.signal)
          .then((a) => setAuths((prev) => ({ ...prev, [t.id]: a })))
          .catch(() => {});
    });
    return () => ctrl.abort();
  }, [dir, reload]);

  // ---- polling: one timer per running publish -------------------------------
  const runsRef = useRef(runs);
  runsRef.current = runs;
  useEffect(() => {
    const running = Object.values(runs).filter((r) => r.state === "running");
    if (!running.length) return;
    let live = true;
    const timers = running.map((r) =>
      window.setTimeout(async () => {
        if (!live) return;
        try {
          const { run } = await fetchRun(dir, r.target);
          if (!live || !run) return;
          setRuns((prev) => ({ ...prev, [r.target]: run }));
          // A finished publish changes what the page knows about the app: the
          // record it now has, and the address under the row.
          if (run.state !== "running") reload();
        } catch {
          // A poll that fails is a poll; the next one is 500 ms away and the
          // run is on the server either way.
        }
      }, pollDelay(Date.now() - r.started_at * 1000)),
    );
    return () => {
      live = false;
      for (const t of timers) window.clearTimeout(t);
    };
  }, [runs, dir, reload]);

  const onLogin = async (target: PublishTarget) => {
    setBusy((b) => ({ ...b, [target.id]: "login" }));
    try {
      const auth = await login(target.id);
      setAuths((prev) => ({ ...prev, [target.id]: auth }));
      if (auth.status !== "ready")
        pushToast({ msg: auth.detail || `Not signed in to ${target.label}.`, tone: "error" });
    } catch (e) {
      pushToast({ msg: (e as Error).message, tone: "error" });
    } finally {
      setBusy((b) => ({ ...b, [target.id]: "" }));
    }
  };

  const onPublish = async (target: PublishTarget) => {
    setBusy((b) => ({ ...b, [target.id]: "deploy" }));
    try {
      const run = await deploy(dir, target.id);
      setRuns((prev) => ({ ...prev, [target.id]: run }));
    } catch (e) {
      pushToast({ msg: (e as Error).message, tone: "error" });
    } finally {
      setBusy((b) => ({ ...b, [target.id]: "" }));
    }
  };

  const onForget = async (target: PublishTarget) => {
    const published = target.published;
    if (!published) return;
    const ok = window.confirm(
      `Forget ${published.project}?\n\n` +
        `${displayUrl(published.url)} stays up and keeps working — this only stops ` +
        `fused-render treating it as this app's deployment.\n\n` +
        `The next publish will create a NEW address, and anyone still using the old ` +
        `one keeps a copy whose saved progress the new one cannot see.`,
    );
    if (!ok) return;
    setBusy((b) => ({ ...b, [target.id]: "forget" }));
    try {
      await forget(dir, target.id);
      setRuns((prev) => {
        const next = { ...prev };
        delete next[target.id];
        return next;
      });
      await reload();
    } catch (e) {
      pushToast({ msg: (e as Error).message, tone: "error" });
    } finally {
      setBusy((b) => ({ ...b, [target.id]: "" }));
    }
  };

  if (load.kind === "loading") return <SkeletonLines rows={6} />;
  if (load.kind === "error")
    return (
      <div className="app-publish">
        <ErrorBanner>{load.message}</ErrorBanner>
      </div>
    );

  const { plan } = load;
  return (
    <div className="app-publish">
      <Summary plan={plan} />
      <div className="app-publish-targets">
        {plan.targets.map((t) => (
          <TargetCard
            key={t.id}
            target={t}
            auth={auths[t.id] ?? null}
            run={runs[t.id] ?? null}
            busy={busy[t.id] ?? ""}
            onLogin={() => onLogin(t)}
            onPublish={() => onPublish(t)}
            onForget={() => onForget(t)}
          />
        ))}
      </div>
    </div>
  );
}

// ---- what the app needs ------------------------------------------------------

function Summary({ plan }: { plan: PublishPlan }) {
  const needs = requirements(plan);
  return (
    <section className="app-publish-summary">
      <h2>What this app needs</h2>
      <ul className="app-publish-cells">
        {needs.map((n) => (
          <li key={n}>{n}</li>
        ))}
      </ul>
      {plan.python_files.length > 0 && (
        <p className="app-publish-caption">
          {plan.python_files.length === 1
            ? "1 Python file"
            : `${plan.python_files.length} Python files`}
          {plan.packages.length > 0 && <> · {plan.packages.join(", ")}</>} — shipped with the
          app and run in the reader's browser.
        </p>
      )}
      {plan.blockers.length > 0 && (
        <ErrorBanner>
          <p className="app-publish-banner-lead">This app cannot be published yet.</p>
          <ul>
            {plan.blockers.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
        </ErrorBanner>
      )}
      {plan.notes.length > 0 && (
        <ul className="app-publish-notes">
          {plan.notes.map((n) => (
            <li key={n}>
              <Info aria-hidden />
              <span>{n}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ---- one provider ------------------------------------------------------------

function TargetCard({
  target,
  auth,
  run,
  busy,
  onLogin,
  onPublish,
  onForget,
}: {
  target: PublishTarget;
  auth: PublishAuth | null;
  run: PublishRun | null;
  busy: string;
  onLogin: () => void;
  onPublish: () => void;
  onForget: () => void;
}) {
  const running = run?.state === "running";
  const ready = canPublish(target, auth) && !running && !busy;
  const live = run?.state === "done" ? run.result : null;
  const address = live?.url ?? target.published?.url ?? null;

  return (
    <section className="app-publish-target" aria-labelledby={`pub-${target.id}`}>
      <header>
        <div>
          <h3 id={`pub-${target.id}`}>{target.label}</h3>
          <p className="app-publish-caption">{target.blurb}</p>
        </div>
        <Button onClick={onPublish} disabled={!ready}>
          {running || busy === "deploy" ? <Loader2 className="app-publish-spin" /> : <Rocket />}
          {target.published ? "Publish update" : "Publish"}
        </Button>
      </header>

      {!target.eligible && (
        <ul className="app-publish-reasons">
          {target.reasons.map((r) => (
            <li key={r}>
              <TriangleAlert aria-hidden />
              <span>{r}</span>
            </li>
          ))}
        </ul>
      )}

      {target.eligible && <AuthRow auth={auth} busy={busy === "login"} onLogin={onLogin} />}

      {/* The bar goes when the run does: a progress bar left standing over a
          failure reads as work still happening. */}
      {run?.state === "running" && <Progress run={run} />}
      {run?.state === "error" && <ErrorBanner>{run.error}</ErrorBanner>}

      {address && (
        <Address
          url={address}
          project={live?.project ?? target.published?.project ?? ""}
          result={live}
          busy={busy === "forget"}
          onForget={onForget}
        />
      )}
    </section>
  );
}

function AuthRow({
  auth,
  busy,
  onLogin,
}: {
  auth: PublishAuth | null;
  busy: boolean;
  onLogin: () => void;
}) {
  if (!auth)
    return <p className="app-publish-caption app-publish-auth">Checking the sign-in…</p>;
  if (auth.status === "ready")
    return (
      <p className="app-publish-auth app-publish-auth-ready">
        <Check aria-hidden />
        Signed in{auth.account ? <> as {auth.account}</> : null}
      </p>
    );
  return (
    <div className="app-publish-auth">
      <p className="app-publish-caption">
        {auth.detail}
        {auth.help_url && (
          <>
            {" "}
            <a href={auth.help_url} target="_blank" rel="noreferrer">
              How to install it
              <ExternalLink aria-hidden />
            </a>
          </>
        )}
      </p>
      {/* No Log in for an unavailable provider: clicking it cannot fix a CLI
          that is not installed, and a button that always fails is worse than
          none. */}
      {auth.status === "needs-login" && (
        <Button variant="outline" size="sm" onClick={onLogin} disabled={busy}>
          {busy ? <Loader2 className="app-publish-spin" /> : <LogIn />}
          {busy ? "Approve in your browser…" : "Log in"}
        </Button>
      )}
    </div>
  );
}

function Progress({ run }: { run: PublishRun }) {
  const fraction = phaseProgress(run);
  return (
    <div className="app-publish-progress">
      <div
        className={
          "app-publish-bar" + (fraction === null ? " app-publish-bar-indeterminate" : "")
        }
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={fraction === null ? undefined : Math.round(fraction * 100)}
        aria-label={run.phase_label}
      >
        <span style={fraction === null ? undefined : { width: `${fraction * 100}%` }} />
      </div>
      <p className="app-publish-caption">{run.phase_label}…</p>
    </div>
  );
}

// ---- the address -------------------------------------------------------------

function Address({
  url,
  project,
  result,
  busy,
  onForget,
}: {
  url: string;
  project: string;
  result: PublishRun["result"];
  busy: boolean;
  onForget: () => void;
}) {
  const [copied, setCopied] = useState(false);
  // Recomputed only when the address changes: the matrix is a few hundred
  // modules of finite-field arithmetic, not something to redo on every render.
  const matrix = useMemo(() => qrMatrix(url), [url]);

  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 1600);
    return () => window.clearTimeout(t);
  }, [copied]);

  return (
    <div className="app-publish-address">
      {matrix && (
        // The point of publishing is that the app is on a phone. A camera gets
        // it there without anyone typing a hostname with a thumb.
        <svg
          className="app-publish-qr"
          viewBox={`0 0 ${matrix.length + 4} ${matrix.length + 4}`}
          role="img"
          aria-label={`QR code for ${displayUrl(url)}`}
        >
          <rect width="100%" height="100%" fill="var(--qr-paper)" />
          <path d={qrPath(matrix)} fill="var(--qr-ink)" />
        </svg>
      )}
      <div className="app-publish-address-body">
        <a className="app-publish-url" href={url} target="_blank" rel="noreferrer">
          {displayUrl(url)}
          <ExternalLink aria-hidden />
        </a>
        <p className="app-publish-caption">
          {result?.updated_in_place
            ? "Updated in place — same address, and every reader's saved progress with it."
            : "Live. Re-publishing keeps this address, so saved progress survives."}
          {result?.bytes ? <> · {formatSize(result.bytes)}</> : null}
        </p>
        {/* Disclosed, not buried: an icon nobody chose is about to be on
            somebody's home screen. */}
        {result?.icon === "generated" && (
          <p className="app-publish-caption">
            No <code>icon.svg</code> in the app, so the home-screen icon is a lettermark drawn
            from its name. Add one to the folder and publish again to replace it.
          </p>
        )}
        {result?.notes?.map((n) => (
          <p className="app-publish-caption" key={n}>
            {n}
          </p>
        ))}
        <div className="app-publish-address-actions">
          <Button
            variant="outline"
            size="sm"
            onClick={async () => setCopied(await copyToClipboard(url))}
          >
            {copied ? <Check /> : <Copy />}
            {copied ? "Copied" : "Copy link"}
          </Button>
          <Button variant="ghost" size="sm" onClick={onForget} disabled={busy}>
            <Unlink />
            Forget {project || "this deployment"}
          </Button>
        </div>
      </div>
    </div>
  );
}
