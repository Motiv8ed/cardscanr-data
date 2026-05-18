# PokéWallet Catalogue Worker

This worker collects PokéWallet catalogue, card, and image-reference metadata. It does not store binary image files, does not create production price files, and does not run provider calls from the app.

The worker is scheduled through Windows Task Scheduler. The scheduled task runs one safe export cycle every 75 minutes, which leaves room under the current 100 requests/hour trial limit while using up to 80 requests per cycle.

## Commands

Install/start scheduled task:

```powershell
.\scripts\start_pokewallet_catalog_worker.ps1
```

Check status:

```powershell
.\scripts\status_pokewallet_catalog_worker.ps1
```

Run one manual cycle:

```powershell
.\scripts\run_pokewallet_catalog_cycle.ps1
```

Stop/remove scheduled task:

```powershell
.\scripts\stop_pokewallet_catalog_worker.ps1
```

## Cycle Behavior

Each cycle runs:

```powershell
python tools\build_pokewallet_catalog_foundation.py --full-catalogue --all-languages --max-requests 80 --resume
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

## Safety

- The worker resumes from `data/pokewallet_catalog_full_state.json`.
- The scheduled task is configured to ignore overlapping starts.
- `scripts\run_pokewallet_catalog_cycle.ps1` also uses `.pokewallet_catalog_cycle.lock` so manual and scheduled cycles cannot overlap.
- Stale lock files are removed when their recorded process no longer exists.
- The cycle stops if unrelated git changes are present.
- The cycle validates before committing.
- Only catalogue/state/index output paths are staged.
- Image endpoints are kept as references only; no binary image files are written.
- Production price files are not built by this worker.

## Runtime Files

These files are local runtime state and are ignored by git:

- `data/pokewallet_catalog_worker_status.json`
- `logs/pokewallet_catalog_worker.log`
- `.pokewallet_catalog_worker.lock`
- `.pokewallet_catalog_cycle.lock`

Use `.\scripts\status_pokewallet_catalog_worker.ps1` to inspect the scheduled task, latest status JSON, catalogue export state, and latest diagnostics.
