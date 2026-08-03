# Catalogue Pack Architecture

## Goal

Replace the monolithic first-install catalogue (~1.18–1.22 GB SQLite) with versioned, independently installable packs. No public catalogue object may exceed **512 MB**.

## Pack inventory

| Pack ID | Kind | Contents |
|---|---|---|
| `core` | core | All `sets` + pack meta; empty card/product tables |
| `en` | language | English cards + aliases + FTS + EN sets |
| `ja` | language | Japanese cards + aliases + FTS + JA sets |
| `ko` | language | Korean cards + aliases + FTS + KO sets |
| `zh-cn` | language | Simplified Chinese cards + aliases + FTS |
| `zh-tw` | language | Traditional Chinese cards + aliases + FTS |
| `th` | language | Thai cards + aliases + FTS |
| `id` | language | Indonesian cards + aliases + FTS |
| `intl-other` | language | fr, de, it, es, pt, es-mx, pt-br, nl, ru, pl |
| `sealed-products` | sealed-products | All sealed products + contents + FTS |

Exact raw/gzip/installed sizes are emitted by `tools/build_catalogue_packs.py` into `PACK_SIZE_MATRIX.json`.

## Default Australian install

- `core`
- `en`
- `sealed-products`

Japanese may be suggested in Catalogue/Storage settings with an explicit download size. It must not download silently.

## Integrity and lifecycle

- Each pack has its own SHA-256, byte length, and schema version (`searchIndexSchemaVersion` 2.1.0 / pack schema 2.2.0).
- Transport uses gzip (Flutter already stream-decompresses gzip and verifies uncompressed size).
- Install is atomic: previous verified pack retained until the replacement verifies.
- Interrupted downloads resume via Range requests (existing downloader).
- Failed optional language packs must not invalidate core/`en`.
- User collection data is outside pack storage and unaffected.
- Canonical IDs remain stable across packs (`canonical_printing_id` / `canonical_base_id` / `product_variant_id`).
- Search combines all installed packs (ATTACH + UNION, or merged local view).
- Missing optional packs are reported honestly in UI (“Japanese catalogue not installed”).

## Delta updates

Initial release status: **post-release**.

Safe strategy under investigation:

1. Monthly rebuilt immutable base packs
2. Small interim version-to-version patch bundles declaring required base SHA
3. Transactional apply + expected final SHA + rollback on failure

Until deltas ship, pack updates redownload the changed pack only (not every installed pack if unchanged).

## Object keys

```
v2/catalog/pokemon/packs/<releaseId>/<packId>/<sha256>/<packId>.sqlite
v2/catalog/pokemon/packs/<releaseId>/<packId>/<sha256>/<packId>.sqlite.gz
v2/catalog/pokemon/packs/<releaseId>/catalogue.packs.manifest.json
```

Active production pointer (when activated) must use the CardScanR custom domain only.

## Builder

```
python tools/build_catalogue_packs.py \
  --database <monolith.sqlite> \
  --output-dir <out> \
  --public-base-url https://<custom-domain> \
  --catalogue-release-id <id>
```
