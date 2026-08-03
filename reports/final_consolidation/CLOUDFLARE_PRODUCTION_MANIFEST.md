# Cloudflare Production Manifest Activation

Generated: 2026-08-03

## Status

| Gate | Result |
|---|---|
| Production active pointer HTTP | **200** |
| Packs active pointer HTTP | **200** |
| Immutable release uploaded | **PASS** |
| Default packs resolve | **PASS** |
| Canary2/3/4 preserved | **PASS** |
| Canary4 packs preserved | **PASS** |
| NULL_CARD_IMAGE_URLS | **0** |
| NULL_PRODUCT_IMAGE_URLS | **0** |
| THIRD_PARTY_RUNTIME_IMAGE_URLS | **0** |
| FULL_IMAGE_LIBRARY_AUTO_DOWNLOAD | **false** |
| IMAGES_EMBEDDED_IN_CATALOGUE_PACKS | **0** |
| Monolith 1.2GB as normal mobile release | **NOT activated** |

## Public base (QA / interim production host)

`https://pub-258b8de1c4964f538a8cb08022761430.r2.dev`

Custom CardScanR hostname: **not configured** (owner action — see below).

## Release

| Field | Value |
|---|---|
| Release ID | `production-packs-20260803` |
| Rollback release | `canary4-packs-20260803` |
| Manifest SHA-256 | `eeda5d4ff6cd631a4f39c1814b000870c95655cbdce6b9e57ca9756e45f5b9e3` |
| Manifest bytes | 19309 |
| Cache-Control (active) | `public, max-age=60, must-revalidate` |
| Cache-Control (immutable packs) | `public, max-age=31536000, immutable` |

## Active pointers

| Key | URL | Status |
|---|---|---|
| Production search pointer | `…/v2/catalog/pokemon/search/catalogue.manifest.json` | 200 |
| Packs active pointer | `…/v2/catalog/pokemon/packs/active/catalogue.packs.manifest.json` | 200 |
| Immutable release manifest | `…/v2/catalog/pokemon/packs/production-packs-20260803/catalogue.packs.manifest.json` | 200 |

## Default AU packs

| Pack | Compressed bytes | SHA-256 (sqlite) |
|---|---:|---|
| core | 63171 | `5b2c06d5c9cc581f3724c1c1aa4b53a58d351837bcfe85eafc3ab25fa70d7e6b` |
| en | 19553247 | `4a7518700a145f248d46f4fe3e6d698207a12ec6d076c2ea876d46ac610ffb85` |
| sealed-products | 2850308 | `05b93a8d3ec386f7e89128769a6d48ce632527be270a6c3ac9f557a52d338cdb` |
| **Default install total** | **22466726 (~21.4 MiB)** | |

Optional language packs remain optional: ja, ko, zh-cn, zh-tw, th, id, intl-other.

## Placeholder / image base

- Placeholder: `…/v2/catalog/pokemon/placeholders/card_missing.webp` (HEAD 200, 1204 bytes)
- Image base public URL: r2.dev interim host (same bucket)
- CardScanR-controlled workers.dev image URLs remain accepted as first-party

## Pre-activation rollback evidence

- Search pointer before activation: **HTTP 404**
- Packs active before activation: canary4 packs manifest (18142 bytes)
- Canaries left untouched at immutable version keys

## Hostname / custom domain

| Probe | Result |
|---|---|
| Cloudflare Zones API | empty / insufficient token scope |
| R2 custom domains API | 403 |
| `cardscanr.com` DNS | NXDOMAIN |
| Suitable owned hostname | unavailable |

**Owner action required (hostname only):** provide zone + API token with Zone:DNS:Edit and R2 custom-domain permissions, or confirm existing hostname. Git merge and pointer activation are **not** blocked.

## Secrets

Redacted. Credentials used from local `cloudflare_env.local.json` (not committed).
