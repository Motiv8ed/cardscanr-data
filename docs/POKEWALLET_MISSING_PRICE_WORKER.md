# PokeWallet Missing JP Price Worker

This worker automates the proven JP missing-set PokeWallet import process so you do not have to manually run dry-run/write/release loops.

## Scope

- Targets JP only.
- Uses missing-set mode only (`--only-missing-set-prices`).
- Skips already-priced JP set files.
- Does not collect provider catalog data.
- Does not refresh EN prices.

## Why It Writes Directly

This workflow has already been validated through repeated bounded imports, validation checks, and release cycles. The worker therefore defaults to write mode instead of dry-run mode.

Dry-runs still call the API and consume quota. Use dry-run only when you explicitly want diagnostics.

## Safe Budget Defaults

The importer enforces safe request limits using a buffer.

- Safe hourly budget: 90 requests/hour
- Safe daily budget: 900 requests/day
- Plan limits remain 100/hour and 1000/day

If budget is blocked, the worker can sleep and retry or exit clearly.

## Run Until Complete

PowerShell wrapper (recommended):

```powershell
.\scripts\run_pokewallet_missing_price_worker.ps1 -Language jp -MaxNewSetsPerCycle 20 -UntilComplete -Commit -Push -Validate -SleepWhenBudgetBlocked -PollSeconds 300 -ExportChatGPTReport
```

Python entrypoint:

```powershell
python tools/run_pokewallet_missing_price_worker.py --language jp --max-new-sets-per-cycle 20 --until-complete --commit --push --validate --sleep-when-budget-blocked --poll-seconds 300 --export-chatgpt-report
```

## Dry-Run Mode (Optional)

```powershell
.\scripts\run_pokewallet_missing_price_worker.ps1 -Language jp -MaxNewSetsPerCycle 20 -UntilComplete -DryRunOnly -SleepWhenBudgetBlocked -PollSeconds 300
```

## Stop Safely

Press Ctrl+C in the terminal.

The worker writes a latest summary report each run, so restarting is safe and resumable.

The wrapper and worker now emit live timestamped stage logs so you can see progress while the run is active.

Examples:

- `[2026-05-25T09:00:00Z] worker started`
- `[2026-05-25T09:00:01Z] cycle 1 starting`
- `[2026-05-25T09:00:03Z] running importer: maxNewSets=40`
- `[2026-05-25T09:01:05Z] importer finished: status=ok apiRequests=40 importedRecords=5487 endpointFailures=0`
- `[2026-05-25T09:01:06Z] validation starting`
- `[2026-05-25T09:01:30Z] validation passed`
- `[2026-05-25T09:02:11Z] exporting ChatGPT report`

Child process output is streamed live and prefixed by stage, for example:

- `[importer] ...`
- `[validate_cache] ...`
- `[report_dataset_coverage] ...`
- `[report_data_health] ...`
- `[release] ...`
- `[export] ...`

During long-running stages, heartbeat lines are printed periodically:

- `[2026-05-25T09:05:00Z] heartbeat stage=importer elapsed=180s latestImported=3200 hourlyUsed=41 hourlyRemaining=49`

Secrets are redacted in log output. Full API keys are never printed.

## Resume

Run the same command again. Missing-set mode will continue from remaining JP sets and skip existing JP price files.

## Monitor In A Second Terminal

Use the watch helper to keep a live dashboard while the worker runs in another terminal:

```powershell
.\scripts\watch_pokewallet_missing_price_worker.ps1
```

The watch output shows:

- latest worker report summary
- latest importer report summary
- `git status --short`
- last 5 `pokewallet_missing_price_worker_runs.jsonl` entries
- active matching worker/importer Python processes

## Outputs and Monitoring

Worker outputs:

- `reports/pokewallet_missing_price_worker_latest.json`
- `reports/pokewallet_missing_price_worker_latest.md`
- `reports/pokewallet_missing_price_worker_runs.jsonl`

Importer output used by the worker:

- `reports/pokewallet_price_import_latest.json`
- `reports/pokewallet_price_import_latest.md`

Check:

- `status`, `stopReason`, `cyclesBlockedByBudget`
- `totalApiRequests`, `totalImportedRecords`
- JP before/after counts and file counts
- last selected set IDs and importer status
- validation and pushed commit hashes
- `nextRecommendedCommand`

## Budget Block Behavior

When budget blocks a cycle with zero API calls, the worker does not treat it as a hard failure.

- With sleep mode: waits for next safe window and retries.
- Without sleep mode: exits with a clear budget-blocked summary.
- With `-StopAfterDailyBudget`: exits once daily safe budget is exhausted.

Budget-block logs now include hourly/daily usage and remaining budget, wait estimate, and poll seconds. While sleeping, the worker prints a repeat sleep message each poll interval so you can see it is active.

## If It Appears Stuck

1. Check for heartbeat lines in the worker terminal.
2. Check the latest importer and worker reports in `reports/`.
3. Run the watch script in a second terminal and confirm process activity.
4. If no new output and no heartbeats appear for several minutes, stop with Ctrl+C and resume with the same command.

## Safe Stop And Resume

Stopping with Ctrl+C is safe. The worker writes latest reports each run and missing-set mode is resumable. Restart with the same flags to continue.
