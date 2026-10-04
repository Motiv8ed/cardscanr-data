# US_GB_CA_SERIAL_CANARY_REPORT

Task: `CARDSCANR-US-GB-CA-SERIAL-CANARY-AND-CONTINUOUS-ENABLEMENT`  
Result: **PARTIAL**  
Freeze HEAD: `cb49bcd0` (`fix(pricing): restore owned-daily enablement import for serialized canaries`)  
Prior offline commit: `31469197` (`fix(pricing): prepare supported markets for serialized canaries`)

No US/GB/CA `SEARCH_SUBMISSION_STARTED` events were written.

## Current AU state (re-read live, not the old snapshot)

- owned_daily flag: **false** (already stopped from prior consecutive-transient hard stop; not cleared)
- availability: **PROBE_REQUIRED** (v1 AU slot, revision 38)
- nextProbeAt: `2026-10-04T07:25:00.448963Z` (elapsed; still PROBE_REQUIRED, probe not in flight)
- last success: `2026-10-04T06:05:26.270188Z`
- last failure: `2026-10-04T06:09:22.739695Z`
- ops AU cooldown until `2026-10-04T06:24:22Z` (expired)
- active challenges: 0
- worker status file: HARD_STOP `MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES` (ledger consecutiveTransient=3)
- competing pricing workers: none
- global dispatcher: idle, activeBrowserJobs=0
- AU freshness/history: not reset

AU remains logically the production AU market. Dispatch is already paused (flag false). This task did not disable AU permanently and did not clear AU availability.

## Blocked-region status fix

JP/EU no longer call the eBay work gate as if they were runnable.

- `classify_continuous_gate(JP/EU)` → `workerState=BLOCKED`, `provider=NONE`
- `region_status_row` does not show SELECTING
- `pick_fair_market` ignores JP/EU
- owned-daily scheduler skips `BLOCKED_NEEDS_PROVIDER`
- job_runner refuses JP/EU execution
- Tests: `tests/test_us_gb_ca_serial_canary_offline.py`

## Other offline fixes required before live

- Market switch AU→US / US→GB / GB→CA forces **COLD_START** and clears prior tab context
- Same-market INTER_CARD remains INTER_CARD
- `linux_x11_ebay_search` homepage is market-specific (`CARDSCANR_MARKETPLACE_HOME`), not hardcoded `ebay.com.au`
- `is_ebay_marketplace_host` recognizes ebay.com / .com.au / .co.uk / .ca
- Restored `owned_daily_full_enable` import (NameError would have blocked scheduler)

## Global dispatcher

- concurrency 1
- JP/EU never selected
- idle at canary time

## Tests

- `tests.test_us_gb_ca_serial_canary_offline` PASS
- `tests.test_multi_region_continuous_pricing` PASS
- related demand/availability/continuous/UI-search PASS (86 tests in the combined focused run)

## Why live US/GB/CA canaries did not start

US precheck against the live control plane: **ok** (HEALTHY, 0 challenges, correct host/currency/homepage, JP/EU blocked).

US owned-daily **dry-run** (no live navigation):

- `OWNED_DAILY_ALLOWED_MARKETS=US`
- targets scanned: 231 owned printings
- `jobsEnqueued=0`
- top skip reason: **market_not_allowed** for all 231
- health.byMarket only lists **AU/AUD** due keys (204 due AU)

The production owned-daily target list is **home-market AU keys only**. There is no due US/USD (or GB/GBP, CA/CAD) owned key for the scheduler to pick. Creating US/GB/CA keys by cloning AU printings would be a forced/hand-picked path, not the real demand-aware scheduler for that market.

Therefore the first live submission was **not** started. Code freeze after first event was not reached.

## US / GB / CA canaries

All: submissions=0, healthy=0, not started.

## Continuous enablement

- AU: flag remains false (prior hard stop); market still browser-capable
- US/GB/CA: not enabled
- JP/EU: BLOCKED_NEEDS_PROVIDER

Do not multiply 20/200 budgets. Fairness not observed live (no multi-market enablement).

## Next engineering

1. Product/RPC: `list_owned_market_pricing_targets` (or equivalent) must emit independent US/GB/CA market-price keys for owned printings when those markets are enabled — without FX substitutes.
2. Then run US 5/5 canary through the existing single browser, then GB, then CA.
3. Isolate consecutive-transient hard-stop per market before flipping `owned_daily_full_enable` true again (AU ledger still has consecutiveTransient=3).
