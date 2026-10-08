# CardScanR EN/JA Image Independence — Final Pass Report

**Date:** 2026-10-08  
**CardScanR base:** `20a9d551` (+ this pass commits)  
**cardscanr-data base:** `97b7b12d` (+ this pass commits)  
**Branch:** `task/en-jp-image-independence-20261007`

## Verdict

`CARDSCANR_EN_JP_IMAGE_INDEPENDENCE_FINAL_PASS`

| | EN | JA | Combined |
| --- | ---: | ---: | ---: |
| Catalogue cards | 39,558 | 28,453 | 68,011 |
| Local master | **37,343** | **26,195** | **63,538** |
| CardScanR-hosted (CDN) | **37,342** | **26,195** | **63,537** |
| Unresolved | **2,215** | **2,258** | **4,473** |
| Independent (hosted) % | 94.40% | 92.06% | **93.42%** |

100% remains the target. Remaining gaps are genuine after multi-source resolution (see ledger). No fabricated matches.

## What changed in this pass

1. **Scrydex policy corrected** — written Scrydex Support authorization (2026-10-07) recorded; `imageRehostingStatus` → `approved_written_authorization_cardscanr_cdn`. Evidence: `SCRYDEX_WRITTEN_AUTHORIZATION_2026-10-07.md`.
2. **Multi-source ingestion resolver** (idempotent, resumable) — `tools/image_independence_multisource_resolver.py`:
   owned local/R2 → catalogue permitted URL (incl. Scrydex) → current TCGdex API/set map → pokemontcg by mapped set → Scrydex CDN by provider/set code → validate → hash → local master → R2 → CardScanR URL + provenance.
3. **PokéWallet-only rejected as a source conclusion** — alternates searched via set name/code/collector identity (no PokéWallet auth bypass).
4. **Dead URLs re-resolved** via current TCGdex/Scrydex identity, not stale path retry alone.
5. **Strict identity** — fail-closed on ambiguous set maps / name mismatches.
6. **Local master required** for every success; R2 upload via Cloudflare API (Wrangler OAuth).
7. **Catalogue + search** rewritten to CardScanR CDN only after hosted verification.

## Rescue accounting (vs prior PARTIAL 23,696 hosted)

Prior unresolved **44,314** → now **4,473** (rescued **39,841** into local+hosted).

| Rescue dimension | Count | Notes |
| --- | ---: | --- |
| From previous PokéWallet-only classification | **~36,591** | Prior 41,063 − remaining PokéWallet third-party ~4,472 |
| By current TCGdex re-resolution (primary provider) | **14,665** | `tcgdex_set_map*` match basis |
| By Scrydex written authorization | **25,099** | `sourceProvider=scrydex` (includes former 343 + PW/dead alternates) |
| From other permitted sources | **76** | `pokemon_tcg_api` / pokemontcg.io via mapped set |
| Prior CDN reconcile retained | **23,696** | Unchanged owned bytes |

Dimensions overlap (a former PW card rescued via Scrydex counts in both PW and Scrydex rows).

## Remaining unresolved (after multi-source exhaustion)

| Category | Count | Meaning |
| --- | ---: | --- |
| `auth_only_source_no_alternate` | **~2,689–3,614** | PokéWallet URL; alternate resolution attempted and failed |
| `ambiguous_identity` | **~928** | Set/name/collector mapping not unique — fail closed |
| `corrupt_or_unreachable_across_sources` | **855** | Candidates found but bytes unreachable/invalid |
| `dead_source_only_after_reresolution` | **~1–2** | Live catalogue still on dead pokemontcg.io after re-resolution |
| `not_yet_resolved` | **4** | Edge cases |

**Genuine PokéWallet-only remaining (catalogue still on api.pokewallet.io):** **4,472**  
**Dead-source-only remaining:** **2** (`images.pokemontcg.io`)

### Representative examples (alternates attempted)

- `pokemon|en|1455|004/009|rockets_scizor_4_winner` — PokéWallet; `set_map:no_set_mapping` / no permitted alternate.
- `pokemon|jp|23730|001/128|koffing` — PokéWallet; `set_map:ambiguous_*` fail-closed.
- `pokemon|en|ex10|!|unown` — pokemontcg.io dead after current resolution.
- `pokemon|en|22872|SVP 175|espeon_ex_175` — candidates tried; corrupt/unreachable across sources.

## Storage

- **Local master:** `D:\cardscanr-data\data\images\master\<lang>\<set>\<card-id>\` (~63,538 `display.webp`)
- **R2:** `cardscanr-card-images` (alias keys `cards/<lang>/<set>/<card_id>/display.webp`)
- **Public:** `https://cardscanr-images.andygore149.workers.dev`
- Third-party URLs retained only under `imageProvenance` for hosted cards.

## Tests / gates

See final handoff block in chat / companion `image_independence_summary.json` for exact commands run in this pass.

## Artifacts

- `reports/image_independence/CARDSCANR_EN_JP_IMAGE_INDEPENDENCE_FINAL.md` (this file)
- `reports/image_independence/en_jp_image_master_manifest.csv`
- `reports/image_independence/en_jp_unresolved_images.csv`
- `reports/image_independence/image_independence_summary.json`
- `reports/image_independence/SCRYDEX_WRITTEN_AUTHORIZATION_2026-10-07.md`
- cardscanr-data: `tools/image_independence_multisource_resolver.py`, `tools/image_independence_upload_local_master.py`, provider ledger update
