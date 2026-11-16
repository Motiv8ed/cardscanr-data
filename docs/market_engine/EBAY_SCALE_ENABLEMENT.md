# eBay AU scale features — PC deploy & enablement (later)

All flags default **OFF**. No deploy or merge from the cloud agent; Andy enables on the AU pricing PC after merging.

## Deploy (on pricing PC)

1. Fetch branches: `fix/ebay-unattended-recovery-20261116` (stack base) then `feat/ebay-set-sweeps-cache-20261116`.
2. Merge or run the worker from the feature branch worktree after review.
3. **Do not** apply `20261116120000_ebay_cross_user_price_cache_proposed.sql` to production until explicitly approved.

## Enable (incremental)

Set in the live eBay worker environment (e.g. `scripts/run_ebay_price_worker.ps1` or task env):

| Variable | Default | Purpose |
|----------|---------|---------|
| `EBAY_SCALE_ENABLED` | `false` | Master gate for all scale hooks |
| `EBAY_SCALE_CROSS_USER_CACHE` | `false` | Value-tier freshness windows + skip fresh jobs |
| `EBAY_SCALE_VALUE_FIRST_QUEUE` | `false` | Cross-user dedupe + priority lanes |
| `EBAY_SCALE_DIRECT_SOLD_URL` | `false` | `LH_Sold=1&LH_Complete=1&_ipg=240` direct nav (AU) |
| `EBAY_SCALE_SET_SWEEP` | `false` | Set/rarity sweep pricing path |
| `EBAY_SCALE_SET_SWEEP_EXACT_AUD` | `20` | Cards above this AUD get per-card searches |

Recommended order: `EBAY_SCALE_ENABLED=true` + `EBAY_SCALE_DIRECT_SOLD_URL=true` (probe one card), then cache + queue, then set sweeps on a canary set.

## Unchanged safety contract

- 20 searches/hour and 60/24h submission caps
- HARD_STOP latch thresholds
- Browser-driven AU `ebay.com.au` access only (HTTPS)
- `owned_daily_full_enable.flag` still required for full owned-daily enqueue