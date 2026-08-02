# Catalogue storage boundary

Generated: `2026-08-03`  
Project: Supabase `qstcdlczasmvexpgbpjk` · R2 bucket `cardscanr-catalog`  
Branch: `feature/worldwide-pokemon-catalogue-products-20260802`

## Rule

Authoritative worldwide Pokémon card/product catalogue data must not be stored as a normalized relational copy in Supabase.  
Supabase retains only dynamic user/application data.  
Cloudflare R2 (+ CDN) owns public catalogue artifacts and app-facing images.

## Classification

### Supabase dynamic data (retain)

| Object | Consumer |
|---|---|
| `auth.*` | Supabase Auth |
| `public.user_profiles` | Flutter profiles |
| `public.user_collections` | Flutter collections mirror |
| `public.user_cards` | Flutter owned cards |
| `public.scan_sessions` | Flutter scan history |
| `public.customer_sync_preferences` | Customer portal sync |
| `public.customer_collection_items` | Customer portal |
| `public.customer_binders` | Customer portal |
| `public.customer_binder_memberships` | Customer portal |
| `public.customer_sync_operations` | Sync ack log |
| `public.customer_sync_checkpoints` | Sync cursors |
| `public.market_price_*` | Market engine (dynamic pricing) |
| `public.pokemon_card_image_records` | Legacy EN image pipeline metadata (until Flutter stops querying it) |
| `public.card_image_manifests` (+ views `card_image_manifests_current`, `card_image_manifests_with_legacy_records`) | Legacy image-manifest lookup |

### Cloudflare public catalogue data

| Artifact | Path / prefix | Consumer |
|---|---|---|
| Active search manifest | `v2/catalog/pokemon/search/catalogue.manifest.json` | Flutter catalogue updater |
| Immutable search DB | `v2/catalog/pokemon/search/versions/<sha256>/catalogue.sqlite` | Flutter local search |
| Immutable version manifest | `v2/catalog/pokemon/search/versions/<sha256>/manifest.json` | Verification / rollback |
| Rollback manifests | `v2/catalog/pokemon/search/rollbacks/<sha256>/manifest.json` | Rollback |
| Card images | `v2/catalog/pokemon/cards/<language>/<set>/<printing>/<image-sha>.webp` | App image CDN |
| Product images | `v2/catalog/pokemon/products/<region>/<product>/<image-sha>.webp` | App image CDN |
| Thumbnails | `v2/catalog/pokemon/thumbnails/<sha>.webp` | App image CDN |
| Legacy EN image objects | Worker host `cardscanr-images.andygore149.workers.dev` / existing keys | Existing installed clients |
| Pages contract | `https://cardscanr-cache.pages.dev/v1/...` | Manifest/bootstrap |

Public base currently verified: `https://pub-258b8de1c4964f538a8cb08022761430.r2.dev`  
Image worker currently verified: `https://cardscanr-images.andygore149.workers.dev`  
`images.cardscanr.com` is **not** currently resolving DNS.

### Cloudflare private archive data

| Artifact | Location |
|---|---|
| Source mirrors / provider snapshots | Local runtime `D:\CardScanR_worldwide_runtime_20260802\mirrors` (+ optional private R2 archive) |
| Recovery bundles / encrypted staging snapshots | Private R2 archive only when encrypted |
| Raw originals / acquisition evidence | Local validation caches; not public prefixes |

### Local build/runtime data

| Path | Role |
|---|---|
| `D:\CardScanR_worldwide_runtime_20260802\staging\worldwide-catalogue.sqlite` | Canonical normalized staging DB |
| `...\publication\search_*` | Search-index builds |
| `...\card_image_validation*` / `product_image_validation*` | Image acquisition caches/checkpoints |
| `...\migration_chunks` | One-off migration helpers |

### Git-managed data

Schemas, migrations, publication tooling, compact manifests, checksums, reports under `reports/`, validation scripts/tests.

## Supabase catalogue tables (remove)

Exact live names from applied migration `worldwide_catalogue_foundation*`:

`franchises`, `languages`, `regions`, `source_providers`, `import_runs`, `source_snapshots`, `source_records`, `eras`, `series`, `sets`, `set_releases`, `card_designs`, `card_printings`, `card_variants`, `card_text_localisations`, `abilities`, `attacks`, `card_images`, `sealed_products`, `accessories`, `sealed_product_variants`, `product_contents`, `product_images`, `marketplace_mappings`, `image_validation_results`, `image_acquisition_attempts`, `record_provenance`, `publication_runs`, `publication_artifacts`, `unresolved_items`, plus temp `_cardscanr_mcp_sql_chunks`.

Forward migration: `supabase/migrations/20260803080000_remove_worldwide_catalogue_from_supabase.sql`.

## Enforcement

1. Production publication path: `tools/publish_worldwide_catalogue.py` → R2 only.
2. `tools/publish_staging_to_supabase.py` requires `--i-understand-this-writes-catalogue-to-supabase` and rejects production project ref `qstcdlczasmvexpgbpjk` unless `--allow-production-catalogue-load` is also set.
3. Validation: `tools/validate_catalogue_storage_boundary.py` and `tests/test_catalogue_storage_boundary.py`.

## Consumers

| Consumer | Catalogue source | User data source |
|---|---|---|
| Flutter app | Local SQLite from R2 active/canary manifest | Supabase Auth + user/sync tables |
| Customer portal | Must use R2/static/Worker index, not Supabase catalogue tables | Supabase customer_* tables |
| Admin/dashboard | Same as portal | Supabase dynamic tables |
| Data build tools | Local staging SQLite | n/a |
