# polyu-tennis-booker

Auto-books a PolyU tennis court 7 days ahead, daily at 08:30 HKT.

## What it does

- A Cloudflare Worker (`infra/cloudflare-worker/`) calls GitHub's
  `workflow_dispatch` API at 08:20 HKT every day. (GitHub's own scheduled
  cron is too unreliable — multi-hour delays and full-day misses observed.)
- The booker sleeps in-process until 08:30:00.000 HKT, then logs in and
  tries to book a court 7 days ahead.
- Slot priority: 19:30–20:30, then 18:30–19:30, then 20:30–21:30, then
  17:30–18:30 (configurable in `src/config.py:SLOT_PRIORITY`).
- Books one slot per run; any court. No success notification.
- On no-slot-available or any error: workflow exits 1, GitHub emails you.
- A second cron in the same Worker fires at 08:35 HKT and opens a GitHub
  issue if no successful run exists for the day (which auto-emails you) —
  so you find out even when GH never managed to run the workflow at all.

See `docs/superpowers/specs/` and `docs/superpowers/plans/` for design and
build documents (the most recent files are authoritative).

## Local development

```bash
uv sync
uv run playwright install chromium
uv run pytest
```

Local dry-run (does not click final Submit):

```bash
POLYU_USERNAME='...' POLYU_PASSWORD='...' \
    uv run book-tennis --dry-run --skip-sleep
```

## Deployment

1. **Push to a private GitHub repo.**

   ```bash
   gh repo create polyu-tennis-booker --private --source=. --remote=origin --push
   ```

2. **Add Secrets** (Settings → Secrets and variables → Actions, or via gh):

   ```bash
   gh secret set POLYU_USERNAME --body 'your-username'
   gh secret set POLYU_PASSWORD --body 'your-password'
   ```

3. **Deploy the Cloudflare Worker** (the daily trigger). Full runbook in
   `infra/cloudflare-worker/README.md` — short version:

   ```bash
   cd infra/cloudflare-worker
   npm install
   npx wrangler login
   npx wrangler secret put GITHUB_PAT       # paste a fine-grained PAT
   npx wrangler deploy
   ```

   The PAT needs scope: `Actions: read/write`, `Issues: read/write`,
   `Contents: read`, `Metadata: read` on this repo only.

4. **Verify the workflow is registered.** The Actions tab should show
   "Daily Tennis Booking" (workflow_dispatch only — no `schedule:` block).

5. **First-day smoke test.** Trigger manually:

   ```bash
   gh workflow run "Daily Tennis Booking" -f dry_run=true -f skip_sleep=true
   gh run watch
   ```

   Download the artifact bundle and inspect `pre_submit.png`. It should show
   the confirmation page with the agreement checkbox ticked, ready to submit.

6. **Real run** (clicks Submit — actually books a court):

   ```bash
   gh workflow run "Daily Tennis Booking" -f dry_run=false -f skip_sleep=true
   ```

7. **Let it run on schedule.** From day 2 onwards, the Cloudflare Worker
   fires `workflow_dispatch` at 08:20 HKT (UTC 00:20). The runner cold-starts
   for ~3-5 min, then the booker sleeps until 08:30:00.000 before issuing
   the booking request. At 08:35 HKT the watchdog checks for a successful
   run and opens a GitHub issue if none exists.

## Updating selectors when the PolyU UI changes

Symptoms: workflow exit 1, log shows `Selector ... is not configured` or a
Playwright timeout on a specific element.

Fix:

```bash
POLYU_USERNAME='...' POLYU_PASSWORD='...' \
    uv run python scripts/discover_selectors.py
```

Open `artifacts/*.html`, update `src/config.py:Selectors`, commit, push.

## Costs

- **GitHub Actions** free tier: 2000 min/month for private repos. This
  workflow uses ~1-2 min/run = 30-60 min/month. Far under quota.
- **Cloudflare Workers** free tier: 100,000 requests/day. The Worker fires
  twice a day (08:20 + 08:35 HKT) — five orders of magnitude under quota.

## File map

```
.github/workflows/book.yml         # workflow_dispatch only; CF Worker triggers it
src/
├── booker.py                      # async login → search → pick slot → submit
├── config.py                      # URLs, slot priority, live PolyU selectors
├── dates.py                       # HKT date math + sleep_until_hkt
└── log.py                         # logger with password redaction
scripts/
└── discover_selectors.py          # interactive selector discovery tool
infra/cloudflare-worker/
├── src/worker.ts                  # scheduled() handler: dispatch + watchdog
├── wrangler.toml                  # 2 cron triggers (08:20 + 08:35 HKT)
└── README.md                      # deployment + PAT rotation runbook
tests/                             # offline unit tests
docs/superpowers/
├── specs/                         # design docs (most recent is authoritative)
└── plans/                         # implementation plans
```
