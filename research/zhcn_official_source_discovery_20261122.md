# Simplified Chinese official-source discovery evidence — 2026-11-22

Status: RESEARCH ONLY. No source card records imported, no artwork downloaded, no image URLs staged, no rights clearance.

## Directly observed official first-party entry points

1. https://www.pokemon.cn/tcg/other/17199.html — official announcement (2022-10-26) describes the "宝可梦卡牌会员" WeChat mini-program: native card search by product number, Pokémon type, ability, energy, and card detail including evolution relationships. It reports nearly 1,000 card types in the first release. **This is a source-discovery lead, not an accessible bulk API or license.**
2. https://www.pokemon.cn/tcg/other/19154.html — official announcement (2025-11-25) of "宝可梦卡牌收藏工具" WeChat mini-program, explicitly showing per-number print-style variants and foil patterns. This establishes why product number + name alone is insufficient for exact physical printing matching.
3. https://www.pokemon.cn/tcg-rules-regulation — official rules page (updated 2026-11-16) states regulation mark and product number identify printed cards, and special cards may position product number elsewhere. Useful identity fields, not an image reuse grant.
4. https://www.pokemon.cn/tcg/other/17201.html — first-party release notice (2022-09-28) confirms the mainland Simplified Chinese product launch on 2022-10-28.

## Separate third-party discovery (do not ingest without permission)

https://pktcg.app/cn/series/ displays a third-party catalogue of 13,045 cards across 128 sets at inspection. This count is a site-level display, not 13,045 missing live CardScanR identities, and its data/artwork license was **not verified**. Do not scrape or import it on the strength of this evidence.

## Safe acquisition contract

- Obtain robots and terms/rights clearance before automated collection.
- Capture original source URL, retrieval time, HTTP status, source-body SHA256, language zh-cn, set/product code, native name, printed number, regulation mark, rarity, variant/foil identity and official detail-page ID where available.
- Quarantine incomplete or ambiguous variant identities; never map same-name or same-number prints by inference.
- Compare exact identities against authenticated live missing printing records; record candidate count separately from confirmed new live links.
- No downloads, mirroring, public artwork or URL staging until explicit rights verification.
- Exclude duanxr/PTCG-CHS-Datasets (noncommercial/no redistribution) and do not treat CardCorpus CC BY metadata as artwork rights.

## Run receipts

Evidence generated from publicly accessible official webpages on 2026-11-22. Newly acquired card-level image URLs: **0**. Verified new canonical identities: **0**. Live vault writes: **0**. New source leads: **4 official first-party pages + 1 third-party catalogue lead**. No claim of commercial reuse permission.
