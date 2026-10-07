# Owned-daily control-plane audit (2026-09-27 resume)

## Authoritative persistent configuration

| Setting | Authority |
|---|---|
| `OWNED_DAILY_FULL_ENABLE` | **Only** `reports/runtime/owned_daily_full_enable.flag` |
| Writer | `Set-OwnedDailyFullEnableFlag` in `scripts/live_ebay_worker_config.ps1` |
| Readers | `ensure_live_ebay_pricing_runtime.ps1` (worker + scheduler), process env |
| Mirror (non-authoritative) | `reports/runtime/live_ebay_runtime_config.json` |

Audit trail for flag mutations: `reports/runtime/owned_daily_full_enable_audit.jsonl`.

## Root cause of unexpected full-enable re-arm

**Task:** `CardScanR-LiveEbayPricingRuntime`  
**Chain:** Task Scheduler (logon + every 15 min) → `ensure_live_ebay_pricing_runtime_hidden.vbs` → `ensure_live_ebay_pricing_runtime.ps1 -Component both`  

**Previous defect:** ensure hard-coded `$env:OWNED_DAILY_FULL_ENABLE = "true"` and `Write-LiveEbayRuntimeConfig` **rewrote** `owned_daily_full_enable.flag` to `true` on every ensure. After an operator set the flag false for the precedence incident, the next ensure run silently flipped it back to true.

## Fixes applied this resume

1. Ensure **reads** the flag only (never invents true).
2. `Write-LiveEbayRuntimeConfig` **no longer writes** the flag (mirror JSON only).
3. Explicit `Set-OwnedDailyFullEnableFlag` is the sole persistent writer.
4. Pricing tasks remain absent until deliberately re-registered after gates pass.

## Mechanisms capable of launching pricing (inventory)

| Mechanism | Current state | Can flip FULL_ENABLE? |
|---|---|---|
| `CardScanR-LiveEbayPricingRuntime` | **ABSENT** | Would only read flag (after fix) |
| `CardScanR-BulkReferencePricingSync` | **ABSENT** (restore Phase B) | No — reference sync does not touch flag |
| `CardScanR-OwnedDailyPriceScheduler` | **ABSENT** (never a separate task; owned pass lives in market scheduler) | N/A |
| `CardScanR-EcbFxRefresh` | Ready (daily FX) | No |
| HKCU Run / Startup | No CardScanR pricing entries | No |
| `run_owned_daily_price_scheduler.ps1 -FullEnable` | Manual / pilot only (process env) | Process-scoped only; does not write flag |
| Active worker/scheduler processes | Stopped at audit | N/A |

## Required invariant

No background task may silently change `owned_daily_full_enable.flag`.  
Full enable is set only by an explicit control-plane write after Phase I gates pass.
