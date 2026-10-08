# CardScanR EN/JA Image Independence — Pass 5 Discovery

**Date:** 2026-10-08  
**Bases:** CardScanR `6bda31a7` · cardscanr-data `27239764`  
**Branch:** `task/en-jp-image-independence-20261007`

## Verdict

`CARDSCANR_EN_JP_IMAGE_INDEPENDENCE_PASS5_DISCOVERY_COMPLETE`

Every remaining unresolved record (2,474) was searched with the pass5 multi-source discovery pipeline. Auto-rehost stayed limited to permitted independence hosts (TCGdex / pokemontcg.io / Scrydex written auth, plus Wayback of those hosts). Fan/official/search hits were queued for rights review — never used to bypass PokéWallet auth.

| Metric | Value |
| --- | ---: |
| Hosted after pass5 | **65,538** |
| Independent % | **96.36%** |
| Remaining unresolved | **2,473** |
| Acquired+hosted this pass | **1** |
| Rights-review candidate rows | **2,516** |
| Awaiting permission (Serebii etc.) | **16 cards / 43 URLs** |
| Auth-only after full pass5 search | **2,457** |
| Genuinely empty discovery | **0** (PokéWallet URL retained as gated evidence on nearly all) |

## Recovered by source (auto-acquire)

| Source | Count |
| --- | ---: |
| Catalogue permitted URL (`images.pokemontcg.io`) | **1** (`pokemon|en|ex10|?|unown`) |
| Wayback of permitted hosts | 0 |
| Lightweight Scrydex/pokemontcg constructions | 0 (no valid bytes / placeholders) |

## What was searched (all 2,474)

1. Catalogue URLs — PokéWallet recorded as auth-gated (no Wayback bypass); other hosts Wayback CDX + live fetch when permitted.
2. Lightweight stamp/promo/external-id CDN constructions (no paid APIs).
3. Limitless TCG HTML search (EN) — concrete `/cards/{set}/{num}` paths only.
4. Serebii card pages (EN set-slug heuristics).
5. Official `pokemon-card.com` keyword search (JA).
6. DuckDuckGo HTML fallback when no non-PokéWallet review candidates surfaced.
7. Brave Image Search API — **not used** (no API key; paid registration not approved).

## Rights-review queue (do not silent-rehost)

| Source | Rows | Rights status |
| --- | ---: | --- |
| Catalogue PokéWallet | 2,470 | `pokewallet_auth_gated_no_bypass` |
| Serebii images | 43 | `fan_site_rights_review` |
| Other catalogue URLs | 3 | `unknown_host_rights_review` |

CSV: `reports/image_independence/en_jp_pass5_rights_review_candidates.csv`

## Sources blocked without payment / written permission

- Brave Image Search API (no key; paid registration not approved)
- PokéWallet image API (auth-gated; needs written rehost auth class of Scrydex 2026-10-07)
- `pokemon-card.com` official assets (registry: metadata_only)
- Pokellector (watermark; evidence-only in existing worldwide tooling)
- Serebii / Bulbagarden / Limitless / pkmncards (fan/community — review only)
- Marketplace listing photos (link/evidence only)

## Unlock paths

1. PokéWallet written rehost authorization → unlocks ~2,470 gated catalogue images.
2. Written permission for official JP assets and/or selected fan CDNs with clear terms.
3. Collector scans for WCD / deck exclusives / academy kits / JA intro packs with no public permitted CDN.
4. Approve paid Brave (or similar) image API for discovery only, then rights-gate acquisitions.

## Artifacts

- `tools/image_independence_pass5_discovery.py`
- `tools/test_image_independence_pass5_helpers.py`
- `reports/image_independence/pass5_discovery_summary.json`
- `reports/image_independence/en_jp_pass5_rights_review_candidates.csv`
- `reports/image_independence/en_jp_pass5_undiscoverable_or_blocked.csv`
- `data/images/independence/pass5/discovery_progress.jsonl`
