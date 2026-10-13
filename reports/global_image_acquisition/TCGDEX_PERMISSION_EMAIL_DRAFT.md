# TCGdex permission email — draft only (do not send)

**Status:** draft for owner review — not sent  
**Prepared:** 2026-10-13  
**Suggested To:** TCGdex maintainers (https://tcgdex.dev / GitHub `tcgdex/cards-database` / project contact)  
**Suggested Subject:** Permission clarification — CardScanR commercial use of `assets.tcgdex.net` card images

---

Hello TCGdex team,

We build **CardScanR**, a commercial Pokémon TCG collector / identification mobile app. We already use TCGdex **card metadata** under the MIT licence for the `cards-database` project, with attribution retained.

We are writing to request **written clarification** about use of card **images** hosted on `assets.tcgdex.net` (separate from the MIT metadata licence).

### What we need clarified

1. **Bulk CDN access / download** — May CardScanR download card images from `assets.tcgdex.net` in bulk (rate-limited, non-abusive) for server-side caching?
2. **Long-term storage** — May we retain those originals (and derived WebP thumbnails / display sizes) in our own storage indefinitely for product operation and backups?
3. **Commercial in-app display** — May CardScanR show those images (and our resized derivatives) to end users inside our paid / free commercial app for identification and collection management?
4. **Thumbnails and rehosting** — May we rehost resized thumbnails and display assets on CardScanR’s own CDN (currently Cloudflare R2) rather than hotlinking `assets.tcgdex.net` at runtime?
5. **Attribution / conditions** — Any required attribution, rate limits, geographic limits, language limits, or takedown process we should follow?
6. **What is not requested** — We are **not** asking TCGdex to grant Pokémon Company / Creatures / GAME FREAK intellectual-property rights. We understand TCGdex distribution permission (if any) is independent of underlying Pokémon artwork copyright, and that MIT metadata licensing does **not** confer Pokémon artwork rights.

### Current interim posture

Until we have written clarification (or an explicit owner risk acceptance):

- Non-English / non-Japanese TCGdex images may be staged only in **non-production** validation storage.
- We will **not** promote or publicly serve newly acquired TCGdex artwork into production CDN.
- We will not sublicense Pokémon artwork.

Happy to adapt to a preferred licence text, attribution line, or allowlist process.

Thank you for maintaining TCGdex and for any guidance you can provide.

Best regards,  
[CardScanR owner name]  
[contact email]  
[company / app store listing if useful]

---

## Operator notes (internal)

- Do **not** send without owner approval.
- Keep a copy of any reply under `reports/global_image_acquisition/` with date stamp.
- A “yes” from TCGdex still does not replace Pokémon IP clearance for public marketing assets if counsel requires it.
- KO / zh-Hans artwork is largely **absent** from TCGdex image fields; this email does not unlock those languages.
