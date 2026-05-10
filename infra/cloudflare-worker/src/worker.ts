/**
 * Tennis booking trigger + watchdog.
 *
 * Two cron triggers in wrangler.toml share this scheduled() handler:
 *  - 00:20 UTC (08:20 HKT): dispatch the booking workflow
 *  - 00:35 UTC (08:35 HKT): check today had a successful run, else open issue
 */

export interface Env {
  GITHUB_PAT: string;       // CF secret: `wrangler secret put GITHUB_PAT`
  GITHUB_OWNER: string;
  GITHUB_REPO: string;
  WORKFLOW_FILE: string;
}

const GITHUB_API = "https://api.github.com";
const USER_AGENT = "polyu-tennis-trigger";

export default {
  async scheduled(event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    const minute = new Date(event.scheduledTime).getUTCMinutes();
    if (minute === 20) {
      ctx.waitUntil(triggerWorkflow(env));
    } else if (minute === 35) {
      ctx.waitUntil(checkAndAlert(env));
    } else {
      console.log(`unexpected cron minute=${minute}; ignoring`);
    }
  },
};

/** YYYY-MM-DD in Asia/Hong_Kong. en-CA locale yields ISO date format. */
export function getTodayHKT(now: Date = new Date()): string {
  return now.toLocaleDateString("en-CA", { timeZone: "Asia/Hong_Kong" });
}

/** Compares a GitHub run's created_at (ISO UTC) against an HKT YYYY-MM-DD. */
export function isRunFromTodayHKT(createdAtIso: string, todayHKT: string): boolean {
  const runDateHKT = new Date(createdAtIso).toLocaleDateString("en-CA", {
    timeZone: "Asia/Hong_Kong",
  });
  return runDateHKT === todayHKT;
}

async function triggerWorkflow(env: Env): Promise<void> {
  const url = `${GITHUB_API}/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/${env.WORKFLOW_FILE}/dispatches`;
  const body = JSON.stringify({ ref: "main" });
  // One retry on 5xx; transient GitHub API blips are common.
  for (let attempt = 1; attempt <= 2; attempt++) {
    const resp = await fetch(url, {
      method: "POST",
      headers: githubHeaders(env),
      body,
    });
    if (resp.status === 204) {
      console.log(`workflow dispatched (attempt ${attempt})`);
      return;
    }
    const text = await resp.text();
    console.error(`dispatch failed status=${resp.status} body=${text.slice(0, 500)}`);
    if (resp.status < 500 || attempt === 2) return; // 4xx won't get better; 5xx after retry → give up
    await sleep(2000);
  }
}

interface WorkflowRun {
  name: string;
  created_at: string;
  conclusion: string | null;  // null while in progress
  status: string;
  html_url: string;
}

async function checkAndAlert(env: Env): Promise<void> {
  const today = getTodayHKT();
  const runsUrl =
    `${GITHUB_API}/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/runs` +
    `?per_page=10`;
  const resp = await fetch(runsUrl, { headers: githubHeaders(env) });
  if (!resp.ok) {
    console.error(`runs fetch failed status=${resp.status}`);
    // Best-effort: open the issue anyway so the user investigates.
    await openIssue(env, today, "(could not fetch run list to confirm)");
    return;
  }
  const data = (await resp.json()) as { workflow_runs: WorkflowRun[] };
  const todaysRuns = data.workflow_runs.filter(
    (r) => r.name === "Daily Tennis Booking" && isRunFromTodayHKT(r.created_at, today),
  );
  const success = todaysRuns.find((r) => r.conclusion === "success");
  if (success) {
    console.log(`watchdog OK: success run ${success.html_url}`);
    return;
  }
  const lastRunUrl = todaysRuns[0]?.html_url ?? "(no run for today)";
  await openIssue(env, today, lastRunUrl);
}

async function openIssue(env: Env, todayHKT: string, lastRunUrl: string): Promise<void> {
  const url = `${GITHUB_API}/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/issues`;
  const manualTrigger = `https://github.com/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/${env.WORKFLOW_FILE}`;
  const body = [
    `Watchdog ran at 08:35 HKT and found no successful "Daily Tennis Booking" run for ${todayHKT}.`,
    ``,
    `**Last run for today:** ${lastRunUrl}`,
    ``,
    `**Manually retry:** ${manualTrigger} → "Run workflow" → uncheck "Skip sleep" if it's still before 08:30 HKT.`,
    ``,
    `If it's already past noon HKT, the slot is almost certainly gone — close this issue without action.`,
  ].join("\n");
  const resp = await fetch(url, {
    method: "POST",
    headers: githubHeaders(env),
    body: JSON.stringify({
      title: `Tennis booking missed ${todayHKT}`,
      body,
    }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    console.error(`issue create failed status=${resp.status} body=${text.slice(0, 500)}`);
  } else {
    console.log(`watchdog opened issue for ${todayHKT}`);
  }
}

function githubHeaders(env: Env): Record<string, string> {
  return {
    "Authorization": `Bearer ${env.GITHUB_PAT}`,
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": USER_AGENT,
    "Content-Type": "application/json",
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
