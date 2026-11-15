# Unattended CardScanR worldwide acquisition — PR #43 expansion

**Owner goal:** CardScanR finds the official sources and new sets/cards itself, archives original-language metadata and image bytes, verifies every saved file, maps it to the correct printing/variant, and prepares versioned website/Android catalogue inputs. Owner-supplied links remain *optional*, not the normal workflow.

## Already available; do not rescrape existing archive

The data repository has official website collectors for Taiwan, Hong Kong, Japan, mainland China and Korea, plus `config/worldwide_source_registry.json`, a worldwide staging SQLite DB, prior image validation checkpoints and a deterministic versioned publication exporter (`cardscanr_worldwide/publication_export.py`). The source-feed PR adds a **small bounded site-index-to-card-to-image workflow**. Do not restart old 14,276-image Taiwan/HK inventories blindly; reconcile the existing on-PC `D:\CardScanR_worldwide_runtime_20260802` and `D:\CardScanR_Archive` first.

## What now runs without owner input (where an execution environment exists)

```powershell
# See enabled official sources and gap sizes from existing staged SQLite. No network.
python tools/auto_acquire_catalogue.py --worldwide-db D:\CardScanR_worldwide_runtime_20260802\staging\worldwide-catalogue.sqlite plan

# Sources/seed URLs are preconfigured. This command *tries* download batches only
# from explicitly enabled, scoped, robots-permitted sources; it does not ask for URLs.
python tools/auto_acquire_catalogue.py --archive D:\CardScanR_Archive --max-items 50 --per-source-items 25 collect

# Create an independent, immutable QA/review package from actual saved files.
# Existing source provenance and exact card/variant mappings must pass the gate.
python tools/auto_acquire_catalogue.py --archive D:\CardScanR_Archive --worldwide-db D:\CardScanR_worldwide_runtime_20260802\staging\worldwide-catalogue.sqlite stage --crosswalk D:\CardScanR_Archive\crosswalk_reviewed.jsonl --output D:\CardScanR_Archive_Exports\review-20261115
```

The default official-source entries are provided in `config/catalogue_source_feed.json` for TW, HK, CN, KR, JP. TW/HK have **card search list** seed pages with pagination and detail links. Those entries remain disabled until reviewed for source-specific robots/terms and parsing accuracy. The program does not silently guess that a source is authorised. Disabling a source blocks network collection, not planning and verification. Site JavaScript or changing URL structures require adapting the existing source-specific collector, not guessing HTML results.

### Real processing chain

1. **Discover** provider roots and set/card indexes from configured official sources; scan release pages and follow pagination at low rate. No owner-supplied link required. The scheduled worker should also run existing official specialised collectors and check new set releases, respecting publisher access limits.
2. **Gap check** against actual worldwide staging DB by `region_code`, `language_code` and exact printing, separately measuring candidate URLs versus technically validated assets. Never replace the existing canonical ID namespace.
3. **Collect** bounded HTML and original image bytes to a per-source, checksum-named private archive. SQLite checkpoints resume after each completed item. Robots checks, HTTPS host/path scoping, response-size limits, no redirects to unexpected destinations, and a single-writer lock apply.
4. **Stage** an immutable `source_records.jsonl`, `card_candidates.jsonl`, `app_image_import_candidates.jsonl`, and `summary.json`. Re-read files and recompute SHA256/byte-size from disk, join images to the original card page, preserve metadata, language, region, rights and original URL. A missing file, bad hash, no exact crosswalk, unknown rights or unknown canonical variant must **not** enter the app-import candidate list.
5. **Import and publish after evidence gates.** The candidate JSONL is a safe *input* to the existing worldwide canonical importer and versioned `publication_export.py` pipeline. It is **not** an already-installed Android SQLite pack or permission to publish images. The app and website must load the same verified mapping; testing image rendering, immutable version IDs, delta installs and rollback is the separate release gate.

A reviewed crosswalk JSONL line uses `source_id`, `source_card_url`, `match_status: "verified"`, `canonical_printing_id`, `card_variant_id`. The linked candidate must exist in the read-only worldwide staging DB. Rights must explicitly be `approved_for_mirror`. `publicationApproved` stays **false** even for staged candidates; a deliberate separate release action is required.

## Remaining deployment dependency (important)

The CardScanR PC Workbench v0.8.6 does **not** expose an allowed catalogue batch-execution action and blocks arbitrary shell. ChatGPT's two-hour schedule cannot itself start the Windows Python collector through this Bridge. Implement a narrowly scoped `catalogue_collect_batch` dispatcher **or** use a dedicated allowlisted existing Windows/n8n runner with 2-hour cadence, file-lock/lease, bounded time/items, timeout and status/stop/report receipts. It must invoke only preconfigured source IDs, not arbitrary commands/URLs, and report real per-language new saved image bytes/hashes and exact staging matches. Until this exists and a 10–25-item real local batch is verified, **automatic collection is not live**. Do not label a PR merge as a successful collector run.

## Verification

`python -m unittest discover -s tests -p test_auto_acquisition.py -v`

The standalone auto-acquisition unit suite exercises source discovery planning, unattended bounded batch selection without owner URLs, metadata coverage gap counting, image SHA/byte verification, unsafe/unmatched images staying out, and positive exact variant crosswalk staging. It is offline unit evidence, not a substitute for live official-site collection.