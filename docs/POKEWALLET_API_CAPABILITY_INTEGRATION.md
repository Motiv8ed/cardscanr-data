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
python tools/import_pokewallet_set_prices.py --languages jp --max-sets 3 --dry-run
python tools/import_pokewallet_set_prices.py --languages jp --max-sets 3 --write
```

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
