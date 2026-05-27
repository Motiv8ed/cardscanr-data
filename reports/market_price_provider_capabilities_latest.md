# Market Price Provider Capabilities

Generated: 2026-05-27T01:24:34Z

> **Live eBay scraping enabled: no**  
> Live eBay access is disabled until provider/legal/terms approach is approved.

## Summary

- Registered: ebay_disabled, manual, mock
- Enabled: mock, manual
- Disabled: ebay_disabled
- Default allowed: mock, manual

### Next recommended provider step

Decide on eBay access method: (a) eBay Browse API with OAuth, (b) Apify actor, (c) local browser worker. Then implement as a new provider module and add to the registry allow-list after legal/terms sign-off.

## Provider details

### mock — ✅ enabled

- Live network required: no
- Secrets required: no
- Safe for cloud/Codex: yes
- Returns evidence listings: yes
- Returns confidence score: yes
- Supported markets: AU, US, GB, CA, EU
- Supported languages: en, jp
- Supported currencies: AUD, USD, GBP, CAD, EUR
- Next step: Already functional. Extend fixture cards if needed.
- Notes: Returns deterministic fake evidence seeded from the search request fields.

### manual — ✅ enabled

- Live network required: no
- Secrets required: no
- Safe for cloud/Codex: yes
- Returns evidence listings: yes
- Returns confidence score: no
- Supported markets: AU, US, GB, CA, EU
- Supported languages: en, jp
- Supported currencies: AUD, USD, GBP, CAD, EUR
- Next step: Add real sold-listing CSV/JSON export data to data/manual_market_prices/ and re-run the importer.
- Notes: Reads from a local JSON file — no network required.

### ebay_disabled — 🚫 disabled

- Live network required: yes
- Secrets required: yes
- Safe for cloud/Codex: no
- Returns evidence listings: no
- Returns confidence score: no
- Supported markets: AU, US, GB, CA, EU
- Supported languages: en, jp
- Supported currencies: AUD, USD, GBP, CAD, EUR
- Next step: Choose one: (a) eBay Browse API with OAuth, (b) Apify eBay scraper actor, (c) local browser worker. Obtain legal/terms sign-off, then implement as a new provider module.
- Notes: liveEbayScrapingEnabled: false. Live eBay access is disabled until provider/legal/terms approach is approved.

