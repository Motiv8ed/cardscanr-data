# Phase 5B: Live eBay Provider Validation

Phase 5B keeps the real eBay browser provider in a controlled validation mode. The mock provider remains the default, scheduler live eBay processing stays disabled, and real lookups still require both:

- `MARKET_LOOKUP_PROVIDER=ebay_browser`
- `ENABLE_EBAY_REAL_LOOKUP=true`

The browser provider uses locally installed Google Chrome with the dedicated CardScanR profile at `D:\cardscanr-data\.browser_profiles\cardscanr`. It does not use the personal/default Chrome profile.

## Market Scope

Current eBay mode is marketplace pricing:

- AU means `ebay.com.au`, `AUD`, and listings visible to Australian buyers.
- US means `ebay.com`, `USD`, and listings visible to US buyers.
- GB means `ebay.co.uk`, `GBP`, and listings visible to GB buyers.
- CA means `ebay.ca`, `CAD`, and listings visible to Canadian buyers.

Marketplace pricing can include international sellers shipping into that buyer market. That is acceptable in this phase because those listings still influence what a buyer in that market can see and pay. Domestic-sellers-only mode is reserved for a future pass and is represented by the optional `EBAY_MARKET_SCOPE` config, which currently supports `marketplace`.

## Provider-Only Debug

Run one AU lookup without writing to Supabase:

```powershell
.\scripts\debug_ebay_browser_provider.ps1 -Headed -Market AU -Currency AUD -CardName "Charizard ex" -CollectorNumber "125/197" -SetName "Obsidian Flames"
```

Debug artifacts are written under:

- `reports/ebay_browser_debug/latest/page.html`
- `reports/ebay_browser_debug/latest/screenshot.png`
- `reports/ebay_browser_debug/latest/debug_summary.json`
- `reports/ebay_browser_debug/runs.jsonl`

If `resultCount=0` while the screenshot shows listings, inspect `candidate_selector_counts`, `visible_result_text_sample`, and `parser_errors` in `debug_summary.json`.

## Market Matrix Debug

Run provider-only validation across AU/US/GB/CA:

```powershell
.\scripts\debug_ebay_browser_market_matrix.ps1 -Headed -Markets AU,US,GB,CA -MaxResults 30 -PauseBetweenMarketsSeconds 20
```

The wrapper preserves the comma-separated market list and passes `--markets AU,US,GB,CA` to Python. Quoted input also works:

```powershell
.\scripts\debug_ebay_browser_market_matrix.ps1 -Headed -Markets "AU,US,GB,CA" -MaxResults 30 -PauseBetweenMarketsSeconds 20
```

Reports are written to:

- `reports/ebay_browser_market_matrix_latest.json`
- `reports/ebay_browser_market_matrix_runs.jsonl`
- `reports/ebay_browser_debug/market_matrix/latest/<market>/`
- `reports/chatgpt_uploads/ebay_browser_market_matrix_latest.zip`

Each market report includes the provider domain, marketplace id, search URL, selector counts, first sanitized listings, block/captcha status, and a quality summary. The quality summary counts likely useful comps, price ranges, missing prices, international-origin hints, pick-your-card listings, bundle/lot listings, graded listings, sealed listings, currency mismatches, structured price usage, fallback price usage, and rejected non-price percentage/feedback numbers.

## One-Card Live Write Smoke

The live write smoke processes exactly one selected card/market through the normal request/queue/worker pipeline. It uses `request_market_price_refresh(...)`, so a fresh cache returns `cache_fresh` and no forced refresh is created.

```powershell
$env:CONFIRM_LIVE_EBAY_WRITE = "true"
.\scripts\run_ebay_browser_live_write_smoke.ps1 -Market AU -Currency AUD -CardName "Charizard ex" -CollectorNumber "125/197" -SetName "Obsidian Flames"
```

By default this respects `request_market_price_refresh(...)` cooldowns. If the cache is fresh, the smoke report will set:

- `live_lookup_performed = false`
- `used_cached_result = true`
- `pricing_model_validated = false`

For backend-only validation with service-role credentials, force a new refresh explicitly:

```powershell
$env:CONFIRM_LIVE_EBAY_WRITE = "true"
.\scripts\run_ebay_browser_live_write_smoke.ps1 -ForceRefresh -Market AU -Currency AUD -CardName "Charizard ex" -CollectorNumber "125/197" -SetName "Obsidian Flames"
```

`-ForceRefresh` is not the default and does not change normal app/user cooldown behavior. If Supabase rejects the forced refresh, inspect the smoke report error and PostgREST response body.

Reports are written to:

- `reports/ebay_browser_live_write_smoke_latest.json`
- `reports/ebay_browser_live_write_smoke_runs.jsonl`
- `reports/chatgpt_uploads/ebay_browser_live_write_smoke_<timestamp>.zip`

The report includes the refresh RPC action, job id, worker result, cache summary, evidence counts, included comps, rejected comps, cooldown data, and sanitized market/provider information.

### RPC 404 Troubleshooting

If the live write smoke fails with `/rest/v1/rpc/request_market_price_refresh` returning `404`:

- Confirm the `public.request_market_price_refresh` migration has been applied to the Supabase project.
- Confirm the Python RPC payload uses the SQL argument names: `p_game`, `p_card_name`, `p_normalized_card_name`, `p_set_name`, `p_set_code`, `p_collector_number`, `p_language`, `p_variant`, `p_condition`, `p_market_country`, `p_currency`, `p_fingerprint`, `p_reason`, and `p_force_refresh`.
- Inspect the PostgREST response body in the smoke report or console output. `PGRST202` usually means PostgREST cannot find a function matching the supplied argument names or its schema cache has not refreshed.
- Do not include Supabase keys or local env files when sharing diagnostics.

## Safety

Do not run the scheduler with the live provider yet. Bulk live worker mode requires `CONFIRM_LIVE_EBAY_WORKER=true` when `MARKET_LOOKUP_PROVIDER=ebay_browser`, and should stay reserved for controlled operator runs.

Shared cache cooldown and `request_market_price_refresh(...)` protect the local browser provider from repeated user refreshes. If a matching card/market cache is still fresh, the app receives cache/cooldown information instead of causing another eBay lookup.

The provider does not bypass captcha or block pages. If eBay shows captcha, verification, robot, unusual traffic, access denied, or blocked-page text, the provider raises a block error with safe diagnostics.

## ChatGPT Upload Bundles

Debug and smoke wrappers create sanitized upload bundles after successful runs. If automatic bundling fails, the wrapper prints the manual command and the original debug/smoke run still succeeds.

Manual bundle commands:

```powershell
.\scripts\create_market_engine_upload_bundle.ps1 -Kind ebay_browser_market_matrix
.\scripts\create_market_engine_upload_bundle.ps1 -Kind ebay_browser_debug
.\scripts\create_market_engine_upload_bundle.ps1 -Kind ebay_browser_live_write_smoke
.\scripts\create_market_engine_upload_bundle.ps1 -Kind market_price_engine_smoke
```

HTML is excluded by default. Include it only when needed for parser debugging:

```powershell
.\scripts\create_market_engine_upload_bundle.ps1 -Kind ebay_browser_market_matrix -IncludeHtml
```

Safe to upload:

- `debug_summary.json`
- `screenshot.png`
- sanitized latest report JSON
- small sanitized JSONL run logs
- `bundle_manifest.json`

Never upload:

- `.browser_profiles/`
- `supabase_env.local.json`
- `.env` or `.env.local`
- Chrome cookies, local storage, or Login Data
- files or fields containing keys, tokens, secrets, passwords, authorization headers, or cookies

The bundle script redacts secret-like JSON fields and skips blocked paths. Prefer screenshot plus `debug_summary.json` over full `page.html`.
