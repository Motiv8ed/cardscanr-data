# Image validation checkpoint

- Checkpoint: `D:\CardScanR_worldwide_runtime_20260802\product_image_validation\checkpoint.sqlite`
- Cached objects: `5253`
- Observed bytes: `7466422560`

## Asset outcomes

| Outcome | Distinct URLs |
|---|---:|
| fail | 3 |
| not_found | 1 |
| pass | 5,556 |
| pending | 3,022 |

## Provider outcomes

| Provider | Outcome | URLs | Candidates |
|---|---|---:|---:|
| pokemon-cn-official | pass | 3,062 | 3,106 |
| pokemon-japan-products-official | pending | 1,946 | 1,946 |
| pokemon-korea-products-official-archive | fail | 3 | 3 |
| pokemon-korea-products-official-archive | not_found | 1 | 1 |
| pokemon-korea-products-official-archive | pass | 2,301 | 2,547 |
| pokemon-us-products-official-archive | pending | 1,076 | 1,422 |
| ptcg-chs-datasets | pass | 193 | 193 |

## Product coverage

- Provider: `pokemon-cn-official`
- Total products: `162`
- Products with at least one verified image: `0`
- Products without a verified image: `162`
- Candidate statuses: `acquired_transient=3,106`

## Failure classes

| Outcome | Error | URLs |
|---|---|---:|
| fail | ValueError: implausible decoded image: PNG 1501x5 | 2 |
| fail | ValueError: implausible decoded image: PNG 1024x3 | 1 |
| not_found | HTTP 410 | 1 |

Transient fetch_url values are intentionally excluded from this report.
