# Product image gap release classification

- Classified at: `2026-08-02T12:43:46.064788+00:00`
- Variants without a `pass` image validation: `729`

## Counts by class

| Class | Count |
|---|---:|
| `asia_expansion_sku_no_pack_art_url` | 326 |
| `asia_gallery_invalid_or_placeholder_asset` | 2 |
| `asia_local_product_gallery_unavailable` | 9 |
| `china_community_product_image_rights_blocked` | 24 |
| `china_product_image_transient_only` | 162 |
| `historical_theme_deck_no_image_source` | 188 |
| `japan_accessory_product_no_image` | 1 |
| `product_parser_false_positive` | 10 |
| `us_product_gap_evidence_no_image` | 7 |

## Status rollup

| Status | Count |
|---|---:|
| `blocked_external` | 719 |
| `classified_nonblocking` | 10 |

## Notes

- China leftovers remain `acquired_transient`: direct `image.pokemon.com.cn` URLs return HTML without a page-issued fetch URL.
- Asia `*-official` expansion SKUs are inventory identities; only `*-products-official` galleries supply dedicated pack art.
- SG/MY/PH local sealed galleries are absent; see `ASIA_PRODUCT_GALLERY_GAPS_20260802.md`.
- Parser false positives are `classified_nonblocking` and do not block card publication.
