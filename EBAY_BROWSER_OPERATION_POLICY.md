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

Backend owns fallback order. Australian default:

```text
EBAY_AU -> EBAY_US -> EBAY_GB -> EBAY_CA
```

The home marketplace must be attempted first. Every attempted marketplace must record result counts, accepted/rejected comparable counts, confidence, no-price reason, and selected marketplace.

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
