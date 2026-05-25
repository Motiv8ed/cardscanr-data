# Market Price Engine — Phase 4A: Demo Market Data

## Purpose

Phase 4A adds a repeatable seed/demo tooling layer that generates realistic Market Price Engine data for multiple local markets using the **existing mock provider/worker pipeline**.

It makes it easy to create sample `market_price_keys`, refresh jobs, snapshots, evidence, and cache rows for:

| Market | Currency | eBay Domain |
|--------|----------|-------------|
| AU | AUD | ebay.com.au |
| US | USD | ebay.com |
| GB | GBP | ebay.co.uk |
| CA | CAD | ebay.ca |

The same card in different markets produces **separate keys, separate jobs, separate cache rows, separate evidence URLs**, and market-specific currency/domain output.

Demo cards are clearly labelled (`[DEMO]` prefix for classic set; `Smoke Test` prefix for the smoke set).  No real user data is used.  The script is safe to run repeatedly without data loss.

---

## Why this matters

Without seed data, it is hard to verify the end-to-end market routing pipeline or to demonstrate Flutter UI work.  Phase 4A creates a stable, repeatable set of rows that cover all four primary markets so developers can:

- Confirm that market routing (currency, domain, evidence URLs) works correctly.
- Inspect Supabase rows for AU/US/GB/CA simultaneously.
- Run dry-runs in CI without any DB writes.
- Use the seeded cache data as a fixture for future Flutter UI development.

---

## Required environment variables

| Variable | Notes |
|----------|-------|
| `SUPABASE_URL` | Your Supabase project REST URL. |
| `SUPABASE_SERVICE_ROLE_KEY` | Service role key (never commit). |
| `MARKET_LOOKUP_PROVIDER` | Must be `mock` (default). |

---

## Commands

### Dry-run (no DB writes)

```bash
python scripts/seed_market_price_demo_data.py --dry-run
```

PowerShell:

```powershell
.\scripts\run_market_price_demo_seed.ps1 -DryRun
```

Dry-run outputs the seed plan as JSON and writes `reports/market_price_demo_seed_latest.json` without making any Supabase calls.

---

### Enqueue demo jobs only (no processing)

```bash
python scripts/seed_market_price_demo_data.py \
    --markets AU,US,GB,CA \
    --cards smoke,classic \
    --enqueue-only
```

PowerShell:

```powershell
.\scripts\run_market_price_demo_seed.ps1 -Markets AU,US,GB,CA -Cards smoke,classic -EnqueueOnly
```

Creates `market_price_keys` and `market_price_refresh_jobs` rows only.  Run the worker separately to produce cache/snapshot/evidence rows.

---

### Enqueue and process in one pass

```bash
python scripts/seed_market_price_demo_data.py \
    --markets AU,US,GB,CA \
    --cards all \
    --process \
    --max-jobs 50
```

PowerShell:

```powershell
.\scripts\run_market_price_demo_seed.ps1 -Markets AU,US,GB,CA -Cards all -Process -MaxJobs 50
```

This is the recommended command for a complete demo dataset.

---

## CLI options

| Option | Default | Description |
|--------|---------|-------------|
| `--markets` | `AU,US,GB,CA` | Comma-separated market codes. |
| `--cards` | `smoke,classic` | Card set: `smoke`, `classic`, `all`. |
| `--enqueue-only` | off | Enqueue jobs; do not run the worker. |
| `--process` | off | Run the mock worker after enqueueing. |
| `--max-jobs` | `50` | Max jobs per worker run. |
| `--dry-run` | off | Show plan only; no DB calls. |

---

## Demo card sets

### Smoke set (`--cards smoke`)

| Card | Set | Collector # |
|------|-----|-------------|
| Smoke Test Charizard ex | Smoke Test Set | 001/999 |

### Classic set (`--cards classic`)

| Card | Set | Collector # |
|------|-----|-------------|
| [DEMO] Charizard ex | Obsidian Flames | 125/197 |
| [DEMO] Umbreon VMAX | Evolving Skies | 215/203 |
| [DEMO] Pikachu | Base Set | 58/102 |
| [DEMO] Mewtwo | Base Set | 10/102 |

---

## Inspecting resulting Supabase rows

After running `--process`, check Supabase Table Editor for:

### `market_price_keys`
Filter by `card_name` like `[DEMO]` or `Smoke Test`.  You should see separate rows for each card × market combination.

### `market_price_cache`
Join on `price_key_id`.  Confirm:
- `currency` = `AUD` / `USD` / `GBP` / `CAD` (matching selected market)
- `market_country` = `AU` / `US` / `GB` / `CA`
- `confidence` = `high`, `medium`, or `low`

### `market_price_snapshots`
Check `diagnostics_json.providerDomain` to confirm market routing:
- AU → `ebay.com.au`
- US → `ebay.com`
- GB → `ebay.co.uk`
- CA → `ebay.ca`

### `market_sold_listing_evidence`
Check `listing_url` — the domain should match the expected eBay domain for each market.

---

## Report output

Two report files are written to `reports/`:

| File | Description |
|------|-------------|
| `market_price_demo_seed_latest.json` | Latest run report (overwritten each run). |
| `market_price_demo_seed_runs.jsonl` | Append-only history of all runs. |

Sensitive keys (`SUPABASE_SERVICE_ROLE_KEY`, `apikey`, etc.) are redacted in the report.

### Example dry-run report

```json
{
  "dryRun": true,
  "finishedAtUtc": "2026-05-25T06:20:00Z",
  "plan": [
    {
      "card_name": "Smoke Test Charizard ex",
      "collector_number": "001/999",
      "currency": "AUD",
      "expected_domain": "ebay.com.au",
      "fingerprint": "pokemon|en|smoke-test|001/999|smoke_test_charizard_ex|raw|raw|au|aud",
      "market_country": "AU",
      "set_code": "smoke-test"
    }
  ],
  "planItemCount": 1,
  "startedAtUtc": "2026-05-25T06:20:00Z",
  "status": "dry_run"
}
```

---

## How this helps future Flutter UI work

- Provides stable demo rows across all four target markets for UI development without real eBay data.
- Confirms market routing is correct so Flutter can safely use `market_country` + `currency` from user settings.
- The `get_market_price_bundle` RPC returns cache, snapshot, and evidence for any seeded fingerprint — Flutter can be tested against this.
- Running `--dry-run` in CI validates the seed plan without any Supabase dependency.

---

## Unit tests

```bash
python -m unittest discover -s tests -p "test_market_engine_*.py"
```

The `tests/test_market_engine_demo_seed.py` file covers:

- Demo card definitions are valid (required fields, non-empty, correct labels)
- Market list parsing (`parse_markets`, `parse_card_filter`)
- Expected domain/currency assertions per market
- Fingerprints are unique per card × market
- Dry-run does not call the Supabase client
- Report redaction via `sanitize_for_report`
- Repeated seed planning is deterministic

An optional integration test (`DemoSeedIntegrationTest`) is included but **skipped unless** `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `MARKET_LOOKUP_PROVIDER=mock` are set in the environment.

---

## Limitations

- Mock provider only — no real eBay data.
- Demo cards are fictional identities; prices are deterministic mock values based on the fingerprint hash.
- The script does not delete existing data; it is append-safe.
- The integration test is skipped in environments without Supabase credentials.
- `--process` runs a single `run_once` cycle; for large datasets run `workers/market_price_worker.py` separately with a suitable `--max-jobs` value.
