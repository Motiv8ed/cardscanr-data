# POST_SOLD_PAGE_HEALTH_CLOSURE_REPORT

**Task:** `CARDSCANR-POST-SOLD-PAGE-HEALTH-AND-CDP-CLASSIFICATION-CLOSURE`  
**Result:** PASS  
**Live network:** 0 eBay navigations / 0 SEARCH_SUBMISSION_STARTED / 0 Sold clicks  
**OWNED_DAILY_FULL_ENABLE:** false

## Rowlet exact evidence (historical)

| Field | Value |
|-------|-------|
| Card | Rowlet / Perfect Order / 10 |
| attemptId | `58eb8ff4-b94a-48ce-ad1c-a4e7ce986db2` |
| priceKeyId | `e225fd92-2f65-4bda-9a5b-08c0dcacf462` |
| runtimeMode | INTER_CARD |
| SEARCH_SUBMISSION_STARTED | true |
| Sold exact identity | true |
| Historical x11SoldStateVerified | true (filter-only) |
| Capture | FAILED |
| Historical reported failure | `CDP_TARGET_NOT_FOUND` |
| workerDiagnostics.targetCount | 1 |
| targetId | `6F2D4B657DB8B44B13FA42DCDFF96FA4` |
| URL | `…Rowlet…&LH_Sold=1` |
| title | `Error Page \| eBay` |
| hasWs | true |
| scorer reason | `sorry_error` (score=-500) |

### Misclassification root cause

The capture worker **did see** a current page target. It rejected it as unhealthy (`score=-500`, reason historically labeled `sorry_error`) then collapsed the selection failure into `CDP_TARGET_NOT_FOUND` because no *viable* target remained.

**Actual observed condition:** `CURRENT_TARGET_PRESENT_BUT_UNHEALTHY_ERROR_PAGE`  
**New canonical class:** `MARKETPLACE_ERROR_PAGE` / page class `EBAY_ERROR_PAGE`

## Part A — FILTER_STATE_CHECKS vs PAGE_HEALTH_CHECKS (before)

### FILTER_STATE_CHECKS (pre-change)

- `LH_Sold=1` in URL via `url_has_lh_sold` / `classify_post_sold_url`
- Unexpected filter transition (`LH_PrefLoc` etc. without `LH_Sold`)
- about:blank / challenge / sorry short-circuits

### PAGE_HEALTH_CHECKS (pre-change)

- **Not required for `x11SoldStateVerified`**
- X11 success was effectively `LH_Sold=1` + not sorry/challenge/blank
- Title `Error Page | eBay` was folded into `is_ebay_sorry_page` but wait loops could still accept URL-only success before title settled, then capture saw Error Page

## Old / new Sold verification semantics

| Flag | Meaning |
|------|---------|
| `soldFilterStateVerified` | `LH_Sold=1` after exact Sold interaction |
| `soldPageHealthVerified` | Healthy Sold-results page (hostname, title, no error/sorry/challenge/403, structure when body available) |
| `x11SoldStateVerified` | **BOTH** required (capture-ready) |

Stable `Error Page | eBay` through bounded `PAGE_HEALTH_VERIFICATION` → terminal `EBAY_ERROR_PAGE`, capture/parse/write `NOT_RUN`.

## Target-selection taxonomy

| Outcome | Class |
|---------|-------|
| No plausible page target | `CDP_TARGET_NOT_FOUND` |
| Target exists, attach fails | `CDP_TARGET_ATTACH_FAILURE` |
| Expected target exists but unhealthy (Error/SORRY) | `MARKETPLACE_ERROR_PAGE` |
| Unhealthy challenge page | `TARGET_REJECTED_UNHEALTHY_PAGE` |
| Page present, missing LH_Sold | `CDP_TARGET_URL_MISMATCH` |

Rejection evidence includes: `targetCount`, `targetsSeen`, `expectedTargetFound`, `expectedTargetId/Url/Title`, `rejectionReasons`, `healthClassification`.

## Control-plane incident policy

- Stable marketplace Error Page → `TEMPORARY_EBAY_SERVER_FAILURE` / `TRANSIENT_EBAY` (`ebay_error_page`)
- **Not** CAPTCHA / `CHALLENGE_REQUIRED` unless challenge evidence present
- Current job stops safely; last-good retained; no freshness success; no automatic same-query retry
- Distinct from local `POST_SOLD_CAPTURE_FAILURE`

## Rowlet offline replay

- `soldFilterStateVerified=true`
- `soldPageHealthVerified=false`
- `x11SoldStateVerified=false`
- `expectedTargetFound=true`
- capture `NOT_RUN`
- terminal `EBAY_ERROR_PAGE` / selection `MARKETPLACE_ERROR_PAGE`
- **Not** `CDP_TARGET_NOT_FOUND`

## Healthy replay (13 success-class cards)

- `healthyReplayCount=13`
- `falseNegativeCount=0`

## Zero-network proof

All work offline via fixtures under `reports/artifacts/post_sold_page_health_closure/fixtures/`. No live eBay activity.

## Key code

- `cardscanr_market_engine/providers/sold_page_health.py` (new)
- `sold_navigation_phases.py`, `linux_x11_gui_fsm.py`, `linux_x11_ebay_sold.py`
- `post_sold_capture.py`, `post_sold_capture_worker.py`
- `ebay_browser_provider.py`, `owned_daily_outcomes.py`, `marketplace_ops_state.py`, `job_runner.py`
- `tests/test_post_sold_page_health_closure.py`

## Regressions

- Exact Sold targeting, Sold phase/persistence, capture, demand scheduler, INTER_CARD URL correlation: PASS (offline suites)
