# Release-gate audit

- Generated: `2026-08-02T12:52:00+00:00`
- Staging DB: `D:\CardScanR_worldwide_runtime_20260802\staging\worldwide-catalogue.sqlite`
- Integrity mode used for gate check: `quick` (`pragma quick_check` ok, FK failures 0)

## Gate results (must be zero / clean)

| Gate | Value |
|---|---:|
| unclassified_unresolved_items | 0 |
| open_unresolved_items | 0 |
| unexplained_official_release_shortfalls | 0 |
| external_blocker_state_mismatches | 0 |
| running_import_runs | 0 |
| secret_bearing_card_image_urls | 0 |
| secret_bearing_product_image_urls | 0 |
| collector_collision_groups_needing_review | 0 |
| orphan_product_contents | 0 |
| missing_core_provenance | 0 |
| officially_printed_languages_without_records | 0 |

## Accepted external residuals (not gate failures)

- Product variants without pass image: `729` (719 blocked_external, 10 parser nonblocking) — `PRODUCT_IMAGE_GAP_RELEASE_CLASSIFICATION_20260802.md`
- Official count shortfalls classified: `659` — `OFFICIAL_RELEASE_SHORTFALL_CLASSIFICATION_20260802.md`
- Asia unparsed special-card archive pages: `37` classified_nonblocking — `ASIA_UNPARSED_PRODUCT_PAGE_CLASSIFICATION_20260802.md`
- European official localized DB: Incapsula/CAPTCHA — existing regional blocker reports
- Asia card-image residuals: 1 TH empty body, 9 HK empty `/card-img/` URLs
- China product images: `acquired_transient` only (direct CDN returns HTML)

## Verdict

**RELEASE_GATE_ACCEPTED_FOR_CANARY**

Staging may proceed to an immutable non-production R2 canary. Do not activate the production worldwide manifest or write production Supabase until canary verification and owner activation steps complete.
