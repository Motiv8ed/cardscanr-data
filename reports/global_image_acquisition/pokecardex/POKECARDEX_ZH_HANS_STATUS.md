# PokéCardex zh-Hans — finalised status

Branch: `task/global-image-acquisition-20261013`  
Permission: written 2026-10-13 (private Archive). Images **staged only**.

## Phase 1 — reconciled arithmetic

| Item | Count |
|------|------:|
| Standard metadata | 8,931 |
| On-disk standard displays | 8,922 |
| True CDN 404s | 9 |
| Unexpected missing | 0 |
| **Sum** | **8,931** (balanced) |
| Staged `display.webp` total | 8,932 |
| Orphan/extra display paths (preserved) | 12 |
| Non-standard metadata | 6,108 |
| Variants linked to standard base | 6,108 |
| TCGdex staging preserved | 10,049 |
| Production-cleared images | 0 |

### Standard classification

| Class | Count |
|-------|------:|
| Known CN via TCGdex set (absent from catalogue) | 6,750 |
| Chinese-exclusive candidates | 938 |
| Promo unmapped uncertain | 685 |
| Parallel printing candidates | 516 |
| Utility energy | 42 |
| Catalogue zh-Hans soft hits | 0 |

Zero catalogue set-code overlap ≠ zero duplicate physical cards — most “unmatched vs catalogue” rows are known CN printings via TCGdex.

### Near-duplicates

15 art-crop near-duplicate pairs inventoried for human review (`zh_hans_near_duplicate_review.json`). No assets deleted.

## Phase 2 — catalogue review package

- Path: `catalogue_review/pokecardex_zh_hans_printings_review.jsonl`
- Canonical IDs via `cardscanr_global_catalogue.contracts`
- Attribution required; artwork placeholders / staging refs only
- **Not merged** into live `cards.jsonl`

## Phase 3 — Korean

See `../KOREAN_ACQUISITION_OPPORTUNITY.md` — TCGdex KO ~7,875 official cards proxy vs 239 catalogue printings; no KO bytes downloaded.

## Evidence ZIP

Written to the user Downloads folder (`%USERPROFILE%\Downloads\CardScanR_pokecardex_zh_hans_evidence_*.zip`).
