# Chinese metadata RC — final release-readiness

Generated: 2026-10-14T09:10:00Z

## Recommendation: **GO** for metadata-only merge (isolated branches)

**HOLD** for production pack CDN install, artwork promotion, live catalogue cutover, and deploy.

## Performance (host, same machine)

| Metric | RC 14,923 | Baseline build98 41,185 | Delta |
|---|---|---|---|
| SQLite size | 32.5 MB (gzip 2.7 MB) | 172.7 MB | **−140 MB** |
| Cold open median | 3.1 ms | 2.8 ms | +0.3 ms |
| Collector lookup median | 26.8 ms | 159 ms | **faster** |
| Set+collector median | 28.7 ms | 170 ms | **faster** |
| Name FTS median | 0.03 ms | 0.03 ms | ~flat |
| Flutter install-path cold | **37 ms** | n/a | — |
| Flutter number search | **80 ms** (24 hits) | n/a | — |

Python tracemalloc peak: RC 145 KB vs baseline 168 KB (relative only).

## On-device

- Device: BKQ N49 (`AUWE026116000543`), Android 17.
- RC pack + `installed_pack.json` **adb-pushed** to `/data/local/tmp/cardscanr_rc_zh_cn/` (QA sandbox, not app production pack root).
- Device has no `sqlite3` binary; byte-identical host pack exercised via Flutter `PackedCatalogueLookupService` + Python benches.
- Full interactive binder UI / RSS on-device: **not run** (`integration_test` intentionally omitted from app package; no production install).

## Invariants

`verify_zh_hans_rc_release_invariants.py`: **16/16 PASS**

- 993 exclusions still excluded
- 28 links preserve live canonical IDs + identity fields
- EN/JA and all non–zh-Hans language counts unchanged
- Pack + new rows: placeholders only; PokéCardex attribution present
- Website attribution pages intact

## Diff safety

- App/website vs `origin/main`: attribution HTML + QA tests only (no Build 179 / thumb / scanner product changes).
- Data branch includes acquisition tooling/reports; staging **paths appear only in audit reports**, not in catalogue image URL fields or RC pack.
- Baseline Feebas `searchByNumber` failure: **out of scope** (separate scanner task).

## Regression

- Data pytest focused: **33 passed**
- Flutter RC pack tests: **PASS** (install layout + QA pack)
- Feebas REAL pack: known baseline FAIL (unchanged)

## PR dependency order (do not push without approval)

1. **Data** `task/global-image-acquisition-20261013` — catalogue tools, RC reports, integration manifests first  
2. **App/website** `task/global-image-acquisition-20261013` — PokéCardex attribution + Flutter RC tests (depends on data pack layout for local QA paths only; website attribution is independently mergeable)

## Release blockers (post-merge still HOLD)

1. No live `cards.jsonl` cutover without explicit owner approval  
2. No CDN/public pack publish of RC zh-cn  
3. 993 gated rows + 15 near-dups remain review queue  
4. Artwork IP clearance  
5. Optional: on-device binder UI smoke once integration harness allowed
