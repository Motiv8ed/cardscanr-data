# CardScanR App Data Contract (`public/v1`)

This document defines the **strict app-facing contract** for CardScanR static data under `public/v1`.

## 1) Public base URL and namespace

- The production app contract is namespaced under **`/v1/`**.
- App clients should treat `/v1/` as the contract root and avoid depending on non-`/v1/` paths.
- In `/v1/index.json`, dataset URLs are **origin-relative paths** (for example, `/v1/catalog/pokemon/en/sets.json`), so the app should resolve them against the current deployment origin.

## 2) Production app-facing files

The following files are production/stable (or intended-stable) app-facing contract paths:

- `/v1/index.json`
- `/v1/app-config.json`
- `/v1/supported-games.json`
- `/v1/supported-sources.json`
- `/v1/catalog/pokemon/en/sets.json`
- `/v1/catalog/pokemon/en/cards/{setId}.json`
- `/v1/catalog/pokemon/jp/sets.json`
- `/v1/catalog/pokemon/jp/cards/{setId}.json`
- `/v1/prices/status.json`
- `/v1/prices/current/pokemon/en/status.json`
- `/v1/prices/current/pokemon/en/{setId}.json`
- `/v1/prices/current/pokemon/jp/status.json`
- `/v1/images/cache-policy.json`

## 3) Experimental / non-contract files

The following paths are **not** app contract yet and must be treated as experimental/internal:

- `/v1/provider-catalog/**`
- `/v1/diagnostics/**`
- `/v1/history/**`
- `/v1/prices/pokemon/{language}/sample.json`

## 4) Canonical identity rules

Canonical IDs for app and backend convergence:

- `canonicalCardId = game|language|setId|collectorNumber|normalizedName`
- `priceIdentityId = canonicalCardId|variant|condition|market|currency`

Transitional aliases currently present in output:

- `canonicalBaseId` exists in catalogue output.
- `canonicalId` exists in prices/history output.
- Future code should converge on `canonicalCardId` and `priceIdentityId`.

## 5) Source ID policy

Canonical source IDs are lowercase `snake_case`:

- `pokemon_tcg_api`
- `tcgdex`
- `tcgdex_tcgplayer`
- `tcgdex_cardmarket`
- `pokewallet`
- `ebay_sold_manual`
- `manual`
- `manual_seed`
- `unavailable`

Current outputs may still contain transitional or legacy source names; those should be normalized in a future task.

## 6) Price status policy

Canonical price statuses:

- `priced`
- `no_result`
- `not_configured`
- `rate_limited`
- `network_error`
- `provider_error`
- `stale`
- `unavailable`
- `disabled`

Current contract behavior:

- Current files primarily provide `priced` records and set/language-level availability.
- Future price files should support explicit per-card `no_result` and error records.

## 7) Market / currency policy

Future app-facing price records must include:

- `market`
- `country`
- `currency`
- `sourceCurrency`
- `targetCurrency`
- `conversionPolicy`

Current limitations:

- EN prices mostly return provider/native currency, mostly USD.
- AU/eBay/local sold pricing is not implemented yet.
- JP production pricing is unavailable.

## 8) Image policy

Current app-facing catalogue image fields use upstream URLs:

- `imageSmall`
- `imageLarge`

Contract notes:

- No card image binaries are stored in this repository today.
- `/v1/images/cache-policy.json` is policy metadata only.
- `provider-catalog` image references are not production image files.

## 9) Current coverage (known snapshot)

From current output/diagnostics snapshot:

- Pokémon only.
- EN catalogue: **172 sets**, **20,237 cards**.
- JP catalogue: **162 sets**, **6,246 cards**, **5 failed sets**, **97 partial sets**.
- EN current price files: **160 set files**, **31,415 records**.
- JP prices: unavailable.
- Tracked history: small tracked subset only.

## 10) App consumption guidance

Recommended app behavior:

1. Fetch `/v1/index.json` first.
2. Fetch `/v1/supported-games.json`.
3. Discover language support from current catalogue/pricing status until a dedicated supported-languages manifest exists.
4. Fetch sets per game/language.
5. Fetch cards by `setId`; filter locally by `collectorNumber` + `normalizedName`.
6. Fetch EN prices by `setId`; filter locally by `collectorNumber` + `normalizedName` + `variant` + `condition`.
7. Treat missing price file as dataset unavailable.
8. Treat missing card in a present price file as “no price for that card.”
9. Do not overwrite an existing valid local price with missing/error/unavailable values.
10. Use JP price status as unavailable.
11. Use `imageSmall` / `imageLarge` directly.

## 11) Near-term contract gaps

- No supported-languages manifest.
- No supported-markets manifest.
- Source IDs are inconsistent in current output.
- Status enums are not fully normalized.
- No explicit market/country fields in current app-facing price records.
- No per-card explicit `no_result`/error records in current app-facing price files.
- No search endpoint/index.
- JP normalization is inconsistent.
- No local image binaries.
- `provider-catalog` remains experimental.
