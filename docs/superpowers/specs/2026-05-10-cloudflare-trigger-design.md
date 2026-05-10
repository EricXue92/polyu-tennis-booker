# Cloudflare Worker as Primary Trigger + Daily Watchdog

**Date:** 2026-05-10
**Status:** Design — pending implementation

## Background

The booker has been live for 2 days and missed both:

- **2026-05-09**: GitHub's scheduled cron fired at 04:00 UTC (12:00 HKT) instead of
  the configured 00:15 / 00:20 / 00:25 UTC — a ~3.5 h delay. The booker correctly
  woke up immediately (`sleep_until_hkt` returns 0 when the target has passed) and
  attempted to book, but every priority slot for 2026-05-16 was already taken.
- **2026-05-10**: GitHub's scheduled cron didn't fire at all. No workflow run for
  the day.

Root cause is GitHub Actions' known unreliability for scheduled workflows on
low-activity private repos. The three hedged starts in `book.yml` all share the
same scheduler — when GitHub deprioritizes the workflow, all three skip together.

This problem cannot be solved within GitHub Actions' scheduling layer.

## Goals

1. Trigger the existing `book.yml` workflow on time, every day, with > 99% reliability.
2. Be notified when a day still fails (so manual retry is possible before the
   booking window closes).
3. No double-booking: the booker tries fallback slots if the top one is taken,
   so two parallel runs would book two different courts. Only one trigger per day.
4. Minimal new infrastructure. Zero recurring cost.

## Non-goals

- Running the Python booker outside GitHub Actions (the current code, secrets,
  artifact uploads, and Chromium install all stay where they are).
- Solving GitHub's scheduled-cron unreliability in general.
- Real-time push notifications (email-via-GitHub-issue is good enough — the
  user checks email anyway).

## Architecture

```
┌──────────────────────────┐
│ Cloudflare Worker        │      One file, two cron triggers
│ (single deployment)      │
│                          │
│  cron 20 0 * * *         │ ── 08:20 HKT (00:20 UTC) ── PRIMARY TRIGGER
│  cron 35 0 * * *         │ ── 08:35 HKT (00:35 UTC) ── WATCHDOG
└─────────┬────────────────┘
          │
          ├── PRIMARY ──→  POST /repos/{owner}/{repo}/actions/workflows/book.yml/dispatches
          │                Body: { "ref": "main" }
          │                Auth: Bearer <PAT>
          │                → GitHub spins up runner (~3-5 min)
          │                → uv sync + chromium install
          │                → book-tennis (sleep_until_hkt → 08:30:00.000 → book)
          │
          └── WATCHDOG ─→  GET /repos/{owner}/{repo}/actions/runs?created=>=<today>&per_page=10
                           If no run with conclusion=success exists for today:
                             POST /repos/{owner}/{repo}/issues
                             { "title": "Tennis booking missed YYYY-MM-DD", "body": ... }
                           GitHub auto-emails the repo owner.
```

**Why 08:20 HKT for the primary trigger:** GitHub workflow_dispatch latency
plus runner queue plus setup is normally < 5 min, occasionally up to 8 min.
A 10-min runway lands the booker on the page comfortably before 08:30. Earlier
than that wastes runner setup time idling; later cuts it close on bad days.

**Why 08:35 HKT for the watchdog:** The booker takes ~30 s after waking up to
either succeed (exit 0) or report no-slot (exit 1). 5 min after the booking
attempt is enough margin for any normal-case completion. If a run is still
queued at 08:35, that itself is the problem the watchdog should flag.

**Why Cloudflare Workers Cron:** edge-network scheduling, < 1 s drift in
practice, free tier (100k requests/day) covers our 2 invocations/day with
five orders of magnitude to spare. PAT lives in encrypted Worker Secrets,
not in a third-party SaaS dashboard.

## Components

### 1. Cloudflare Worker (`infra/cloudflare-worker/`)

New top-level directory in this repo. Self-contained:

```
infra/cloudflare-worker/
├── README.md           # how to deploy / rotate PAT
├── wrangler.toml       # CF deployment config
├── package.json        # dev dependencies (wrangler)
├── tsconfig.json
└── src/
    └── worker.ts       # entry point
```

**`worker.ts` shape** (~80 lines TypeScript):

```typescript
export interface Env {
  GITHUB_PAT: string;       // CF secret
  GITHUB_OWNER: string;     // wrangler.toml var
  GITHUB_REPO: string;      // wrangler.toml var
  WORKFLOW_FILE: string;    // wrangler.toml var (e.g. "book.yml")
}

export default {
  async scheduled(event: ScheduledEvent, env: Env, ctx: ExecutionContext) {
    const minute = new Date(event.scheduledTime).getUTCMinutes();
    if (minute === 20) {
      await triggerWorkflow(env);
    } else if (minute === 35) {
      await checkAndAlert(env);
    }
  },
};

async function triggerWorkflow(env: Env): Promise<void> { ... }
async function checkAndAlert(env: Env): Promise<void> { ... }
```

The `scheduled` handler dispatches on minute because Cloudflare passes both
crons through the same handler. UTC minutes are stable (no DST in HKT).

### 2. GitHub Actions workflow (`.github/workflows/book.yml`)

**Change:** remove the three `schedule:` cron entries. Keep
`workflow_dispatch:`. The Worker becomes the sole trigger.

Existing inputs (`dry_run`, `skip_sleep`) stay — useful for manual retry from
the GitHub UI when the watchdog alerts.

### 3. PAT

Fine-grained token, scoped to this repo only:

- Repository access: `polyu-tennis-booker` only
- Permissions:
  - `Actions: Read and write` (dispatch + read run status)
  - `Issues: Read and write` (create issue on miss)
  - `Metadata: Read-only` (mandatory)
  - `Contents: Read-only` (workflow_dispatch needs ref resolution)
- Expiration: 1 year. Renewal procedure documented in worker README.

Stored as Cloudflare Worker secret `GITHUB_PAT` (`wrangler secret put`).

## Data flow

**Primary trigger (08:20 HKT daily):**

1. Cloudflare schedules the Worker. Worker reads `GITHUB_PAT` from secrets.
2. Worker POSTs to `/repos/.../actions/workflows/book.yml/dispatches` with
   `{ "ref": "main" }`. Expected: 204 No Content.
3. On non-204 response, Worker logs to CF dashboard and (best-effort) creates
   an issue. The watchdog will catch it at 08:35 anyway.
4. GitHub queues the workflow → runner picks it up → existing booker logic runs.
5. The booker calls `sleep_until_hkt(time(8, 30, 0))` — sleeps until 08:30:00.000
   regardless of when the runner became ready. No code change in the booker.

**Watchdog (08:35 HKT daily):**

1. Worker computes today's date in HKT.
2. Worker GETs `/repos/.../actions/runs?per_page=10` (no date filter via API
   parameter — the `created` query param needs ISO format and is fiddly; we
   filter client-side over the most recent 10 runs, which always covers
   today + recent history).
3. Filter for runs whose `created_at` falls on today's HKT date AND `name`
   == "Daily Tennis Booking".
4. If any such run has `conclusion === "success"`: do nothing.
5. Otherwise, POST `/repos/.../issues` with title `"Tennis booking missed
   YYYY-MM-DD"` and a body linking to the most recent run (if any) plus the
   workflow's manual-trigger URL.
6. GitHub emails the repo owner (xuelong0208@gmail.com is already on the
   notification list as repo owner — verify in account settings if not).

## Error handling

- **Worker fails to fetch CF secrets**: extremely rare; CF dashboard logs the
  error. Watchdog still fires from a separate scheduled invocation.
- **GitHub API returns 401/403**: PAT expired or scope wrong. Worker logs the
  status code and response body to CF dashboard. Watchdog at 08:35 will not
  find a successful run → opens issue → user investigates and rotates PAT.
- **GitHub API returns 5xx**: transient. Worker retries once with 2 s
  backoff. If still failing, log + rely on watchdog.
- **CF Worker outage at 08:20**: the watchdog at 08:35 still tries to run
  (independent invocation). It will see no successful run and open an issue.
  User can manually trigger via GitHub UI.
- **CF Worker outage covering both 08:20 AND 08:35**: silent miss. Acceptable
  failure mode — Cloudflare's edge network has 99.99%+ uptime; this is
  extraordinarily rare. User notices when they have no court and can manually
  trigger.

## Testing

- **Local Worker dev**: `wrangler dev --test-scheduled` lets you trigger the
  scheduled event manually with a fake timestamp. Use this to exercise both
  the 08:20 and 08:35 branches without waiting.
- **End-to-end on CF**: deploy, then `wrangler tail` to watch real-time logs
  on the next 08:20/08:35 fire. Confirm the GitHub workflow gets dispatched
  and (separately) confirm the watchdog correctly sees the success.
- **Watchdog false-positive guard**: trigger the workflow manually with
  `--dry-run` (which still exits 0 on a successful walk through), then verify
  the watchdog at 08:35 does NOT open an issue.
- **Watchdog true-positive**: don't trigger the workflow at all on a test day
  → watchdog at 08:35 should open an issue.
- No automated tests for the Worker beyond the deployment smoke test. The
  Worker is small and the failure mode (issue gets opened) is self-correcting:
  if the watchdog spuriously opens issues we'll see immediately.

## Deployment

One-time setup steps (documented in `infra/cloudflare-worker/README.md`):

1. Create fine-grained PAT (procedure above).
2. `cd infra/cloudflare-worker && npm install`
3. `npx wrangler login` (browser auth to Cloudflare).
4. Edit `wrangler.toml` to set `GITHUB_OWNER` and `GITHUB_REPO`.
5. `npx wrangler secret put GITHUB_PAT` (paste the token at the prompt).
6. `npx wrangler deploy`.
7. Verify in CF dashboard: Workers → `polyu-tennis-trigger` → Triggers shows
   the two cron schedules.
8. Edit `.github/workflows/book.yml`: remove the three `schedule:` lines.
   Commit and push.
9. Manually fire once to confirm: in CF dashboard, "Send" a test scheduled
   event for `0 20 * * *`. Watch `gh run list` to see the dispatch land.

Recurring maintenance:

- PAT rotation (~1 year): regenerate, `wrangler secret put GITHUB_PAT` again.
- If `book.yml` filename changes: update `WORKFLOW_FILE` var in `wrangler.toml`.

## Out of scope (explicit)

- **Hedged dispatch**: only one trigger per day. Two parallel triggers would
  cause double booking (the booker's `pick_slot` falls back to the next
  priority slot when the top is taken, so two runs would each successfully
  book a different court).
- **Cross-day catch-up**: if a day misses, the watchdog notifies but does not
  attempt to book. Booking 6-days-ahead instead of 7 is a different and
  worse window for slot availability.
- **Auto-retry on dispatch failure**: the watchdog catches it 15 min later;
  by 08:35 the user has 7+ hours of waking time to manually retry from the
  GitHub UI before the next day's window opens.
- **Replacing GitHub Actions**: the runner, secrets, artifact uploads, and
  Chromium install stay in CI. Only the trigger moves.

## File map (after this work)

```
.github/workflows/book.yml                    # SCHEDULE removed; workflow_dispatch only
infra/cloudflare-worker/
├── README.md
├── wrangler.toml
├── package.json
├── tsconfig.json
└── src/worker.ts
docs/superpowers/specs/2026-05-10-cloudflare-trigger-design.md  # this file
```

No changes to `src/`, `tests/`, or the booker's behavior.
