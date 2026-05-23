# PokeWallet API Capability Audit

- generatedAtUtc: 2026-05-23T21:23:09Z
- apiKeyPresent: yes
- requests: 16 succeeded / 18 attempted
- recommendation: Build a staged /prices importer behind diagnostics-only output, then validate source/currency/status counts before public promotion.

## Endpoint Availability

| Endpoint | HTTP | Availability | Usable prices | Notes |
|---|---:|---|---|---|
| /health | 200 | available | no |  |
| /sets | 200 | available | no |  |
| /sets/:setCode EN | 200 | available | no |  |
| /sets/:setCode JP | 200 | available | no |  |
| /prices/:setCode EN | 200 | available | yes | pro candidate |
| /prices/:setCode JP | 200 | available | yes | pro candidate |
| /prices/:setCode EN source=tcg | 200 | available | yes | pro candidate |
| /prices/:setCode EN source=cm | 200 | available | yes | pro candidate |
| /cards/:id | 200 | available | yes |  |
| /cards/:id/price-history | 200 | available | yes | pro candidate |
| /sets/trending | 200 | available | yes | pro candidate |
| /analytics/top-cards | 200 | available | yes | pro candidate |

## Price Findings

- /prices works: yes
- JP price availability: usable_prices_found
- CardMarket-only useful: yes
- TCGPlayer USD useful: yes

## Set Metadata Refresh Stage

- /sets available: yes
- fields captured: set_id, set_code, name, language, release_date, card_count
- API samples captured: 8
- ambiguous set-code mappings found: 8
- numeric set-id mapping samples: 8

## Image Endpoint Audit

- attempted: yes
- samples succeeded: 4 / 6
- low image: status=404 type=application/json bytes=326
- high image: status=404 type=application/json bytes=328
- low image: status=200 type=image/jpeg bytes=15727
- high image: status=200 type=image/jpeg bytes=170772
- low image: status=200 type=image/jpeg bytes=17607
- high image: status=200 type=image/jpeg bytes=169114

## Set Logo Cache Plan

- endpoint: /sets/:setCode/image
- fetched in this audit: no
- reason: Not fetched by default because this audit limits image-cache probes to 3 low and 3 high card images.
- candidate en: 604 (BS)
- candidate jp: 23599 (SV2a)

## Integration Plan

Set metadata refresh:
- Match by numeric provider set_id before set_code.
- Treat duplicate language/set_code mappings as ambiguous and require manual review.
- Only fill missing provider metadata fields; do not overwrite better app catalogue names, release dates, card counts, logos, or symbols.
- Keep provider metadata separate from app canonical set metadata until validation approves promotion.

Set logo/image cache:
- Probe set logos through an explicit allowlist before enabling any cache writes.
- Record status code, content type, and size before storing binaries.
- Do not overwrite better existing app set logos or symbols without review.
- Keep downloaded logo binaries ignored by Git.

Price importer:
- Add a staged importer that reads /prices/:numericSetId into a temporary diagnostics file first.
- Import by numeric Pokewallet set_id when available; use set_code only as a reviewed fallback.
- Preserve provider variants and map them into Stage 1 variant/condition fields without collapsing finishes.
- Store TCGPlayer USD and CardMarket EUR as separate source/currency records.
- Do not convert currencies until a validated conversion system exists.
- Do not fabricate missing market, low, high, or history values.
- Mark JP current pricing unavailable when the endpoint returns no usable JP price records.
- Only promote into public/v1/prices/current after validate_cache and focused count/source/status checks pass.
