# PokéWallet Catalogue Worker

This worker collects PokéWallet catalogue, card, and image-reference metadata. It only runs when you start it.

It is budget-aware and dynamically schedules the next cycle based on hourly/daily request usage instead of always sleeping a fixed 75 minutes.

The default mode is manual loop mode. It is not installed into Windows Task Scheduler by default. Scheduled task mode remains available only if you explicitly run `scripts\install_pokewallet_catalog_scheduled_task.ps1`.

The worker also supports an until-complete mode that keeps running one cycle per interval until all currently known provider language exports are complete (or until rate limit halts the run).

You can also force until-complete mode via environment variable:

```powershell
$env:POKEWALLET_WORKER_UNTIL_COMPLETE = "true"
```

CARDSCANR alias is also supported:

```powershell
$env:CARDSCANR_WORKER_UNTIL_COMPLETE = "true"
```

## Commands

Start manual worker:

```powershell
.\scripts\start_pokewallet_catalog_worker.ps1
```

Start manual worker in until-complete mode:

```powershell
.\scripts\start_pokewallet_catalog_worker.ps1 -UntilComplete
```

Run worker loop directly in until-complete mode:

```powershell
.\scripts\run_pokewallet_catalog_worker_loop.ps1 -UntilComplete
```

Or double-click:

```text
scripts\start_pokewallet_catalog_worker.bat
```

Status:

```powershell
.\scripts\status_pokewallet_catalog_worker.ps1
```

Or double-click:

```text
scripts\status_pokewallet_catalog_worker.bat
```

Stop:

```powershell
.\scripts\stop_pokewallet_catalog_worker.ps1
```

Or double-click:

```text
scripts\stop_pokewallet_catalog_worker.bat
```

Manual one-cycle run:

```powershell
.\scripts\run_pokewallet_catalog_cycle.ps1
```

Manual one-cycle run for a single language:

```powershell
.\scripts\run_pokewallet_catalog_cycle.ps1 -Language zh -MaxRequests 80
```

Manual one-cycle run for all languages:

```powershell
.\scripts\run_pokewallet_catalog_cycle.ps1 -AllLanguages -MaxRequests 80
```

Optional scheduled task install:

```powershell
.\scripts\install_pokewallet_catalog_scheduled_task.ps1
```

Optional scheduled task removal:

```powershell
.\scripts\uninstall_pokewallet_catalog_scheduled_task.ps1
```

## Cycle Behavior

Each cycle runs one of these commands:

```powershell
python tools\build_pokewallet_catalog_foundation.py --full-catalogue --all-languages --max-requests 80 --resume
```

or, for targeted language cycles:

```powershell
python tools\build_pokewallet_catalog_foundation.py --full-catalogue --resume --language zh --max-requests 80
```

Then it validates:

```powershell
python tools\validate_cache.py
```

If validation passes and expected catalogue files changed, it stages only catalogue export outputs, commits with:

```text
Expand PokéWallet provider catalogue export
```

and pushes the commit when `pushAfterCycle` is enabled in `data/pokewallet_catalog_config.json`.

In until-complete mode, the worker prioritizes incomplete languages in this order by default:

```json
["zh", "jp", "en"]
```

This priority can be configured in `data/pokewallet_catalog_config.json` under `fullCatalogueWorker.languagePriority`.

Until-complete loop behavior:

- run one cycle
- validate
- commit/push if changed
- calculate the next safe delay from budget and pacing
- repeat until complete, rate-limited, or stopped

## Budget-Aware Rate Limiting

The worker tracks request usage and keeps throughput near 80-90 requests/hour by default while leaving safety headroom.

Budget defaults:

- provider plan baseline: `100/hour`, `1000/day`
- worker target budget: `90/hour`, `900/day`
- safety buffer: `10`

Supported environment variables:

- `POKEWALLET_MAX_REQUESTS_PER_HOUR=90`
- `POKEWALLET_MAX_REQUESTS_PER_DAY=900`
- `POKEWALLET_REQUEST_SAFETY_BUFFER=10`
- `POKEWALLET_WORKER_UNTIL_COMPLETE=true`

CARDSCANR aliases (preferred for app-wide consistency) are also supported:

- `CARDSCANR_MAX_REQUESTS_PER_HOUR=90`
- `CARDSCANR_MAX_REQUESTS_PER_DAY=950`
- `CARDSCANR_REQUEST_SAFETY_BUFFER=10`
- `CARDSCANR_WORKER_UNTIL_COMPLETE=true`

Optional live usage support:

- If a usage endpoint is configured and enabled, the worker attempts to read live usage before each cycle.
- If live usage is unavailable or parsing fails, it falls back to a local request ledger.

Ledger fallback:

- Local runtime ledger file: `data/pokewallet_catalog_worker_rate_ledger.json`.
- Stores per-cycle request counts and timestamps.
- Used to estimate hourly and daily usage windows.

Dynamic next-wait logic:

- if hourly budget remains: continue with paced delay
- if hourly budget is exhausted: wait until hourly reset window
- if daily budget is exhausted: stop until next daily reset

Logs include:

- requests used this cycle
- estimated hourly usage
- estimated daily usage
- remaining hourly budget
- remaining daily budget
- next wait reason

## Safety

- The worker resumes from `data/pokewallet_catalog_full_state.json`.
- It uses up to 80 requests per cycle by default.
- Trial-safe defaults are retained: approximately 100/hour and 1000/day with headroom (`hourlyReserveRequests`, `dailyReserveRequests`, `stopWhenDailyRemainingBelow`).
- The manual loop uses `.pokewallet_catalog_worker.lock` to prevent duplicate loops.
- Each cycle uses `.pokewallet_catalog_cycle.lock` so manual, one-cycle, and optional scheduled runs cannot overlap.
- Stale lock files are removed when their recorded process no longer exists.
- The cycle stops if unrelated git changes are present.
- The cycle validates before committing.
- Only catalogue/state/index output paths are staged.
- Image endpoints are kept as references only; no binary image files are written.
- Catalogue output is metadata + image references only (`imageStorageMode=provider_reference_only`, `binaryImagesStored=false`).
- Production price files are not built by this worker.
- The worker stops after a provider rate-limit status instead of continuing.

## Image Handling

- Current catalogue export stores provider metadata and image reference endpoints only.
- Binary images are not downloaded and not written by this worker.
- Large-scale binary image storage belongs to a future dedicated pipeline (for example Cloudflare R2, Supabase Storage, or Firebase), not this git repository.
- GitHub should not store thousands of provider image binaries.

## Progress and Stop

Check progress at any time:

```powershell
.\scripts\status_pokewallet_catalog_worker.ps1
```

Stop the worker loop cleanly:

```powershell
.\scripts\stop_pokewallet_catalog_worker.ps1
```

## Runtime Files

These files are local runtime state and are ignored by git:

- `data/pokewallet_catalog_worker_status.json`
- `data/pokewallet_catalog_worker_rate_ledger.json`
- `logs/pokewallet_catalog_worker.log`
- `.pokewallet_catalog_worker.lock`
- `.pokewallet_catalog_cycle.lock`

Use `.\scripts\status_pokewallet_catalog_worker.ps1` to inspect the manual loop, optional scheduled task, catalogue export state, and latest diagnostics.
