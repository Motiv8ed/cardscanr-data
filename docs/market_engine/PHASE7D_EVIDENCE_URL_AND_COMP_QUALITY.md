# Phase 7D eBay Evidence URL and Comp Quality Hardening

Phase 7D keeps the local eBay browser provider explicitly gated:

```powershell
$env:MARKET_LOOKUP_PROVIDER = "ebay_browser"
$env:ENABLE_EBAY_REAL_LOOKUP = "true"
```

The default provider remains `mock`.

## Evidence URLs

The provider stores evidence `listing_url` only when the parsed href is a valid HTTPS eBay `/itm/` URL. Relative item hrefs are normalized to the selected provider domain. Tracking parameters are removed and the item ID is preserved.

Evidence raw metadata includes:

- `url_quality`
- `item_id`
- `original_href`
- `normalized_listing_url`
- `provider_domain`

Generic marketplace/search URLs are diagnostic-only and are not stored as evidence item links.

## Comp Quality

Raw single-card lookups reject graded, sealed, bundle/lot, variation, pick-your-card, digital, proxy/custom, oversized, conflicting collector-number, conflicting explicit set-code, wrong-card-name, and multi-number listings.

Snapshots include:

- `price_spread_ratio`
- `confidence_warnings`
- `included_price_distribution`
- `url_quality_counts`

Evidence `raw_json.compQuality` includes match, shipping, outlier, URL-quality, and inclusion diagnostics.

## Controlled Debug Lookup

Run one local provider lookup without writing to Supabase:

```powershell
.\scripts\debug_ebay_browser_provider.ps1 -Headed -Market AU -Currency AUD -CardName "Charizard ex" -CollectorNumber "125/197" -SetName "Obsidian Flames"
```

Inspect:

- `reports/ebay_browser_debug/latest/debug_summary.json`
- `reports/ebay_browser_debug/latest/screenshot.png`

## Controlled Live Write Smoke

Run exactly one suspicious-card write smoke:

```powershell
$env:CONFIRM_LIVE_EBAY_WRITE = "true"
.\scripts\run_ebay_browser_live_write_smoke.ps1 -ForceRefresh -Market AU -Currency AUD -CardName "Charizard ex" -CollectorNumber "125/197" -SetName "Obsidian Flames"
```

Inspect:

- `reports/ebay_browser_live_write_smoke_latest.json`
- `reports/ebay_browser_debug/latest/debug_summary.json`

Create a shareable redacted upload bundle:

```powershell
.\scripts\create_market_engine_upload_bundle.ps1 -Kind ebay_browser_live_write_smoke
```
