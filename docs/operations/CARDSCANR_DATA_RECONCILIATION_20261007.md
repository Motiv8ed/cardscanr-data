# CARDSCANR_DATA_RECONCILIATION

**Verdict: PARTIAL**  
**Date:** 2026-10-07  
**Worktree:** `D:\CardScanR_Data\cardscanr-data` (== `D:\cardscanr-data` junction)  
**Branch:** `cursor/cjk-search-normalizer-safe` (alias `pricing/us-gb-ca-canary-stack`)  
**HEAD:** `047b508c`  
**origin/main:** `3d1b16a6` (unchanged)

Did **not** touch: product main, Ops, Runtime, Facebook, Play, or live pricing processes/Chrome.

---

## Naming correction

Branch name suggests CJK/search-normalizer work. **That is incorrect for this tip.**

- This branch is a **pricing / owned-daily / US-GB-CA canary** stack.
- Real CJK-related tip lives on `tools/en-jp-production-packs-20260926` (`a17dfa3e` â€” EN/JP pack path without packaging CJK).

---

## COMMITS_AHEAD (origin/main..HEAD)

Originally **32** unique commits (local-only; now pushed). Reconciliation added **3** preserve commits â†’ **35** ahead.

### Original 32 (by theme)

| Theme | Count | Purpose |
| --- | ---: | --- |
| canary / multi-region | 16 | US/GB/CA serial canaries, sparse NO_PRICE handling, attempt budgets, P0 candidates |
| owned-daily / UI-search | 5 | UnifyÑ‡Ð¸Ñ‚ÑŒ owned-daily with UI search path, INTER_CARD, fan-out markets |
| sold-nav / control-plane | 5 | Sold control targeting, page health, Xvfb, dual checkpoints |
| scheduler | 2 | Demand-aware verified-local + continuous AU |
| gaming-pause | 1 | Recover stuck gaming pause |
| other / docs | 3 | Docs + misc pricing fixes |

Full list (oldestâ†’newest of original stack ends at `6ab08cd6`; see `git log origin/main..6ab08cd6 --oneline`).

### Preserve commits (this reconciliation)

1. `d8f89fd6` â€” ignore local probe dumps / runtime state  
2. `750c35c8` â€” sold-page health + P0 canary backoff (was uncommitted WIP)  
3. `7023761b` â€” preserve support tools, SQL bootstrap, closure notes, catalogue tests  

Remote now has:

- `origin/cursor/cjk-search-normalizer-safe`
- `origin/pricing/us-gb-ca-canary-stack` (same SHA; clearer name)

---

## DIRTY_BREAKDOWN (before reconciliation)

| Class | Approx | Notes |
| --- | ---: | --- |
| SOURCE_CHANGE | ~70 | 20 modified tracked + untracked tools/tests |
| DATA_CHANGE | 0 | â€” |
| GENERATED | ~3370 | `reports/_*`, artifacts packs, zips, probes |
| RUNTIME | many under `reports/runtime/` | live scheduler/worker state JSON â€” kept on disk |
| CACHE/TEMP | few | `.deb`, `.bak_*`, `supabase/.temp` |
| UNKNOWN | 2 | supabase CLI temp â†’ ignored |

After: **working tree clean** (generated/runtime remain on disk, gitignored).

Hold copy of uncommitted canary report WIP:  
`D:\CardScanR_Data\artifacts\local\data_reconciliation_20261007\wip_report_artifacts\`

---

## MERGED_TO_DATA_MAIN

**None.** Intentionally not fast-forwarded/merged. Stack changes `workers/`, `scripts/`, and market engine vs `origin/main`; merging would redefine canonical main without an explicit product decision. Live checkout already runs from this worktree/branch for path-based tasks.

---

## RETAINED_UNMERGED

- Entire **36-commit** stack on `pricing/us-gb-ca-canary-stack` / `cursor/cjk-search-normalizer-safe`
- Ignored on-disk evidence under `reports/`, `artifacts/`, `reports/runtime/`
- Underscore probe scripts `tools/_*` (gitignored, kept on disk)
- Separate branch `tools/en-jp-production-packs-20260926` (CJK/pack work â€” not part of this tip)

---

## REMOVED/IGNORED

- Expanded `.gitignore` for probe dumps, runtime JSON, `*_latest.json`, `*.deb`, supabase `.temp`, artifact `_zip_stage` / `code_snapshot` duplicates
- No destructive deletes of evidence packs

---

## LIVE_PRICING_IMPACT

- Scheduled tasks still point at `D:\cardscanr-data\scripts\...` â€” **Ready**, paths resolve  
- No worker/script edits applied beyond committing already-present WIP (same bytes that were dirty)  
- Windows CDP **9226 was DOWN** during audit; **not restarted** (do-not-touch pricing production)  
- BulkReference / EcbFx do not require 9226 for their script entrypoints  

---

## TESTS

```text
python -m unittest tests.test_canary_control_plane \
  tests.test_challenge_classification_representation \
  tests.test_post_sold_page_health_closure
â†’ Ran 46 tests â€” OK
```

---

## ANDREW_ACTION_REQUIRED

**Decide whether to merge `pricing/us-gb-ca-canary-stack` â†’ `cardscanr-data` `main`.**

That is the reviewed preservation tip (`047b508c`). It is the real pricing canary/owned-daily stack (not CJK). Until merged, `origin/main` stays at `3d1b16a6` while this worktree remains the live path for data-repo scripts.
