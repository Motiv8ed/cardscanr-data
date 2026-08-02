# R2 free-tier storage plan

Generated: `2026-08-03`

## Cloudflare free allowance (R2)

Typical Cloudflare R2 free tier (verify on current Cloudflare plan page):
- **10 GB** storage / month
- **1 million** Class A operations / month
- **10 million** Class B operations / month
- Egress to Cloudflare CDN: $0 on R2

If exceeded, rough public list pricing historically ~**$0.015/GB-month** storage (confirm live).

## Current measured usage (`cardscanr-catalog`)

| Prefix family | Objects | Bytes |
|---|---:|---:|
| `v2/catalog/pokemon/search` | 2 | 897,103,540 |
| `canary/global-catalogue/v2` | 4 | 611,502,816 |
| `v1/catalog/pokemon/search` | 3 | 361,398,299 |
| `v2/internal-beta/catalog` | 2 | 339,178,884 |
| `v1/images/cards-manifest.*` | 1 | 104,016,295 |
| probes | 2 | 28 |
| **Total before image mirror** | **14** | **~2.31 GB** |

`cardscanr-card-images` was **inaccessible** with the current R2 API token used by this worktree; image worker host `cardscanr-images.andygore149.workers.dev` remains the legacy EN image delivery surface.

Production active v2 manifest was **404** (not activated). Canary2 DB is present and immutable.

## Local bytes available to mirror

| Class | Unique SHA-256 | Local source bytes (approx) |
|---|---:|---:|
| Card validation caches | 70,413 | (per-region caches; deduped by SHA) |
| Product validation caches | 5,253 | includes **3,062** China `pokemon.com.cn` pass assets |
| **Total unique** | **75,666** | product pass cache alone ~7.5 GB originals |

App-facing plan uploads **WebP derivatives only**:
- display ≤ 800 px long edge
- thumb ≤ 300 px long edge
- keys: `v2/catalog/pokemon/images/by-sha/<sha>/display.webp` (+ `thumb.webp`)
- originals retained locally / private archive, not required in public R2

## Optimized projection

Assuming average ~45 KB display + ~12 KB thumb after WebP (conservative; measure after first 1k uploads):

| Item | Projection |
|---|---:|
| 75,666 × (display+thumb) | ~4.3 GB |
| Existing catalogues/canaries | ~2.3 GB |
| Placeholder + manifests | < 5 MB |
| **Projected public total** | **~6.6 GB** |

This fits inside a 10 GB free allowance with headroom if older duplicate canaries are left in place.  
If overage appears:

1. Keep only one rollback + one previous production search DB publicly.
2. Move older canaries / v1 search DBs to private archive or local recovery.
3. Do **not** delete unique original evidence.

Private/local (no app impact):
- staging SQLite (3.27 GB)
- source mirrors (~7.6 GB)
- raw validation caches
- encrypted recovery bundles

## Monthly cost if over free tier

Example: 12 GB stored public → ~2 GB billable × $0.015 ≈ **$0.03/month** storage (operations usually dominate only under chatty clients; app uses immutable URLs + on-device cache).

## China transient recovery

Product checkpoint contains **3,062** `image.pokemon.com.cn` assets with `status=pass`, local cache files, and SHA-256. These are being uploaded to CardScanR R2; temporary signed/page URLs must never appear in the published catalogue.
