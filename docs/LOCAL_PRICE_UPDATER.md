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
