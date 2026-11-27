# Atlas brief: two eBay pricer code fixes (27 Nov 2026)

This is a brief only. Please implement the code fixes in a **follow-up PR**. Do not treat this documentation PR as the fix, and do not merge it as a product change.

Times below are Australia/Brisbane (AEST, UTC+10).

Authority: Andy, 27 Nov 2026, t1046s6568: "Leave the pricer running and send both fixes to Atlas as a PR." The live pricer was left running and was not changed for this brief.

## What to do

Open a follow-up implementation PR against `Motiv8ed/cardscanr-data` `main` (currently `1b6498c538648f8cd2dae8e6304e70677fd20c25`, which includes data PR #179). GitHub Actions must stay `workflow_dispatch` only. No admin merge, bypass, or force push. Independent QA still applies before any merge or live deploy. Andy's yes is still required before changing the live checkout or restarting the pricer.

Please do **not** pause or edit the running live pricer, the `CardScanR-LiveEbayPricingRuntime` task, or `D:\CardScanR_Data\cardscanr-data` as part of writing the fix.

---

## Live context (do not regress)

The PC live checkout is on local branch `runtime/pr179-capped-multimarket-20261127` at `9762fce9c4b4997a6b526d6c8323ce20d0b5d141`. That commit is **local only** (not on GitHub). It is `main` at `1b6498c5` plus one caps commit that restores the launcher settings previously carried on the old runtime branch:

- `MARKET_WORKER_ALLOWED_MARKETS` and `MARKET_SCHEDULER_ALLOWED_MARKETS` = `AU,US,GB,CA` (short test, Andy 27 Nov 05:51)
- `MARKET_WORKER_ENFORCE_SAFETY_BUDGET=true`
- `MAX_LIVE_SUBMISSIONS_PER_HOUR=60`
- `MAX_LIVE_SUBMISSIONS_PER_DAY=300`
- `MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES=3`
- also from the same historical cap block: `MARKET_WORKER_MIN_JOB_SPACING_SECONDS=20`, `MAX_TRANSIENT_MARKETPLACE_FAILURES_PER_HOUR=5`
- `owned_daily` stays **OFF** (`owned_daily_full_enable.flag=false`)
- `CardScanR-BulkReferencePricingSync` stays **Disabled**. Do not run `-Action start` on it.

Atlas may want to upstream that caps commit (or an equivalent) so `main` does not relaunch without the budget and caps.

The CHALLENGE_REQUIRED latch was cleared at 17:13:14 AEST on 27 Nov via `ContinuousSafetyBudget.record_operator_recovery()`, with Andy's verbatim approval recorded in the safety ledger. Latch epoch went from 5 to 6 and is still clear. A real challenge, 403, or similar safeguard must still be allowed to re-latch. Do not add a bypass.

Local backup branch `backup/live-20261126-7c6dfbd1` (also unpushed) keeps the two 26 Nov commits that were on the live folder before this deploy:

- `4253a9cf` Separate passive frame failures from eBay challenges and add owner reconciliation
- `7c6dfbd1` Record sanitized native AU recovery failure stage and safety refusal

Those include `tools/ebay_au_challenge_reconcile.py` and "false challenge" handling. Atlas may want them, but they were intentionally not included in the 27 Nov live switch.

---

## Fix 1: `search_results_not_confirmed` on every sold-search job

### Symptom (fresh read-only counts at 18:38 AEST, 27 Nov)

After Xvfb/Chrome were brought up and the task was restarted hidden at **17:25:05 AEST**, every sold-search job has failed the same way. No prices have been written. No challenge or 403 has been recorded. AU remains `PROBE_REQUIRED` (`confirmedHealthy=false`, last outcome `probe_finalize_timeout_safe`). The CAPTCHA latch is still clear.

From `reports/runtime/live_ebay_worker.out.log` since 17:25:05 (log 60 lines, last write 18:38:13):

| Count | Result |
| --- | --- |
| 12 | jobs claimed and failed |
| 12 | `TEMPORARY_BROWSER_FAILURE` / `Desktop sold navigation failed: search_results_not_confirmed` |
| 0 | challenges / CAPTCHA / access-denied |
| 0 | `UPDATED_FROM_EBAY`, `UNCHANGED_FROM_EBAY`, or any priced write |
| 0 | official recovery probe completions (`PROBE_HEALTHY_STREAK_PENDING` or similar) |

Jobs, all AU/AUD, all the same error (card names and set codes only):

1. Golduck `sv3pt5` 55 (`6c1307bf-…`), cooldown 180s
2. Parasect `sv3pt5` 47 (`88a6e6b0-…`), cooldown 270s
3. Oddish `sv3pt5` 43
4. Nidoqueen `sv3pt5` 31
5. Blastoise ex `sv3pt5` 9
6. Ivysaur `sv3pt5` 2
7. Rigid Band `sv3pt5` 165
8. Bulbasaur `sv3pt5` 1 (non_holo)
9. Pikachu `me2pt5` 55
10. Dragonite `basep` 5
11. Pikachu `swsh12pt5gg` GG30 (holo)
12. Greninja `sm10` 117 (holo)

From job 3 onwards the paced cooldown stayed at 360s. The worker and scheduler were still running at 18:38 (PIDs unchanged since the 17:25 start). Chrome did reach eBay sold-search pages (headed CardScanR profile on WSL display :99, titles in the `for sale | eBay` pattern). The failure is confirmation after submit, not "Chrome cannot open eBay".

### Where it is raised on `main` (`1b6498c5`)

The reason is produced after the GUI has already confirmed that the query is visible and that submit happened, but the post-submit page is not classified as ordinary `/sch/` results:

- `tools/linux_x11_ebay_search.py` around **1572–1578**: if `gate.query_visible` and `gate.submitted` but the result is not `ok` and no more specific error is set, `error` becomes `search_results_not_confirmed` and `classification` becomes `SEARCH_RESULTS_NOT_CONFIRMED`.
- `cardscanr_market_engine/providers/linux_x11_ebay_nav.py` around **397–402**: if `queryVisibleConfirmed` and `submitted` but the leftover error is `SEARCH_INPUT_NOT_CONFIRMED`, it is rewritten to `search_results_not_confirmed`.
- `cardscanr_market_engine/providers/linux_x11_gui_fsm.py` around **724–740** (`on_search_post_submit_page`): if the page is not a challenge, 403, sorry/error page, eBay Live, or alternate surface, and `results_ok` / `ordinaryResults` is false, it notes `search_results_not_confirmed_after_submit` and does not move the phase to `ORDINARY_RESULTS_CONFIRMED`.

Ordinary-results confirmation (same files, especially `linux_x11_ebay_search.py` around **720–731** and `is_ordinary_sch_results_url` / `query_represented_in_url` in `linux_x11_gui_fsm.py` around **398–420**) currently requires a `/sch/` URL that contains `_nkw=`, is not an eBay Live URL, and whose `_nkw` tokens overlap the job query, plus a title token / `for sale` + `ebay` title check. If current eBay AU/US/GB/CA sold-search markup or URL shape no longer meets that (for example `_nkw` missing or renamed, a different results host/path, or title wording changed), every job will fail exactly as we are seeing, while Chrome still "looks" like it reached a sold-search page.

### Suggested direction

- Re-examine selector and result-confirmation logic against **current** eBay AU, US, GB and CA sold-search markup and URL shape (including `_nkw` / `/sch/` / `LH_Sold` assumptions).
- Keep challenge, 403, sorry/error-page, rate-limit and cooldown detection running first. Do not weaken those.
- Prefer confirming the job query from the live results URL after normalisation, not only from a title string.
- Do not enable `owned_daily`. Do not raise caps. Do not change HARD_STOP policy except as needed to avoid misclassifying a results-page change as a challenge.

### Acceptance tests (ideas)

- Unit/fixture: a current-style AU sold-search HTML/URL fixture (query visible, submitted, ordinary `/sch/` results, no captcha) is classified `ORDINARY_RESULTS_CONFIRMED`, not `search_results_not_confirmed`.
- The same fixture with a challenge title/URL or 403 still latches or stops as today.
- A sorry / generic Error Page still follows the existing transient / availability path, not confirmation success.
- `_nkw` or equivalent query parameter still has to match the job query (no false confirm on the wrong card).
- US/GB/CA fixtures, or shared helpers, so the multi-market test launcher is covered.
- Revert-proof: reverting the confirmation change makes the new fixture fail.
- 0 new test-name failures versus `main`.

---

## Fix 2: `cardscanr_ensure_xvfb.sh` CRLF, plus Xvfb/Chrome preflight

### Symptom

The 17:14 AEST job (Dewgong `sv3pt5` 87, before the Xvfb workaround) failed `linux_chrome_cdp_failed` in COLD_START with stderr `DISPLAY_RECOVER: invoking ensure_xvfb` then `ERROR: DISPLAY :99 not ready`, and stdout `CDP_FAIL`. Xvfb was not running. `cardscanr_chrome_ctl.sh` (WSL prefix `~/.local/cardscanr-gui/bin`) calls `${CARDSCANR_ENSURE_XVFB_SH:-/mnt/d/cardscanr-data/tools/cardscanr_ensure_xvfb.sh}`.

The live checked-out `tools/cardscanr_ensure_xvfb.sh` has **CRLF** line endings. Invoking it in WSL fails immediately with `/bin/bash^M: bad interpreter: No such file or directory` (exit 126). That is why auto-recover did not start display `:99`.

`.gitattributes` on `main` already forces LF for `*.json`, `*.py`, `*.yml`, `*.yaml` and `*.txt`, but **not** `*.sh`. A Windows checkout therefore stores the ensure script with CRLF.

### Temporary workaround (not a fix)

An LF-only copy was run as `/tmp/cardscanr_ensure_xvfb.lf.sh` (not in the repo). Xvfb PID 371 then stayed up on `:99`, and later jobs got past Chrome startup. That `/tmp` copy is lost on a WSL restart. The repo file was not edited.

### What to implement

1. Normalise `tools/cardscanr_ensure_xvfb.sh` (and other shell scripts in this repo) to LF.
2. Add a `.gitattributes` rule such as `*.sh text eol=lf` (and consider `*.ps1` / `*.vbs` if those also break when the line ending is wrong).
3. Restore an Xvfb/Chrome preflight so the worker is not started when display `:99` or CDP is not ready. `tools/ebay_runtime_preflight.py` is **absent on `main`**. It existed on the older runtime branch and used to be called from `scripts/ensure_live_ebay_pricing_runtime.ps1` (never opens eBay; jobs stay queued if not ready). Please restore an equivalent official tool and hook it from the launcher, without loosening caps or changing market policy.

### Acceptance tests (ideas)

- A checkout of the branch on Windows still has an LF shebang in `cardscanr_ensure_xvfb.sh` (`file` / no `^M`).
- A unit or script test fails if a tracked `*.sh` file contains CRLF.
- Preflight: when display `:99` is down, the launcher or preflight reports not ready and does not start a pricing job; when Xvfb/CDP are ready on `about:blank`, it reports ready. No eBay navigation in preflight.

---

## Out of scope / constraints

- Do not change production SQL or run `supabase db push`.
- Do not start `CardScanR-BulkReferencePricingSync`.
- Do not read or print cookies, tokens, passwords, or DPAPI files.
- Do not add visible Windows windows.
- Keep GitHub Actions as `workflow_dispatch` only.
