# Native Windows AU pricing deployment and rollback

This procedure is for a separately authorized cutover. Leave the existing marketplace hold and the disabled production scheduler in place during isolated staging. Use the Windows user's active console session. Chrome opens its window in the background with the dedicated profile at `%LOCALAPPDATA%\CardScanR\NativePricingChrome`. The owner must authorize marketplace work through the existing controls before the production smoke run.

## Verify the release offline

In an isolated Windows checkout of the reviewed commit, with Chrome installed and 9232 free, run this command from the checkout root. The test uses local HTML and a fake queue and cache service. It starts and waits for real Windows worker processes and closes the fixture browser.

```powershell
cmd /d /c 'set CARDSCANR_RUN_WINDOWS_FIXTURE=1&& python -m pytest -q tests/test_windows_pricing_worker_fixture.py'
```

Record the exact commit and test output. The Windows rollout helper runs one poll of the normal AU worker. The Linux rollout remains available in Linux mode.

## Deploy after approval

1. Record the currently deployed Git SHA, the worker launch command, and its process-scoped configuration. Verify the configured production checkout is clean and that its parent and reports directories are the expected ones. Keep the same checkout path so the existing attempt records, budget ledger and incident state remain authoritative. Keep the scheduled runtime disabled while doing the smoke run.
2. Let any active job finish, then confirm no other pricing worker is running. Fetch canonical `main` and verify the approved release SHA is present and an ancestor of that branch. Switch the clean production checkout to the approved SHA in place with `git switch --detach $ReleaseSha`. Verify `git rev-parse HEAD` matches it. Leave the current profile, availability, cooldown, incident and attempt files in place.
3. From the repository root, run the following in a fresh PowerShell process under the active Windows user. Keep the existing credential loader, safety budget and marketplace controls. The command starts only the worker, with one queue poll and at most one claimed AU job.

   ```powershell
   $env:EBAY_BROWSER_NAV_MODE = 'desktop_win32'
   $env:MARKET_LOOKUP_PROVIDER = 'ebay_browser'
   $env:EBAY_BROWSER_ENABLED = 'true'
   $env:CONFIRM_LIVE_EBAY_WORKER = 'true'
   $env:MARKET_WORKER_ALLOWED_MARKETS = 'AU'
   & .\scripts\run_market_price_worker.ps1 -Once -MaxJobs 1
   ```

4. Inspect `reports\market_price_worker_latest.json`, the job's capture receipt, and the matching event in `reports\runtime\live_nav_attempts`. Verify the worker reports `desktop_win32`, the result is from AU/AUD, and the attempt ID agrees across records. Verify the Chrome listener is loopback port 9232 and owned by the dedicated browser in the active session. Stop the rollout on a gate refusal, a wrong owner, a failed capture, an unaccounted submission, or a pricing mismatch. An empty queue proves startup only. Keep scheduled operation disabled pending separate acceptance of the one-job result and the scheduler configuration.

A consumed native queue job ID retains its attempt record across restarts. Review its capture and queue state before creating any replacement job through the authorized workflow.

## Roll back

1. Stop launching new worker polls. Let any active job reach its boundary and exit. Keep the scheduled runtime disabled. Close the verified native browser with the following command, run from the candidate checkout. It checks ownership before asking Chrome to exit and verifies that 9232 is free.

   ```powershell
   python -c "from cardscanr_market_engine.native_windows_pricing_browser import close_browser; close_browser()"
   ```
2. In the same clean production checkout, run `git switch --detach $PreviousSha` using the recorded pre-deployment SHA. Restore the prior worker launch configuration, including the prior navigation mode, before any separately approved restart.
3. Preserve all current runtime state, attempts, pricing data, incidents and cooldowns. Verify `git rev-parse HEAD` reports the previous SHA, and confirm the worker and scheduler remain stopped. Reconcile any completed job or recorded submission before authorizing further work.

## Owner-authorized native recovery

The historical owner-review script pins an earlier incident and Linux runtime. Its mutating and resume modes are limited to that original operation. For the native candidate, leave the production task and owned-daily flag disabled. The normal worker will wait in `PROBE_REQUIRED`.

1. Have the account owner resolve the marketplace issue through the official channel and obtain express permission for the intended automated access. Have Atlas separately approve the incident reconciliation with current evidence. The native probe requires an already reconciled AU challenge, the authoritative gate in `PROBE_REQUIRED`, no active challenge or authentication incident, no active cooldown, and no probe already in flight. The probe does not reconcile an active incident.
2. Under the active Windows user, verify the dedicated native Chrome owner on port 9232. The owner reviews the normal AU sold or item page in that exact browser. Collect the exact browser binding with `python -c "import json; from cardscanr_market_engine.native_au_pricing_recovery import reviewed_browser_identity; print(json.dumps(reviewed_browser_identity()))"` and retain it as `browserIdentity` in the same receipt. It contains the PID, Windows session ID, Chrome browser ID, and page target ID. Record the approval reference, exact release SHA, reconciled incident ID, and one reviewed AU/AUD price key. Record SHA-256 for the authoritative availability, marketplace operations and incident ledger files after reconciliation.
3. Supply a local JSON receipt with schema `cardscanr-owner-native-au-probe-v1` and fields `release`, `browserIdentity`, `incidentId`, `priceKeyId`, `marketplaceAuthorizationReference`, `expiresAtUtc` (no more than 30 minutes in the future), and `stateSha256` (keys `availability`, `operations`, and `incidents`, lowercase hashes). Keep production scheduling disabled. From the deployed checkout, run the following only after the preceding approval and verification steps.

   ```powershell
   & .\.venv\Scripts\python.exe tools/owner_reviewed_au_pricing_recovery.py --mode native-probe --approval OWNER_REVIEWED_NORMAL_EBAY_PAGE_SINGLE_PROBE --review-receipt $OwnerReviewedReceipt
   ```

4. Review the result and the durable receipt under `reports/runtime/native_recovery_receipts`. A successful capture returns `PROBE_HEALTHY_STREAK_PENDING` and preserves the normal health streak requirement. The default recovery streak is three healthy checks total, including the initial probe. Two further healthy checks are therefore required at the default setting. Honor the configured `EBAY_AVAILABILITY_RECOVERY_STREAK` and verify `confirmedHealthy` before resuming scheduling. Obtain separate authorization for follow-up checks. The probe performs one budgeted search and makes no queue or pricing database writes. It will not enable the scheduler or issue follow-up searches. A challenge, cooldown, local failure, abandoned claim, or newer state change requires owner review before any further action. Keep the receipt, attempt event and all incident evidence during recovery and rollback.
