# build98 REAL zh-cn Feebas pack failure — diagnosis

## Verdict vs Chinese RC candidate

**Not a regression introduced by the Chinese metadata RC.**  
Same failure reproduces on the canonical `D:\CardScanR` tree (branch `cursor/build181-camera-perf-lifecycle`) and on this isolated app worktree. Packed-catalogue assertions inside the same test pass; failure is later.

## Failure site

- Test: `build98_real_zhcn_catalogue_test.dart` → `REAL production zh-cn pack parses and returns Feebas for 209/207`
- Assertion: line **345** `expect(result.candidates, isNotEmpty)` on `FastCollectionCandidateSearchService.searchByNumber`
- Inputs: OCR-shaped `ParsedCardText` with footer `HY8C 209/207 AR`, language `zh-Hans`
- Pack used: `D:\CardScanR\artifacts\build98_zhcn\zh-cn.sqlite` (181MB, 41,185 cards)

## What works

- Pack health: ready, >40k cards.
- Direct `PackedCatalogueLookupService.lookupBySetHints` for `csv8c` + `209/207` finds the row (SQL confirmed: `set_code=CSV8C`, name `丑丑鱼`, rarity `AR`).
- Fixture-based Build 94/98 zh-cn tests: **PASS**.
- RC local QA pack (14,923 rows) via `zh_hans_rc_qa_pack_test.dart`: **PASS**.

## Root cause class

Scanner/fast-collection candidate path with OCR set token `HY8C` against the **August 2026 production zh-cn pack**, not the RC metadata catalogue. Dual pack rows exist (`CSV8C`/`丑丑鱼` and `cn-csv8c`/`Feebas`); the failing step is `searchByNumber` candidate assembly, not metadata absence.

## Scope decision

- **Out of scope to fix here** (user: do not modify existing scanner work).
- Chinese RC remains metadata-only and does not change scanner code or production packs.
- Smallest next action for Feebas: separate scanner-task to debug `FastCollectionCandidateSearchService.searchByNumber` with HY8C on baseline main; do not block metadata RC on that path.
