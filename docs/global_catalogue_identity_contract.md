# Global Canonical Printing Identity Contract

Every catalogue row represents one exact physical printing. Identity is not inferred from a card name or artwork.

## Identity hierarchy

- `canonicalSetId`: game + language + evidenced region + canonical provider set identity.
- `canonicalBaseId`: game + language + region + canonical set digest + normalized collector number.
- `canonicalPrintingId`: canonical base + evidenced physical variant.
- `canonicalArtworkId`: populated only when artwork equivalence is independently proven; otherwise `null`.

The current TCGdex set-level ingestion uses `cardVariant=unspecified`, so records remain provisional rather than
claiming that holo, reverse-holo, stamped, promo, first-edition, and other printings have been fully separated.

## Required evidence

Exact identity requires language, region where relevant, set identity, collector number, and variant evidence.
Name-only, Pokémon-only, visually similar artwork, translated set names, and matching collector numbers in another
set are never sufficient.

## Provenance and confidence

Provider card/set IDs and source language are retained. Stronger verified facts may enrich a record; weaker data
cannot overwrite them. Conflicts and one-to-many crosswalks are quarantined. `canonicalArtworkId`, rarity,
regulation mark, designation, and English aliases remain null or empty when the set response does not prove them.

## Identifier stability

Identifiers are deterministic and percent-escaped. Existing app identifiers remain available in
`provider_crosswalk.jsonl`; no production identifier is rewritten by this staging rollout.

## Image safety

An image candidate is not a verified image. Artwork is wired only after exact identity, terms, download validation,
normalization, immutable R2 upload, and object verification all pass. Provider URLs remain internal provenance.
