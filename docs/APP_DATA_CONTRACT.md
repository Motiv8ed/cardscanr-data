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
- `/v1/supported-languages.json`
- `/v1/supported-markets.json`
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

`/v1/supported-sources.json` source entries use this shape:

```json
{
  "id": "pokemon_tcg_api",
  "aliases": ["pokemonTcgApi"],
  "description": "Pokémon TCG API market prices",
  "enabled": true
}
```

Rules:

- `id` is canonical and must be lowercase `snake_case`.
- `aliases` is an array of accepted legacy identifiers for app/client fallback matching.
- App matching order is: exact canonical `id`, then any `aliases`.
- During migration, legacy aliases must be retained for at least one app release cycle before removal.

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
3. Fetch `/v1/supported-languages.json` and `/v1/supported-markets.json` to determine which languages and markets are available, beta, or planned.
4. Filter onboarding and settings UI to `visibility: "public"` entries; show `visibility: "beta"` with a disclaimer badge.
5. Fetch sets per game/language.
6. Fetch cards by `setId`; filter locally by `collectorNumber` + `normalizedName`.
7. Fetch EN prices by `setId`; filter locally by `collectorNumber` + `normalizedName` + `variant` + `condition`.
8. Treat missing price file as dataset unavailable.
9. Treat missing card in a present price file as “no price for that card.”
10. Do not overwrite an existing valid local price with missing/error/unavailable values.
11. For markets where `pricingStatus` is `"planned"` or `"unavailable"`, show "Pricing not available in your region yet" — never show a broken/error state.
12. If `supported-languages.json` or `supported-markets.json` cannot be fetched, fall back to hardcoded defaults: EN/USD available, all others planned.
13. Use JP price status as unavailable.
14. Use `imageSmall` / `imageLarge` directly.

## 11) Near-term contract gaps

- Status enums are not fully normalized.
- No explicit market/country fields in current app-facing price records.
- No per-card explicit `no_result`/error records in current app-facing price files.
- No search endpoint/index.
- JP normalization is inconsistent.
- No local image binaries.
- `provider-catalog` remains experimental.

## 12) Stage 1 EN price contract rollout

EN current price records now include both legacy and new identity fields for backward compatibility:

- Legacy: `canonicalId` (kept for existing app readers)
- New: `canonicalCardId`, `priceIdentityId`

Current EN records also include additive market/currency/status metadata fields:
`market`, `country`, `sourceCurrency`, `targetCurrency`, `conversionPolicy`,
`status`, `confidence`, and compact `diagnostics`.

Compact example record:

```json
{
  "canonicalId": "pokemon|en|base1|4|charizard|holo|near_mint",
  "canonicalCardId": "pokemon|en|base1|4|charizard",
  "priceIdentityId": "pokemon|en|base1|4|charizard|holo|near_mint|us|usd",
  "setId": "base1",
  "collectorNumber": "4",
  "normalizedName": "charizard",
  "variant": "holo",
  "condition": "near_mint",
  "market": "us",
  "country": "US",
  "currency": "USD",
  "sourceCurrency": "USD",
  "targetCurrency": "USD",
  "conversionPolicy": "none",
  "status": "priced",
  "confidence": "medium",
  "source": "pokemon_tcg_api",
  "marketPrice": 123.45,
  "lowPrice": 100.0,
  "highPrice": 150.0,
  "fetchedAtUtc": "2026-05-19T23:41:51Z",
  "nextExpectedPriceUpdateAtUtc": "2026-05-20T08:41:51Z",
  "staleness": {
    "ageSeconds": 0,
    "freshForSeconds": 86400,
    "staleAfterSeconds": 172800,
    "status": "fresh"
  },
  "diagnostics": {
    "sourceRecordStatus": "priced",
    "notes": []
  }
}
```

## 13) supported-languages.json and supported-markets.json

### `/v1/supported-languages.json`

Describes which game/language combinations are available, beta, or planned.

**Top-level fields:** `schemaVersion`, `generatedAtUtc`, `languages` (array)

**Per-entry fields:**

| Field | Type | Description |
|---|---|---|
| `game` | string | Matches `id` in `supported-games.json` |
| `language` | string | Lowercase code (`en`, `jp`); matches path segments in `catalog/` and `prices/` |
| `displayName` | string | English display name for app UI |
| `nativeName` | string | Display name in the native language |
| `enabled` | boolean | Whether the app should surface this language at all |
| `visibility` | string | `"public"` \| `"beta"` \| `"internal"` \| `"hidden"` |
| `catalogueStatus` | string | `"available"` \| `"partial"` \| `"unavailable"` \| `"planned"` |
| `pricingStatus` | string | `"available"` \| `"partial"` \| `"unavailable"` \| `"planned"` |
| `defaultMarket` | string | The `market` key from `supported-markets.json` to use by default |
| `defaultCurrency` | string | ISO 4217 currency code |
| `notes` | array | Human-readable caveats for app and developer consumption |

**App visibility rules:**
- `visibility: "public"` → show without qualification
- `visibility: "beta"` → show with "Preview" or "Beta" badge; display `notes`
- `visibility: "internal"` / `"hidden"` → do not render

### `/v1/supported-markets.json`

Describes which pricing markets are available, planned, or hidden.

**Top-level fields:** `schemaVersion`, `generatedAtUtc`, `markets` (array)

**Per-entry fields:**

| Field | Type | Description |
|---|---|---|
| `market` | string | Lowercase stable key, used as a path segment and in `priceIdentityId` |
| `country` | string \| null | ISO 3166-1 alpha-2 code, or `null` for multi-country regions |
| `countryName` | string | Human-readable name |
| `currency` | string | ISO 4217 code |
| `enabled` | boolean | Whether the app should allow users to select this market |
| `visibility` | string | `"public"` \| `"beta"` \| `"planned"` \| `"hidden"` |
| `pricingStatus` | string | `"available"` \| `"partial"` \| `"unavailable"` \| `"planned"` |
| `supportedSources` | array | Canonical `id` values from `supported-sources.json` |
| `ebayDomain` | string \| null | eBay region domain when applicable, else `null` |
| `notes` | array | Developer and app-facing caveats |

**Coherence rule:** `enabled: true` must never be combined with `pricingStatus: "planned"` — that would be a lie to the app.

### Builder refresh policy

`generatedAtUtc` is refreshed on every build. All other fields are human-curated via:
- `data/supported_languages_config.json`
- `data/supported_markets_config.json`

The builder derives `pricingStatus` from live `prices/current/{game}/{language}/status.json` and `catalogueStatus` from `catalog/{game}/{language}/sets.json`, but never auto-promotes `visibility` or `enabled`.
