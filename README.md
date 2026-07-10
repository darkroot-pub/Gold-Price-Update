# Gold & Silver Price Auto-Fetch

Automatically fetches daily Gold and Silver prices (per tola) from [fenegosida.org](https://fenegosida.org/) — the official site of the Federation of Nepal Gold & Silver Dealers' Association — and writes them to Firebase Firestore. Runs on a schedule via GitHub Actions.

## What it does

Twice a day, GitHub Actions runs two Python scripts that:
1. Fetch the live HTML from fenegosida.org
2. Parse out the current **Fine Gold (9999)** and **Silver** rates, per tola
3. Write the latest rate to Firestore (`global_data/gold_info` and `global_data/silver_info`)
4. Append a timestamped record to history collections (`gold_history` and `silver_history`)

## Files

| File | Purpose |
|---|---|
| `fetch_gold.py` | Fetches and parses the gold price, writes to Firestore |
| `fetch_silver.py` | Fetches and parses the silver price, writes to Firestore (with proxy fallbacks in case of IP blocking) |
| `requirements.txt` | Python dependencies (`requests`) |
| `.github/workflows/gold-silver-price.yml` | GitHub Actions workflow — schedule + manual trigger |

## How parsing works

Both scripts strip the page's HTML down to plain text first, then anchor on
the visible label text (e.g. `"per 1 tola"`, `"SILVER"`) rather than raw
HTML tag positions. This makes them resilient to the site's markup changing
(new wrapper `<div>`s, ad widgets, etc.) as long as the visible wording on
the page stays roughly the same. Each parser has 2–3 fallback strategies if
the primary anchor pattern doesn't match.

## Setup

### 1. Firebase secrets

In your repo: **Settings → Secrets and variables → Actions**, add:

| Secret | Description |
|---|---|
| `FIREBASE_PROJECT_ID` | Your Firebase project ID |
| `FIREBASE_API_KEY` | A Firebase Web API key with Firestore REST access |

### 2. Firestore structure

```
global_data/
  gold_info    { rate, updatedBy, lastUpdated, source, note }
  silver_info  { rate, updatedBy, lastUpdated, source, note }

gold_history/    (auto-appended, one doc per run)
silver_history/  (auto-appended, one doc per run)
```

### 3. Schedule

Configured in the workflow file (times in UTC, adjust as needed):

```yaml
schedule:
  - cron: '55 3 * * *'   # 9:40 AM NPT
  - cron: '25 5 * * *'   # 11:10 AM NPT
```

## Manual testing

The workflow includes `workflow_dispatch`, so you can trigger a run manually:

1. Go to the **Actions** tab
2. Select **Daily Gold & Silver Price Update** from the sidebar
3. Click **Run workflow**
4. Watch the live logs — each script prints which parsing strategy matched and the price found

> Note: manual runs write to Firestore the same as scheduled runs. If you want a dry run that doesn't touch Firestore, comment out the `write_to_firestore()` / `log_to_history()` calls in `main()` before testing, then revert.

## Troubleshooting

- **"Could not parse gold/silver price"** — the site's wording may have changed beyond what the fallback patterns expect. Check the live page and adjust the anchor phrases (`"per 1 tola"`, `"SILVER"`, etc.) in the relevant `parse_*_price()` function.
- **"Run workflow" button missing** — `workflow_dispatch` must be present in the workflow file **on your default branch**, not just a feature branch or PR.
- **Firestore write failures** — verify the API key has Firestore access and the project ID is correct.
