# eBay Browser Operation Policy

Policy date: 2026-06-30

## Intended Use

`ebay_browser` is approved for CardScanR MVP/closed-beta pricing validation using Andrew's controlled backend-machine browser session. It must not run on customer Android devices.

## Required Defaults

```text
MARKET_LOOKUP_PROVIDER=ebay_browser
EBAY_BROWSER_ENABLED=true
EBAY_BROWSER_MAX_CONCURRENCY=1
EBAY_BROWSER_MIN_DELAY_SECONDS=20
EBAY_BROWSER_CHALLENGE_STOP=true
EBAY_BROWSER_KILL_SWITCH=false
EBAY_BROWSER_CACHE_FIRST=true
EBAY_BROWSER_MAX_REQUESTS_PER_HOUR=20
EBAY_BROWSER_MAX_REQUESTS_PER_DAY=100
MARKET_CACHE_PROVIDER_ERROR_HOURS=1
MARKET_CACHE_PROVIDER_CHALLENGE_HOURS=12
```

Legacy `ENABLE_EBAY_REAL_LOOKUP=true` remains accepted for existing scripts, but new configuration should use `EBAY_BROWSER_ENABLED=true`.

## Safety Rules

- Use only the dedicated `cardscanr` browser profile.
- Never log cookies, passwords, tokens, authorization headers, or full browser profile paths.
- Process one browser lookup at a time.
- Use cache rows and pending-job checks before opening the browser.
- Do not repeatedly search the same card/market after a no-evidence, provider-error, challenge, or access-block result.
- Stop immediately on CAPTCHA, verification, human-check, auth-required, unusual-traffic, or access-denied pages.
- Do not bypass CAPTCHA.
- Do not rotate identities, proxies, accounts, or user agents to avoid restrictions.
- Do not continue marketplace fallback after challenge, access block, or authentication-required states.
- Store normalized pricing evidence and safe diagnostics only.
- Do not store full HTML unless a short-lived local debug run explicitly requests it.

## Marketplace Policy

Backend owns marketplace selection from the canonical price key
(`market_country` + `currency`). Australian default for *user preference
defaults* must not override an explicit requested market on a job.

Supported live browser routes:

```text
AU / AUD → ebay.com.au (EBAY_AU)
US / USD → ebay.com (EBAY_US)
GB / GBP → ebay.co.uk (EBAY_GB)
CA / CAD → ebay.ca (EBAY_CA)
```

Cross-marketplace fallback is disabled. An AU job must not accept US/UK/CA
comps (even with currency conversion), and a US job must not query
`ebay.com.au`. If eBay redirects to a different marketplace domain, the
provider fails with `provider_marketplace_mismatch`.

Language remains part of search/filter identity and is independent of
marketplace. A Japanese-language card with pricing market AU queries
`ebay.com.au`.

The home marketplace must be attempted. Every attempt must record result
counts, accepted/rejected comparable counts, confidence, no-price reason,
and selected marketplace.

## Cache Policy

Recommended minimum cache periods:

- Valid high-confidence price: 24 hours
- Medium-confidence price: 12 hours
- Low/limited-evidence price: 6 hours
- No-price/no-comps result: 3 hours
- Provider error: 1 hour
- Challenge/access block/auth required: 12 hours before manual retry consideration

## Diagnostics

Use structured stages:

```text
EBAY_BROWSER_STAGE=cache_check
EBAY_BROWSER_STAGE=browser_launch
EBAY_BROWSER_STAGE=marketplace_attempt
EBAY_BROWSER_STAGE=results_loaded
EBAY_BROWSER_STAGE=challenge_detected
EBAY_BROWSER_STAGE=comparables_filtered
EBAY_BROWSER_STAGE=estimate_normalized
EBAY_BROWSER_STAGE=no_price
EBAY_BROWSER_STAGE=complete
```

Safe values include case hash, marketplace, attempt number, result count, accepted/rejected counts, outcome, and duration. Unsafe values must be redacted.

## Kill Switch

Set `EBAY_BROWSER_KILL_SWITCH=true` to disable the provider immediately. The factory and provider config both reject this state.

## Live Validation Limit

For production validation, run exactly one card refresh, claim at most one worker job, use one browser context at a time, and stop on challenge or verification without retry loops.

## Current operational constraint (2026-09-27)

This is **current eBay/browser behaviour**, not a permanent architectural requirement.

### AU sold evidence

- AU sold comps currently require **headed Chrome** using the authenticated CardScanR profile:
  - `EBAY_BROWSER_MODE=headed`
  - `EBAY_BROWSER_HEADLESS=false`
  - `EBAY_BROWSER_USER_DATA_DIR=<repo>/.browser_profiles/cardscanr`
- Headless AU sold navigation currently fails with eBay’s generic **SORRY / Something went wrong** page even when the same UI filter clicks succeed headed.
- Prefer normal navigation:
  1. Homepage warm-up → active search URL (no `LH_Sold` / `LH_Complete` deep-link), or homepage → typed search
  2. Click **Sold items**
  3. Click **Completed items** when available
- Direct sold deep-links (`LH_Sold=1&LH_Complete=1` on first navigation) may return SORRY on ebay.com.au and are **not** the preferred AU path.
- Current flakiness (2026-09-27): after a burst of AU sold navigations, Sold/Completed clicks may intermittently return SORRY or omit the Sold control. This is treated as temporary navigation failure with last-good retention — not CAPTCHA bypass territory.
- If **Sold** succeeds but **Completed** lands on SORRY, the provider recovers to the Sold results URL and continues when sold evidence is visible (Completed is preferred, not mandatory when Sold evidence already exists).
- Production ensure script starts a **single** worker with concurrency 1 and max-jobs 1 so Chrome instances do not fight the profile lock.
- Do **not** create a second competing Chrome profile.
- Do **not** bypass CAPTCHA / human checks. If eBay requests verification, stop and restore the session manually in the CardScanR profile, then restart the runtime.
- Human re-verification may occasionally be required; treat that as recoverable ops, not a code defect.

### Owned-daily enablement

- Persistent flag: `reports/runtime/owned_daily_full_enable.flag`
- Runtime snapshot: `reports/runtime/live_ebay_runtime_config.json`
- Startup path: `scripts/ensure_live_ebay_pricing_runtime.ps1` (reads the persistent flag; does **not** force full enable)
- Owned pass runs **once per UTC day** inside `market_price_scheduler` when `OWNED_DAILY_FULL_ENABLE=true`, using the 24h last-successful-refresh due rule (not calendar-midnight churn).
- After the 2026-09-27 price-precedence incident, keep full enable **false** until the capped pilot is explicitly re-authorized.

### Customer-facing source precedence (2026-09-27)

Verified eBay sold estimates are the primary customer-facing market estimate.

1. Fresh verified eBay sold estimate  
2. Stale-but-valid previous eBay sold estimate (within 7 days)  
3. Supported structured market fallback (international estimate)  
4. Reference/static provider (tcgdex / static_reference / etc.)  
5. Unavailable  

Lower tiers may store secondary observations (`reference_price` / snapshots) but must not silently overwrite a valid higher-tier selected `current_market_price`. Shared policy: `cardscanr_market_engine/price_source_precedence.py`.

