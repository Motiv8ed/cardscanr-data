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

Raw log tail:

```powershell
Get-Content .\logs\local_price_updater.log -Tail 80 -Wait
```

Status fields:

- `Running` tells you whether the updater PID is still alive.
- `PID` is the current background process ID.
- `Phase` shows the current loop stage: starting, pulling, updating, validating, committing, pushing, sleeping, error, or stopped.
- `State` summarizes whether the updater is actively working or sleeping.
- `Batch size` and `Interval` show the active loop settings.
- `Update start`, `Update elapsed`, and `Est. finish` describe the current cycle. The finish time is only an estimate based on the previous completed cycle duration.
- `Last update`, `Last push`, `Last commit`, and `Last duration` summarize the most recent successful cycle.
- `Next update` and `Time remaining` show when the next sleep window ends.
- `Last sets` lists the most recent planned or updated set IDs captured by the updater.
- `Last error` shows the most recent failure or skipped-cycle reason, if any.
- `Recent logs` shows the last 20 lines from the local updater log.

Local runtime files:

- `logs\local_price_updater.log`
- `logs\local_price_updater_status.json`
- `logs\local_price_update_last_result.json`

These files are local only and are ignored by git.
