import { describe, expect, it } from "bun:test";
import {
  PHASE_ORDER,
  canPublish,
  displayUrl,
  formatCycles,
  phaseProgress,
  pollDelay,
  readingAge,
  requirements,
  runwayLabel,
  runwayTone,
  type PublishAuth,
  type PublishCycles,
  type PublishFunding,
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
    funding: false,
    eligible: true,
    reasons: [],
    published: null,
    run: null,
    cycles: null,
    ...over,
  };
}

function funding(over: Partial<PublishFunding> = {}): PublishFunding {
  return {
    funded: true,
    identity: true,
    principal: "un4fu-tqaaa-aaaab-qadjq-cai",
    balance: 2e12,
    minimum: 1e12,
    transfer_command: "icp cycles transfer 1T un4fu-tqaaa-aaaab-qadjq-cai -n ic",
    detail: "",
    help_url: null,
    ...over,
  };
}

function cycles(over: Partial<PublishCycles> = {}): PublishCycles {
  return {
    balance: 3e12,
    idle_burned_per_day: 1e11,
    read_at: new Date().toISOString(),
    days_left: 30,
    fresh: true,
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

describe("canPublish with a funded target", () => {
  const icp = target({ id: "icp-canister", funding: true, auth: auth("ready") });

  it("needs cycles as well as eligibility and a ready CLI", () => {
    expect(canPublish(icp, auth("ready"), funding())).toBe(true);
    expect(canPublish(icp, auth("ready"), funding({ funded: false }))).toBe(false);
  });

  it("does not grey the button while the funding probe is still in flight", () => {
    // The probe is a second request. A button that goes dead for two seconds
    // because a provider CLI is slow reads as a refusal, and the server checks
    // again before it spends anything.
    expect(canPublish(icp, auth("ready"), null)).toBe(true);
  });

  it("ignores funding entirely for a target that has none", () => {
    const pages = target({ auth: auth("ready") });
    expect(canPublish(pages, auth("ready"), funding({ funded: false }))).toBe(true);
  });
});

describe("formatCycles", () => {
  it("uses the units the transfer command accepts", () => {
    // A figure read on the page has to be typeable back into a terminal, and
    // fifteen-digit numbers do not compare.
    expect(formatCycles(1e12)).toBe("1T");
    expect(formatCycles(2.5e12)).toBe("2.5T");
    expect(formatCycles(8.5e11)).toBe("850B");
    expect(formatCycles(12e6)).toBe("12M");
    expect(formatCycles(999)).toBe("999");
  });

  it("agrees with the transfer command the server builds", () => {
    // Both sides format the minimum; a mismatch would print a command whose
    // amount is not the one the panel says is needed.
    expect(funding().transfer_command).toContain(formatCycles(funding().minimum));
  });
});

describe("runwayTone", () => {
  it("escalates well before the canister actually freezes", () => {
    // Running out means frozen and eventually deleted WITH ALL ITS STATE. A
    // number that only goes red on the last day is a warning nobody can act on.
    expect(runwayTone(cycles({ days_left: 200 }))).toBe("ok");
    expect(runwayTone(cycles({ days_left: 20 }))).toBe("low");
    expect(runwayTone(cycles({ days_left: 3 }))).toBe("critical");
  });

  it("treats an unknown burn as unknown, not as fine", () => {
    expect(runwayTone(cycles({ days_left: null }))).toBe("unknown");
  });
});

describe("runwayLabel", () => {
  it("says days until days stop being the useful unit", () => {
    expect(runwayLabel(cycles({ days_left: 0.4 }))).toBe("less than a day left");
    expect(runwayLabel(cycles({ days_left: 1.9 }))).toBe("about 1 day left");
    expect(runwayLabel(cycles({ days_left: 45 }))).toBe("about 45 days left");
    expect(runwayLabel(cycles({ days_left: 400 }))).toBe("about 13 months left");
  });

  it("has nothing to say when there is no burn figure", () => {
    expect(runwayLabel(cycles({ days_left: null }))).toBeNull();
  });
});

describe("readingAge", () => {
  it("labels a stale reading rather than hiding it", () => {
    // A runway with an "as of" on it beats a spinner.
    const now = Date.parse("2026-09-06T12:00:00Z");
    expect(readingAge(cycles({ read_at: "2026-09-06T11:40:00Z" }), now)).toBe("as of just now");
    expect(readingAge(cycles({ read_at: "2026-09-06T06:00:00Z" }), now)).toBe("as of 6h ago");
    expect(readingAge(cycles({ read_at: "2026-09-05T11:00:00Z" }), now)).toBe("as of yesterday");
    expect(readingAge(cycles({ read_at: "2026-09-01T12:00:00Z" }), now)).toBe("as of 5 days ago");
  });

  it("does not pretend to know when a stamp is unreadable", () => {
    expect(readingAge(cycles({ read_at: "last tuesday" }))).toBe("as of an unknown time");
  });
});
