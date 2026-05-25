# Market Price Engine — Market Locale Routing

## Why `market_country` and `currency` are part of price identity

Market prices are local-market facts, not global facts.  
The same card can have different sold prices in AU vs US vs GB, so CardScanR includes `market_country` and `currency` in the `market_price_keys.fingerprint`.

That keeps cache/snapshot/evidence rows scoped to the user’s selected market and prevents accidental cross-country price reuse.

## AU vs US vs GB are separate caches

Example (same card identity except market):

- `pokemon|en|smoke-test|001/999|smoke_test_charizard_ex|raw|raw|au|aud`
- `pokemon|en|smoke-test|001/999|smoke_test_charizard_ex|raw|raw|us|usd`
- `pokemon|en|smoke-test|001/999|smoke_test_charizard_ex|raw|raw|gb|gbp`

These map to different `market_price_keys.id` values, so scheduler dedupe/active-job checks stay per key/market.

## Supported eBay marketplace routing (current)

| market_country | currency | marketplace | provider_marketplace_id | provider_domain | search_locale | display_name |
|---|---|---|---|---|---|---|
| AU | AUD | ebay | EBAY_AU | ebay.com.au | en-AU | Australia |
| US | USD | ebay | EBAY_US | ebay.com | en-US | United States |
| GB | GBP | ebay | EBAY_GB | ebay.co.uk | en-GB | United Kingdom |
| UK | GBP | ebay | EBAY_GB (alias to GB) | ebay.co.uk | en-GB | United Kingdom |
| CA | CAD | ebay | EBAY_CA | ebay.ca | en-CA | Canada |
| DE | EUR | ebay | EBAY_DE | ebay.de | de-DE | Germany |
| FR | EUR | ebay | EBAY_FR | ebay.fr | fr-FR | France |
| IT | EUR | ebay | EBAY_IT | ebay.it | it-IT | Italy |
| ES | EUR | ebay | EBAY_ES | ebay.es | es-ES | Spain |

## Flutter market selection guidance

Flutter should send user-selected `market_country` and `currency` from onboarding/profile settings into market price requests.

- Country/currency should be explicit user state, not inferred server-side.
- Codes should use ISO-style values (`AU`, `US`, `GB`, `AUD`, `USD`, `GBP`).
- If a user changes market settings, requests should use the new values so a separate local cache key is used.

## Unsupported country policy

Unsupported market routes must fail clearly and cleanly.  
They must **not** silently fall back to US (`EBAY_US`) because silent fallback can write wrong-market prices to cache.

## Future provider requirements

Any future provider adapter must:

1. resolve and accept locale routing (`market_country`, `currency`, `marketplace`, `provider_marketplace_id`, `provider_domain`, `search_locale`, `display_name`)
2. return market-aware diagnostics and evidence URLs for the resolved domain
3. keep deterministic mock/testing behavior separated by market fingerprint
4. fail unsupported routes explicitly (no hidden global fallback)
