# PokéCardex zh-Hans acquisition — final status

Worktree branch: `task/global-image-acquisition-20261013`  
Permission: written 2026-10-13 (private Archive evidence). Chinese images **staged only**.

## Acquisition (standard inventory)

| Metric | Count |
|--------|------:|
| Metadata rows (all versions) | 15,039 |
| Standard (v1) target | 8,931 |
| Acquire progress `ok` | 8,922 |
| Permanent CDN 404 (true gaps) | 9 |
| Staged `display.webp` | 8,932 |
| CS1AC recovered (CDN `CS1aC`) | 217 |
| NRGY recovered (sort-keyed) | 43 |
| Preserved TCGdex staging | **10,049** |

True remaining CDN gaps are primarily a handful of 30THC / SMP / SVP slots that 404 on BunnyCDN. Energy rows that printed `—` are staged under `s{sort}_v1`.

## Layers (keep distinct)

| Layer | Count |
|-------|------:|
| Metadata coverage (standard) | 8,931 |
| Acquired images | 8,932 |
| Visually verified | 8,932 |
| Production-cleared | **0** |

Near-duplicate art pairs across different collectors: 14 (flagged for review; not production-cleared).

## Crosswalk PokéCardex ↔ TCGdex

- 59 CHN sets → **40** mapped to TCGdex zh-cn (31 exact + 9 half-set `CS15C↔CS1.5C`)
- EN bridges: 2 (incl. manual Collect 151 / 30th promo)
- Unmatched sets: 17 (promos, radiant-energy packs, some CN exclusives)
- Standard rows inside mapped TCGdex sets: **6,750** (known CN printings missing from CardScanR catalogue — not automatically net-new)
- Genuinely unmapped standard candidates: **2,181** (need human/visual review before calling “new printings”)
- Non-standard 6,108 rows: reverse / reverse-masterball / special-alternate **variants** of collector numbers

Zero catalogue set-code overlap ≠ zero duplicate cards.

## Attribution (isolated website, not deployed)

Footer + `/data/` FAQ: “Data courtesy of PokéCardex” → https://pokecardex.com

## Korean

Investigation only — TCGdex `ko` 95 sets; no KO bytes downloaded. See `../korean_image_sources_investigation.md`.

## Evidence ZIP

`D:\CardScanR_Archive\Downloads\CardScanR_pokecardex_zh_hans_evidence_*.zip`  
(Private permission body remains under Archive `rights_evidence\pokecardex\`.)
