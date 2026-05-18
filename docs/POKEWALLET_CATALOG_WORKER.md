# PokéWallet Catalogue Worker

This worker collects PokéWallet catalogue, card, and image-reference metadata. It only runs when you start it, and it repeats every 75 minutes while the worker window/process is left running.

The default mode is manual loop mode. It is not installed into Windows Task Scheduler by default. Scheduled task mode remains available only if you explicitly run `scripts\install_pokewallet_catalog_scheduled_task.ps1`.

## Commands

Start manual worker:

```powershell
.\scripts\start_pokewallet_catalog_worker.ps1
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

Optional scheduled task install:

```powershell
.\scripts\install_pokewallet_catalog_scheduled_task.ps1
```

Optional scheduled task removal:

```powershell
.\scripts\uninstall_pokewallet_catalog_scheduled_task.ps1
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
- It uses up to 80 requests per cycle by default.
- The manual loop uses `.pokewallet_catalog_worker.lock` to prevent duplicate loops.
- Each cycle uses `.pokewallet_catalog_cycle.lock` so manual, one-cycle, and optional scheduled runs cannot overlap.
- Stale lock files are removed when their recorded process no longer exists.
- The cycle stops if unrelated git changes are present.
- The cycle validates before committing.
- Only catalogue/state/index output paths are staged.
- Image endpoints are kept as references only; no binary image files are written.
- Production price files are not built by this worker.
- The worker stops after a provider rate-limit status instead of continuing.

## Runtime Files

These files are local runtime state and are ignored by git:

- `data/pokewallet_catalog_worker_status.json`
- `logs/pokewallet_catalog_worker.log`
- `.pokewallet_catalog_worker.lock`
- `.pokewallet_catalog_cycle.lock`

Use `.\scripts\status_pokewallet_catalog_worker.ps1` to inspect the manual loop, optional scheduled task, catalogue export state, and latest diagnostics.
