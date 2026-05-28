# Phase 5: Local eBay Browser Provider

Phase 5 adds an opt-in Playwright provider that can run on your own PC as a backend worker. It does not use Apify, paid APIs, or third-party scraping services.

The default provider remains:

```text
MARKET_LOOKUP_PROVIDER=mock
```

Real eBay browser lookup only runs when both flags are set:

```text
MARKET_LOOKUP_PROVIDER=ebay_browser
ENABLE_EBAY_REAL_LOOKUP=true
```

## Setup

Install Python dependencies, then install the Chromium browser used by Playwright:

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

Browser installation is never done by application code.

## Safety Defaults

Start with concurrency 1. The browser provider opens one browser context/page per lookup and parses the first result page only.

Relevant environment controls:

```text
EBAY_BROWSER_HEADLESS=true
EBAY_BROWSER_MAX_RESULTS=30
EBAY_BROWSER_TIMEOUT_SECONDS=45
EBAY_BROWSER_COOLDOWN_SECONDS=20
EBAY_BROWSER_MIN_SECONDS_BETWEEN_REQUESTS=20
EBAY_BROWSER_USER_DATA_DIR=
MARKET_PROVIDER_MAX_REQUESTS_PER_MINUTE=2
MARKET_PROVIDER_MAX_REQUESTS_PER_DAY=200
```

This phase implements max results, timeout, and minimum spacing between provider requests. Keep worker concurrency at 1 while validating local behavior.

## Local Debug

The debug command runs one provider lookup and does not write to Supabase.

```powershell
$env:MARKET_LOOKUP_PROVIDER="ebay_browser"
$env:ENABLE_EBAY_REAL_LOOKUP="true"
python scripts/debug_ebay_browser_provider.py --market AU --currency AUD --card-name "Charizard ex" --collector-number "125/197" --set-name "Obsidian Flames"
```

PowerShell wrapper:

```powershell
.\scripts\debug_ebay_browser_provider.ps1 -Market AU -Currency AUD -CardName "Charizard ex" -CollectorNumber "125/197" -SetName "Obsidian Flames"
```

## Worker

The provider does not create jobs and does not bypass cooldown gating. It only receives jobs already claimed by the backend worker.

User-triggered refreshes should go through `request_market_price_refresh(...)`. That shared cache gate prevents repeated user taps from creating duplicate browser lookups.

Run the worker with:

```powershell
$env:MARKET_LOOKUP_PROVIDER="ebay_browser"
$env:ENABLE_EBAY_REAL_LOOKUP="true"
.\scripts\run_market_price_worker.ps1 -Once -MaxJobs 1
```

## Market Routing

The worker resolves the eBay domain from the price key market:

- `AU/AUD` -> `ebay.com.au`
- `US/USD` -> `ebay.com`
- `GB/GBP` -> `ebay.co.uk`
- `CA/CAD` -> `ebay.ca`

Search URLs include sold/completed listing parameters where practical: `LH_Sold=1` and `LH_Complete=1`.

## Captcha And Blocks

The provider detects obvious block or verification pages using page title/body terms such as `captcha`, `verify`, `robot`, `unusual traffic`, `access denied`, and `blocked`.

If detected, it raises a provider blocked error. It does not attempt to bypass captcha or verification.

## Scaling Limits

This local browser provider is a controlled backend tool, not a high-scale scraping platform. It is not guaranteed to scale to 100k users without shared cache cooldowns, queue dedupe, rate limits, and careful operational monitoring.

The shared `request_market_price_refresh(...)` gate protects the provider by reusing fresh cache results and active jobs before browser work begins.
