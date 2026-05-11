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
    prices/
      pokemon/
        en/sample.json         ← English Pokémon prices (AUD)
        jp/sample.json         ← Japanese Pokémon prices (AUD)
    diagnostics/
      latest-build.json        ← Build metadata written by the CI job

data/
  cards_to_track.json          ← Cards the build script generates prices for

tools/
  build_price_cache.py         ← Builds price files + updates index.json
  validate_cache.py            ← Validates JSON, sha256, required fields, etc.

.github/workflows/
  update-price-cache.yml       ← Runs every 6 h (+ manual trigger), commits changes
  validate-cache.yml           ← Runs on every push / PR
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

# Validate the cache
python tools/validate_cache.py
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
