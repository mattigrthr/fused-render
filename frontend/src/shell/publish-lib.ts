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

export interface PublishTarget {
  id: string;
  label: string;
  blurb: string;
  capabilities: Capability[];
  auth: PublishAuth | null;
  eligible: boolean;
  /** Full sentences, shown verbatim under a disabled row. Empty iff eligible. */
  reasons: string[];
  published: PublishedAt | null;
  run: PublishRun | null;
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
 *  Publish button needs, and the reason it is a function: "eligible" and
 *  "signed in" fail for unrelated reasons and are fixed in different places. */
export function canPublish(target: PublishTarget, auth: PublishAuth | null): boolean {
  return target.eligible && auth?.status === "ready";
}

/** The grid cells this app needs, in reading order, already worded. */
export function requirements(plan: PublishPlan): string[] {
  return [plan.runtime, plan.state].map((c) => plan.capability_labels[c] ?? c);
}
