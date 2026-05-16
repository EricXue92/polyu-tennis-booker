# polyu-tennis-trigger (Cloudflare Worker)

Triggers the `polyu-tennis-booker` GitHub Actions workflow at 07:30 HKT every day,
and opens a GitHub issue at 08:35 HKT if no successful booking happened for the day.

The 60-minute lead before 08:30 HKT slot-open absorbs GitHub Actions runner
queue delays (observed up to 35 min). The booker uses an internal wall-clock
sleep to land its first request at 08:30:00.000 HKT regardless of when it
started.

GitHub Actions' own scheduled cron is unreliable on low-activity private repos
(skipped or hours-late firings observed in practice). Cloudflare Workers Cron
fires on time within seconds.

## One-time setup

### 1. Create a GitHub fine-grained PAT

GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token.

- **Token name:** `cf-worker-tennis-trigger`
- **Expiration:** No expiration (GitHub will warn — accept; the scope below is
  narrow enough that the blast radius is bounded)
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

### 3. Confirm the GitHub-side scheduled cron is gone

Check `.github/workflows/book.yml` — the `on:` block should contain only
`workflow_dispatch:`, no `schedule:`. (Removed in the same commit series that
introduced this Worker.)

### 4. Verify in the Cloudflare dashboard

Workers & Pages → `polyu-tennis-trigger` → Settings → Triggers.
Confirm two cron triggers: `30 23 * * *` and `35 0 * * *`.

To verify dispatch end-to-end without waiting for 07:30 HKT:

```bash
npx wrangler tail   # live logs
```

Then in the Cloudflare dashboard, open the Worker → Triggers, click the
three-dot menu next to the `30 23 * * *` cron and select **"Trigger"** (or
similar wording). You should see `workflow dispatched` in the tail. Then:

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
```

In another terminal:

```bash
curl "http://localhost:8787/__scheduled"
```

Wrangler uses **real wall-clock UTC time** for the scheduled event, so the
worker branches on whatever the current UTC minute is. Expected output in the
wrangler dev terminal:

- If current UTC minute is `30`: `dispatch failed status=401 body=...` (the
  fake PAT correctly fails GitHub auth — confirms the dispatch path executed)
- If current UTC minute is `35`: `runs fetch failed status=401` followed by
  `issue create failed status=401 body=...`
- Any other minute: `unexpected cron minute=N; ignoring`

Seeing the 401 means the code path reached GitHub — the test passes.

To exercise a specific branch on demand, temporarily hardcode the minute in
`src/worker.ts` (e.g., change `if (minute === 30)` to `if (true)`) and revert
before committing.

## Architecture

See `docs/superpowers/specs/2026-05-10-cloudflare-trigger-design.md` and
`docs/superpowers/plans/2026-05-10-cloudflare-trigger.md`.
