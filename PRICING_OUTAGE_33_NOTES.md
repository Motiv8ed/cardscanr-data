# Pricing outage #33 — scoped fix notes

## What changed

- Applied production **t323u R0** worker logic: `MARKET_WORKER_ENFORCE_SAFETY_BUDGET` enforces transient/hourly safety gates in staged (non-continuous) mode; optional `MARKET_WORKER_MIN_JOB_SPACING_SECONDS` spacing after jobs.
- Added `_emit_gated_heartbeat` and call it on every gated loop branch (continuous HARD_STOP / BUDGET_WAIT / COOLDOWN, staged HARD_STOP / BUDGET_WAIT, browser halt / transient halt) **before** sleep/continue/return so Supabase `market_price_pipeline_heartbeats` reflects live gate state while the process is alive.
- Extended `scripts/report_ebay_pricing_health.py` to read local `continuous_au_worker_status.json` and `continuous_au_safety_ledger.json` and surface `worker.gateState`, `worker.gateReason`, and `worker.note`; HARD_STOP latch forces `worker.healthy=false` even when a PID is present.

## Why

On 6 Nov 2026 the AU worker correctly latched HARD_STOP for three consecutive transient marketplace failures. Monitoring showed a stale/crashed heartbeat because gated cycles never upserted heartbeats; the health script could report the worker as healthy while pricing was DOWN.

## What this does NOT do

- Does not clear or weaken HARD_STOP, change safety thresholds, re-enable owned_daily, modify the eBay provider, or alter scheduled tasks / running processes.
- Does not commit production ledger changes; owner review is still required to resume.

## Reference

Motiv8ed/cardscanr-data#33
