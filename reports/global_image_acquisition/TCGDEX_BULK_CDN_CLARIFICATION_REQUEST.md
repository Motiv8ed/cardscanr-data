# TCGdex bulk CDN / commercial rehosting — clarification request (draft)

**Status:** draft only — not sent in this run  
**Date prepared:** 2026-10-13  
**Purpose:** Seek written clarification before promoting staged `assets.tcgdex.net` files into CardScanR production CDN / public serving.

## Context

CardScanR already:
- Uses TCGdex **metadata** under the MIT `cards-database` licence (with attribution / provenance retained).
- Hosts **EN/JA** card images on CardScanR CDN after the EN/JA independence programme.
- Is staging non-EN/JA public TCGdex asset URLs into **isolated non-production** storage for identity/integrity validation only.

We understand: **MIT metadata licensing does not confer Pokémon artwork rights.**

## Clarification sought

1. May CardScanR download and cache `assets.tcgdex.net` card images server-side for its own collector app (identification, collection display)?
2. May CardScanR rehost resized WebP derivatives on its own CDN for the same product use?
3. Are there rate limits, attribution, or takedown requirements for bulk/non-EN language acquisition?
4. Does any grant exclude commercial mobile-app use?

## Interim CardScanR posture

- Staging downloads: allowed only for validation under operational public-host policy.
- Production promotion / public serving of newly staged non-EN/JA TCGdex assets: **blocked** until written clarification or explicit owner acceptance of risk.
- No sublicensing of Pokémon IP.

## Contact

Use TCGdex project contact channels documented at https://tcgdex.dev / GitHub `tcgdex/cards-database`.  
Do not send this draft without owner approval.
