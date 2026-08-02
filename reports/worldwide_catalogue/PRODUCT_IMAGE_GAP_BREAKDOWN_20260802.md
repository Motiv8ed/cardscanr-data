# Product image gap breakdown

- Generated after Asia card-image completion
- Sealed product variants: `4618`
- Variants with at least one `pass` image validation: `3889`
- Variants without a pass image: `729`

## By sealed-product provider

| Provider | Variants without pass image |
|---|---:|
| pokemontcg-data | 188 |
| pokemon-cn-official | 162 |
| pokemon-asia-tw-official | 112 |
| pokemon-asia-hk-official | 112 |
| pokemon-asia-id-official | 87 |
| ptcg-chs-datasets | 24 |
| pokemon-asia-th-official | 15 |
| pokemon-us-product-gap-evidence | 7 |
| pokemon-asia-tw-products-official | 4 |
| pokemon-asia-sg/ph/my-official | 3 each |
| pokemon-asia-id/hk-products-official | 3 each |
| pokemon-asia-th-products-official | 2 |
| pokemon-japan-products-official | 1 |

## Notes

- China (`pokemon-cn-official`) leftovers are dominated by `acquired_transient` signed URLs that are intentionally not app-publication eligible.
- `pokemon-asia-*-official` product rows come from trainer-site expansion inventories; many are set/filter identities rather than gallery sealed SKUs with dedicated pack art.
- Exact Asia gallery leftovers that failed technical validation:
  - `pokemon-asia-th-products-official` → archive HTML URL treated as image (`.../archives/1989/`)
  - `pokemon-asia-tw-products-official` → invalid/news placeholder asset
- SG/MY/PH still have no local sealed-product gallery index (US expansion links only); see `ASIA_PRODUCT_GALLERY_GAPS_20260802.md`.
