# Phase 5D: Price Model Hardening

Phase 5D separates two price views for live eBay comps:

- Item price: the sold card price only, excluding shipping or delivery.
- Landed price: the sold card price plus shipping or delivery.

CardScanR's main card ownership valuation now uses item price. This means `recommended_price` and `current_market_price` represent the card/item market value, not the buyer's delivered replacement cost.

Landed price is still useful, especially for Australian marketplace results where overseas sellers may show a lower card price and higher delivery. It helps answer what an Australian buyer may typically pay to replace the card from eBay AU-visible listings.

Future Flutter wording should treat these as separate values:

- Main: `Current market value: $X AUD`
- Secondary: `Typical delivered eBay AU cost: $Y AUD`

## Storage Policy

Existing cache and snapshot columns remain compatible:

- `median_price`
- `average_price`
- `low_price`
- `high_price`
- `recommended_price`
- `current_market_price`

These compatibility fields now use item-price statistics.

Landed-price statistics are stored in snapshot `diagnostics_json.priceViews`:

- `itemPrice.median`
- `itemPrice.average`
- `itemPrice.low`
- `itemPrice.high`
- `itemPrice.recommended`
- `landedPrice.median`
- `landedPrice.average`
- `landedPrice.low`
- `landedPrice.high`
- `landedPrice.recommended`
- `priceBasis = item_price`
- `landedPriceAvailable = true`

## Comp Quality

Evidence diagnostics classify each comp with:

- `exact_card_match`
- `likely_same_card`
- `variation_listing`
- `sealed_or_pack`
- `graded_when_raw`
- `currency_mismatch`
- `possible_outlier_item_price`
- `possible_outlier_landed_price`
- `included`

Outlier filtering now compares item price against item-price distribution. Landed-price outliers are diagnostic, not automatic rejection. Exact low/free-delivery comps should not be rejected just because other listings have high delivery charges.

Variation and pick-your-card listings remain rejected because they are not clean single-card comps.

## Live Write Smoke

The one-card live write smoke report includes both price views in `cache_price_summary`:

- `item_recommended_price`
- `item_median_price`
- `item_low_price`
- `item_high_price`
- `landed_recommended_price`
- `landed_median_price`
- `landed_low_price`
- `landed_high_price`
- `price_basis = item_price`
- `landed_price_available = true`

If the smoke returns `request_market_price_refresh.action = cache_fresh`, it used the existing cache and did not validate a new Phase 5D calculation. Rerun with service-role credentials and `-ForceRefresh`:

```powershell
$env:CONFIRM_LIVE_EBAY_WRITE = "true"
.\scripts\run_ebay_browser_live_write_smoke.ps1 -ForceRefresh -Market AU -Currency AUD -CardName "Charizard ex" -CollectorNumber "125/197" -SetName "Obsidian Flames"
```
