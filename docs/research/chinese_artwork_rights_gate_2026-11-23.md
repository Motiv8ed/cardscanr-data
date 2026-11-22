# Chinese artwork provenance gate — 2026-11-23

## Evidence from repository main (review required)
- Commit `9b42ee10440ff44feab674d6023b46d884c5657a` describes private Chinese image URL references using a CardCorpus `index.sized` path.
- Commits `4710b26d15c71ec835b0e348231dad3b90366ee5` and `89e9e0a132caec06975cb63fb442a5bab1bd34a8` describe 1,925 and 1,576 exact Chinese printing crosswalk matches. These are pre-existing records, **not new live imports**.
- CardCorpus CC BY 4.0 metadata licensing does **not** by itself license Pokémon artwork or establish permission to reproduce or redistribute images.
- duanxr/PTCG-CHS-Datasets is excluded from commercial ingestion under the stated restriction.

## Mandatory independent gates
1. Verify rights/terms for the exact source image host and specific proposed use; record license or written permission evidence separately for artwork versus metadata.
2. Require HTTPS and exact hostname/path allowlists, official printing identity, source response SHA256, locale, set, number, native name, finish and physical variant.
3. Cross-check authenticated *current* missing canonical IDs and private source URL inventory; quarantine ambiguous art and previously staged/imported rows.
4. Classify crosswalk candidates separately from verified official image URL evidence and actual live distinct linked cards.
5. Do not download image binaries, publish artwork, stage uncleared references, or write production data on this audit branch.

## Unresolved
No independent artwork reuse grant was established in this audit. Therefore the referenced CardCorpus paths must **not** be represented as rights-cleared official Pokémon China image URLs. Existing crosswalk counts cannot be counted as new live links without authenticated vault verification.

## Next evidence acquisition
Use first-party Chinese card search/detail pages to collect bounded HTML response hashes and variant IDs, subject to robots/terms checks. Compare to a current authenticated missing-card export. Only then generate quarantine-reviewed private candidate manifests; do not assume that official public visibility grants commercial image reuse.
