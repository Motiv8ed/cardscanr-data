# (b) Current production and rollback — t589u read-only follow-up

10 November2026, Australia/Brisbane (AEST). Identity check passed before deploy inspection. No task/process/config/safety state changed; no tests, worker execution, eBay contact or restricted transfer.

## VERIFIED identity and deployment metadata

Desktop **2b76a077-8723-4d8f-a45b-8293febe35e0**, label Motiv8ed, reports Win32NT. D:\CardScanR-worktrees exists; D:\cardscanr-data contains workers/market_price_worker.py; scheduled task CardScanR-LiveEbayPricingRuntime exists. This IS the Windows CardScanR worker PC. b3c382f1 is the Android phone; initial P0 mapping was wrong and is superseded. Original receipt retained as PREVIOUS_AUTHORIZED_MACHINE_STATUS.json.

- **Junction:** D:\cardscanr-data -> **D:\CardScanR_Data\cardscanr-data**. Both paths report the same Git identity.
- **HEAD:** `93eba7296bd41ca1a417e67c2c67d70f1e3f8d8a`; parent `44171e92a755587d02a1e59fab7ca553374f7eb0`. Commit18October2026 18:04:46 AEST, “fix(pricing): extend cache freshness when reference prices are unchanged”. This full HEAD marker is now verified, not merely historical short93eba729.
- **Dirty:**17 tracked modified files; exact paths in PC_DEPLOYMENT_AND_TASK.json. Includes worker/scheduler, provider, runner, navigation/GUI/lifecycle and launch/tool changes. Untracked files were not globally enumerated; no clean-deployment claim.
- **Key hashes:**23 present files and one missing canonical classifier module in PC_DEPLOYED_FILE_HASHES.json. Covers worker/scheduler, provider, old sold-health, capture/converter, navigation/safety/config, Windows launch wrappers and Windows-deployed copies of WSL .sh/search/Sold tools. No file payload transferred.
- marketplace_browser_page_state.py is **absent**; classifier remains embedded/legacy in deployed provider. This is not deployed PR31. Deployed sold_page_health.py, post_sold_capture.py, navigation_runtime_context.py and linux_x11_ebay_sold.py hash exactly to the inspected d1 baseline; several common modules match all four pins. Dirty worker/provider/launcher/tool hashes do not match the four stored box source pins. Exact comparisons and LF->CRLF handling in PC_TO_BOX_SOURCE_MATCHES.json.

## IMPORTANT: task and processes are present, not “everything stopped”

Task **Ready, enabled=true**, last run **10November16:15:51 AEST**, result0. It runs wscript.exe from the physical deployment root and the hidden VBS launcher. Task-definition SHA256 `0ec116f8bbcf2bb41880e24762b4149c83af55a9539033c74b050ce68cbc7a59`.

Existing worker **PID17600**, scheduler **PID31296**, both C:\Python314\python.exe, both created **6November10:09:05 AEST**. Worker arguments --max-jobs1 --poll-seconds5; scheduler --poll-seconds60. PID markers say already_running and checked **10November16:15:51/52 AEST**. These are read-only observations of pre-existing processes; nothing was started/restarted/stopped by this review. A running process does not establish active pricing/navigation, and no live activity was probed. Production remains NO-GO; do not equate that policy with an assertion that processes/task are disabled.

## VERIFIED persisted HARD_STOP / OFF state; live enforcement UNVERIFIED

reports/runtime/continuous_au_safety_ledger.json records lastHardStop **MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES**, consecutiveTransient3;8 submission records and4 transient records. Last updated **6November10:34:45.819699 AEST**.

continuous_au_worker_status.json records **HARD_STOP**, enabled=false, marketAU, consecutiveTransientFailures3; last updated **6November10:34:45.820185 AEST**. owned_daily_full_enable.flag is **false**, and mirrored runtime config says false/concurrency1. Exact file hashes and safe fields in PC_PERSISTED_RUNTIME_STATE.json and PC_CAPS_AND_OVERRIDE_EVIDENCE.json. Existing old stop-intent metadata is retained without mutation.

These are persisted records last updated6November, not proof every currently running process is enforcing them or absent. No process environment/memory/credentials read, no latch clear and no work execution performed. P0 cannot certify live safety enforcement solely from old status plus running PIDs.

## Backups and operational rollback — NONE VERIFIED

Observed within inspected worker locations:

- scripts/ensure_live_ebay_pricing_runtime.ps1.bak-r3-20261106,12530bytes, SHA256 `ab00f217ad94875dfc5b240b3ed65bd31f6d7c5e5db444c424e0e085e2532035`.
- reports/runtime/continuous_au_safety_ledger.json.bak-r3-20261106_095956,516bytes, SHA256 `295b494b3dccadae933d7de547df9d19768076c66af83ce6ab7ab364f982cece`.
- reports/runtime/continuous_au_worker_status.json.bak-r3-20261106_095956,670bytes, SHA256 `57308124afab3c15f36acbb180590d62ec1a9780be1843492646654b1241a376`.
- Root cardscanr_data_snapshot_20260617_184918.zip,738636bytes. Contents, secrets/privacy and package completeness NOT inspected; not accepted as rollback merely because named snapshot.
- Within the inspected D:\CardScanR_Data child-directory listing, only current cardscanr-data had the worker entrypoint. This is scoped evidence, not an exhaustive search of the computer.

**There is NO verified complete runnable operational rollback artifact.** A launcher backup and state files do not identify compatible source/dependencies/Windows+WSL tools/config/DB bundle. Current dirty fullHEAD is an identified working deployment, not a reproducible accepted artifact. No backup rehearsal/runnability test is allowed or claimed. Failed704/989 and unapproved d1 remain unsuitable as asserted rollback targets.

## Access limitation — STOPPED, no workaround

After five successful sanitized read-only Shell inspections, structured Read calls for actual deployed worker/ensure/run-wrapper source were refused: **“Path is outside the allowed local-exec root”** for D:\CardScanR_Data\cardscanr-data. Four requests were already submitted in parallel; no retry after the refusal. Further PC file inspection stopped. No Shell, junction, WSL, copy, alternate path or credentials were used to bypass it. Already obtained receipts were retained. Existing authorized BOX Git objects were compared to captured hashes; unmatched dirty content was not recovered.

Consequences: actual selected Linux-side executable/tool paths and hashes, additional external UIA helpers, complete backup validation, full dirty launcher/entrypoint call order and live-process effective env remain UNVERIFIED. Ubuntu was listed running, but no Linux shell/tool read followed the refusal. Windows-deployed .sh hashes do not prove which Linux-side copies are selected. Use sanctioned local-exec-root permission for the real directory to continue, not a symlink/copy workaround.

## Is deployed code a better development baseline than d1?

**No, not currently established.** It is a valuable operational reference, with fullHEAD and key hashes now known, but17 uncommitted tracked changes, unmatched key files, incomplete bundle provenance, no runnable rollback and no independent acceptance make it a worse reproducible branch origin than the clean inspected d1 candidate. Do not overwrite/discard dirty production files. Prefer owner-selected d1 for isolated offline A unless a separately authorized preservation/reconstruction effort produces a reviewed immutable production manifest/pin. Neither baseline is automatically approved; production ancestry to d1 remains unverified.
