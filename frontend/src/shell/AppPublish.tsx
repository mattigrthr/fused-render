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
// One target costs money instead of asking for an account. Publishing to an ICP
// canister is paid for in cycles the author transfers themselves, from their own
// terminal, and that is a publish PRECONDITION with its own button rather than a
// fourth auth state — a disabled row saying "not signed in" would be both wrong
// and useless, because there is nothing to sign in to. Pressing Fund cycles the
// first time is also the moment the publishing identity is created, and the only
// moment its seed phrase exists: it is shown once, behind a warning, and dropped.
// Nothing on this page writes it to storage of any kind.
//
// A publish is a background run, not a request: the first one fetches a Python
// runtime and uploads megabytes. The page starts it, polls it by phase, and
// finds it still going if you leave and come back — the run lives in the
// server, not in this component's state.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  Copy,
  Download,
  ExternalLink,
  Info,
  Loader2,
  LogIn,
  RefreshCw,
  Rocket,
  ShieldAlert,
  TriangleAlert,
  Unlink,
  Wallet,
} from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { PublishTargetIcon } from "@platform/ui/PublishIcons";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { copyToClipboard } from "@platform/lib/clipboard";
import { pushToast } from "@platform/lib/toast";
import { formatSize } from "@platform/lib/format";
import {
  canPublish,
  createIdentity,
  deploy,
  displayUrl,
  fetchAuth,
  fetchCycles,
  fetchFunding,
  fetchPlan,
  fetchRun,
  forget,
  formatCycles,
  login,
  phaseProgress,
  pollDelay,
  readingAge,
  requirements,
  runwayLabel,
  runwayTone,
  type PublishAuth,
  type PublishCycles,
  type PublishFunding,
  type PublishIdentity,
  type PublishPlan,
  type PublishRun,
  type PublishTarget,
} from "./publish-lib";
import { QR_QUIET, qrMatrix, qrPath } from "./qr";

type Load =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ok"; plan: PublishPlan };

export default function AppPublish({ dir }: { dir: string }) {
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [auths, setAuths] = useState<Record<string, PublishAuth>>({});
  const [runs, setRuns] = useState<Record<string, PublishRun>>({});
  const [busy, setBusy] = useState<Record<string, string>>({});
  const [fundings, setFundings] = useState<Record<string, PublishFunding>>({});
  const [cycles, setCycles] = useState<Record<string, PublishCycles | null>>({});
  // The seed phrase, held for exactly as long as it is on screen. Component
  // state and nothing else: no localStorage, no sessionStorage, no store that
  // survives a navigation. It cannot be asked for again, from us or from the
  // provider, so the flow does not continue until the author says they have it.
  const [minted, setMinted] = useState<Record<string, PublishIdentity>>({});

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
    setFundings({});
    setMinted({});
    reload(ctrl.signal).then((plan) => {
      // Auth is asked for per target, AFTER the report is on screen: each probe
      // runs a provider CLI, and the eligibility half of this page needs no
      // network at all.
      for (const t of plan?.targets ?? []) {
        fetchAuth(t.id, ctrl.signal)
          .then((a) => setAuths((prev) => ({ ...prev, [t.id]: a })))
          .catch(() => {});
        if (!t.funding) continue;
        fetchFunding(t.id, ctrl.signal)
          .then((f) => setFundings((prev) => ({ ...prev, [t.id]: f })))
          .catch(() => {});
        // The plan already carried the last reading; only a STALE one costs a
        // round trip, and a failed refresh leaves the old number on screen.
        setCycles((prev) => ({ ...prev, [t.id]: t.cycles }));
        if (t.published && !t.cycles?.fresh)
          fetchCycles(dir, t.id, { signal: ctrl.signal })
            .then(({ cycles: c }) => setCycles((prev) => ({ ...prev, [t.id]: c })))
            .catch(() => {});
      }
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
          if (run.state !== "running") {
            const plan = await reload();
            // A first publish is also the first moment there is a canister to
            // read a balance from, and the runway is the number the author most
            // needs after spending cycles. Asking here rather than waiting for
            // the next page load.
            const t = plan?.targets.find((x) => x.id === r.target);
            if (t?.funding && t.published)
              fetchCycles(dir, r.target)
                .then(({ cycles: c }) => setCycles((prev) => ({ ...prev, [r.target]: c })))
                .catch(() => {});
          }
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

  const onFund = async (target: PublishTarget) => {
    // The first press is what creates the identity — there is no separate setup
    // step, and nobody who never publishes here ends up with a key on their
    // disk. A later press only refreshes the balance, because the transfer
    // happens in the author's own terminal and the UI has no other way to learn
    // it landed.
    setBusy((b) => ({ ...b, [target.id]: "fund" }));
    try {
      const current = fundings[target.id];
      if (current && !current.identity) {
        const created = await createIdentity(target.id);
        setMinted((prev) => ({ ...prev, [target.id]: created }));
      }
      const next = await fetchFunding(target.id);
      setFundings((prev) => ({ ...prev, [target.id]: next }));
    } catch (e) {
      pushToast({ msg: (e as Error).message, tone: "error" });
    } finally {
      setBusy((b) => ({ ...b, [target.id]: "" }));
    }
  };

  // Dropping the phrase is the whole point: once the author says they have it,
  // this app can never show it again, and neither can the provider.
  const onPhraseSaved = (target: PublishTarget) =>
    setMinted((prev) => {
      const next = { ...prev };
      delete next[target.id];
      return next;
    });

  const onRefreshCycles = async (target: PublishTarget) => {
    setBusy((b) => ({ ...b, [target.id]: "cycles" }));
    try {
      const { cycles: c, error } = await fetchCycles(dir, target.id, { refresh: true });
      setCycles((prev) => ({ ...prev, [target.id]: c }));
      // A refresh that failed still leaves the last reading painted, so the
      // error is a toast rather than an empty panel.
      if (error) pushToast({ msg: error, tone: "error" });
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
            funding={fundings[t.id] ?? null}
            cycles={cycles[t.id] ?? null}
            minted={minted[t.id] ?? null}
            run={runs[t.id] ?? null}
            busy={busy[t.id] ?? ""}
            onLogin={() => onLogin(t)}
            onPublish={() => onPublish(t)}
            onForget={() => onForget(t)}
            onFund={() => onFund(t)}
            onPhraseSaved={() => onPhraseSaved(t)}
            onRefreshCycles={() => onRefreshCycles(t)}
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
  funding,
  cycles,
  minted,
  run,
  busy,
  onLogin,
  onPublish,
  onForget,
  onFund,
  onPhraseSaved,
  onRefreshCycles,
}: {
  target: PublishTarget;
  auth: PublishAuth | null;
  funding: PublishFunding | null;
  cycles: PublishCycles | null;
  minted: PublishIdentity | null;
  run: PublishRun | null;
  busy: string;
  onLogin: () => void;
  onPublish: () => void;
  onForget: () => void;
  onFund: () => void;
  onPhraseSaved: () => void;
  onRefreshCycles: () => void;
}) {
  const running = run?.state === "running";
  // A phrase on screen blocks everything else. It exists exactly once, and a
  // Publish that ran underneath it would be the author's attention leaving the
  // one thing on this page they cannot get back.
  const ready = canPublish(target, auth, funding) && !running && !busy && !minted;
  const live = run?.state === "done" ? run.result : null;
  const address = live?.url ?? target.published?.url ?? null;

  return (
    <section className="app-publish-target" aria-labelledby={`pub-${target.id}`}>
      <header>
        <div>
          <h3 id={`pub-${target.id}`}>
            <span className="app-publish-mark">
              <PublishTargetIcon target={target.id} />
            </span>
            {target.label}
          </h3>
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

      {target.eligible && target.funding && (
        minted ? (
          <SeedPhrase identity={minted} onSaved={onPhraseSaved} />
        ) : (
          <Funding
            funding={funding}
            published={target.published !== null}
            busy={busy === "fund"}
            onFund={onFund}
          />
        )
      )}

      {target.eligible && target.funding && cycles && (
        <Cycles reading={cycles} busy={busy === "cycles"} onRefresh={onRefreshCycles} />
      )}

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

// ---- funding: a precondition with its own button -----------------------------

/** A field the author copies rather than reads — a principal, a command.
 *
 *  Both are strings nobody retypes correctly, and one of them moves money. */
function CopyField({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 1600);
    return () => window.clearTimeout(t);
  }, [copied]);
  return (
    <div className="app-publish-copyfield">
      <span className="app-publish-caption">{label}</span>
      <div>
        <code className={mono ? "app-publish-mono" : undefined}>{value}</code>
        <Button
          variant="ghost"
          size="sm"
          aria-label={`Copy ${label}`}
          onClick={async () => setCopied(await copyToClipboard(value))}
        >
          {copied ? <Check /> : <Copy />}
        </Button>
      </div>
    </div>
  );
}

/**
 * Fund cycles: the affordance that stands where a disabled row with a paragraph
 * under it would otherwise be.
 *
 * The first press creates the publishing identity — there is no wizard and no
 * setup step, and an author who never publishes to this target never gets a key
 * on their disk. Later presses only re-read the balance, because the transfer
 * happens in the author's own terminal and this page has no way to learn it
 * landed except by asking again.
 *
 * This is also where a future Buy cycles payment flow would land. Today it
 * explains; the affordance is in the right place either way.
 */
function Funding({
  funding,
  published,
  busy,
  onFund,
}: {
  funding: PublishFunding | null;
  published: boolean;
  busy: boolean;
  onFund: () => void;
}) {
  if (!funding)
    return <p className="app-publish-caption app-publish-auth">Checking the balance…</p>;
  // Funded and already live: the cycles readout below carries the number, and a
  // second panel saying the same thing is noise.
  if (funding.funded && published) return null;
  return (
    <div className="app-publish-funding">
      <div className="app-publish-funding-head">
        <p className="app-publish-caption">{funding.detail}</p>
        <Button variant="outline" size="sm" onClick={onFund} disabled={busy}>
          {busy ? <Loader2 className="app-publish-spin" /> : <Wallet />}
          {funding.identity ? "Check balance" : "Fund cycles"}
        </Button>
      </div>
      {funding.identity && funding.principal && (
        <>
          <CopyField label="Your principal" value={funding.principal} mono />
          {funding.transfer_command && (
            <CopyField label="Transfer cycles from your terminal" value={funding.transfer_command} />
          )}
          <p className="app-publish-caption">
            A canister needs about {formatCycles(funding.minimum)} cycles to start
            {funding.balance !== null && <> — this principal holds {formatCycles(funding.balance)}</>}
            . fused-render never moves your money: the transfer runs in your own terminal.
            {funding.help_url && (
              <>
                {" "}
                <a href={funding.help_url} target="_blank" rel="noreferrer">
                  How to get cycles
                  <ExternalLink aria-hidden />
                </a>
              </>
            )}
          </p>
        </>
      )}
    </div>
  );
}

/**
 * The seed phrase, shown once, at the only moment it exists.
 *
 * Deliberately an inline panel rather than a modal: a modal that dismisses on a
 * stray click is the wrong container for something that cannot be shown again.
 * Nothing continues until the author explicitly says they have it, and pressing
 * that button is what makes this app forget the phrase.
 *
 * The warning is serious without being apocalyptic, because the situation is
 * recoverable: the signing key stays in the OS keyring and `icp identity export`
 * gets it out at any later time, so clicking past this loses portability, not
 * access. An apocalyptic warning about a recoverable situation is how you teach
 * people to ignore warnings. The genuinely unrecoverable case — this phrase and
 * the keyring both gone — gets one sentence, not a wall of red.
 */
function SeedPhrase({
  identity,
  onSaved,
}: {
  identity: PublishIdentity;
  onSaved: () => void;
}) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 1600);
    return () => window.clearTimeout(t);
  }, [copied]);

  const save = () => {
    const blob = new Blob(
      [
        `fused-render publishing identity\n`,
        `principal: ${identity.principal}\n\n`,
        `${identity.seed_phrase}\n\n`,
        `Anyone holding this phrase controls every canister published from this\n`,
        `identity and every cycle in this principal. Keep it somewhere private.\n`,
      ],
      { type: "text/plain" },
    );
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "fused-render-seed-phrase.txt";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <section className="app-publish-seed" aria-label="Your seed phrase">
      <p className="app-publish-seed-lead">
        <ShieldAlert aria-hidden />
        This is the only time this phrase is shown.
      </p>
      <ul className="app-publish-notes">
        <li>
          <span>
            Anyone holding it controls every canister you publish here and every cycle in
            this principal.
          </span>
        </li>
        <li>
          <span>
            fused-render is showing it once and does not store it. There is no way to ask us
            for it later, and we will never ask you for it — nor will anyone legitimate.
          </span>
        </li>
        <li>
          <span>
            Clicking past it does not lose your cycles: the key itself stays in your OS
            keyring, and <code>icp identity export fused-render</code> gets it out. Losing
            this phrase <em>and</em> the keyring — a wiped machine with no backup — is the
            case that cannot be undone.
          </span>
        </li>
      </ul>
      <p className="app-publish-seed-phrase">{identity.seed_phrase}</p>
      <CopyField label="Principal" value={identity.principal} mono />
      <div className="app-publish-address-actions">
        <Button
          variant="outline"
          size="sm"
          onClick={async () => setCopied(await copyToClipboard(identity.seed_phrase))}
        >
          {copied ? <Check /> : <Copy />}
          {copied ? "Copied" : "Copy phrase"}
        </Button>
        <Button variant="outline" size="sm" onClick={save}>
          <Download />
          Save to a file
        </Button>
        <Button size="sm" onClick={onSaved}>
          <Check />
          I&rsquo;ve saved this
        </Button>
      </div>
    </section>
  );
}

/**
 * Balance and idle burn, as a runway.
 *
 * The pair is the point: a balance alone says nothing about how long it lasts,
 * and a canister that runs out of cycles is frozen and eventually deleted with
 * everything in it. That is a dead man's switch on the author's app, so it is
 * stated next to the number rather than buried, and a low runway looks like a
 * warning rather than a data point.
 *
 * Refreshed at most once a day and cached beside the app, so a stale reading is
 * labelled with when it was taken rather than replaced by a spinner.
 */
function Cycles({
  reading,
  busy,
  onRefresh,
}: {
  reading: PublishCycles;
  busy: boolean;
  onRefresh: () => void;
}) {
  const tone = runwayTone(reading);
  const runway = runwayLabel(reading);
  return (
    <div className={`app-publish-cycles app-publish-cycles-${tone}`}>
      <div className="app-publish-cycles-figures">
        <strong>{formatCycles(reading.balance)} cycles</strong>
        <span className="app-publish-caption">
          {runway ?? "no burn rate reported"}
          {reading.idle_burned_per_day > 0 && (
            <> · {formatCycles(reading.idle_burned_per_day)}/day idle</>
          )}
          {" · "}
          {readingAge(reading)}
        </span>
      </div>
      {(tone === "low" || tone === "critical") && (
        <p className="app-publish-cycles-warning">
          <TriangleAlert aria-hidden />
          <span>
            A canister that runs out of cycles is frozen and eventually deleted, along with
            everything in it. Top this one up before it gets there.
          </span>
        </p>
      )}
      <Button variant="ghost" size="sm" onClick={onRefresh} disabled={busy}>
        {busy ? <Loader2 className="app-publish-spin" /> : <RefreshCw />}
        Refresh
      </Button>
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
          viewBox={`0 0 ${matrix.length + 2 * QR_QUIET} ${matrix.length + 2 * QR_QUIET}`}
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
