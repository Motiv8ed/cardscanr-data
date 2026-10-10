# CARDSCANR_EN_JP_IMAGE_INDEPENDENCE_100_PERCENT_VERIFIED

Generated: `2026-10-10T08:14:42Z`

## Totals

| Metric | Count |
|--------|------:|
| Total EN+JA canonical cards | **68,011** |
| Local master (display.webp + asset.json) | **68,011** |
| Hosted on CardScanR CDN | **68,011** |
| Missing | **0** |
| Duplicate canonical mappings | **0** |
| Duplicate public URL / hosted keys | **0** |
| Local hash mismatches | **0** (full sha256 pass) |
| CDN HEAD/GET failures after repair | **0** |

- EN: 39,558 / 39,558
- JA: 28,453 / 28,453
- Independent: **100.0%**

## Closeout repairs

Eight previously bound aliases failed live CDN checks (7× HTTP 404, 1× space in object key for `pkmtch-SV-P 162`). Local masters were intact; objects were re-uploaded and the Giratina Match Battle URL sanitized to `pkmtch-sv-p_162`. No already-correct assets were deleted.

## Recovery backups (no deletes)

- Hash-verified inventory: `D:\CardScanR_Archive\backups\en_jp_image_independence_100pct_20261010T080450Z`
- Post-repair CDN-verified inventory: `D:\CardScanR_Archive\backups\en_jp_image_independence_100pct_20261010T081442Z`
- Live master root: `D:\CardScanR_Data\cardscanr-data\data\images\master` (also `D:\cardscanr-data\data\images\master`)
- Seller-photo originals: `D:\cardscanr-data\data\images\independence\pass10_candidates\`

## Regression gate

`tests/test_en_jp_image_independence_regression.py` fails closed if any new EN/JA catalogue card lacks local-master + CardScanR CDN imagery.

## Rights / provenance note

Seller listing photos retain attribution with
`rightsStatus=seller_photo_attribution_retained_licence_not_inferred`.
Public availability is **not** treated as a seller licence grant.

## Validation commands (this closeout)

See companion `closeout_verification_result.json` and test run logs under `reports/image_independence/`.
