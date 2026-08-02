# Collector-number collision reconciliation

The staging catalogue contained 10,467 set/release and collector-number groups with more than one
normalized printing. These records were not merged by name.

- 10,466 groups are classified as provider-distinct printings sharing a reported collector number.
  Their provider record IDs and source-record hashes are distinct; the source image identities are
  also distinct where supplied. This includes Simplified Chinese products that intentionally assign
  the same printed number to multiple provider-distinct cards or treatments.
- One group was a source-field error: PokémonTCG record `zsv10pt5-80` had provider ID and image 80,
  but its `number` field said 60, colliding with Escavalier. Independent Black Bolt inventories
  corroborate Antique Cover Fossil as 080/086.
- CardScanR corrected the normalized collector number to 80, preserved the original pinned source
  payload unchanged, and added a separate versioned correction record with evidence.
- Remaining collision groups needing review: 0.
- Unclassified collision groups: 0.
- SQLite integrity / foreign keys after reconciliation: PASS / 0 failures.

The correction is declared in `config/worldwide_corrections.json` and is guarded by an exact expected
old value, so a future upstream repair cannot be silently overwritten.
