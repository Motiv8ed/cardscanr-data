# Chinese catalogue release candidate — verdict

Generated: 2026-10-14T08:46:00Z

## Verdict: **PASS (metadata-only, isolated branch)** — merge/deploy **BLOCKED**

Chinese metadata RC is ready for human review on the isolated data branch. It is **not** cleared for merge, public artwork, or production pack promotion.

## Exact counts

| Item | Count |
|---|---|
| Source PokéCardex review rows | 15,039 |
| Integrated new rows | 14,046 |
| Excluded (promo 943 + utility energy 50) | 993 |
| Worktree zh-Hans total | **14,923** |
| Original live zh-Hans preserved | **877 / 877** |
| High-confidence links (not new cards) | 28 |
| Near-dup pairs still review-only | 15 |
| EN / JA | unchanged |
| KO new TCGdex objects | 0 |

## App-level evidence (actual paths, not fixtures alone)

| Check | Result |
|---|---|
| Local RC zh-cn pack built from worktree `cards.jsonl` | 14,923 cards / 63 sets / placeholders only |
| Python local QA (`run_zh_hans_rc_local_qa.py`) | **14/14 PASS** (set browse, CN name FTS, collector #, set+collector, variants, placeholders, 877 preserved, website attribution) |
| Flutter `zh_hans_rc_qa_pack_test.dart` against RC pack | **PASS** |
| Flutter Build94 + fixture Build98 + variants + binders | **PASS** |
| Build98 REAL production pack `searchByNumber` Feebas | **FAIL (baseline)** — see `BUILD98_FEEBAS_DIAGNOSIS.md` |
| Data pytest (catalogue/search/identity helpers) | **46 passed** |
| Website PokéCardex attribution (index/data/pricing/trust) | **PASS** |
| Staging/public artwork URLs on RC pack | **0 leaks** |

## Remaining issues

1. **993** gated rows (mostly unmapped promos) still need human/confidence review before any later inclusion.
2. **15** near-duplicate art pairs unresolved (correctly not merged).
3. Baseline Feebas `FastCollectionCandidateSearchService` failure on production pack (scanner path; out of RC scope).
4. Artwork remains placeholders — Pokémon IP review required before any image promotion.
5. Worktree `cards.jsonl` is gitignored (regenerable); commit tools + manifests + reports only.

## Smallest next action

Owner review of this RC package + sign-off checklist; **do not merge**. Optionally open a separate scanner ticket for HY8C/`searchByNumber` Feebas on baseline main. For Korean: follow `korean/KOREAN_ALTERNATIVE_SOURCES.md` — no bulk acquire.
