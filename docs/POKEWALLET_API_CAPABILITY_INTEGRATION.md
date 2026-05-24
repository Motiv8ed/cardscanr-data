# PokeWallet API Capability Integration

This note documents the safe integration path for extra PokeWallet API capabilities discovered by `tools/audit_pokewallet_api_capabilities.py`.

## Current guardrails

- Read API keys from environment variables only.
- Do not log, report, or commit API key values.
- Do not fabricate prices.
- Do not convert currencies until a validated conversion system exists.
- Do not bulk-download images.
- Keep image binaries ignored by Git.
- Keep the current app catalogue, image manifest, and public price status working while new data is staged.

## Set metadata refresh stage

The audit captures a diagnostics-only set metadata stage from `/sets` with these fields:

- `set_id`
- `set_code`
- `name`
- `language`
- `release_date`
- `card_count`

Promotion rules:

- Match by numeric PokeWallet `set_id` before matching by `set_code`.
- Treat duplicate language/set-code mappings as ambiguous.
- Only fill missing provider metadata fields.
- Do not overwrite better app catalogue names, release dates, counts, logos, or symbols without a focused review.
- Keep provider metadata separate from canonical app set metadata until validation approves promotion.

## Price importer design

If `/prices/:numericSetId` is available, add a staged importer before writing public files:

- Fetch one numeric set at a time into a temporary diagnostics output.
- Preserve provider variants and finishes.
- Store TCGPlayer USD and CardMarket EUR as separate source/currency records.
- Do not collapse TCGPlayer and CardMarket records into one value.
- Do not convert currencies without a separate validated conversion source.
- Mark JP pricing unavailable only when the tested JP endpoint returns no usable JP price records.
- Promote into `public/v1/prices/current` only after focused count, source, currency, and status checks pass.

Implemented staged importer:

```powershell
python tools/import_pokewallet_set_prices.py --languages jp --source both --max-sets 20 --dry-run --fit-budget
python tools/import_pokewallet_set_prices.py --languages jp --source both --max-sets 20 --write --fit-budget
```

## Request budget guardrails

PokeWallet API request limits currently used by this importer:

- Hourly hard limit: 100 requests/hour
- Daily hard limit: 1000 requests/day

The importer now uses safe request limits by default (90/hour and 900/day) and tracks usage in:

- `data/pokewallet_price_request_ledger.json`

Important behavior:

- Dry-runs consume real API requests and are counted in the ledger.
- Write runs also consume real API requests.
- A `50` set dry-run followed by `50` set write can exhaust the hourly budget.
- By default, runs fail safely when planned requests exceed remaining safe budget.
- `--fit-budget` trims selected sets to stay inside remaining safe budget.
- `--wait-for-budget` waits for hourly budget to recover and prints reset estimates.

Budget overrides:

- Env:
	- `POKEWALLET_PRICE_MAX_REQUESTS_PER_HOUR`
	- `POKEWALLET_PRICE_MAX_REQUESTS_PER_DAY`
	- `POKEWALLET_PRICE_REQUEST_SAFETY_BUFFER`
- CLI:
	- `--max-requests-per-hour`
	- `--max-requests-per-day`
	- `--request-safety-buffer`
	- `--budget-ledger-path`
	- `--fit-budget`
	- `--wait-for-budget`
	- `--respect-budget` (default)
	- `--ignore-budget` (manual override only)

Recommended safe patterns:

- `20` set dry-run + `20` set write
- Or always use `--fit-budget` so request counts are auto-trimmed
- Avoid `50` dry-run + `50` write on a `100/hour` limit

Full pipeline entrypoint:

```powershell
.\scripts\run_cardscanr_full_data_pipeline.ps1 -ImportPokeWalletPrices -PokeWalletPriceMaxSets 3 -Validate -ExportChatGPTReport
```

## Image and logo cache design

The audit probes only 3 low and 3 high card image endpoints and writes no image binaries.

Future logo/image work should:

- Add an explicit allowlist for any `/sets/:setCode/image` logo probes.
- Record content type, byte size, status, and source endpoint before caching binaries.
- Keep cache policy reference-first unless a caller explicitly opts into local caching.
- Keep downloaded image directories ignored by Git.
