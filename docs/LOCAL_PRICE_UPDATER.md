# Local Price Updater

This project now supports a local-first EN current-price refresh flow.

## What It Does

- Refreshes a small rotating batch of EN sets per run.
- Validates artifacts after each run.
- Optionally commits and pushes if changes exist.
- Persists a cursor in `data/scheduled_price_refresh_state.json` so each run advances to the next batch.

## Run Manually

From repository root:

```powershell
.\.venv\Scripts\python.exe tools\run_local_price_update.py --batch-size 10
```

Dry run (no writes):

```powershell
.\.venv\Scripts\python.exe tools\run_local_price_update.py --batch-size 10 --dry-run
```

Commit and push (if changed):

```powershell
.\.venv\Scripts\python.exe tools\run_local_price_update.py --batch-size 10 --commit --push
```

PowerShell helper:

```powershell
.\scripts\run_local_price_update.ps1 -BatchSize 10 -Commit -Push
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

## One-click background updater

Use the background loop when you want a low-maintenance updater that sleeps between runs.

Start (PowerShell):

```powershell
.\scripts\start_local_price_updater.ps1 -BatchSize 10 -IntervalMinutes 120
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

Logs:

```text
logs\local_price_updater.log
```

Recommended interval:

- Start with 120 minutes.
- Move to 60 minutes later if stable in your environment.

Notes:

- The updater runs `git pull --ff-only` before each cycle and skips the cycle on pull failure.
- The updater skips the cycle if unrelated uncommitted changes already exist.
- API keys must come from environment variables; never hardcode them.
- Cloudflare Pages deploys automatically after each successful GitHub push.
- The loop sleeps most of the time, so CPU and memory usage should stay minimal.
