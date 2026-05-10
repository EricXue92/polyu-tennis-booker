# Cloudflare Worker Trigger + Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace GitHub Actions' unreliable scheduled cron with a Cloudflare Worker that triggers `book.yml` via `workflow_dispatch` at 08:20 HKT and opens a GitHub issue at 08:35 HKT if no successful run exists for the day.

**Architecture:** Single Cloudflare Worker, two cron triggers in one `wrangler.toml`. The worker dispatches inside `scheduled()` based on the firing minute (20 vs 35). PAT lives in CF encrypted secrets. GitHub Actions workflow keeps `workflow_dispatch:` and loses `schedule:`.

**Tech Stack:** TypeScript, Cloudflare Workers, wrangler CLI. No test framework — deliberate (per spec); the worker is small, integration-heavy, and self-correcting via the watchdog.

**Spec:** `docs/superpowers/specs/2026-05-10-cloudflare-trigger-design.md`

---

## File Structure

```
infra/cloudflare-worker/
├── README.md                # PAT creation, deployment, rotation
├── package.json             # devDeps: wrangler, typescript, @cloudflare/workers-types
├── tsconfig.json
├── wrangler.toml            # bindings, two cron triggers, vars (owner/repo/workflow file)
├── .gitignore               # node_modules, .wrangler/, .dev.vars
└── src/
    └── worker.ts            # ~120 lines: scheduled handler + 4 helpers

.github/workflows/book.yml   # MODIFY: drop the three `- cron:` schedule entries
```

GitHub repo identity (used in `wrangler.toml`):

- Owner: `EricXue92`
- Repo: `polyu-tennis-booker`
- Workflow file: `book.yml`

(Confirmed from the artifact upload URL in the failed run: `https://github.com/EricXue92/polyu-tennis-booker/actions/runs/25591124776/...`)

---

## Task 1: Scaffold Cloudflare Worker project

**Files:**
- Create: `infra/cloudflare-worker/package.json`
- Create: `infra/cloudflare-worker/tsconfig.json`
- Create: `infra/cloudflare-worker/wrangler.toml`
- Create: `infra/cloudflare-worker/.gitignore`

- [ ] **Step 1: Create the directory and `package.json`**

```bash
mkdir -p infra/cloudflare-worker/src
```

`infra/cloudflare-worker/package.json`:

```json
{
  "name": "polyu-tennis-trigger",
  "version": "0.1.0",
  "private": true,
  "description": "Cloudflare Worker that triggers polyu-tennis-booker workflow_dispatch on time",
  "scripts": {
    "dev": "wrangler dev --test-scheduled",
    "deploy": "wrangler deploy",
    "tail": "wrangler tail"
  },
  "devDependencies": {
    "@cloudflare/workers-types": "^4.20250101.0",
    "typescript": "^5.4.0",
    "wrangler": "^3.80.0"
  }
}
```

- [ ] **Step 2: Create `tsconfig.json`**

`infra/cloudflare-worker/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "es2022",
    "module": "es2022",
    "moduleResolution": "bundler",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "lib": ["es2022"],
    "types": ["@cloudflare/workers-types"],
    "isolatedModules": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "noEmit": true
  },
  "include": ["src/**/*.ts"]
}
```

- [ ] **Step 3: Create `wrangler.toml`**

`infra/cloudflare-worker/wrangler.toml`:

```toml
name = "polyu-tennis-trigger"
main = "src/worker.ts"
compatibility_date = "2026-05-10"

[vars]
GITHUB_OWNER = "EricXue92"
GITHUB_REPO = "polyu-tennis-booker"
WORKFLOW_FILE = "book.yml"

# 00:20 UTC = 08:20 HKT — primary trigger
# 00:35 UTC = 08:35 HKT — watchdog
[triggers]
crons = ["20 0 * * *", "35 0 * * *"]
```

- [ ] **Step 4: Create `.gitignore` for the worker dir**

`infra/cloudflare-worker/.gitignore`:

```
node_modules/
.wrangler/
.dev.vars
*.log
```

- [ ] **Step 5: Verify scaffold compiles**

Run:

```bash
cd infra/cloudflare-worker && npm install
```

Expected: `node_modules/` populates, no errors. `package-lock.json` is created.

- [ ] **Step 6: Commit**

```bash
git add infra/cloudflare-worker/package.json infra/cloudflare-worker/tsconfig.json \
        infra/cloudflare-worker/wrangler.toml infra/cloudflare-worker/.gitignore \
        infra/cloudflare-worker/package-lock.json
git commit -m "scaffold cloudflare worker for tennis booker trigger"
```

---

## Task 2: Implement worker entrypoint and helpers

**Files:**
- Create: `infra/cloudflare-worker/src/worker.ts`

The worker has two responsibilities split by firing minute:

- minute 20 → `triggerWorkflow()` POSTs to `/repos/.../actions/workflows/book.yml/dispatches`
- minute 35 → `checkAndAlert()` GETs recent runs, opens an issue if no success today

Pure helpers `getTodayHKT()` and `isRunFromTodayHKT()` isolate the timezone math (UTC+8, no DST) so it's obvious what's being compared.

- [ ] **Step 1: Write the full worker**

`infra/cloudflare-worker/src/worker.ts`:

```typescript
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
```

- [ ] **Step 2: Type-check the worker**

Run:

```bash
cd infra/cloudflare-worker && npx tsc --noEmit
```

Expected: no output (clean type check).

- [ ] **Step 3: Commit**

```bash
git add infra/cloudflare-worker/src/worker.ts
git commit -m "implement cloudflare worker: dispatch + watchdog handlers"
```

---

## Task 3: Local smoke test of both code paths

The worker has two branches (minute 20 vs 35). `wrangler dev --test-scheduled` exposes a local URL `http://localhost:8787/__scheduled?cron=...` that simulates a cron firing. We can use it to verify both paths without waiting for real cron times.

This task does NOT hit the real GitHub API — we'll mock the `GITHUB_PAT` to a dummy value and check that the worker's logged behavior is what we expect (a 401 from GitHub, which proves the code path executed and reached the API).

- [ ] **Step 1: Create a local `.dev.vars` with a placeholder PAT**

`infra/cloudflare-worker/.dev.vars` (NOT committed; covered by .gitignore):

```
GITHUB_PAT=ghp_invalid_for_local_testing_only
```

- [ ] **Step 2: Start `wrangler dev` in a background terminal**

```bash
cd infra/cloudflare-worker && npx wrangler dev --test-scheduled
```

Wait for `Ready on http://localhost:8787` line.

- [ ] **Step 3: Trigger the 08:20 path (primary dispatch)**

In another terminal:

```bash
curl "http://localhost:8787/__scheduled?cron=20+0+*+*+*"
```

Expected in the wrangler dev output:

```
dispatch failed status=401 body={"message":"Bad credentials",...}
```

This proves: the cron minute parsing works, the URL is correct, the Authorization header is sent. 401 is the correct response to a fake PAT.

- [ ] **Step 4: Trigger the 08:35 path (watchdog)**

```bash
curl "http://localhost:8787/__scheduled?cron=35+0+*+*+*"
```

Expected in wrangler dev output:

```
runs fetch failed status=401
issue create failed status=401 body={"message":"Bad credentials",...}
```

Same reasoning: code path executed end-to-end, GitHub correctly rejected the fake PAT.

- [ ] **Step 5: Trigger an unexpected minute (defensive branch)**

```bash
curl "http://localhost:8787/__scheduled?cron=0+12+*+*+*"
```

Expected:

```
unexpected cron minute=0; ignoring
```

- [ ] **Step 6: Stop wrangler dev**

Ctrl-C in the wrangler dev terminal.

- [ ] **Step 7: Commit nothing**

This task only verifies behavior; no files changed (`.dev.vars` is gitignored).

---

## Task 4: Write deployment + rotation README

**Files:**
- Create: `infra/cloudflare-worker/README.md`

This is a runbook the user will follow once now and again at PAT renewal time. It must be self-sufficient — assume the reader has not read the spec.

- [ ] **Step 1: Write the README**

`infra/cloudflare-worker/README.md`:

````markdown
# polyu-tennis-trigger (Cloudflare Worker)

Triggers the `polyu-tennis-booker` GitHub Actions workflow at 08:20 HKT every day,
and opens a GitHub issue at 08:35 HKT if no successful booking happened for the day.

GitHub Actions' own scheduled cron is unreliable on low-activity private repos
(skipped or hours-late firings observed in practice). Cloudflare Workers Cron
fires on time within seconds.

## One-time setup

### 1. Create a GitHub fine-grained PAT

GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token.

- **Token name:** `cf-worker-tennis-trigger`
- **Expiration:** No expiration (GitHub will warn — accept; scope is narrow,
  see Architecture section in the spec for the rationale)
- **Repository access:** Only select repositories → `polyu-tennis-booker`
- **Repository permissions:**
  - **Actions:** Read and write
  - **Issues:** Read and write
  - **Contents:** Read-only
  - **Metadata:** Read-only (auto-included)

Generate, copy the `github_pat_...` token. It is shown only once.

### 2. Set up Cloudflare

```bash
cd infra/cloudflare-worker
npm install
npx wrangler login                     # opens browser for CF account auth
npx wrangler secret put GITHUB_PAT     # paste the PAT at the prompt
npx wrangler deploy
```

### 3. Disable the GitHub-side scheduled cron

Already done in `.github/workflows/book.yml` (the `schedule:` block was removed
when this Worker was deployed). The workflow keeps `workflow_dispatch:` so the
Worker (and the GitHub Actions UI) can still trigger it.

### 4. Verify

Cloudflare dashboard → Workers & Pages → `polyu-tennis-trigger` → Settings → Triggers.
Confirm two cron triggers: `20 0 * * *` and `35 0 * * *`.

To verify dispatch end-to-end without waiting for 08:20 HKT:

```bash
npx wrangler tail   # live logs
```

Then in the Cloudflare dashboard, click "Send" on the `20 0 * * *` trigger to
fire it manually. You should see `workflow dispatched` in the tail. Then:

```bash
gh run list --workflow="Daily Tennis Booking" --limit 1
```

Should show a freshly created run with `event=workflow_dispatch`.

## PAT rotation (only if compromised)

The PAT is configured with no expiration. Only rotate if you suspect it
leaked. Procedure:

1. Generate a new PAT (same procedure as setup, same scopes).
2. `cd infra/cloudflare-worker && npx wrangler secret put GITHUB_PAT` — paste new token.
3. Delete the old PAT from GitHub.

No worker redeploy needed; secrets take effect immediately on the next invocation.

If dispatches ever start failing for some other reason, the watchdog at
08:35 HKT will open a GitHub issue every day until you investigate.

## Local development

```bash
echo "GITHUB_PAT=ghp_dummy" > .dev.vars
npx wrangler dev --test-scheduled
# in another terminal:
curl "http://localhost:8787/__scheduled?cron=20+0+*+*+*"   # primary
curl "http://localhost:8787/__scheduled?cron=35+0+*+*+*"   # watchdog
```

The dummy PAT will get 401s from GitHub — that's expected; it confirms the
code paths reach the API.

## Architecture

See `docs/superpowers/specs/2026-05-10-cloudflare-trigger-design.md`.
````

- [ ] **Step 2: Commit**

```bash
git add infra/cloudflare-worker/README.md
git commit -m "docs: cloudflare worker setup, rotation, and local dev runbook"
```

---

## Task 5: Remove GitHub scheduled cron from book.yml

The Cloudflare Worker is now the sole trigger source. Removing the GitHub crons prevents accidental double-booking on the rare occasion both fire on the same morning.

**Files:**
- Modify: `.github/workflows/book.yml` lines 3-11 (the `schedule:` block and its comment)

- [ ] **Step 1: Edit `.github/workflows/book.yml`**

Replace lines 3-12 (everything from `on:` down through the last cron line) with:

```yaml
on:
  workflow_dispatch:
    inputs:
      dry_run:
        description: "Dry run (don't click final Submit)"
        type: boolean
        default: false
      skip_sleep:
        description: "Skip the wait until 08:30 HKT (run immediately)"
        type: boolean
        default: true
```

The full `on:` block is now just `workflow_dispatch:` — no `schedule:` key.

- [ ] **Step 2: Sanity-check the workflow YAML still parses**

```bash
gh workflow view "Daily Tennis Booking" --yaml 2>&1 | head -20
```

(This reads from the remote — only useful after pushing. Locally, just visually inspect the file: confirm `on:` has only `workflow_dispatch:` and no `schedule:`.)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/book.yml
git commit -m "remove github scheduled cron; cloudflare worker is sole trigger"
```

---

## Task 6: Update CLAUDE.md to reflect new architecture

**Files:**
- Modify: `CLAUDE.md` (the "Three hedged cron starts" bullet under Architecture)

The CLAUDE.md currently says "Three hedged cron starts" with a description that is now wrong. Replace with the new triggering architecture so future Claude sessions don't get confused.

- [ ] **Step 1: Replace the "Three hedged cron starts" bullet**

In `CLAUDE.md`, find the bullet starting with `- **Three hedged cron starts.**` (under "Things that aren't obvious from a single file") and replace the entire bullet with:

```markdown
- **External Cloudflare Worker triggers the workflow.** GitHub Actions'
  scheduled cron proved unreliable (skipped days, multi-hour delays), so a
  Cloudflare Worker (`infra/cloudflare-worker/`) calls
  `workflow_dispatch` API at 08:20 HKT daily. The booker still calls
  `sleep_until_hkt` to land on 08:30:00.000. A second cron in the same
  Worker fires at 08:35 HKT and opens a GitHub issue if no successful run
  exists for the day (the issue auto-emails the repo owner). The workflow
  itself has no `schedule:` block — `workflow_dispatch` only.
```

- [ ] **Step 2: Add `infra/cloudflare-worker/` to the file map (top of CLAUDE.md if any, or as a new "Infrastructure" section if missing)**

CLAUDE.md doesn't currently have a file map (the README does). Skip this step — the new bullet above sufficiently signposts the new directory.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude.md): cloudflare worker replaces github scheduled cron"
```

---

## Task 7: Push and prepare for deployment

This task does NOT deploy the Worker — that requires the user to interactively `wrangler login` and supply a PAT. It pushes the code so the user can deploy when ready.

- [ ] **Step 1: Push the branch**

```bash
git push origin main
```

- [ ] **Step 2: Tell the user what's next**

The user must now:

1. Create the fine-grained PAT (procedure in `infra/cloudflare-worker/README.md`).
2. Run the deployment commands from that README:
   ```bash
   cd infra/cloudflare-worker
   npm install
   npx wrangler login
   npx wrangler secret put GITHUB_PAT
   npx wrangler deploy
   ```
3. Verify the cron triggers in the Cloudflare dashboard.
4. Optionally trigger one manually from the dashboard to confirm end-to-end.

After the Worker is deployed, the next 08:20 HKT cron will fire automatically. No further action needed unless the watchdog opens an issue.

---

## Self-review

**Spec coverage:**
- ✅ Architecture (Worker, two crons, GitHub API): Tasks 1, 2
- ✅ Watchdog opens GitHub issue: Task 2 (`openIssue` function)
- ✅ Single trigger / no double-booking: Task 5 removes GH crons
- ✅ PAT scope and creation: Task 4 README
- ✅ Local testing: Task 3
- ✅ Deployment runbook: Task 4 README + Task 7 handoff
- ✅ Rotation procedure: Task 4 README
- ✅ Error handling (5xx retry, 4xx logged): Task 2 `triggerWorkflow`

**Placeholder scan:** None found — every code block is complete and runnable.

**Type consistency:** `Env` interface matches all four uses. `WorkflowRun` interface matches what the GitHub `actions/runs` endpoint returns. `getTodayHKT` and `isRunFromTodayHKT` use the same `en-CA` / `Asia/Hong_Kong` formatter.

**One scope note:** No deployment automation in the plan. Deployment requires interactive `wrangler login` + secret entry, which can't be scripted. Task 7 is a handoff, not a task an agentic worker can complete.
