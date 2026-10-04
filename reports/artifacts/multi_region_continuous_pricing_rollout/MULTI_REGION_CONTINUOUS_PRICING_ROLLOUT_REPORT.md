# MULTI_REGION_CONTINUOUS_PRICING_ROLLOUT_REPORT

Task: `CARDSCANR-MULTI-REGION-CONTINUOUS-PRICING-ROLLOUT`  
Result: **PARTIAL**  
Date (UTC context): 2026-10-04

Offline multi-region verified-local **scheduling/control-plane** is implemented and tested.  
Live US/GB/CA canaries were **not started**: AU continuous remains the sole live browser market; AU availability is currently `PROBE_REQUIRED`; global browser concurrency stays 1. JP and EU are **BLOCKED_NEEDS_PROVIDER** from existing configuration (no invented endpoints).

## Region provider audit

| Region | Currency | Canonical market | Provider | Marketplace | Search | Sold | Currency parse | Scheduler | Freshness | Continuous worker | Production-ready | Status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| AU | AUD | AU | ebay_browser | ebay.com.au | RENDERED_UI_X11 | Sold items | AUD patterns | yes | market-specific | yes (enabled) | true | CONTINUOUS |
| US | USD | US | ebay_browser | ebay.com | RENDERED_UI_X11 | Sold items (EN) | USD patterns | yes | market-specific | not live-enabled | true (offline) | READY_FOR_BROWSER_CANARY |
| GB | GBP | GB | ebay_browser | ebay.co.uk | RENDERED_UI_X11 | Sold items (EN) | GBP patterns | yes | market-specific | not live-enabled | true (offline) | READY_FOR_BROWSER_CANARY |
| CA | CAD | CA | ebay_browser | ebay.ca | RENDERED_UI_X11 | Sold items (EN) | CAD patterns | yes | market-specific | not live-enabled | true (offline) | READY_FOR_BROWSER_CANARY |
| JP | JPY | JP | NONE | — | — | no native sold route | n/a | n/a | identity exists | no | false | BLOCKED_NEEDS_PROVIDER |
| EU | EUR | EU (display only) | NONE | no ebay.eu | — | — | n/a | n/a | identity exists | no | false | BLOCKED_NEEDS_PROVIDER |

Authoritative sources:

- `cardscanr_market_engine/marketplaces.py` `_EBAY_MARKETS` (AU/US/GB/CA + DE/FR/IT/ES EUR country sites; no JP; no EU).
- `cardscanr_market_engine/international/market_fallback_policy.py` `NO_NATIVE_SOLD_ROUTE_MARKETS = {NZ, JP}`; `MARKET_DISPLAY_NAMES["EU"]="Europe"`.
- `cardscanr_market_engine/marketplace_ops_state.py` `SUPPORTED_LIVE_MARKETS = (AU, US, GB, CA)`.
- `cardscanr_market_engine/fingerprints.py` fingerprint includes `market_country|currency` independent of card language.

EU is **not** mapped to DE/FR/IT/ES as verified-local EU. Those country sites exist for EUR but would mint DE/FR/IT/ES price keys, not `eu|eur`, unless the owner later chooses a canonical EU country. This task does not invent that mapping.

JP verified-local cannot use the current eBay browser sold path. Existing JP policy is foreign-estimate fallback, which is **not** verified-local.

## Configuration

Central registry: `cardscanr_market_engine/region_pricing_registry.py`

- Search mode: `RENDERED_UI_X11` for every browser-ready market.
- Direct query-bearing search URLs remain forbidden (`UNACCOUNTED_SEARCH_URL_NAVIGATION`).
- AU continuous env still forces `OWNED_DAILY_ALLOWED_MARKETS=AU` so AU is not silently expanded.
- `apply_continuous_multi_region_env()` can allow `AU,US,GB,CA` under `GLOBAL_BROWSER_PRICING_CONCURRENCY=1` **after** a region canary. Not applied to the live AU worker in this rollout.

Preserved AU policy:

- HOT 12h / NORMAL 24h
- HIGH demand `requests_24h >= 3`
- lanes 50/30/20
- anti-starvation + dedupe
- retries inside pricing attempt: 0
- concurrency: 1
- `MAX_LIVE_SUBMISSIONS_PER_HOUR=20` **global**
- `MAX_LIVE_SUBMISSIONS_PER_DAY=200` **global**

## Marketplace / domain mapping

- AU → `https://www.ebay.com.au/`
- US → `https://www.ebay.com/`
- GB → `https://www.ebay.co.uk/`
- CA → `https://www.ebay.ca/`
- JP → none (do not invent `ebay.co.jp` sold)
- EU → none (do not invent `ebay.eu`)

## Price identity / freshness isolation

Fingerprints remain `...|{market}|{currency}`. Same printing has independent AU/US/GB/CA/JP/EU records.

Demand is identity-keyed (`price_key_id` and `fingerprint|market`). Demand raises priority only; it cannot mark a fresh market due.

Proof (offline): AU age 2h → `FRESH_SKIP`; US age 30h → `DUE`. See `tests/test_multi_region_continuous_pricing.py` and existing `tests/test_demand_aware_scheduler.py` `test_market_isolation_same_printing`.

## Control-plane isolation

`ebay_availability_state.json`:

- Existing v1 single snapshot remains the **AU** slot (no rewrite of current AU `PROBE_REQUIRED` on load).
- First non-AU mutation migrates the file to `{version:2, markets:{AU:..., US:...}}`.
- `browser_work_allowed(market=)` / gate / job_runner / owned_daily scheduler now pass market.
- Marketplace ops cooldowns were already per-market for AU/US/GB/CA.
- A US SORRY/cooldown no longer overwrites AU availability.

Global hard safety still applies to true integrity / challenge / unaccounted search URL outcomes.

## Global dispatcher

`cardscanr_market_engine/market_dispatcher.py`

- Global concurrency 1 (`acquire_browser_slot` refuses a second job).
- Fair pick: market age + capped backlog + starvation credit.
- Owned-daily scheduler filters due work to the picked market, then applies 50/30/20 lanes inside that market.

## Global budgets

`ContinuousSafetyBudget` still enforces 20/1h and 200/24h globally and now tags per-market submission counts.

## Fixtures / tests

- `tests/fixtures/multi_region_pricing/currency_samples.json`
- `tests/test_multi_region_continuous_pricing.py` (identity, freshness, cooldown isolation, dispatcher, budgets, currencies, query-URL guard, Sold labels)
- Focused + related suites: **pass** (76 focused + 37 broader related)

JPY/EUR parser live paths are **not** enabled; tests assert JP/EU blocked instead of faking PASS.

## Region canaries

| Region | Attempted | Healthy | Status |
|---|---|---|---|
| US | 0 | 0 | NOT_STARTED — READY_FOR_BROWSER_CANARY |
| GB | 0 | 0 | NOT_STARTED — READY_FOR_BROWSER_CANARY |
| CA | 0 | 0 | NOT_STARTED — READY_FOR_BROWSER_CANARY |
| JP | 0 | 0 | BLOCKED_NEEDS_PROVIDER |
| EU | 0 | 0 | BLOCKED_NEEDS_PROVIDER |

Reason live canaries were not started:

1. Commit-before-canary rule: offline support is now in place.
2. AU continuous worker already owns the single global browser.
3. Production AU availability is `PROBE_REQUIRED` (transient recovery), not HEALTHY.
4. Starting a second Chrome/X11 session would violate `GLOBAL_BROWSER_PRICING_CONCURRENCY=1`.
5. No patch-and-continue during a live canary — better to wait for AU browser idle + owner go-ahead than to collide with AU recovery.

## Region enablements

- AU: `marketEnabled=true` (pre-existing continuous)
- US/GB/CA: `marketEnabled=false` (offline-ready, canary pending)
- JP/EU: blocked

## Unsupported-region blockers

### JP

- Required: a legitimate JP sold/completed local marketplace provider with JPY evidence, Sold control identity, and page-health for that site.
- Existing: foreign eBay estimate fallback only (`NO_NATIVE_SOLD_ROUTE`).
- Next task: `CARDSCANR-JP-VERIFIED-LOCAL-PROVIDER` (not ebay_browser AU path reuse; no FX substitute).

### EU

- Required: owner-chosen canonical EU marketplace (e.g. DE `ebay.de` **as** EU only if product identity is redefined), or a true pan-EU provider.
- Existing: DE/FR/IT/ES EUR eBay country routes; EU display name only.
- Next task: `CARDSCANR-EU-CANONICAL-MARKET-DECISION`.

## AU regression

- AU allowlist env unchanged (`OWNED_DAILY_ALLOWED_MARKETS=AU` in `apply_continuous_au_env`).
- AU availability file format preserved until a non-AU slot is written.
- AU freshness/history not rewritten.
- Demand-aware AU tests still pass.

## Final continuous status

- AU: CONTINUOUS (subject to current `PROBE_REQUIRED` recovery)
- US/GB/CA: READY_FOR_BROWSER_CANARY
- JP/EU: BLOCKED_NEEDS_PROVIDER
- Global dispatcher: implemented, concurrency 1
- Owned_daily: still AU-only in the live enablement env

## Next gate

`SUPPORTED_REGIONS_OFFLINE_READY_LIVE_CANARIES_AWAIT_AU_BROWSER`

Owner should:

1. Allow AU probe/recovery to complete (or explicitly authorize sharing the single browser).
2. Run US 5/5 canary only, then enable US continuous.
3. Repeat GB, then CA.
4. Leave JP/EU blocked until a real provider exists.
