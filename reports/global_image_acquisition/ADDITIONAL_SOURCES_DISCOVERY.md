# Additional worldwide image/data sources (discovery)

Research pass for `task/global-image-acquisition-20261013`.  
No prohibited scraping; no production ingestion from uncertain rights.

## Already integrated / inventoried

- TCGdex API + `assets.tcgdex.net` (primary multilingual candidate set)
- Pokémon TCG API / `images.pokemontcg.io` (EN)
- Scrydex (EN/JA; written CDN authorization)
- PokéWallet (auth-gated; discovery-only for rehost)
- CardScanR EN/JA local master + R2 CDN (production independence complete)

## Additional sources identified

| Source | Expected yield | Rights posture | Next action |
| --- | --- | --- | --- |
| TCGdex GitHub `cards-database` | Metadata completeness; not artwork | MIT for DB rows only | Keep using for metadata; never treat as artwork licence |
| Limitless TCG / similar tournament DBs | Sparse promo / alternate art refs | ToS typically disallow bulk reuse | Discovery-only |
| PokéBeach / Bulbapedia card articles | Set checklists, some thumbs | Fan wiki licences ≠ Pokémon artwork grant | Reference identity only |
| TCGplayer / Cardmarket product images | Near-complete commercial EN/EU | Marketplace ToS; no redistribution grant | Do not scrape |
| eBay sold listings / seller photos | Gap fillers for obscure prints | Seller photo rights unclear | Manual rights review only (as EN/JA seller-photo queue) |
| Japanese official Pokémon Card Game site | JA completeness | Official ToS; no bulk grant | Discovery-only |
| Pokémon Card Game Trainers Website Asia | ID/TH/ZH-Hant dense coverage | Express prohibition on copying Data | Discovery-only |
| Pokémon Korea card list | KO set fill for sparse 239 rows | Prior agreement required | Seek permission |
| Chinese simplified official / PTCG CN community datasets | zh-Hans (877 missing candidates) | Unclear / often unlicensed mirrors | Seek permission or collector photos |
| Community GitHub image mirrors of TCGdex/PTCG | Duplicate of hosts above | Usually no better rights than origin | Prefer origin hosts; avoid double-unknown provenance |
| Collector photo intake (CardScanR) | Long-tail variants, stamped promos | Requires contributor licence terms | Build after promotion policy settled |

## Languages with zero or sparse TCGdex image candidates

From global `cards.jsonl` (this run census):

- **ko**: 239 printings, **0** image candidates
- **zh-Hans**: 877 printings, **0** image candidates
- Large no-candidate residuals also in **zh-Hant** (5290), **de** (5106), **fr** (3349), **ja** global-catalogue (2949), **id** (1636), **th** (1697)

These gaps cannot be closed by TCGdex URL acquisition alone.

## 2026-10-13 TCGdex live probe (`ko_zhcn_tcgdex_card_presence.json`)

| Lang | Sets listed | Non-empty set details | Cards with `image` URL |
| --- | ---: | ---: | ---: |
| ko | 95 | 3 | **0** |
| zh-cn | 57 | 8 | **0** |

Conclusion: expanding TCGdex metadata for KO/zh-Hans does **not** unlock artwork downloads. Official permission or collector intake is required.
