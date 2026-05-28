# Market Price Refresh Cooldown Policy

CardScanR market prices are keyed by card identity plus market identity. A refresh for one shared key updates the cache for every user who asks for that same card, condition, country, and currency.

The refresh gate prevents repeated local provider lookups for the same key. If one AU/AUD user refreshes Charizard ex raw and another AU/AUD user taps refresh two hours later, the second request receives the existing cached result plus cooldown metadata instead of creating another job.

## Cooldown Rules

Manual user refreshes use a 6 hour default cooldown.

The backend shortens or lengthens that window from the current cache and price key signals:

- High-value: 4 hours when `current_market_price >= 100` or `recommended_price >= 100`.
- Popular: 4 hours when `popularity_score >= 10` or `inventory_count >= 10`.
- High-value and popular: 2 hours.
- Low-value/common: 12 hours when both current and recommended prices are below 10, `popularity_score < 3`, and `inventory_count < 3`.
- No cache: refresh immediately.
- Active queued/running job: reuse the active job and do not enqueue another one.

Configured defaults:

```text
MARKET_REFRESH_DEFAULT_COOLDOWN_HOURS=6
MARKET_REFRESH_HIGH_VALUE_COOLDOWN_HOURS=4
MARKET_REFRESH_POPULAR_COOLDOWN_HOURS=4
MARKET_REFRESH_HOT_CARD_COOLDOWN_HOURS=2
MARKET_REFRESH_LOW_VALUE_COOLDOWN_HOURS=12
```

## Per-Market Isolation

Cooldowns are per price key. AU/AUD and US/USD are separate keys even for the same card, so a fresh AU cache does not block a US refresh.

## App Refresh Behavior

Flutter should eventually call `request_market_price_refresh` instead of enqueueing directly.

The RPC returns:

- `active_job_exists` when a queued/running job already exists for the key.
- `cache_fresh` when the shared cache is still inside its cooldown.
- `job_enqueued` when a new refresh job was created.

The response includes `cache_last_updated_at`, `cooldown_hours`, `cooldown_until`, `cooldown_reason`, and `cache_is_fresh` so the app can show the cached price and explain when another refresh is available.

## Force Refresh

`force_refresh` is reserved for the service role for now. Normal authenticated users cannot bypass cooldowns. A future premium/admin path can be added by extending the RPC authorization check.

## Provider Protection

This protects the future local eBay/browser provider from being spammed by repeated taps across users. The worker still only processes jobs that passed the shared cache gate, and active job dedupe remains enforced by the queue.
