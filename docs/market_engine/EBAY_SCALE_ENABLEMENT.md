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
| `EBAY_SCALE_SHADOW_AUDIT` | `false` | Append-only local audit log for set sweeps |
| `EBAY_SCALE_SET_SWEEP_LIVE` | `false` | Allow set-sweep medians to become live prices (default off) |

Recommended order: `EBAY_SCALE_ENABLED=true` + `EBAY_SCALE_DIRECT_SOLD_URL=true` (probe one card), then cache + queue, then set sweeps on a canary set.

## Live trial with shadow audit (first PC run)

Use this for the first real set-sweep on one small AU set **without changing live card prices**:

```bash
EBAY_SCALE_ENABLED=true
EBAY_SCALE_SET_SWEEP=true
EBAY_SCALE_SHADOW_AUDIT=true
EBAY_SCALE_SET_SWEEP_LIVE=false
# keep other EBAY_SCALE_* false until earlier phases are proven
CONFIRM_LIVE_EBAY_WORKER=true
EBAY_BROWSER_ENABLED=true
# owned_daily / continuous caps unchanged — do not alter safety env
```

Audit output (append-only, no Supabase):

- `reports/runtime/ebay_scale_audit/set_sweep_audit.jsonl`
- `reports/runtime/ebay_scale_audit/set_sweep_audit.csv`

Override log dir: `EBAY_SCALE_AUDIT_DIR=D:\cardscanr-data\reports\runtime\ebay_scale_audit`

After a sweep run, summarise:

```bash
python -m cardscanr_market_engine.ebay_scale.audit_report --since 2026-11-16T00:00:00Z
```

Markdown lands under `reports/ebay_scale/set_sweep_audit_report_*.md`.

The worker also schedules up to **10** highest-value cards in the set for normal per-card searches in the same run (comparison anchors). Those jobs still count against existing 20/h and 60/24h caps — **do not** raise caps.

Promote to live set-sweep pricing only after shadow review:

```bash
EBAY_SCALE_SET_SWEEP_LIVE=true
```

Keep `EBAY_SCALE_SHADOW_AUDIT=true` until agreement rates look stable.

## Unchanged safety contract

- 20 searches/hour and 60/24h submission caps
- HARD_STOP latch thresholds
- Browser-driven AU `ebay.com.au` access only (HTTPS)
- `owned_daily_full_enable.flag` still required for full owned-daily enqueue