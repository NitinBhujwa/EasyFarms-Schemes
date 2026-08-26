# EasyFarms — myScheme Sync (GitHub Actions)

Scheduled sync that pulls scheme data from myScheme and stores it as
structured JSON files directly in this repo. No server, no database —
the repo itself is the datastore and the public report.

## Setup

1. Push this to a repo (can stay public — no secrets are ever committed).
2. Go to **Settings → Secrets and variables → Actions → New repository secret**
   and add:
   - `MYSCHEME_API_KEY` — required
   - `TELEGRAM_BOT_TOKEN` — optional, only needed for failure alerts
   - `TELEGRAM_CHAT_ID` — optional, only needed for failure alerts
3. Go to the **Actions** tab and enable workflows if prompted.
4. To test immediately instead of waiting for the schedule: **Actions → myScheme Sync → Run workflow**.

## What runs

`.github/workflows/sync.yml` runs `scripts/sync_myscheme.py` once a day
(cron is editable in the workflow file) and, if anything changed, commits
`data/` and `reports/` back to the repo.

## Layout

```
data/
  schemes/<scheme_id>.json   # normalized record per scheme, upserted in place
  raw/<scheme_id>.json       # raw detail + documents API response
  index.json                 # lightweight index of every scheme (id, slug, hash, active)
reports/
  latest.json                # most recent run's public report — overwritten each run
```

`index.json` and files under `data/schemes/` are safe to fetch directly
via `raw.githubusercontent.com` if another service needs to read this
data without cloning the repo.

## Notes

- A scheme that disappears from myScheme's search results is marked
  `"active": false` in its file and in the index — never deleted.
- Files are only rewritten when their content hash actually changes, so
  an unchanged scheme produces no git diff on a given run.
- If the sync fails outright, the workflow still commits a `failed`
  report to `reports/latest.json`, and the Actions tab shows a red ❌ —
  that failure is itself part of the public signal.
