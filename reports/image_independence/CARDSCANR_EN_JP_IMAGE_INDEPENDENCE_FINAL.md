# CardScanR EN/JA Image Independence — Pass 4 Final Report

**Date:** 2026-10-08  
**CardScanR base:** `fb4ec343` (+ this pass)  
**cardscanr-data base:** `2092e3c7` (+ this pass)  
**Branch:** `task/en-jp-image-independence-20261007`

## Verdict

`CARDSCANR_EN_JP_IMAGE_INDEPENDENCE_PASS4_COMPLETE`

| | EN | JA | Combined |
| --- | ---: | ---: | ---: |
| Catalogue cards | 39,558 | 28,453 | 68,011 |
| Local master | **38,312** | **27,225** | **65,537** |
| CardScanR-hosted (CDN) | **38,312** | **27,225** | **65,537** |
| Unresolved | **1,246** | **1,228** | **2,474** |
| Independent (hosted) % | 96.85% | 95.68% | **96.36%** |

Baseline before this pass: **63,537** hosted / **4,473** unresolved (93.42%).  
This pass recovered **+2,000** hosted (run stats **2,006** acquired+uploaded; **2,003** timestamped pass4 sidecars; **+1** prior local-only Unown reconciled).

No fabricated matches. No PokéWallet auth bypass. No purchases.

## Pass 4 recovery by method

| Method / match basis | Recovered | Notes |
| --- | ---: | --- |
| Scrydex CDN (provider id / code) | **1,170** | Includes JA e-Card/PCG aliases; placeholder JPEG rejected |
| TCGdex stamp collectors (`BST 006` → swsh5) | **718** | PPS1–7 underlying expansion art |
| TCGdex EN alias maps (GO, McDonald’s, …) | **87** | `swsh10.5` / `2021swsh`→`mcd21` |
| Scrydex JA authoritative aliases | **218** | E1–E5 / ADV / PCG English PokéWallet names |
| Scrydex stamp collectors | **114** | PPS stamp ids on Scrydex |
| pokemontcg.io mapped | **28** | McDonald’s `mcd21`, GO `pgo`, promos |
| Other (catalogue / authoritative) | **~3** | Edge cases |
| **Pass 4 total (sidecar-dated)** | **2,003** | Plus prior Unown local→hosted reconcile |

Provider mix (pass4 sidecars): scrydex 1170 · tcgdex 805 · pokemon_tcg_api 28.

## What changed

1. **Local-only reconcile** — uploaded `pokemon|en|ex10|?|unown` so local == hosted.
2. **JA authoritative aliases** — English PokéWallet set names → TCGdex JA ids (E1–E5, ADV1–5, PCG1–9).
3. **Promo / stamp collectors** — `SVP 175`, `BST 006` → set+number with fail-closed name checks; SWSH/SV abbreviation→TCGdex map.
4. **Image set overrides** — `swsh10.5`→`pgo`, `2021swsh`→`mcd21`, etc.
5. **Scrydex missing-image guard** — reject fixed placeholder SHA `fd7c3800…` (HTTP 200 empty art).
6. **Cross-script names** — EN catalogue vs JA TCGdex names allowed only with authoritative set+unique collector.
7. **Catalogue apply** — 65,537 cards rewritten to CardScanR CDN display URLs.
8. **Manual acquisition queue** — definitive ledger for the remaining 2,474.

## Remaining unresolved (definitive)

| Category | Count | Meaning |
| --- | ---: | --- |
| `auth_only_source_no_alternate` | **2,462** | PokéWallet-only; no permitted alternate found |
| `no_permitted_image_after_reresolution` | **5** | Alternates tried; no usable bytes |
| `not_yet_resolved` | **7** | Edge / progress gaps |

**Catalogue third-party remaining:** `api.pokewallet.io` **2,470** · `images.pokemontcg.io` **1**

### Largest remaining sets (no public permitted CDN)

| n | Lang | Set | Why blocked |
| ---: | --- | --- | --- |
| 195 | EN | Prize Pack Series Cards (22880) | Product-specific; not PPS1–7 stamp set |
| 171 | EN | World Championship Decks | Staff/deck exclusives |
| 144 | EN | Deck Exclusives | No public CDN |
| 143 | EN | Miscellaneous Cards & Products | Mixed exclusives |
| 96 | JA | EX Battle Boost | No TCGdex image set / no Scrydex art |
| 77 | EN | Blister Exclusives | Retail exclusives |
| 53 | EN | League & Championship Cards | League stamps |
| 40+40 | JA | Intro Pack Bulbasaur/Squirtle | Float collectors; no public art |
| … | … | Battle Academy / TCG Classic / Trick or Trade / Deck Kits | Product kits |

Cleared entirely in this pass (examples): Base Expansion Pack, Town on No Map, Wind from the Sea, Split Earth, PCG/ADV mapped sets, PPS1–7, Pokémon GO, McDonald’s 25th Anniversary.

## Unlock paths for further recovery

1. **PokéWallet written rehost authorization** (same class as Scrydex 2026-10-07) — unlocks most of the 2,462 auth-only rows without identity risk.
2. **Collector physical scans** — JA kits/intro packs, WCD/deck exclusives, Prize Pack Series Cards (22880), League stamps.
3. **New permitted APIs/CDNs** with clear rehost terms for product exclusives.
4. **Identity repairs** only with printed number + set evidence (fail-closed; do not guess EX Battle Boost → S9a).

## Storage / provenance

- Local master: `data/images/master/<lang>/<set>/<card-id>/`
- R2: `cardscanr-card-images` · CDN: `https://cardscanr-images.andygore149.workers.dev`
- Canonical IDs preserved; third-party URLs provenance-only on hosted cards.
- No new app runtime third-party image dependency introduced.

## Artifacts

- `reports/image_independence/en_jp_image_master_manifest.csv`
- `reports/image_independence/en_jp_unresolved_images.csv`
- `reports/image_independence/en_jp_manual_acquisition_queue.csv`
- `reports/image_independence/en_jp_manual_acquisition_queue.md`
- `reports/image_independence/pass4_recovery_breakdown.json`
- `reports/image_independence/image_independence_summary.json`
- `reports/image_independence/SCRYDEX_WRITTEN_AUTHORIZATION_2026-10-07.md`
- `tools/image_independence_multisource_resolver.py`
- `tools/image_independence_manual_queue.py`
