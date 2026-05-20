# cardscanr-data

Static data / price-cache repository for the **CardScanR** Flutter app.

Deployed to **Cloudflare Pages** using the `public/` folder as the build
output directory.

---

## Repository structure

```
public/                        ← Cloudflare Pages build output
  _headers                     ← CORS / Cache-Control headers for /v1/*
  v1/
    index.json                 ← Dataset manifest (sha256, URLs, versions)
    app-config.json            ← Feature flags consumed by the Flutter app
    supported-games.json       ← Enabled card games
    supported-sources.json     ← Enabled price sources
    supported-languages.json   ← Language/catalogue/pricing availability manifest
    supported-markets.json     ← Market/currency/pricing availability manifest
    prices/
      pokemon/
        en/sample.json         ← English Pokémon sample prices (USD, provider-native)
        jp/sample.json         ← Japanese Pokémon sample prices (pricing unavailable)
    diagnostics/
      latest-build.json        ← Build metadata written by the CI job

data/
  cards_to_track.json          ← Cards the build script generates prices for
  supported_languages_config.json  ← Curated source of truth for supported-languages.json
  supported_markets_config.json    ← Curated source of truth for supported-markets.json

tools/
  build_price_cache.py         ← Builds price files + updates index.json
  validate_cache.py            ← Validates JSON, sha256, required fields, etc.

.github/workflows/
  update-price-cache.yml       ← Runs every 12 h (+ manual trigger), commits changes
  validate-cache.yml           ← Runs on pull requests (+ manual trigger)
```

---

## Canonical key format

```
game|language|setId|collectorNumber|normalizedName|variant|condition
```

Example: `pokemon|en|base1|4|charizard|holo|near_mint`

---

## Running locally

```bash
# Build the price cache
python tools/build_price_cache.py

# Build only EN current prices (batch-friendly mode)
python tools/build_price_cache.py current_prices

# Validate the cache
python tools/validate_cache.py

# Report EN current-price Stage 1 migration progress
python tools/report_en_current_price_migration.py

# Local-first batch updater (build + validate)
python tools/run_local_price_update.py --batch-size 10
```

No third-party packages are required.  
Optional environment variables:

| Variable | Purpose |
|---|---|
| `POKEMON_TCG_API_KEY` | Pokémon TCG API key (reserved for future live-fetch integration) |

---

## Cloudflare Pages setup

1. Set **Build output directory** to `public`.  
2. Leave the build command empty (this is a static repo — CI commits the files
   directly).  
3. The `public/_headers` file applies CORS + cache headers automatically.

---


## App-facing data contract

For production app integration rules and stability scope of `public/v1`, see:

- [`docs/APP_DATA_CONTRACT.md`](docs/APP_DATA_CONTRACT.md)

Source IDs in `/v1/supported-sources.json` are canonical lowercase `snake_case`.
Legacy IDs are exposed through per-source `aliases` for backward-compatible app matching during transition windows.
