# PR 1 (data) — Chinese metadata RC + acquisition tooling (DRAFT PR)

**Branch:** `task/zh-hans-metadata-rc-pr-20261014`  
**Base:** `main`  
**Depends on:** nothing  
**Blocks:** PR 2 can merge after this is accepted (website attribution may land independently)

## Summary

- Integrate PokéCardex zh-Hans metadata into isolated worktree catalogue (877 → 14,923) with placeholders + attribution.
- Reconcile 15,039 source rows (14,046 in / 993 gated).
- RC pack install layout, perf benches, invariant verification, Korean alternative-sources doc (no new KO acquire).
- Supporting acquisition/audit tooling from the same task branch.

## Test plan

- [x] `python tools/verify_zh_hans_rc_release_invariants.py` → PASS
- [x] `python tools/bench_zh_hans_rc_pack_perf.py`
- [x] Focused pytest on zh-Hans helpers / catalogue identity
- [ ] Owner review of 993 exclusions policy
- [ ] Explicit approval before live catalogue cutover / CDN pack publish

## Out of scope

- Artwork promotion, production pack install, Feebas scanner fix, Build 179 / thumb work
