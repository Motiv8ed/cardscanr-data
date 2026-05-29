# Phase 5: Local eBay Browser Provider

Phase 5 adds an opt-in Playwright provider that can run on your own PC as a controlled backend worker. It does not use Apify, paid APIs, or third-party scraping services.

The default provider remains:

```text
MARKET_LOOKUP_PROVIDER=mock
```

Real eBay browser lookup only runs when both flags are set:

```text
MARKET_LOOKUP_PROVIDER=ebay_browser
ENABLE_EBAY_REAL_LOOKUP=true
```

## Browser Profile

The provider uses locally installed Google Chrome through Playwright `channel="chrome"`. It does not use bundled Chromium by default, and it does not use the user's personal/default Chrome profile.

Dedicated profile:

```text
EBAY_BROWSER_PROFILE_NAME=cardscanr
EBAY_BROWSER_USER_DATA_DIR=D:\cardscanr-data\.browser_profiles\cardscanr
```

If the directory is missing, the provider creates it. `.browser_profiles/` is ignored by git.

To reset the local browser state, stop the worker and delete:

```powershell
Remove-Item -LiteralPath "D:\cardscanr-data\.browser_profiles\cardscanr" -Recurse -Force
```

## Setup

Install Python dependencies, then install Playwright browser support:

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

The provider launches installed Chrome with `channel="chrome"`. Browser installation is never done by application code.

## Safety Defaults

Start with concurrency 1. The browser provider opens one persistent Chrome context/page per lookup and parses the first result page only.

Relevant environment controls:

```text
EBAY_BROWSER_ENGINE=chrome
EBAY_BROWSER_CHANNEL=chrome
EBAY_BROWSER_PROFILE_NAME=cardscanr
EBAY_BROWSER_USER_DATA_DIR=D:\cardscanr-data\.browser_profiles\cardscanr
EBAY_BROWSER_HEADLESS=true
EBAY_BROWSER_MAX_RESULTS=30
EBAY_BROWSER_TIMEOUT_SECONDS=45
EBAY_BROWSER_COOLDOWN_SECONDS=20
EBAY_BROWSER_MIN_SECONDS_BETWEEN_REQUESTS=20
MARKET_PROVIDER_MAX_REQUESTS_PER_MINUTE=2
MARKET_PROVIDER_MAX_REQUESTS_PER_DAY=200
```

This phase implements max results, timeout, dedicated profile creation, personal profile refusal, and minimum spacing between provider requests. Keep worker concurrency at 1 while validating local behavior.

## Local Debug

The debug command runs one provider lookup and does not write to Supabase. The output includes sanitized browser config and the Chrome profile path.

Debug artifacts are written for each debug run:

```text
reports/ebay_browser_debug/latest/page.html
reports/ebay_browser_debug/latest/screenshot.png
reports/ebay_browser_debug/latest/debug_summary.json
reports/ebay_browser_debug/runs.jsonl
```

Inspect `screenshot.png` first to confirm eBay rendered visible results, then inspect `debug_summary.json` for selector counts, parser errors, page title, current URL, query text, and a body/result text sample. If `resultCount=0` while the screenshot shows results, the browser and query are working and the next fix should target result-card selectors or text parsing. `page.html` is captured without cookies/local storage and is useful for checking changed eBay markup.

Headed AU debug:

```powershell
.\scripts\debug_ebay_browser_provider.ps1 -Headed -Market AU -Currency AUD -CardName "Charizard ex" -CollectorNumber "125/197" -SetName "Obsidian Flames"
```

Equivalent direct Python command:

```powershell
$env:MARKET_LOOKUP_PROVIDER="ebay_browser"
$env:ENABLE_EBAY_REAL_LOOKUP="true"
$env:EBAY_BROWSER_ENGINE="chrome"
$env:EBAY_BROWSER_CHANNEL="chrome"
$env:EBAY_BROWSER_PROFILE_NAME="cardscanr"
$env:EBAY_BROWSER_USER_DATA_DIR="D:\cardscanr-data\.browser_profiles\cardscanr"
$env:EBAY_BROWSER_HEADLESS="false"
python scripts/debug_ebay_browser_provider.py --market AU --currency AUD --card-name "Charizard ex" --collector-number "125/197" --set-name "Obsidian Flames"
```

## Worker

The provider does not create jobs and does not bypass cooldown gating. It only receives jobs already claimed by the backend worker.

User-triggered refreshes should go through `request_market_price_refresh(...)`. That shared cache gate prevents repeated user taps from creating duplicate browser lookups.

Run one local browser job:

```powershell
$env:MARKET_LOOKUP_PROVIDER="ebay_browser"
$env:ENABLE_EBAY_REAL_LOOKUP="true"
$env:EBAY_BROWSER_ENGINE="chrome"
$env:EBAY_BROWSER_CHANNEL="chrome"
$env:EBAY_BROWSER_PROFILE_NAME="cardscanr"
$env:EBAY_BROWSER_USER_DATA_DIR="D:\cardscanr-data\.browser_profiles\cardscanr"
$env:MARKET_WORKER_CONCURRENCY="1"
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
