# Read-only TCGdex metadata reconciliation and Korean gap diagnosis

## Purpose

Use a real TCGdex discovery batch to distinguish already represented card metadata, provider candidates absent from the staging snapshot and ambiguous printing identities. A record missing from the snapshot is **not automatically a newly discovered physical printing**.

Input: unmodified GitHub Actions ZIP containing manifest.json and card_candidates.jsonl. The reconciler verifies SHA256, byte count, row count and provider identity.

## Run against the CardScanR staging snapshot

Download the metadata artifact from:
https://github.com/Motiv8ed/cardscanr-data/actions/runs/44227144510

On the owner PC with a **checkpointed, read-only copy** of the worldwide staging database:

    python tools/reconcile_tcgdex_metadata_batch.py --archive "D:\CardScanR_Archive\metadata_discovery\CardScanR-TCGdex-Metadata-Expanded-20261118.zip" --database "D:\CardScanR_worldwide_runtime_20260802\staging\worldwide-catalogue.sqlite" --output "D:\CardScanR_Archive\metadata_discovery\reconciled-20261118"

The reconciler opens SQLite in mode=ro&immutable=1, checks for active WAL and refuses live/uncheckpointed databases. Exact matching requires a TCGdex source provider, same set release code, same language, and same collector number. Multiple matches stay ambiguous, and any recognized record retains its unverified physical variant status.

Without a staging snapshot, omit --database for integrity validation only. All candidates will be classified requires_staging_snapshot; no missing-card claims can be made from that.

Outputs in a new empty directory: reconciliation.jsonl and report.json, with per-language counts and hashes. Nothing writes to app, staging, Supabase, Cloudflare/R2 or images.

## Korean source diagnosis

The first three sampled TCGdex Korean sets had no card records. This is a sampling/provider coverage gap, not proof that Korean cards don't exist.

The bounded Korean probe queries:
- The fixed Korean set index at https://api.tcgdex.net/v2/ko/sets
- The fixed Korean card index at https://api.tcgdex.net/v2/ko/cards
- At most 12 set details; it favours index entries that explicitly report nonzero card counts.
- Only metadata counts, hashes and errors. No images or production writes.

If provider coverage is incomplete, investigate CardScanR's existing Korean collectors and each source's permissions before any acquisition.

## Next integration gates

Review actual read-only staging matches. Quarantine ambiguous region and finish variants, preserve canonical IDs and user ownership, then stage approved metadata using the existing worldwide importer. Artwork acquisition/public release remain separately permission-gated.
