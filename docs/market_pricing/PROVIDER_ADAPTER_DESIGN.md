# Provider Adapter Design

This document describes the market price provider adapter architecture for CardScanR.

---

## Overview

The provider adapter layer sits between the market pricing worker and any concrete
evidence source.  It defines shared contracts so that future providers can plug in
without rewriting the worker.

```
market_pricing_worker.py
        │
        ▼
MarketPriceProviderRegistry
        │
        ├── MockMarketPriceProvider        (enabled)
        ├── ManualMarketPriceProvider      (enabled)
        └── DisabledEbayMarketPriceProvider (disabled — placeholder)
```

---

## Evidence Model

All providers return evidence as `MarketPriceEvidenceListing` instances
(defined in `tools/market_pricing_provider_contracts.py`).

Key fields:

| Field | Type | Notes |
|---|---|---|
| `title` | str | Listing title, normalised |
| `sold_price` | float | Price item sold for |
| `shipping_price` | float | Shipping cost (0 if free) |
| `total_price` | float | `sold_price + shipping_price` |
| `currency` | str | ISO-4217 upper-case (AUD, USD, GBP, CAD) |
| `sold_date` | str | ISO-8601 UTC string |
| `listing_url` | str | URL to original listing |
| `marketplace` | str | Canonical slug (EBAY_AU, EBAY_US, …) |
| `condition` | str | Normalised condition string |
| `graded` | bool | Whether listing is a graded slab |
| `source_provider` | str | Provider name tag |
| `raw_provider_id` | str? | Provider's own listing ID |
| `raw_data` | dict? | Scrubbed copy of raw row (no secrets) |

---

## Provider Contracts

All contracts are in `tools/market_pricing_provider_contracts.py`.

### `MarketPriceSearchRequest`

Describes a single pricing look-up.  Fields cover card identity, market,
currency, query text, exclusion terms, and optional date range.

### `MarketPriceProviderResult`

Successful response: provider name, source, list of `MarketPriceEvidenceListing`,
notes, and optional raw metadata.

### `MarketPriceProviderError`

Structured error payload with error code, message, and flags for whether
the network was attempted and whether the error is safe for cloud.

### `MarketPriceProviderCapabilities`

Static declaration of what a provider can do: supported markets, currencies,
languages, whether it needs live network or secrets, etc.

---

## How Mock and Manual Providers Work

### Mock Provider (`tools/market_price_providers/mock_provider.py`)

- Always enabled.
- Returns deterministic fake listings seeded from a SHA-256 hash of the request fields.
- No network calls.  No secrets.  Safe for CI/CD and Codex.
- Useful for unit tests, smoke tests, and worker dry-runs.

### Manual Provider (`tools/market_price_providers/manual_provider.py`)

- Always enabled.
- Reads already-imported sold-listing rows from a local JSON file.
- Matches rows to the search request by canonical card ID, set ID, market, and language.
- Normalises each row via `market_price_evidence_normalizer.normalize_evidence()`.
- No network calls.  No secrets.  Safe for cloud.

---

## Evidence Normalizer (`tools/market_price_evidence_normalizer.py`)

Handles raw sold-listing dicts from any source and produces clean
`MarketPriceEvidenceListing` instances.

Normalisation steps:

1. **Exclusion term check** — rejects listings whose title contains:
   `proxy`, `custom`, `fake`, `digital`, `lot`, `bundle`, `damaged`
2. **Price** — parses float from strings like `"$15.99"`, `"AU$22.00"`.
3. **Shipping** — missing shipping defaults to `0.0`.
4. **Total price** — `sold_price + shipping_price`.
5. **Currency** — maps symbols (`$`, `£`, `€`, `au$`, …) to ISO codes.
6. **Sold date** — parses many date formats, returns ISO-8601 UTC.
7. **Marketplace** — maps domain substrings to canonical slugs (EBAY_AU, etc.).
8. **Condition** — maps natural-language condition strings to snake_case.
9. **Graded detection** — checks `graded` field or scans title for PSA/BGS/CGC/etc.
10. **Secret scrubbing** — removes `apiKey`, `token`, `secret`, `bearer`, etc.
    from `raw_data` before storage.

Returns `(listing, None)` on success or `(None, reject_reason)` on rejection.

---

## Why Live eBay is Disabled

Live eBay scraping / API access is **not implemented** and is blocked by design:

- The `DisabledEbayMarketPriceProvider` always raises `DisabledProviderError`
  before making any network call.
- The `MarketPriceProviderRegistry` blocks any provider name containing
  `ebay`, `apify`, `browser`, or `live` from being resolved unless
  `_force_allow_live=True` is passed (no such path exists today).
- The reason: legal/terms review is required before any scraping or API access.

---

## Future Provider Options

When legal/terms approval is obtained, one of the following paths can be taken.

### Option A — eBay Browse API (Official)

- Requires eBay developer account and OAuth credentials.
- Implement a new `EbayApiMarketPriceProvider` in `tools/market_price_providers/`.
- Add it to the registry with `_force_allow_live=True` only in production.
- Credentials must be stored in environment variables (never committed).

### Option B — Apify eBay Scraper Actor

- Requires an Apify account and API token.
- Implement a new `ApifyEbayMarketPriceProvider`.
- Actor runs in Apify cloud; no local browser automation needed.
- Token stored in environment variable.

### Option C — Local Browser Worker

- Uses Playwright or Puppeteer running locally or in a dedicated VM.
- Implement a new `LocalBrowserEbayMarketPriceProvider`.
- Not suitable for shared CI/CD.  Legal/ToS review required.

### Option D — Manual CSV / JSON Import

- Already available via `tools/import_manual_sold_listings.py`.
- Operator manually exports sold listings and drops them in
  `data/manual_market_prices/`.
- The manual provider adapts these files into the evidence pipeline.

---

## Legal and Terms Caution

> **Do not scrape eBay without explicit legal/terms approval.**
>
> eBay's ToS prohibit automated scraping.  The official Browse API requires
> registration and has rate limits.  Apify and browser automation may also
> violate ToS if used in ways that simulate a human buyer.
>
> Until a compliant access method is chosen and reviewed, all live eBay
> access must remain disabled in this codebase.

---

## How This Plugs Into the Worker

The worker (`tools/market_pricing_worker.py`) currently uses
`MockMarketListingsProvider` and `ManualMarketListingsProvider` from
`market_pricing_job_queue.py`.

To switch to the new provider adapter layer:

1. Replace `provider_for(args)` in the worker with a call to
   `MarketPriceProviderRegistry.get(args.provider)`.
2. Convert `MarketPriceJob` instances to `MarketPriceSearchRequest` instances.
3. Map `MarketPriceProviderResult.listings` (new) back to `SoldListingEvidence`
   (old) for the existing `aggregate_listings()` call, or refactor the
   aggregator to accept the new evidence type directly.

The refactor is intentionally deferred to keep this phase small and reviewable.
