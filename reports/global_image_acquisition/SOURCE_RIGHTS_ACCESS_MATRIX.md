# Global Pokémon Image Source — Rights / Access Matrix

Generated as part of `task/global-image-acquisition-20261013`.  
Distinguish **technical availability** from **permitted CardScanR use**.

| Source | Languages (tech) | API / bulk | Auth | Image bodies | Staging download | Production CDN rehost | Cost | Decision this run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| TCGdex assets (`assets.tcgdex.net`) | de,en,es,es-419,fr,id,it,ja,pt-BR,th,zh-Hant (+ metadata langs) | REST + set lists; FAQ asks bulk users to cache | none | Public URLs | **Yes** (staging_only) | Pending written permission | $0 | Execute staging acquire; do not promote |
| TCGdex cards-database (GitHub) | multilingual metadata | Git clone / MIT DB | none | Artwork **not** covered by MIT DB licence | N/A (metadata) | N/A | $0 | Metadata reuse OK with attribution; no artwork grant |
| Pokémon TCG API (`images.pokemontcg.io`) | en | Paginated REST; key raises limits | optional key | Public image host | **Yes** (staging_only) | Pending global review (EN/JA already mirrored historically) | $0 | Staging OK; no mass EN re-acquire |
| Scrydex (`images.scrydex.com`) | en, ja | Paid REST (credits) | API key + team | CDN URLs | **Yes** | **Approved** written auth 2026-10-07 | Paid plan | No new paid API calls; EN/JA master already complete |
| PokéWallet | en,ja,zh-*,ko,EU,… | Authenticated image endpoints | API key | Auth-gated | **No** (discovery-only) | Unclear / needs written clarification | Free tier limited; Pro paid | Do not bulk download |
| Official Pokémon.com card DB | en + localised | Consumer site only | n/a | Site images | **No** | No bulk grant | $0 | Discovery / reference only |
| Pokémon Asia trainers sites (id/th/tw/…) | id, th, zh-Hant, … | Consumer search | n/a | Site Data | **No** | Terms prohibit copy/display outside Services | $0 | Discovery only — do not scrape |
| Pokémon Korea official | ko | Consumer site | n/a | Site images | **No** | Prior agreement required | $0 | Discovery only |
| Official CN catalogues / community CN datasets | zh-Hans | Varies | varies | Varies | **No** until written grant | Unclear | unknown | Discovery only |
| GitHub community card dumps / fan archives | varies | ad-hoc | n/a | Often unlicensed scrapes | **No** by default | High copyright risk | $0 | Discovery; do not ingest without provenance+rights |
| Collector submissions | any | CardScanR intake | user grant | Photos | Future path | Needs contributor licence | $0 | Gap path for missing printings |
| Ximilar | n/a | Recognition API | token | Not an artwork source | N/A | N/A | credits | Not used for acquisition |

## Operational posture reused from EN/JA independence

- Production EN+JA master/CDN: **68,011 / 68,011** verified (`image_independence_summary.json`).
- Scrydex written authorization retained under `reports/image_independence/SCRYDEX_WRITTEN_AUTHORIZATION_2026-10-07.md` (app repo mirror).
- Global catalogue `direct_images.jsonl`: **92,817** TCGdex candidates; mirror status still `pending_human_review`.

## Hard stops (need human decision)

1. Written permission (or explicit accepted risk) to **promote** TCGdex/pokemontcg staging bytes into production CDN for non-EN/JA.
2. PokéWallet written clarification for authenticated image archive rehost.
3. Official regional catalogue commercial ingestion permission (Asia / Korea / CN).
4. Any paid Scrydex/PokéWallet/Ximilar spend above $0.
