# Korean (KO) Pokémon TCG image sources — investigation

Generated: 2026-10-13 (updated during global-image-acq worktree session)

## Scope and constraints

- Investigate only; no purchases, no restricted downloads, no production promotion.
- Prefer authorised APIs, explicitly permitted bulk downloads, or collector contributions with clear rights.
- PokéCardex written permission (2026-10-13) covers Chinese hosting retrieval only; PokéCardex has **no KO region**.

## Candidates reviewed

| Source | Type | Auth / licence posture | Image usefulness | Decision |
|--------|------|------------------------|------------------|----------|
| **TCGdex API** (`api.tcgdex.net`) | Public HTTP API | Open data for catalogue metadata; artwork URLs point at third-party/official hosts — MIT-style metadata ≠ Pokémon IP grant | KO language exists in TCGdex ecosystem when sets are populated; must validate per-URL rights before staging | **Primary technical candidate** for metadata + gated staging *only after* URL rights check (same pattern as non-EN/JA TCGdex lane) |
| **Pokémon TCG API** (pokemontcg.io) | Public API | API terms; images typically Pokémon Company / set-publisher assets | Strong EN coverage; KO coverage historically limited / incomplete | Monitor; do not bulk-mirror without rights review |
| **Official Pokémon / TPCi / Korean publisher pages** | Official web | Highest legitimacy when download or press assets are offered | Often promo-only, not full set scans | Prefer for promo gaps; not a full-set pipeline |
| **PokéCardex** | Fan catalogue | Written permission for CN metadata + CN image *host retrieve*; no KO | N/A for KO | Out of scope for KO images |
| **Collector contributions** | User upload | Requires contributor licence + Pokémon IP still pending for production | Can fill rare KO gaps | Accept only with signed contribution terms; stage privately |
| **Scrape of commercial marketplaces / scan blogs** | Unauthorised scrape | Typically disallowed | High legal risk | **Rejected** |

## Recommended path (no acquisition started)

1. Inventory TCGdex `ko` sets/cards counts and image URL hosts (metadata-only probe).
2. Classify each image host: official CDN vs fan mirror vs unknown.
3. Gate any future KO staging behind the same Archive staging root pattern as TCGdex non-EN/JA, with `productionPromotionAllowed=false`.
4. Parallel track: optional collector contribution intake form (rights attestation) — design only until product asks.
5. Do **not** hotlink; do **not** promote KO artwork to production without Pokémon/IP review.

## Probe notes (session)

- PokéCardex CHN discovery confirmed; no KO zone in series menu used for zh-Hans work.
- TCGdex zh-cn set list successfully fetched for CN crosswalk.
- TCGdex `ko` sets probe (metadata only): **95** sets, **95** with official cardCount — see `korean_tcgdex_probe.json` and `pokecardex/tcgdex_sets_ko.json`.
- No KO image bytes downloaded in this session. Image host rights still unreviewed.

## Status

`investigation_only` — no KO downloader, no staging root created, no spend.
