# CardScanR Global Rollout — Master Status

Classification: **PARTIAL**

- Current phase: Phase 10D planned; stopped before image canary execution
- Branch / HEAD: `main` / `c99e82b92fb8d3ef25c7dac50c698c739389b514`
- Languages: de, en, es, es-419, fr, id, it, ja, ko, nl, pl, pt-BR, ru, th, zh-Hans, zh-Hant
- Canonical printing groups: 117665
- Canonical sets: 1495
- Public/free image candidates: 92817
- Verified R2 thumbnails/displays: 0/0
- Migrated existing images: 0
- Unresolved identities: 60905
- Variant-unresolved groups: 117665
- Projected image storage: 11.286 GiB
- Estimated rounded monthly R2 storage cost: US$0.045

## Blockers

- TCGdex artwork rehosting permission is not explicit; public image candidates cannot be copied to R2.
- All set-level canonical records remain cardVariant=unspecified, so physical finish identity is provisional.
- 260 existing Supabase thumbnails lack a safe exact global crosswalk.
- Projected R2 storage is 12.584 GB, above the 10 GB-month free tier; estimated rounded storage cost is US$0.045/month, while the configured unexpected-spend budget is US$0.
- Scrydex requires a paid Starter plan and its terms prohibit mirroring without prior written authorization.
- Production publication and R2 image writes require explicit approval.

## Safety

- Production catalogue/index publication: **not performed**
- Flutter repository modification: **not performed**
- R2 image writes/deletes: **not performed**
- Supabase deletes: **not performed**

Next safe command: `python tools/global_rollout.py status`
Resume command: `python tools/global_rollout.py resume`
