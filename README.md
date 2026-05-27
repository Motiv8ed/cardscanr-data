# cardscanr-data

Static data / price-cache repository for the **CardScanR** Flutter app.

Deployed to **Cloudflare Pages** using the `public/` folder as the build
output directory.

---

## Repository structure

```
public/                        ← Cloudflare Pages build output
  _headers                     ← CORS / Cache-Control headers for /v1/*
  v1/
    index.json                 ← Dataset manifest (sha256, URLs, versions)
    app-config.json            ← Feature flags consumed by the Flutter app
    supported-games.json       ← Enabled card games
    supported-sources.json     ← Enabled price sources
    supported-languages.json   ← Language/catalogue/pricing availability manifest
    supported-markets.json     ← Market/currency/pricing availability manifest
    prices/
      pokemon/
        en/sample.json         ← English Pokémon sample prices (USD, provider-native)
        jp/sample.json         ← Japanese Pokémon sample prices (pricing unavailable)
    diagnostics/
      latest-build.json        ← Build metadata written by the CI job

data/
  cards_to_track.json          ← Cards the build script generates prices for
  supported_languages_config.json  ← Curated source of truth for supported-languages.json
  supported_markets_config.json    ← Curated source of truth for supported-markets.json

tools/
  build_price_cache.py         ← Builds price files + updates index.json
  validate_cache.py            ← Validates JSON, sha256, required fields, etc.

.github/workflows/
  update-price-cache.yml       ← Runs every 12 h (+ manual trigger), commits changes
  validate-cache.yml           ← Runs on pull requests (+ manual trigger)
```

---

## Canonical key format

```
game|language|setId|collectorNumber|normalizedName|variant|condition
```

Example: `pokemon|en|base1|4|charizard|holo|near_mint`

---

## Running locally

```bash
# Build the price cache
python tools/build_price_cache.py

# Build only EN current prices (batch-friendly mode)
python tools/build_price_cache.py current_prices

# Validate the cache
python tools/validate_cache.py

# Report EN current-price Stage 1 migration progress
python tools/report_en_current_price_migration.py

# Local-first batch updater (build + validate)
python tools/run_local_price_update.py --batch-size 10

# Safe long-run EN rotation until completion (budget-aware)
python tools/run_local_price_update.py --batch-size 10 --until-complete

# All-day mode (sleep when hourly budget is exhausted)
python tools/run_local_price_update.py --batch-size 10 --all-day

# All-day mode with explicit target budgets
python tools/run_local_price_update.py --batch-size 10 --all-day --target-hourly-requests 90 --target-daily-requests 990
```

No third-party packages are required.  
Optional environment variables:

| Variable | Purpose |
|---|---|
| `POKEMON_TCG_API_KEY` | Pokémon TCG API key (reserved for future live-fetch integration) |
| `CARDSCANR_MAX_REQUESTS_PER_HOUR` | Hourly request target for updater budgets (default `90`) |
| `CARDSCANR_MAX_REQUESTS_PER_DAY` | Rolling 24h request target for updater budgets (default `990`) |
| `CARDSCANR_REQUEST_SAFETY_BUFFER` | Buffer reserved below provider plan limits (default `10`) |
| `CARDSCANR_WORKER_UNTIL_COMPLETE` | Enables until-complete loop mode for catalog worker when set to true |
| `POKEWALLET_MAX_REQUESTS_PER_HOUR` | Compatibility alias for hourly request target |
| `POKEWALLET_MAX_REQUESTS_PER_DAY` | Compatibility alias for rolling 24h request target |
| `POKEWALLET_REQUEST_SAFETY_BUFFER` | Compatibility alias for request safety buffer |

Recommended local updater settings:

- `CARDSCANR_MAX_REQUESTS_PER_HOUR=90`
- `CARDSCANR_MAX_REQUESTS_PER_DAY=990`
- `CARDSCANR_REQUEST_SAFETY_BUFFER=10`
- `--batch-size 5`

The updater derives `CARDSCANR_CURRENT_PRICE_REQUEST_CAP` automatically from the remaining hourly and daily headroom before each cycle.

## Safe data release workflow

After generating data, run the controlled release command:

```powershell
.\scripts\release_cardscanr_data.ps1
```

Dry-run mode validates and summarizes but does not stage, commit, or push:

```powershell
.\scripts\release_cardscanr_data.ps1 -DryRun
```

Push is always explicit:

```powershell
.\scripts\release_cardscanr_data.ps1 -Push
```

Optional paths are opt-in only:

- `-IncludeDocs` to include `docs/` changes.
- `-IncludeReports` to include `reports/` changes.

The release workflow stages only allowed generated paths (`public/v1`, `data/*.json`, and optional docs/reports) and refuses temporary/cache paths, local image binaries, runtime logs/reports, and secret-like files.

---

## How to upload a clean report to ChatGPT

After running any major command, create a concise upload bundle:

```powershell
# Standalone export (any time)
.\scripts\export_chatgpt_report.ps1

# After a full pipeline run
.\scripts\run_cardscanr_full_data_pipeline.ps1 -NoFetch -BuildAppCatalogue -BuildImages -BuildHistory -Validate -ExportChatGPTReport

# After a release (dry-run safe)
.\scripts\release_cardscanr_data.ps1 -DryRun -ExportChatGPTReport
```

The export writes to `reports/chatgpt_exports/`:
- `cardscanr_chatgpt_report_latest.md` — human-readable summary
- `cardscanr_chatgpt_report_latest.json` — structured data
- `cardscanr_chatgpt_report_latest.zip` — safe bundle with all supporting files

Upload the `.zip` or `.md` file directly to ChatGPT. The bundle excludes `.env`, secrets, credentials, local image binaries, and large runtime logs. The export folder is git-ignored and will not dirty the worktree.

---

## Cloudflare Pages setup

1. Set **Build output directory** to `public`.  
2. Leave the build command empty (this is a static repo — CI commits the files
   directly).  
3. The `public/_headers` file applies CORS + cache headers automatically.

---

## Supabase configuration (local dev)

### Flutter app convention
- The Flutter app uses only the Supabase **anon key** (never the service role key).
- App config files: `supabase_env.json`, `supabase_env.example.json`, `supabase_env.local.json` (local only, not committed).
- Never commit real keys or secrets to git.

### cardscanr-data convention
- The worker/scheduler uses the **service role key** (never the anon key for writes).
- Local config: `supabase_env.local.json` (see `supabase_env.example.json` for format).
- This file is git-ignored and must never be committed.
- Example config:

```json
{
  "SUPABASE_URL": "https://your-project.supabase.co",
  "SUPABASE_ANON_KEY": "your-anon-key-used-by-app-only",
  "SUPABASE_SERVICE_ROLE_KEY": "your-local-worker-service-role-key-do-not-commit"
}
```

- The worker loads config in this order:
  1. **Process environment variables** (highest priority)
  2. `supabase_env.local.json` (if present, only for missing values)
- Secrets are never printed or logged.

### How to run the worker/scheduler with local config

```powershell
# Load env vars for this session (never prints secrets)
. scripts/load_supabase_env.ps1 supabase_env.local.json

# Then run the worker
scripts/run_market_price_worker.ps1

# Or the scheduler
scripts/run_market_price_scheduler.ps1
```

All market engine scripts will attempt to load the local config if present.

### Safety rules
- Never commit `supabase_env.local.json`, `.env`, or any real keys.
- Never put the service role key in the Flutter app or any committed file.
- Only the worker/scheduler uses the service role key, and only from local env or ignored config.
- The anon key is safe for app use, but do not commit real values.

---

## App-facing data contract

For production app integration rules and stability scope of `public/v1`, see:

- [`docs/APP_DATA_CONTRACT.md`](docs/APP_DATA_CONTRACT.md)

Source IDs in `/v1/supported-sources.json` are canonical lowercase `snake_case`.
Legacy IDs are exposed through per-source `aliases` for backward-compatible app matching during transition windows.
