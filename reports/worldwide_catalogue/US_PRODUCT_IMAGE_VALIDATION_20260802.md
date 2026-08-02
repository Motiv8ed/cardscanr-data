# Image validation checkpoint

- Checkpoint: `D:\CardScanR_worldwide_runtime_20260802\product_image_validation_us\checkpoint.sqlite`
- Cached objects: `721`
- Observed bytes: `241377844`

## Asset outcomes

| Outcome | Distinct URLs |
|---|---:|
| fail | 2 |
| not_found | 4 |
| pass | 1,070 |
| pending | 7,506 |

## Provider outcomes

| Provider | Outcome | URLs | Candidates |
|---|---|---:|---:|
| pokemon-cn-official | pending | 3,062 | 3,106 |
| pokemon-japan-products-official | pending | 1,946 | 1,946 |
| pokemon-korea-products-official-archive | pending | 2,305 | 2,551 |
| pokemon-us-products-official-archive | fail | 2 | 2 |
| pokemon-us-products-official-archive | not_found | 4 | 4 |
| pokemon-us-products-official-archive | pass | 1,070 | 1,416 |
| ptcg-chs-datasets | pending | 193 | 193 |

## Product coverage

- Provider: `pokemon-us-products-official-archive`
- Total products: `575`
- Products with at least one verified image: `575`
- Products without a verified image: `0`
- Candidate statuses: `invalid=6, verified=1,416`

## Failure classes

| Outcome | Error | URLs |
|---|---|---:|
| not_found | HTTP 404 | 4 |
| fail | ValueError: implausible decoded image: GIF 14x14 | 2 |

Transient fetch_url values are intentionally excluded from this report.
