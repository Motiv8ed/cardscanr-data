# eBay Browser Provider Audit

Audit date: 2026-06-30

## Scope

This audit covers the existing `ebay_browser` provider used by the CardScanR backend market engine. It intentionally uses Andrew's controlled backend-machine browser session for the MVP/closed-beta pricing workflow.

## Provider Entry Point

- Factory entry point: `cardscanr_market_engine.providers.factory.create_market_comps_provider`
- Provider class: `cardscanr_market_engine.providers.ebay_browser_provider.EbayBrowserSoldCompsProvider`
- Worker path: `workers/market_price_worker.py` -> `MarketPriceJobRunner` -> provider `fetch_comps`

## Browser Engine

- Engine: installed Google Chrome via Playwright Chromium channel `chrome`
- Bundled Chromium fallback is intentionally disabled.
- Headless mode is controlled by `EBAY_BROWSER_HEADLESS`.

## Browser Profile / Session

- Profile name must be `cardscanr`.
- Default user-data directory is the dedicated repo profile under `.browser_profiles/cardscanr`.
- Personal Chrome profile paths are rejected.
- Cookies, tokens, passwords, full profile contents, and personal account identifiers are not logged or exported.

## Authentication / Session Handling

- The provider relies on Andrew's existing controlled browser session when eBay requires account context.
- The provider does not perform credential entry.
- Authentication-required pages are classified as `authentication_required`.
- CAPTCHA, verification, and access-block states stop the lookup and are not bypassed.

## Marketplace Selection

- Backend policy owns marketplace order.
- Australian default: `EBAY_AU`, then `EBAY_US`, `EBAY_GB`, `EBAY_CA`.
- `MarketPriceJobRunner.fetch_fallback_result` attempts home market first and records fallback attempts.
- Fallback stops when enough trustworthy evidence exists.
- Fallback also stops immediately on challenge, access block, or authentication-required state.

## Search URL Construction

- Query construction is in `providers/query_builder.py`.
- Query variants include card name or safe English alias, collector number, language, set code/name where useful, variant, and Pokemon context.
- Query ladder is bounded by `EBAY_BROWSER_MAX_QUERY_ATTEMPTS` with a default cap of five.
- Sold/completed search URLs must include eBay completed/sold flags before parsing proceeds.

## Completed / Sold Filtering

- The current live provider is a sold/completed provider.
- It validates that `LH_Sold=1` and `LH_Complete=1` are present in the search URL.
- Returned evidence is persisted as completed-sale evidence by the job runner.

## Active Listing Filtering

- Active-listing provider mode is not the current browser-provider path.
- Existing Flutter wording distinguishes completed-sale and active-listing evidence when metadata supplies the evidence type.

## Result Extraction

- Candidate extraction uses structured selectors and link/title/price/shipping/date fields.
- Listing URLs are normalized to direct item URLs with tracking/query parameters removed.
- Item price and shipping are parsed separately, and total landed comparable cost is retained.

## Pagination

- The current provider uses bounded query variants rather than page-by-page pagination.
- `max_results` caps parsed results per query.

## Throttling / Concurrency

- Provider lookups are serialized by a process-level lookup lock.
- Request spacing uses the maximum of cooldown and minimum delay settings.
- MVP concurrency is fixed at one; configs above one are rejected.

## Caching

- Cache-first behavior is owned by the scheduler/request layer and cache rows.
- Provider config exposes `EBAY_BROWSER_CACHE_FIRST=true` as the intended operating rule.
- Cache TTLs exist for confidence states and no-comps states; provider-error and challenge cache windows are documented in config diagnostics.

## Retry Behavior

- Query retries are bounded and only advance through the query ladder.
- Timeouts may try the next safe query variant.
- Challenge/access/auth states are terminal for the current lookup and do not fall through to marketplace fallback.

## Challenge / Block Detection

Classified outcomes:

- `success`
- `no_results`
- `authentication_required`
- `challenge_detected`
- `access_blocked`
- `provider_unavailable`
- `timeout`
- `parsing_failure`

The provider detects CAPTCHA, human verification, robot/security challenge, access denied, unusual traffic, login/session-expired, consent/interstitial, maintenance/error, no-results, and normal result pages.

## Timeout Handling

- Browser launch, page navigation, network-idle wait, result-container wait, and parsing stages are timed.
- Timeout diagnostics include stage names and selector counts.
- Full HTML is not included in upload bundles by default.

## Browser Cleanup

- Each Playwright persistent context is closed in `finally`.
- The dedicated profile is reused across runs.

## Evidence Retained

- Normalized item URL, listing title, item price, shipping price, total price, currency, sold date where available, condition text, match/rejection metadata, marketplace metadata, source/display currency metadata, and safe diagnostics.

## Credentials / Session Data Retained

- No cookies, tokens, passwords, or browser-profile files are retained in market evidence.
- Diagnostic sanitization redacts secret-like fields.
- Upload-bundle tests confirm browser profiles and HTML are excluded by default.

## Current Risks

- eBay page structure can change and cause parsing failures.
- CAPTCHA or access challenges can interrupt live lookup; the provider now stops safely.
- Static currency rates require explicit configuration and review.
- Current browser path is backend-local and suitable for MVP/closed-beta, not broad customer-device execution.
- Active-listing browser mode remains future work if current listings are needed from the same provider.
