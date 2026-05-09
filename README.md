# polyu-tennis-booker

Auto-books a PolyU tennis court 7 days ahead, daily at 08:30 HKT.

## What it does

- Runs as a GitHub Actions cron job every day at 08:15 / 08:20 / 08:25 HKT
  (three hedged starts — GitHub's cron can be delayed 5–30 min).
- Sleeps in-process until 08:30:00.000 HKT, then logs in and tries to book.
- Slot priority: 19:30–20:30, then 18:30–19:30, then 20:30–21:30.
- Books one slot per run; any court. No success notification.
- On no-slot-available or any error: workflow exits 1, GitHub emails you.

See `docs/superpowers/specs/2026-05-09-tennis-booking-design.md` for design,
`docs/superpowers/plans/2026-05-09-tennis-booking.md` for the build plan.

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

3. **Verify the workflow is enabled.** The Actions tab should show
   "Daily Tennis Booking". GitHub disables scheduled workflows on inactive
   repos after 60 days; push any commit periodically to keep alive.

4. **First-day smoke test.** Trigger manually:

   ```bash
   gh workflow run "Daily Tennis Booking" -f dry_run=true -f skip_sleep=true
   gh run watch
   ```

   Download the artifact bundle and inspect `pre_submit.png`. It should show
   the confirmation page with the agreement checkbox ticked, ready to submit.

5. **Real run** (clicks Submit — actually books a court):

   ```bash
   gh workflow run "Daily Tennis Booking" -f dry_run=false -f skip_sleep=true
   ```

6. **Let it run on schedule.** From day 2 onwards, the workflow fires
   automatically at 08:15 / 08:20 / 08:25 HKT (UTC 00:15 / 00:20 / 00:25),
   each job sleeping until 08:30:00.000 before issuing the booking request.

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

GitHub Actions free tier: 2000 min/month for private repos. This workflow
uses ~1–2 min/run = 30–60 min/month. Far under quota.

## File map

```
.github/workflows/book.yml   # cron + Playwright runner
src/
├── booker.py                # async login → search → pick slot → submit
├── config.py                # URLs, slot priority, live PolyU selectors
├── dates.py                 # HKT date math + sleep_until_hkt
└── log.py                   # logger with password redaction
scripts/
└── discover_selectors.py    # interactive discovery tool
tests/                       # offline unit tests (17 passing)
docs/superpowers/
├── specs/2026-05-09-tennis-booking-design.md
└── plans/2026-05-09-tennis-booking.md
```
