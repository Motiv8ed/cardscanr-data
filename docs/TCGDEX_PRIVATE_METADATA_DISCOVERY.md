# TCGdex multilingual metadata discovery (private review only)

This task discovers a **small bounded sample** of recent sets and card metadata from TCGdex, for comparison against CardScanR's existing worldwide staging database. It is not a replacement for the existing worldwide canonical identity or publication pipelines.

## Scope and safeguards

- Fixed HTTPS origin: https://api.tcgdex.net/v2.
- Languages: Japanese (ja), Traditional Chinese (zh-tw), Korean (ko), English (en), Thai (th), Indonesian (id), optionally Simplified Chinese (zh-cn).
- Max 3 set-detail samples and 25 card records per set per language; Korean card index may contribute up to 300 extra **metadata-only** records with exact set-prefix checks; serial requests with pacing and 2 MB response limit; cross-domain redirects refused.
- No card artwork downloaded, prices, customer data, Supabase, Cloudflare/R2 writes, or app release.
- Each candidate retains provider card ID, set ID, language, region hint (not proof of distinct regional printing), source URL and response SHA256.
- Finish/variant and canonical CardScanR printing IDs remain unverified; metadata is a candidate.
- Outputs card_candidates.jsonl plus a SHA256 manifest in a new empty output directory.
- TCGdex's database MIT license does not grant Pokemon card-artwork redistribution rights. Preserve existing provider approval and publication gates.
- A partial status is reported when at least one language has no accepted candidates or a set/request failed; zero candidates return nonzero. Set indexes may omit release dates, so undated entries preserve original provider index order rather than assuming lexical ID order establishes recency. Korean card-index entries are admitted only when the card ID matches a unique ID from the same Korean set index.

## Run locally or in GitHub Actions

From the cardscanr-data checkout:

    python tools/tcgdex_metadata_batch.py --languages ja,zh-tw,ko,en,th,id --max-sets 1 --max-cards 20 --output D:\CardScanR_Archive\metadata_discovery\batch-unique-name

Offline fixture checks:

    python -m unittest discover -s tests -p test_tcgdex_metadata_batch.py -v

The workflow TCGdex Private Metadata Discovery runs unit tests on pull requests. On a branch-scoped push or manual workflow dispatch, it also runs one bounded live metadata sample and retains a private artifact for 7 days. It does not set up a periodic task and does not publish the metadata or images.

## Next gate

Compare records with the existing worldwide staging tables by exact provider/region/language identity; report new candidates and duplicate provider IDs. Do not overwrite canonical IDs, physical variants, or user ownership. Import approved metadata only through the existing staged pipeline; artwork remains blocked pending rights evidence, verified image identity, versioned release testing and rollback.
