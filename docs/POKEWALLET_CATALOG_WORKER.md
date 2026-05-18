# Pokewallet Catalogue Worker

This worker repeats the Pokewallet full provider catalogue export at a safe pace for the current trial limit of 100 requests per hour and 1000 requests per day.

It is for catalogue, card, and image-reference metadata only. It does not store binary image files, does not create production price files, and does not run provider calls from the app.

## What It Runs

Each cycle runs:

```powershell
python tools\build_pokewallet_catalog_foundation.py --full-catalogue --all-languages --max-requests 80 --resume
```

Then it validates:

```powershell
python tools\validate_cache.py
```

If validation passes and expected catalogue files changed, it stages only the catalogue export outputs, commits with:

```text
Expand PokéWallet provider catalogue export
```

and pushes the commit when `pushAfterCycle` is enabled in `data/pokewallet_catalog_config.json`.

## Safety Rules

- The worker resumes from `data/pokewallet_catalog_full_state.json`.
- It waits 75 minutes between cycles by default.
- It uses 80 requests per cycle by default, leaving a buffer under the 100 requests/hour trial limit.
- It stops cleanly if the provider returns rate limit status.
- It checks the git worktree before each cycle and stops if unrelated files are dirty.
- It uses `POKEWALLET_API_KEY` from the environment through the existing exporter.
- It stores image endpoint references only; binary images stay out of the repository.

Runtime files:

- Status: `data/pokewallet_catalog_worker_status.json`
- Log: `logs/pokewallet_catalog_worker.log`
- Lock: `.pokewallet_catalog_worker.lock`

## Commands

Start:

```powershell
.\scripts\start_pokewallet_catalog_worker.ps1
```

Status:

```powershell
.\scripts\status_pokewallet_catalog_worker.ps1
```

Stop:

```powershell
.\scripts\stop_pokewallet_catalog_worker.ps1
```

Manual one-cycle run:

```powershell
.\scripts\run_pokewallet_catalog_cycle.ps1
```

## Configuration

Worker settings live in `data/pokewallet_catalog_config.json` under `fullCatalogueWorker`:

- `intervalMinutes`: minutes between cycles, default `75`
- `maxRequestsPerCycle`: request budget per cycle, default `80`
- `validateAfterCycle`: run cache validation before commit
- `commitAfterCycle`: commit successful catalogue batches
- `pushAfterCycle`: push successful catalogue commits
- `lockPath`: prevents duplicate background loops

The worker is disabled by default in config. Running the start script is an explicit local operation.
