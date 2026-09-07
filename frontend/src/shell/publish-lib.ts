// The Publish tab's wire types and its pure helpers (shell/AppPublish.tsx).
//
// The server is the authority on every word the author reads: eligibility
// reasons, provider detail lines, phase labels and failure messages all arrive
// as prose and are rendered unmodified. That is deliberate — a reason worded
// once in Python and reworded again in TypeScript is a reason that can
// disagree with the check that produced it. What lives here instead is the
// shape of the exchange, the polling cadence, and the two or three things only
// a browser knows (how far along a phase is, what a URL looks like when it is
// being read rather than followed).
//
// One value never lives here or anywhere else: the seed phrase a funded target
// prints when it creates an identity. It arrives in exactly one response, is
// rendered once, and is dropped — no localStorage, no sessionStorage, no store
// that outlives the flow. Retaining it would make this app the custodian of a
// credential that moves real money, which is the one thing the publish seam is
// built not to be.
import { getJson, postJson } from "@platform/lib/api";

// ---- what the six routes speak ----------------------------------------------

/** A capability cell on the runtime × state grid, as its wire value
 *  (`"runtime:pyodide"`, `"state:client-local"`, …). Never matched against a
 *  literal here: the plan carries the labels, so the page can name a cell it
 *  was not compiled against. */
export type Capability = string;

export interface PublishAuth {
  /** "ready" | "needs-login" | "unavailable" — three states because they need
   *  three different buttons, and offering Log in for a missing CLI is the
   *  version of this UI that wastes the author's afternoon. */
  status: string;
  account: string | null;
  detail: string;
  help_url: string | null;
}

export interface PublishedAt {
  project: string;
  url: string;
  published_at: number;
}

export interface PublishRunResult {
  url: string;
  project: string;
  updated_in_place: boolean;
  notes: string[];
  bytes: number | null;
  /** "app" when the author shipped an icon.svg, "generated" when the adapter
   *  drew a lettermark — which the page discloses, because an icon nobody chose
   *  is about to be on a home screen. */
  icon: string | null;
}

export interface PublishRun {
  app_dir: string;
  target: string;
  state: "running" | "done" | "error";
  phase: string;
  phase_label: string;
  started_at: number;
  finished_at: number | null;
  result: PublishRunResult | null;
  error: string | null;
}

/** Whether a target that costs money has been paid for, and what to do if not.
 *
 *  Not an auth state, and deliberately not squeezed into one: an ICP canister
 *  is paid for in cycles the author transfers themselves from their own
 *  terminal, with no account anywhere to sign into. "You have not funded a
 *  principal" and "you are not signed in" need different words and a different
 *  button. */
export interface PublishFunding {
  /** True once there is enough to publish with. Gates the button alongside auth. */
  funded: boolean;
  /** Whether an identity exists at all. False means Fund cycles will make one. */
  identity: boolean;
  /** Where the author sends cycles. A 60-character string nobody retypes, so it
   *  is always shown with a copy button. */
  principal: string | null;
  balance: number | null;
  minimum: number;
  /** The exact command, principal already substituted. It runs in the author's
   *  own terminal — fused-render has no key and never moves their money. */
  transfer_command: string | null;
  detail: string;
  help_url: string | null;
}

/** The one response that carries a seed phrase, and the only time it exists.
 *  Shown once, then dropped: never written to localStorage, sessionStorage, or
 *  any state that outlives the flow. */
export interface PublishIdentity {
  target: string;
  principal: string;
  seed_phrase: string;
}

/** What an app's deployment holds and how fast it is draining.
 *
 *  Two numbers because the pair is a runway. A canister that runs out of cycles
 *  is frozen and eventually deleted WITH ALL ITS STATE, so `days_left` is the
 *  number and the balance is the supporting detail. */
export interface PublishCycles {
  balance: number;
  idle_burned_per_day: number;
  /** ISO-8601 UTC. Shown with the figure: a reading with an "as of" beats a
   *  spinner, so a stale one is labelled rather than hidden. */
  read_at: string;
  /** Balance ÷ idle burn. Null when nothing is being burned, which is not
   *  "forever" so much as "the provider did not say". */
  days_left: number | null;
  /** False means older than a day and worth refreshing — not wrong. */
  fresh: boolean;
}

export interface PublishTarget {
  id: string;
  label: string;
  blurb: string;
  capabilities: Capability[];
  auth: PublishAuth | null;
  /** Whether this target costs the author money directly and therefore has a
   *  Fund cycles panel. Comes from the adapter's own shape on the server, so a
   *  provider with nothing to fund cannot grow an empty panel. */
  funding: boolean;
  eligible: boolean;
  /** Full sentences, shown verbatim under a disabled row. Empty iff eligible. */
  reasons: string[];
  published: PublishedAt | null;
  run: PublishRun | null;
  /** The LAST reading, which may be stale — the plan runs no provider CLI. */
  cycles: PublishCycles | null;
}

export interface PublishPlan {
  app_dir: string;
  page: string;
  name: string;
  runtime: Capability;
  state: Capability;
  /** Why this app cannot be published anywhere. Empty is the happy case. */
  blockers: string[];
  /** True but not disqualifying — what will be different once it is hosted. */
  notes: string[];
  python_files: string[];
  packages: string[];
  capability_labels: Record<Capability, string>;
  targets: PublishTarget[];
}

export function fetchPlan(dir: string, signal?: AbortSignal): Promise<PublishPlan> {
  return getJson<PublishPlan>(`/api/publish/plan?path=${encodeURIComponent(dir)}`, { signal });
}

export function fetchAuth(target: string, signal?: AbortSignal): Promise<PublishAuth> {
  return getJson<PublishAuth>(`/api/publish/auth?target=${encodeURIComponent(target)}`, { signal });
}

/** Run the provider's own browser approval. Takes no credential and never
 *  will: the author approves in their browser and the token lands in the
 *  provider CLI's config, where this app cannot read it. */
export function login(target: string): Promise<PublishAuth> {
  return postJson<PublishAuth>("/api/publish/login", { target });
}

/** Start a publish. Resolves when the run EXISTS, not when it finishes. */
export function deploy(
  dir: string,
  target: string,
  opts?: { include?: string[]; exclude?: string[]; project?: string },
): Promise<PublishRun> {
  return postJson<PublishRun>("/api/publish/deploy", { path: dir, target, ...opts });
}

export function fetchRun(
  dir: string,
  target: string,
  signal?: AbortSignal,
): Promise<{ run: PublishRun | null }> {
  return getJson<{ run: PublishRun | null }>(
    `/api/publish/run?path=${encodeURIComponent(dir)}&target=${encodeURIComponent(target)}`,
    { signal },
  );
}

/** Stop treating a provider project as this app's. Deletes nothing at the
 *  provider — the deployment stays up and the old URL keeps working. */
export function forget(dir: string, target: string): Promise<{ forgotten: boolean }> {
  return postJson<{ forgotten: boolean }>("/api/publish/forget", { path: dir, target });
}

/** Whether this target is paid for. Read-only: it must never be the thing that
 *  creates an identity, which is a key on the author's disk. */
export function fetchFunding(target: string, signal?: AbortSignal): Promise<PublishFunding> {
  return getJson<PublishFunding>(`/api/publish/funding?target=${encodeURIComponent(target)}`, {
    signal,
  });
}

/** Mint the publishing identity. Returns the seed phrase, once and only here.
 *  The caller shows it and forgets it — nothing may persist it. */
export function createIdentity(target: string): Promise<PublishIdentity> {
  return postJson<PublishIdentity>("/api/publish/identity", { target });
}

/** The app's balance and idle burn. Served from a day-old cache unless
 *  `refresh` is set; a refresh that fails returns the OLD reading with an
 *  `error` beside it rather than an empty panel. */
export function fetchCycles(
  dir: string,
  target: string,
  opts?: { refresh?: boolean; signal?: AbortSignal },
): Promise<{ cycles: PublishCycles | null; error?: string }> {
  const refresh = opts?.refresh ? "&refresh=true" : "";
  return getJson<{ cycles: PublishCycles | null; error?: string }>(
    `/api/publish/cycles?path=${encodeURIComponent(dir)}&target=${encodeURIComponent(
      target,
    )}${refresh}`,
    { signal: opts?.signal },
  );
}

// ---- the parts a browser decides --------------------------------------------

/** The phases in order, mirroring runs.PHASES. Only the ORDER is here — the
 *  wording comes down with each run, so a phase renamed on the server is
 *  renamed on screen without a rebuild, and a phase this build has never heard
 *  of still draws (see phaseProgress). */
export const PHASE_ORDER = [
  "checking",
  "exporting",
  "runtime",
  "building",
  "uploading",
  "recording",
] as const;

/**
 * How far along a run is, 0–1, or null when the phase is unknown to this build.
 *
 * Null rather than 0 so the bar can go indeterminate instead of lying: a bar
 * pinned at the left while work is clearly happening is the thing that makes
 * people force-quit.
 */
export function phaseProgress(run: PublishRun): number | null {
  if (run.state === "done") return 1;
  const i = (PHASE_ORDER as readonly string[]).indexOf(run.phase);
  if (i < 0) return null;
  // The midpoint of the phase's own slice: a phase that has STARTED is not
  // finished, and claiming the fraction it ends at would have the bar reach
  // full while the upload is still going.
  return (i + 0.5) / PHASE_ORDER.length;
}

/**
 * How long to wait before polling a run again, from how long it has been going.
 *
 * A first publish fetches a runtime and uploads megabytes, so the poll cannot
 * stay at half a second for four minutes; but the first few seconds are where
 * a fast publish finishes, and a slow first poll there would show a spinner
 * for a run that was already done.
 */
export function pollDelay(elapsedMs: number): number {
  if (elapsedMs < 10_000) return 500;
  if (elapsedMs < 60_000) return 1_500;
  return 3_000;
}

/** A URL as something to read rather than follow: no scheme, no trailing
 *  slash. The full URL is still what gets copied and what the QR encodes. */
export function displayUrl(url: string): string {
  return url.replace(/^https?:\/\//, "").replace(/\/$/, "");
}

/** Whether this target can be published to right now — the one condition the
 *  Publish button needs, and the reason it is a function: "eligible", "signed
 *  in" and "paid for" fail for unrelated reasons and are fixed in three
 *  different places.
 *
 *  A funded target with no funding state YET is treated as publishable: the
 *  probe is a second request, and greying the button while it is in flight
 *  makes a slow provider CLI look like a refusal. The server checks again
 *  before it spends anything. */
export function canPublish(
  target: PublishTarget,
  auth: PublishAuth | null,
  funding?: PublishFunding | null,
): boolean {
  if (!target.eligible || auth?.status !== "ready") return false;
  // Funding pays to CREATE the deployment. An app that already has one updates
  // in place — the upload is charged to the canister, which holds its own
  // cycles, not to the principal — so a principal at zero must not block a
  // re-publish. Gating it there would also be the worst possible moment to
  // block: the app is live, and the only way to change what readers see is the
  // button that has just gone grey.
  if (target.funding && funding && !target.published) return funding.funded;
  return true;
}

/** Cycles as an author reads them: `2.4T`, `850B`, `12M`.
 *
 *  Raw cycles are a fifteen-digit number and nobody compares two of those. The
 *  suffixes match the ones the transfer command accepts, so a figure read here
 *  can be typed back into a terminal. Mirrors `icp.format_cycles`. */
export function formatCycles(amount: number): string {
  for (const [size, suffix] of [
    [1e12, "T"],
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "K"],
  ] as [number, string][]) {
    if (Math.abs(amount) >= size) {
      const value = (amount / size).toFixed(1).replace(/\.0$/, "");
      return `${value}${suffix}`;
    }
  }
  return String(amount);
}

/** How alarmed to be about a runway.
 *
 *  Three tones rather than a boolean because a canister that runs out is
 *  frozen and then deleted with everything in it — that is a dead man's switch
 *  on the app, and it deserves more than a number going red on the last day.
 *  `"unknown"` when the provider reported no burn: not knowing is not "fine". */
export function runwayTone(cycles: PublishCycles): "ok" | "low" | "critical" | "unknown" {
  if (cycles.days_left === null) return "unknown";
  if (cycles.days_left < 7) return "critical";
  if (cycles.days_left < 30) return "low";
  return "ok";
}

/** A runway as a phrase, or null when there is no burn figure to divide by. */
export function runwayLabel(cycles: PublishCycles): string | null {
  if (cycles.days_left === null) return null;
  const days = Math.floor(cycles.days_left);
  if (days < 1) return "less than a day left";
  if (days === 1) return "about 1 day left";
  if (days < 90) return `about ${days} days left`;
  const months = Math.floor(days / 30);
  return `about ${months} months left`;
}

/** When a reading was taken, in the words the panel uses.
 *
 *  Always shown beside the figure. A stale number with an "as of" on it is
 *  worth more than a spinner, so nothing here hides one. */
export function readingAge(cycles: PublishCycles, now: number = Date.now()): string {
  const taken = Date.parse(cycles.read_at);
  if (Number.isNaN(taken)) return "as of an unknown time";
  const hours = Math.floor((now - taken) / 3_600_000);
  if (hours < 1) return "as of just now";
  if (hours < 24) return `as of ${hours}h ago`;
  const days = Math.floor(hours / 24);
  return days === 1 ? "as of yesterday" : `as of ${days} days ago`;
}

/** The grid cells this app needs, in reading order, already worded. */
export function requirements(plan: PublishPlan): string[] {
  return [plan.runtime, plan.state].map((c) => plan.capability_labels[c] ?? c);
}
