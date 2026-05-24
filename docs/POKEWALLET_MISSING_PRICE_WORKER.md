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

## Resume

Run the same command again. Missing-set mode will continue from remaining JP sets and skip existing JP price files.

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
