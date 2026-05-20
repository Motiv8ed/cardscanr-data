# Local Price Updater

This project now supports a local-first EN current-price refresh flow.

## What It Does

- Refreshes a small rotating batch of EN sets per run.
- Validates artifacts after each run.
- Optionally commits and pushes if changes exist.
- Persists a cursor in `data/scheduled_price_refresh_state.json` so each run advances to the next batch.
- Tracks provider request usage in state and enforces hourly/daily safety budgets.
- Stops cleanly on provider rate-limit responses and resumes from persisted progress.
- Derives an internal per-cycle request cap for the EN current-price builder so a single batch cannot overshoot the remaining request headroom.

## Run Manually

From repository root:

```powershell
.\.venv\Scripts\python.exe tools\run_local_price_update.py --batch-size 20
```

Dry run (no writes):

```powershell
.\.venv\Scripts\python.exe tools\run_local_price_update.py --batch-size 20 --dry-run
```

Commit and push (if changed):

```powershell
.\.venv\Scripts\python.exe tools\run_local_price_update.py --batch-size 20 --commit --push
```

Run until one full EN rotation completes (safe budget-aware mode):

```powershell
.\.venv\Scripts\python.exe tools\run_local_price_update.py --batch-size 20 --until-complete
```

Optional loop caps:

```powershell
.\.venv\Scripts\python.exe tools\run_local_price_update.py --batch-size 20 --until-complete --max-cycles 30 --cycle-delay-seconds 20
```

PowerShell helper:

```powershell
.\scripts\run_local_price_update.ps1 -BatchSize 20 -Commit -Push
```

## Windows Task Scheduler

Create a task that runs every 30-60 minutes with action:

```text
Program/script: powershell.exe
Add arguments: -ExecutionPolicy Bypass -File "D:\cardscanr-data\scripts\run_local_price_update.ps1" -BatchSize 10 -Commit -Push
Start in: D:\cardscanr-data
```

Use a user account that has access to your git credential helper.

## API Credentials

The updater uses the same environment variables as `tools/build_price_cache.py`.
Set them in your user/system environment (or task-level environment) before scheduling.

Budget env vars (authoritative names):

- `CARDSCANR_MAX_REQUESTS_PER_HOUR` (default `90`)
- `CARDSCANR_MAX_REQUESTS_PER_DAY` (default `950`)
- `CARDSCANR_REQUEST_SAFETY_BUFFER` (default `10`)
- `CARDSCANR_CURRENT_PRICE_REQUEST_CAP` (internal per-cycle cap passed from the updater; normally set automatically)

Compatibility aliases are also accepted:

- `POKEWALLET_MAX_REQUESTS_PER_HOUR`
- `POKEWALLET_MAX_REQUESTS_PER_DAY`
- `POKEWALLET_REQUEST_SAFETY_BUFFER`

Pokewallet diagnostics use `POKEWALLET_API_KEY` from the environment only.

## Pokewallet Pro trial discovery

The Pokewallet Pro trial discovery runner is diagnostics-only. It tests provider coverage and response shapes before any production cache integration is built.

Dry run:

```powershell
python tools\probe_pokewallet_pro_prices.py --dry-run --trial-discovery
```

After a Pro trial is active:

```powershell
python tools\probe_pokewallet_pro_prices.py --enable-pro --trial-discovery --all-languages --max-requests 3000
```

Japanese-only trial pass:

```powershell
python tools\probe_pokewallet_pro_prices.py --enable-pro --trial-discovery --language jp --max-requests 1000
```

Resume:

```powershell
python tools\probe_pokewallet_pro_prices.py --enable-pro --trial-discovery --resume
```

Reset local discovery state:

```powershell
python tools\probe_pokewallet_pro_prices.py --reset-trial-discovery-state
```

Discovery writes:

- `public/v1/diagnostics/pokewallet-pro-trial-discovery-latest.json`
- `data/pokewallet_pro_trial_discovery_state.json`

It does not write production files under `public/v1/prices/current/...`.

Pricing notes:

- TCGPlayer usually represents US market guide data in USD.
- CardMarket usually represents European market guide data in EUR.
- Pokewallet prices are overseas market guide prices, not Australian sold prices.
- Provider currencies are recorded as returned; the cache does not convert currency.
- Apps should display source and currency. App-layer currency conversion can come later.
- True Australian market pricing should come from eBay AU sold listings or local sales data later.

Image notes:

- The Pokewallet image endpoint requires an API key.
- Discovery checks only small image samples and records response metadata.
- Images are not stored in the repository.
- EN/JP app-facing catalogue files currently keep `imageSmall` and `imageLarge` URLs only.
- Local image binary caching is still planned and is not performed by this updater.

## One-click background updater

Use the background loop when you want a low-maintenance updater that sleeps between runs.

Start (PowerShell):

```powershell
.\scripts\start_local_price_updater.ps1 -BatchSize 20 -IntervalMinutes 60
```

Start (double-click):

```text
scripts\start_local_price_updater.bat
```

Stop:

```powershell
.\scripts\stop_local_price_updater.ps1
```

Status:

```powershell
.\scripts\status_local_price_updater.ps1
```

Live watch:

```powershell
.\scripts\watch_local_price_updater.ps1
```

Live watch once (single snapshot):

```powershell
.\scripts\watch_local_price_updater.ps1 -Once
```

Live watch with custom refresh:

```powershell
.\scripts\watch_local_price_updater.ps1 -RefreshSeconds 60
```

Status with logs:

```powershell
.\scripts\status_local_price_updater.ps1 -ShowLogs
```

Logs:

```text
logs\local_price_updater.log
```

Recommended interval:

- Start with 60 minutes.
- Use batch size 20 for the default local rotation.
- With roughly 159 EN current-price files, batch size 20 every 60 minutes is about 8 hours for a full rotation.
- A faster optional setting is batch size 30 every 60 minutes, which is roughly 5-6 hours for a full rotation.

Recommended local updater settings:

- `CARDSCANR_MAX_REQUESTS_PER_HOUR=90`
- `CARDSCANR_MAX_REQUESTS_PER_DAY=950`
- `CARDSCANR_REQUEST_SAFETY_BUFFER=10`
- `--batch-size 5`

The updater derives `CARDSCANR_CURRENT_PRICE_REQUEST_CAP` automatically from the remaining hourly and daily headroom before each cycle.

Notes:

- The updater runs `git pull --ff-only` before each cycle and skips the cycle on pull failure.
- The updater skips the cycle if unrelated uncommitted changes already exist.
- API keys must come from environment variables; never hardcode them.
- Cloudflare Pages deploys automatically after each successful GitHub push.
- The loop sleeps most of the time, so CPU and memory usage should stay minimal.
- Very short intervals create more GitHub commits and more Cloudflare deploys.

## Checking updater progress

Status dashboard:

```powershell
.\scripts\status_local_price_updater.ps1
```

Live watch dashboard:

```powershell
.\scripts\watch_local_price_updater.ps1
```

Watch once and exit:

```powershell
.\scripts\watch_local_price_updater.ps1 -Once
```

Override watch refresh interval:

```powershell
.\scripts\watch_local_price_updater.ps1 -RefreshSeconds 30
.\scripts\watch_local_price_updater.ps1 -RefreshSeconds 60
.\scripts\watch_local_price_updater.ps1 -RefreshSeconds 10
```

Show recent logs in status:

```powershell
.\scripts\status_local_price_updater.ps1 -ShowLogs
```

Raw log tail:

```powershell
Get-Content .\logs\local_price_updater.log -Tail 80 -Wait
```

Status fields:

- Times are shown in AEST (`E. Australia Standard Time`) when available, with local-time fallback if timezone conversion fails.
- `Status`, `Current state`, and the next update/push lines provide the key at-a-glance state.
- `Next update cycle` and `Time until update` are emphasized while sleeping.
- `Current update`, `Elapsed`, and `Estimated finish` are shown while an active update is in progress.
- `Last success`, `Last push`, `Last commit`, and `Last duration` summarize the latest successful cycle.
- `Last sets` shows a compact summary of the latest batch.
- Skipped cycles caused by uncommitted changes are warnings, not errors.
- Recent logs are hidden by default; use `-ShowLogs` when needed.
- Watch mode defaults to a calmer 30-second auto-refresh interval.
- `Next push` means a push will occur only after the next successful update and validation phase.

Local runtime files:

- `logs\local_price_updater.log`
- `logs\local_price_updater_status.json`
- `logs\local_price_update_last_result.json`

These files are local only and are ignored by git.

## App-facing price freshness status

Public status files are now generated for the app cache layer:

- `public/v1/prices/status.json`
- `public/v1/prices/current/pokemon/en/status.json`
- `public/v1/prices/current/pokemon/jp/status.json`

These files are UTC-based and intended for mobile/web app visibility features:

- EN freshness state (`fresh`, `stale`, `very_stale`, `unavailable`)
- last successful EN update and push timestamps
- expected next EN update timestamp
- EN batch size, update interval, and full-rotation estimate
- EN oldest/newest set refresh timestamps
- per-set and per-card freshness metadata in EN set files

Card detail freshness fields (EN set files):

- set-level: `lastSuccessfulPriceUpdateAtUtc`, `nextExpectedPriceUpdateAtUtc`, `status`, `staleness`
- card-level: `fetchedAtUtc`, `nextExpectedPriceUpdateAtUtc`, `staleness`

Flutter card detail wording examples:

- Price checked 41 minutes ago
- Next check expected around 10:14 AM
- Cached latest-known price, not a live quote
- Manual refresh checks the latest cache first

Manual refresh semantics (current app behavior):

1. Re-fetch the latest Cloudflare cache file for the card's set.
2. If unchanged and live provider usage is enabled, optionally try live lookup.
3. Never overwrite a valid saved price with no result, unavailable, or error responses.

Future backend manual refresh (not implemented yet):

- Queue card-level priority refresh.
- Update refresh status when backend queue support exists.

Timezone policy:

- Public API/cache timestamps remain UTC for global clients.
- Local operator dashboard (`status_local_price_updater.ps1`) displays times in AEST.
