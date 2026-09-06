import { describe, expect, it } from "bun:test";
import {
  PHASE_ORDER,
  canPublish,
  displayUrl,
  phaseProgress,
  pollDelay,
  requirements,
  type PublishAuth,
  type PublishPlan,
  type PublishRun,
  type PublishTarget,
} from "./publish-lib";

function run(over: Partial<PublishRun> = {}): PublishRun {
  return {
    app_dir: "/apps/hsk",
    target: "cloudflare-pages",
    state: "running",
    phase: "checking",
    phase_label: "Checking the app",
    started_at: 0,
    finished_at: null,
    result: null,
    error: null,
    ...over,
  };
}

function target(over: Partial<PublishTarget> = {}): PublishTarget {
  return {
    id: "cloudflare-pages",
    label: "Cloudflare Pages",
    blurb: "",
    capabilities: [],
    auth: null,
    eligible: true,
    reasons: [],
    published: null,
    run: null,
    ...over,
  };
}

const auth = (status: string): PublishAuth => ({
  status,
  account: null,
  detail: "",
  help_url: null,
});

describe("phaseProgress", () => {
  it("advances through the phases and never reaches full before done", () => {
    let last = -1;
    for (const phase of PHASE_ORDER) {
      const p = phaseProgress(run({ phase }))!;
      expect(p).toBeGreaterThan(last);
      expect(p).toBeLessThan(1);
      last = p;
    }
    expect(phaseProgress(run({ state: "done", phase: "recording" }))).toBe(1);
  });

  it("goes indeterminate for a phase this build has never heard of", () => {
    // A server that grows a phase must not pin the bar at the left while work
    // is visibly happening.
    expect(phaseProgress(run({ phase: "signing" }))).toBeNull();
  });
});

describe("pollDelay", () => {
  it("is quick while a fast publish could still finish, then slows down", () => {
    expect(pollDelay(0)).toBe(500);
    expect(pollDelay(30_000)).toBe(1_500);
    expect(pollDelay(5 * 60_000)).toBe(3_000);
  });

  it("never speeds back up", () => {
    let last = 0;
    for (const t of [0, 9_999, 10_000, 59_999, 60_000, 600_000]) {
      expect(pollDelay(t)).toBeGreaterThanOrEqual(last);
      last = pollDelay(t);
    }
  });
});

describe("displayUrl", () => {
  it("drops the scheme and a bare trailing slash", () => {
    expect(displayUrl("https://hsk.pages.dev/")).toBe("hsk.pages.dev");
    expect(displayUrl("http://hsk.pages.dev")).toBe("hsk.pages.dev");
  });

  it("leaves a path alone — the address is not always the origin", () => {
    expect(displayUrl("https://hsk.pages.dev/cards")).toBe("hsk.pages.dev/cards");
  });
});

describe("canPublish", () => {
  it("wants both an eligible app and a signed-in provider", () => {
    expect(canPublish(target(), auth("ready"))).toBe(true);
    expect(canPublish(target({ eligible: false }), auth("ready"))).toBe(false);
    expect(canPublish(target(), auth("needs-login"))).toBe(false);
    expect(canPublish(target(), auth("unavailable"))).toBe(false);
    // Auth not asked for yet is not the same as refused, but it is not a yes.
    expect(canPublish(target(), null)).toBe(false);
  });
});

describe("requirements", () => {
  const plan = {
    runtime: "runtime:pyodide",
    state: "state:client-local",
    capability_labels: {
      "runtime:pyodide": "Python in the browser",
      "state:client-local": "saves on the reader's device",
    },
  } as unknown as PublishPlan;

  it("words the two cells the app needs", () => {
    expect(requirements(plan)).toEqual([
      "Python in the browser",
      "saves on the reader's device",
    ]);
  });

  it("falls back to the wire value for a cell this build lacks a label for", () => {
    const grown = { ...plan, runtime: "runtime:wasi" } as PublishPlan;
    expect(requirements(grown)[0]).toBe("runtime:wasi");
  });
});
