# Incident brief for Atlas: Cardmarket / reference pricing stall (22 Nov 2026)

This is a brief only. Nothing has been restarted, redeployed, or changed in production or on the live PC checkout. Please do not merge this as a product change.

Times below are Australia/Brisbane (AEST, UTC+10).

## 1. What the 8:11am health check showed (22 Nov 2026)

From the 22 Nov 08:11 AEST health check:

- The Cardmarket reference sync last succeeded at **21 Nov 20:09 AEST**, and nothing has succeeded since. It should run within 2 hours, so it is now well overdue (about 12 hours by 08:11).
- The refresh queue has **176 jobs waiting**.
- The last queue completion was **20 Nov 18:13 AEST**.
- All **11 owned cards** are priced older than 7 days.

## 2. Likely related cause: BulkReferencePricingSync left disabled after the popup fix

On 21 Nov, at about 20:10 AEST, the Windows scheduled task `CardScanR-BulkReferencePricingSync` was **DISABLED** during the scheduled-task popup fix (the 20:05 popup that appeared while Andy was in game). The hidden-launch fix for that worker lives in data PR #100, which was later merged to `origin/main` as `17fc04f8c01eae1470e90f03661797c1a99f8aa6` at about 20:52 AEST. **That merge has not been deployed into the live data checkout** `D:\\CardScanR_Data\\cardscanr-data` (the `D:\\cardscanr-data` junction points at this checkout). Until that deploy happens, the live scripts still resolve `python.exe`, so the task was left Disabled on purpose.

The live task action must be preserved. It is still the SecureCred wrapper:

`wscript.exe //B //Nologo C:\\Users\\andyg\\AppData\\Local\\CardScanR\\SecureCred\\run_hidden.vbs cardscanr-bulk-reference-sync`

That consumer already calls `ensure_live_bulk_reference_pricing_runtime.ps1 -Action run-once` with injected credentials. **Do not run `-Action start` (or `-Action stop`) on the live PC.** `-Action start` would overwrite the SecureCred-backed task action and drop credential injection.

### Popup-fix receipt (summary)

Source: `08_RELEASES/20261121_popup_fix/RECEIPT.md` (21 Nov, about 20:36-20:40 AEST).

- The 20:05 popup from this task came from two things: `SecureCred.psm1` was missing `CreateNoWindow` (patched locally under `%LOCALAPPDATA%\\CardScanR\\SecureCred`, outside the live data checkout), and the live data script still launched `python.exe`.
- Other CardScanR tasks (Workbench, Bridge Supervisor, Self-Heal, Recovery Watchdog, FB helpers, live eBay pricing runtime, and so on) were patched outside `D:\\CardScanR` / the data checkouts, re-enabled, and verified hidden with 0 new visible windows.
- `CardScanR-BulkReferencePricingSync` was the exception: the SecureCred wrapper was patched and left in place, the data checkout was not edited, and the task was left **Disabled** pending the cardscanr-data hidden-launch fix.

### PR #100 deploy path (summary, held)

Source: `05_QA/20261121_data100_qa/REPORT.md`. Independent QA was **PASS** on head `d05fc061`. GitHub merge is `17fc04f8`. QA explicitly said: do not deploy from that QA alone, and do not use the PR body's `-Action start` note on the live PC.

Approved deploy path, when Andy says yes:

1. Confirm the GitHub merge SHA on `origin/main` (`17fc04f8`).
2. Prepare a throwaway worktree under `D:\\CardScanR_worktrees\\` from a snapshot of the live branch `runtime/reconcile-pr40-20261115`. Live write path is `D:\\CardScanR_Data\\cardscanr-data`. Do not use the `D:\\cardscanr-data` junction as a write path.
3. Merge the exact approved SHA `17fc04f8` into that worktree (ort / merge commit). Do not blindly fast-forward to latest `origin/main` if main has moved past that SHA. Expect a real merge, because the runtime branch was ahead of main.
4. Run the focused tests in the worktree. Only then fast-forward or merge that result into the live checkout on `runtime/reconcile-pr40-20261115`. Leave untracked runtime reports and flags alone.
5. **Do not run** `ensure_live_bulk_reference_pricing_runtime.ps1 -Action start` or `-Action stop`. Leave the existing SecureCred task action in place.
6. Only after the live scripts contain `Resolve-CardScanRRuntimePythonPath` / `pythonw`, re-enable `CardScanR-BulkReferencePricingSync` and start it once. Confirm 0 visible windows and that the worker is `pythonw.exe`.
7. Remove the temporary worktree.

Until that deploy is approved, the task should stay disabled.

## 3. Possible separate cause: eBay HARD_STOP / queue (UNVERIFIED)

The refresh queue has had 176 jobs waiting since the last completion at 20 Nov 18:13 AEST, which is earlier than the 21 Nov 20:09 reference-sync success and earlier than the task being disabled. An eBay `HARD_STOP` or a stuck queue could be a separate or additional cause. That has **not** been verified in this brief. Atlas should treat it as UNVERIFIED and check it rather than assume the disabled Windows task is the only reason owned prices are stale.

## 4. What Atlas should check and decide

Check:

- Confirm the 8:11 health-check numbers if needed (last Cardmarket / reference success, queue depth, last completion, owned-price age).
- Confirm the live task is still Disabled, still uses the SecureCred wrapper, and that `17fc04f8` is still absent from `D:\\CardScanR_Data\\cardscanr-data`.
- Check whether the 176-job refresh queue and the 20 Nov 18:13 last completion are explained by the disabled task, or by eBay `HARD_STOP` / another queue blockage (UNVERIFIED).
- Check that a later deploy would still resolve `pythonw.exe`, keep `CreateNoWindow`, stay AU-only, and leave `owned_daily` off.

Decide (then pack any production or live-checkout step for Andy's yes):

- Whether to deploy `17fc04f8` into the live data checkout using the path above, then re-enable the existing SecureCred task (not `-Action start`).
- Whether the refresh queue / eBay path needs a separate fix before or after that deploy.
- Whether any temporary catch-up is needed for the 11 owned cards once the hourly sync is safely running again.

## 5. Andy's constraints

- No visible windows on the PC. Ever.
- eBay is AU only. `owned_daily` stays OFF.
- Do not run `-Action start` or `-Action stop` against the live SecureCred task.
- Do not write to `D:\\CardScanR` or the live data checkout unless Andy has given a specific yes for that deploy.
- Production / live-runtime / task-enable changes need Andy's specific yes. A QA PASS or this brief is not approval.
- GitHub Actions stay `workflow_dispatch` only. No admin merge, bypass, or force push.
