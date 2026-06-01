# Phase 7G Local Live eBay Worker Launcher

The Flutter app does not scrape eBay or start Chrome. Its `Refresh price` button calls the Supabase refresh-request RPC, which applies cooldown rules and queues an eligible job. The local worker runs separately and processes queued jobs conservatively.

## Start The Worker

From `D:\cardscanr-data`, run:

```powershell
.\scripts\start_live_ebay_worker.ps1
```

Defaults:

- Live provider: `ebay_browser`
- Installed browser: Google Chrome through Playwright channel `chrome`
- Dedicated profile: `D:\cardscanr-data\.browser_profiles\cardscanr`
- Browser mode: headless
- Poll interval: 5 seconds
- Maximum jobs per cycle: 1
- Worker concurrency: 1

The launcher sets the explicit live-worker gates only for its child process. It does not change the repository-wide default provider, start the scheduler, force refresh, or bypass Supabase cooldown rules.

Calling this dedicated launcher is the operator confirmation for local live processing: it sets `CONFIRM_LIVE_EBAY_WORKER=true` only for the current process. `-Headless` is accepted explicitly when desired, and `-ProfilePath` can override the dedicated profile location while retaining the personal-profile rejection guard.

## Useful Commands

Headed debugging, with Chrome visible:

```powershell
.\scripts\start_live_ebay_worker.ps1 -Headed
```

Process one queue-poll cycle and exit:

```powershell
.\scripts\start_live_ebay_worker.ps1 -Once
```

Set conservative queue-poll values explicitly:

```powershell
.\scripts\start_live_ebay_worker.ps1 -PollSeconds 5 -MaxJobs 1
```

Print the resolved configuration without starting the worker:

```powershell
.\scripts\start_live_ebay_worker.ps1 -DryRun
```

PowerShell `-WhatIf` is also supported as a no-start configuration preview.

Verify the local environment, Playwright import, and installed Chrome channel without visiting eBay or claiming jobs:

```powershell
.\scripts\check_live_ebay_worker_config.ps1
```

Stop a running worker with `Ctrl+C` in its PowerShell window.

## Terminal Logs

The launcher prints a sanitized summary before starting. It includes the provider, dedicated profile path, headed/headless mode, polling interval, max jobs, and concurrency. Supabase keys are loaded from `supabase_env.local.json` but never printed.

The worker prints one line per poll cycle. `jobCount=0` means no queued refresh was available. A positive count means the worker claimed and processed queued work. Chrome is normally invisible because headless mode is the default; use `-Headed` when browser visibility is useful for debugging.

## Profile Safety

Never point the launcher at a personal Chrome profile. Paths under `C:\Users\<user>\AppData\Local\Google\Chrome\User Data` are rejected. The dedicated CardScanR profile directory is created automatically when needed.

The worker must remain separate from Flutter. It uses local backend credentials to claim queued jobs, while the app uses the safe refresh-request RPC. Supabase continues to enforce refresh cooldown and queue gating.
