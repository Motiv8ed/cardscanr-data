# Phase 7E Variant-Aware Market Price Filtering

Market-price fingerprints and backend filtering support these ungraded print variants:

- `raw`
- `non_holo`
- `holo`
- `reverse_holo`

`raw` remains the broad compatibility fallback when the card finish is unknown. Existing `raw|raw` cache keys are unchanged.

Aliases such as `non-holo`, `regular`, `normal`, `reverse`, `reverse holo`, and `rev holo` normalize to the stable variant values above.

## Query and Filtering

- `non_holo` excludes holo and reverse terms and rejects explicit holo/reverse-holo listings.
- `reverse_holo` includes `reverse holo` in the provider query and rejects listings without a reverse-holo indicator.
- `holo` includes `holo`, excludes reverse terms where possible, and rejects reverse-holo or weak plain-title matches.
- `raw` keeps the broad ungraded lookup behavior while recording detected variant diagnostics.

Evidence `raw_json.compQuality` includes:

- `requested_variant`
- `detected_variant`
- `variant_match`
- `variant_warning`

Variant-specific estimates with fewer than five reliable included comps include the snapshot warning `insufficient_variant_specific_comps`.

## Espurr Non-Holo Live Write Smoke

Run exactly one controlled refresh:

```powershell
$env:MARKET_LOOKUP_PROVIDER = "ebay_browser"
$env:ENABLE_EBAY_REAL_LOOKUP = "true"
$env:CONFIRM_LIVE_EBAY_WRITE = "true"
.\scripts\run_ebay_browser_live_write_smoke.ps1 -ForceRefresh -Market AU -Currency AUD -CardName "Espurr" -CollectorNumber "036/086" -SetName "Chaos Rising" -Variant non_holo -Condition raw
```

Inspect:

- `reports/ebay_browser_live_write_smoke_latest.json`
- `reports/ebay_browser_debug/latest/debug_summary.json`

Create the redacted upload bundle:

```powershell
.\scripts\create_market_engine_upload_bundle.ps1 -Kind ebay_browser_live_write_smoke
```
