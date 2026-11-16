# CardScanR user-fed catalogue acquisition (private archive only)

This PR adds an **owner-editable feed of sites and exact card/image URLs** for bounded, resumable research. It **does not** auto-enable new providers, run a worker on the owner's PC through the current Bridge, publish to Supabase, or grant image reuse rights.

## Feed in a new place

Edit `config/catalogue_source_feed.json` (or use a copy outside Git). For each source set its unique `id`, language **and** region, exact `page_hosts`/`page_paths`, image CDN `image_hosts`/`image_paths`, optional CSS selectors and a list of seeds. Region is important: TW and HK are separately identifiable despite both being `zh-Hant`. A `seed` has a `kind` (`index`, `card`, or `image`) and HTTPS `url`. Direct images also require a human-supplied `printing_key` so unassigned art is not silently treated as an exact match.

To register a **completely new source site** without writing code:

```powershell
python tools/catalogue_source_feed.py add-source --id ko-archive --name "Korean archive" --region KR --language ko --page-host cards.example.net --page-path /cards/ --image-host images.example.net --image-path /originals/
python tools/catalogue_source_feed.py add-url --source ko-archive --kind index --url "https://cards.example.net/cards/"
```

This command creates the source as disabled with rights marked `permission_pending`. You must verify site use terms and adjust the CSS selectors before enabling. Host and path scopes are never implicitly expanded to arbitrary websites.

Existing official sources for TW, HK, CN, KR and JP are included as **disabled examples**; selectors and robots/terms need source-specific verification before enabling a site. To add a new provider, copy a source entry under a new ID with a narrowly scoped host and URL path. Avoid mirroring restricted or unclear-rights images publicly. The existing `config/worldwide_source_registry.json` remains the rights/provenance authority for publishing decisions.

```powershell
# Check and inspect locally, no network access
python tools/catalogue_source_feed.py validate
python tools/catalogue_source_feed.py collect --source kr-official --dry-run

# Paste a new exact Korean card page or Taiwanese set listing into the feed
python tools/catalogue_source_feed.py add-url --source kr-official --kind card --url "https://pokemoncard.co.kr/cards/EXAMPLE"
python tools/catalogue_source_feed.py add-url --source tw-official --kind index --url "https://asia.pokemon-card.com/tw/card-search/"

# If manually supplying an image URL, provide its claimed *printing* identity for later review
python tools/catalogue_source_feed.py add-url --source tw-official --kind image --url "https://asia.pokemon-card.com/tw/card-img/EXAMPLE.png" --printing-key "TW:EXAMPLE_SET:007/100:regular"

# After checking source terms/robots, selectors, rights and setting enabled=true
python tools/catalogue_source_feed.py collect --source kr-official --archive D:\CardScanR_Archive --max-items 25 --pace-seconds 2
```

For website indexes, `selectors.card_links` discovers individual pages, `selectors.next_page` finds pagination, `selectors.card_image` discovers image assets on card detail pages, and `selectors.fields` extracts known text fields (name, set_name, collector_number, printed_total, rarity, hp, types, artist, description). The selectors are source-specific and may require updates. If HTML is JavaScript-rendered or pagination uses a different interface, use existing site-specific collectors such as `pokemon_asia.py`, `pokemon_japan.py`, `pokemon_korea_archive.py` rather than circumventing protections or claiming full-site coverage. When more image hosts or paths are required, review each explicitly before expanding the allowlist. Copying a working source URL into the feed does **not** guarantee that parser selectors or rights are valid.

## What a real batch does

- Validates HTTPS, exact public host and path allowlists, rejects IP literals/local hosts, verifies DNS isn't private, and blocks unreviewed redirects; honours robots.txt, operator pacing and one-at-a-time archive locks. No auth, bypass, paywall or CAPTCHA workarounds.
- Runs up to 100 attempted queue items per invocation (25 by default). Fetches at most 2 MiB per page and 16 MiB per artwork file; rejects bad MIME and invalid images. Newly discovered links are queued within the same host and allowed paths.
- Writes immutable SHA256-named original files under `ARCHIVE/sources/<source>/<kind>/`, records original URL, region, language, bytes, dimensions, owner-supplied print key if any, fetch timestamp, rights status and explicit `public_release_approved=false` in `ARCHIVE/source-feed-checkpoint.sqlite`.
- Resumes from SQLite checkpoint; completed pages/images aren't fetched again. Failed URLs are recorded (not silently called recovered). Separate review/import must map source identifiers to existing CardScanR canonical printings and their actual variants; **source URLs and user-supplied printing keys aren't canonical validation**.

## The two-hour schedule is not yet an on-PC downloader

ChatGPT's `Weekend Picks + Catalogue` task can check/coordinate work every two hours, but its CardScanR PC Workbench v0.8.6 currently has **no scoped collector-execution action**. A PR merged into this repository does not automatically install or start a Windows/n8n job. For actual unattended downloads: install/run this CLI using the owner's supervised Windows/n8n worker or add a **scoped** Bridge `catalogue_collect_batch` action that invokes only this CLI with explicit source ID, max_items, archive root, lock, timeout, status receipt and kill switch. Never add generic shell or arbitrary URL execution. Prove one 10–25 item batch on the PC, check image hashes and files, then enable the two-hour task to trigger/verify the bounded dispatcher. A locally installed repeating worker may also operate between schedule runs.

## Tests

`python -m pytest tests/test_catalogue_source_feed.py -q`

Offline fixtures validate source additions, SSRF/path/port limits, disabled-source safety, robots/redirect refusal, real PNG decoding/SHA storage, bounded batch results, and checkpoint resume. A **live network test and a Windows Bridge/Task Scheduler activation test are still required** before claiming continuous acquisition works.