# SOLD NAV + CONTROL-PLANE PERSISTENCE CLOSURE

**Task:** CARDSCANR-SOLD-NAVIGATION-TIMEOUT-AND-CONTROL-PLANE-PERSISTENCE-CLOSURE  
**Verdict:** PASS  
**Baseline commit (prior):** 86973b7cd6dfddc0643492aac98b315be54c0ea8  
**Mode:** OFFLINE ONLY — eBay navigations=0, new SEARCH_SUBMISSION_STARTED=0

## Meowth historical evidence

| Field | Value |
|-------|-------|
| search consumed | TRUE (757c9e95-be72-49b2-bbe8-eba9c4b19acc) |
| runtimeMode | COLD_START |
| terminal | SOLD_NAVIGATION_TIMEOUT / FAIL_NAVIGATION |
| ordinaryResultsConfirmed | YES |
| Sold control found | YES (hits=5) |
| Sold click | YES (soldClickSuccess=true) |
| URL before | ordinary Meowth search (no LH_Sold) |
| URL after | LH_PrefLoc=2 **without** LH_Sold=1 |
| pending elapsed | ~27.3s |
| historical phase known | YES — post-click verification / unexpected filter |
| exact root cause proven | ROOT_CAUSE_REPRODUCED (offline replay → SOLD_UNEXPECTED_FILTER_TRANSITION) |
| historical verdict | UNCHANGED (consumed=true) |

Artifact: 
eports/artifacts/linux_sold_nav_1791064288_state.json

## Sold timeout path

{
  "callChain": [
    "job_runner -> ebay_browser_provider -> linux_x11_ebay_nav -> WSL linux_x11_ebay_sold.gui_sold -> wait_sold_pending"
  ],
  "timeoutFile": "tools/linux_x11_ebay_sold.py",
  "timeoutFunction": "wait_sold_pending",
  "legacyTimeoutSeconds": 35,
  "legacyExtensionPolls": "12 x 1.0s",
  "newVerificationBudgetSeconds": 10,
  "successPredicate": "lh_sold=1 in omnibox URL -> x11SoldStateVerified / SOLD_STATE_VERIFIED",
  "meowthStage": "SOLD_NAVIGATION_PENDING after click; URL became LH_PrefLoc=2 without LH_Sold",
  "wslSubprocessTimeoutSeconds": 120
}

## Successful timing comparison

{
  "searchToOrdinaryMs": {
    "n": 28,
    "min": 489,
    "median": 879,
    "p90": 1322,
    "max": 3426
  },
  "ordinaryToSoldControlMs": {
    "n": 16,
    "min": 3527,
    "median": 3725,
    "p90": 4988,
    "max": 6049
  },
  "clickToVerifiedMs": {
    "n": 47,
    "min": 3174,
    "median": 3346,
    "p90": 3358,
    "max": 5371
  },
  "totalT5toT9Ms": {
    "n": 16,
    "min": 7700,
    "median": 8727,
    "p90": 11722,
    "max": 13546
  },
  "meowthPendingMs": 27300,
  "legacyPendingBudgetSeconds": 35
}

Healthy click→verified cluster ~3.2–3.4s; Meowth burned ~27s on wrong filter. Legacy 35s opaque wait was not the primary bug — wrong post-click filter was — but opaque timeout hid the phase.

## Stage-specific timeout policy

{
  "controlDiscoverySeconds": 12.0,
  "controlClickSeconds": 5.0,
  "stateTransitionSeconds": 8.0,
  "stateVerificationSeconds": 10.0,
  "legacyOpaquePendingSeconds": 35.0,
  "evidenceBasis": {
    "clickToVerifiedMedianMs": 3346,
    "clickToVerifiedP90Ms": 3358,
    "clickToVerifiedMaxMs": 5371,
    "ordinaryToControlP90Ms": 4988,
    "headroomPolicy": "finite_stage_budgets_above_p90"
  }
}

- retries: 0
- timeout remains FAIL_NAVIGATION class
- one consumed search remains one consumed search

## Local delayed Sold proofs

Offline 
un_sold_fixture covers immediate / delayed control / delayed verification / near-timeout / discovery timeout / verification timeout / click failure / challenge / passive recaptcha / unexpected filter / page hijack.

## Future diagnostic contract

uild_sold_failure_evidence + sold state ailureEvidence fields: runtimeMode, attemptId, jobId, priceKeyId, query, searchSubmitted, ordinaryResultsConfirmed, URL/title/readyState, Sold control/click, LH_Sold before/after, x11SoldStateVerified, failureStage/Class, elapsed per stage, child rc, stderr summary. Optional DIAGNOSTIC_ONLY screenshot (non-fatal).

## Control-plane WinError root cause

{
  "file": "reports/runtime/ebay_availability_state.json",
  "writerOnGateEval": "get_availability via locked_json_state always wrote on exit (even HEALTHY unchanged)",
  "exactFailure": "atomic_json_state.atomic_write_json -> os.replace PermissionError WinError 5",
  "rootCause": "gate evaluation / stop accounting triggered unnecessary availability rewrite under FileLock; Windows sharing denial on replace",
  "fix": "peek_availability + browser_work_allowed(persist_transitions=False); get_availability writes only on meaningful transition; bounded Windows replace retry; fail closed CONTROL_PLANE_PERSISTENCE_FAILURE"
}

## Gate read/write audit

- evaluate_ebay_browser_work_gate → rowser_work_allowed(persist_transitions=False)
- peek_availability / locked_json_state(write=False) for inspection
- Legitimate incident/cooldown writers unchanged (still locked + atomic)

## Locking contract

All production writers of availability/ops/incidents continue via locked_json_state / mutate APIs. No unlocked whole-file public saves for gate path.

## Windows persistence

- Bounded retry: 6 attempts, backoff 15–200ms
- Transient denial → success after retry
- Persistent denial → CONTROL_PLANE_PERSISTENCE_FAILURE fail closed
- Never treat PermissionError as success
- Concurrent writers: serialized, valid JSON, count integrity proven

## Stop-accounting replay

Injected replace denial during gate evaluation: **0 replace calls** (read-only). Original Sold failure remains primary. OWNED_DAILY_FULL_ENABLE=false.

## Scheduler regression

12h/24h TTL, HIGH≥3, lanes 50/30/20, market isolation, fresh skip, dedupe — unchanged.

## Tests

- Focused: 	ests/test_sold_nav_control_plane_persistence_closure.py (25)
- Broader suite run: atomic/FSM/demand/inter-card/control-plane/capture/owned_daily (100+118 OK in batches)
- Readiness: all flags true

## Zero-network proof

No live eBay; latest SEARCH_SUBMISSION_STARTED remains historical Meowth event; owned_daily flag false.

## Next gate

READY_TO_RETRY_CONTROLLED_25_JOB_ROLLOUT (do not start in this task)
