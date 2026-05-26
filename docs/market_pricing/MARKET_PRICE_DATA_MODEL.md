# Market Price Data Model (Foundation)

This document defines the CardScanR local market pricing foundation for sold-listing style evidence.

Scope in this phase:

- No live eBay scraping.
- No live eBay API calls.
- Only mock/manual providers are enabled.
- Market price outputs are stored separately from existing EN/JP current provider prices.

## Record Scope

A market price record is scoped by:

- game
- language
- canonical card id
- set id
- collector number
- variant
- condition
- graded state
- market country
- currency
- source + source provider

## Required Record Fields

Each normalized record supports:

- game
- language
- canonicalCardId
- setId
- setName
- collectorNumber
- cardName
- variant
- condition
- gradedState
- marketCountry
- currency
- source
- sourceProvider
- sampleCount
- medianPrice
- averagePrice
- lowPrice
- highPrice
- shippingIncluded
- soldDateRange (from/to)
- evidenceListingLinks
- confidenceScore (0 to 1)
- confidenceLabel (low/medium/high)
- outlierFilteringNotes
- lastUpdatedAtUtc
- status

Status values:

- priced
- no_results
- insufficient_data
- stale
- error
- unavailable

## Separation from Existing Pricing

Market prices are intentionally separate from current provider prices:

- Existing EN/JP current prices remain under public/v1/prices/current.
- Market/sold listing prices are stored under public/v1/markets/prices.
- This avoids accidental overwrite of current app pricing baselines.

## Foundation Provider Rules

- mock provider: deterministic synthetic sold listings for test cards.
- manual provider: optional evidence JSON import from data/manual_market_prices/sample_market_sold_listings.json.
- live eBay worker: disabled until legal/terms review is complete.

## Worker Outputs

Report outputs:

- reports/market_pricing_worker_latest.json
- reports/market_pricing_worker_latest.md
- reports/market_pricing_jobs_latest.json

Optional write output (--write, mock/manual only):

- public/v1/markets/prices/{market}/{game}/{language}/{setId}.json

## Query Safety Rules

Query generation includes card identity terms and excludes common noise terms:

- proxy
- custom
- fake
- digital
- lot
- bundle
- damaged (unless damaged condition is selected)

These rules are for safe query construction only. They do not enable live marketplace access.
