# (e) Effective caps and override chain — t589u read-only follow-up

## Summary: configured assignments VERIFIED; live/all-layer effectiveness UNVERIFIED

On confirmed Windows worker PC, current deployed ensure launcher explicitly assigns **MAX_LIVE_SUBMISSIONS_PER_DAY=60**, **MAX_LIVE_SUBMISSIONS_PER_HOUR=20**, worker/browser concurrency1 and worker/scheduler marketsAU. Persistent fullEnable flag and runtime mirror are **false**. These are actual source/config observations, not just repository defaults. However, existing worker/scheduler were created6November; their actual loaded environment/resolved objects were NOT read. Inspector Machine/User/Process cap queries were empty and are **NOT** the existing worker environment. Do not turn source intent into certified live enforcement.

No tests, imports, scripts, worker entrypoints, env loaders, task operations or live probes were executed. No secret values recorded. Structured Read was subsequently refused outside allowed local-exec root; further PC file inspection stopped rather than rerouted.

## Observed deployed assignments (physical root D:\CardScanR_Data\cardscanr-data)

| Setting | Where set / observed | Status |
|---|---|---|
| Global daily60 | scripts/ensure_live_ebay_pricing_runtime.ps1:127 |VERIFIED current launcher literal; existing worker resolved value UNVERIFIED|
| Global hourly20 | same:126 |VERIFIED literal; live value UNVERIFIED|
| Worker concurrency1 | same:114; common helper live_ebay_worker_config.ps1:45 |VERIFIED literal; one Windows worker process observed, universal cross-platform concurrency not proved|
| Browser concurrency1 | same:118; runtime-config EBAY_BROWSER_MAX_CONCURRENCY=1 |VERIFIED literal/mirror; no competing Linux process inventory|
| Worker AU-only | same:112 |VERIFIED literal; live resolved allowlist UNVERIFIED|
| Scheduler AU-only | same:193 |VERIFIED literal; existing scheduler env UNVERIFIED|
| owned_daily/fullEnable | flag=false, runtime JSON=false; launcher:134/204 mirrors $fullEnable |VERIFIED persisted OFF. Helper flag-precedence implementation matches stored source exactly. No live process env assertion|
| Per-browser hourly20/day100 | deployed provider:1594–1595 defaults |VERIFIED defaults from filtered source; no daily<=60 override found in inspected config/launcher assignments|
| Generic provider day200/minute2 | deployed config.py:212–213 |VERIFIED defaults; no daily<=60 override found in inspected config/launcher assignments|
| Global default200 | continuous_safety.py:42 |VERIFIED default, normally overwritten by launcher60 if that path actually supplies worker env|

No independent global-browser limit scalar was obtained from runtime. Actual eBay-browser concurrency default1 validates in provider:1608; shared locks/multiple runners remain separate proof. --max-jobs1 is not alone proof of global concurrency or total daily allowance.

## Override chain and its limits

1. **VERIFIED task->hidden wrapper:** CardScanR-LiveEbayPricingRuntime is enabled/Ready. Task runs wscript.exe with D:\CardScanR_Data\cardscanr-data\scripts\ensure_live_ebay_pricing_runtime_hidden.vbs. That file's deployed bytes exactly match stored source after LF->CRLF. It invokes powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File D:\cardscanr-data\scripts\ensure_live_ebay_pricing_runtime.ps1 **-Component both**. Junction resolves to physical root. Source read from pre-existing box object only where captured hash proves match.
2. **VERIFIED assignments, PARTIAL full order:** deployed ensure launcher is dirty/unmatched. Safe source extraction captured AU/concurrency/maxjobs20h60day and conditional flag assignments above. Shared live_ebay_worker_config.ps1 matches stored source after LF->CRLF; it assigns four-market AU,US,GB,CA default at49 and concurrency1 at45. Ensure launcher has AU assignment; full actual call order/conditional paths were not recovered after Read refusal, so do not assert that every launch route always ends AU-only or that every cap setter reaches existing workers.
3. **Other wrappers VERIFIED source identity, not active-path assumption:** start_live_ebay_worker.ps1 and run_market_price_worker.ps1 exactly match stored source. Start:20 loads helper,28–32 sources load_supabase_env,34 sets live environment,58 invokes run wrapper. Run:13–16 sources env loader again;61–68 forwards Python CLI. It can clear stop intent on exit; it was NOT executed. The scheduled ensure path may directly launch Python rather than this interactive wrapper; no conflation. Existing Windows worker has --max-jobs1 --poll-seconds5; scheduler --poll-seconds60.
4. **Env/config inventory:** root .env/.env.local absent. supabase_env.local.json has variable NAMES SUPABASE_URL and SUPABASE_SECRET_KEY only; no values returned, no cap overrides in its keys. Runtime JSON safely recorded EBAY_BROWSER_MAX_CONCURRENCY1/OWNED_DAILY_FULL_ENABLEfalse; other property names only, no profile value. Env-loader referenced $env names output was empty, but dynamic key loading is possible: this is not proof loader is inert. Loader contents/hash were not fully verified. No secret-file hash or process-memory extraction performed.
5. **Default resolution verified for matching modules:** continuous_safety/config/owned_daily_enablement hashes match existing box objects. They read process environment/defaults; persistent flag takes precedence over env for owned_daily. Provider defaults100/day and generic200/day remain potential effective layer values unless inherited runtime values differ. Current inspector Machine/User/Process allowlisted env was empty; old child environments can differ. No all-layer<=60 proof.
6. **Full dirty worker startup/order unresolved:** deployed worker is modified and hash does not match inspected candidate/head sources. Its exact budget-enforcement/continuous-mode guard and current resolved values could not be read after the local-exec-root refusal. Do not apply clean-base line numbers as if verified on dirty deployed worker.

## 200/day helper hazard — PRESENT; automatic reachability not fully verified

Deployed owned_daily_enablement.py hashes exactly to existing source. apply_continuous_au_env at48–84 unconditionally assigns fullEnable=true and **MAX_LIVE_SUBMISSIONS_PER_DAY=200 at69**, hour20 and concurrency1. It can overwrite a prior60 IF invoked. owned_daily_full_enable at40–45 reads the persistent flag first; that flag is currentlyfalse, so a correctly flag-guarded entrypoint would not invoke it now.

But actual deployed worker is dirty; structured Read of its main/guard was refused. **Do not certify hazard unreachable in deployed code or infer actual child environment from OFF flag alone.** Conclusion: latent callable hazard definitely present; usual flag-controlled activation appears off, actual deployed automatic call graph/live values UNVERIFIED. Changing enablement/config to test it is prohibited. No live overwrite/bypass was reproduced.

## Additional accounting/enforcement blocker

Earlier source inspection found global budget recording in a healthy-result branch and continuous-mode gating in clean candidate worker. Actual deployed R0 worker differs and launcher references MARKET_WORKER_ENFORCE_SAFETY_BUDGET (NAME observed), but its literal value/full enforcement path was not captured before refusal. Therefore neither claim “off means budget disabled” nor “60 definitely counts every failure” is justified on this deployment. Need full sanctioned source/counter audit later; keep every actual failed/challenge/recovery submission accounted. Current ledger has8 old submission records, not a fresh live run-count guarantee.

## What remains to complete effective proof

Obtain sanctioned allowed-root access to actual deploy directory; read only safe dirty launcher/entrypoint/control flow and allowlisted redacted current startup/resolution receipts. No process memory/credential access, no alternate-path workaround, no test/start/refresh. Verify override order for both ensure and interactive routes, fullEnable OFF, force-budget enforcement when OFF, actual per-layer cap values<=60/<=20, shared concurrency1 and AU-only, preserved rolling usage and state. If live effective values cannot be read without execution, mark UNVERIFIED until separately authorized side-effect-free offline harness/preflight exists. No configuration changes are authorized by P0.

## Future validation proposal — NOT executed

After separately authorized B/safety scope: fake-clock60th/61st daily,20th/21st hourly, rolling24h expiry, restart persistence, actual failures/security/recoveries counted, malformed/missing caps fail closed, no helper60->200 reset and no multiple-runner allowance multiplication. Keep tighter per-minute/pacing controls, no ledger reset/latch clear. Fixes/production cap changes need explicit later approval. Current result is **configured launcher60/20/1/AU verified; current live/all-layer enforcement UNVERIFIED**.
