# Custom Domain Blocker (Requirement 4)

## Finding

Production must not use `*.r2.dev`. Discovery on 2026-08-03:

| Probe | Result |
|---|---|
| Cloudflare Zones API (`/zones`) | Empty list with current API token |
| R2 custom domains API | HTTP 403 Forbidden |
| R2 managed domains API | HTTP 403 Forbidden |
| Pages projects API | HTTP 403 Forbidden |
| DNS `cardscanr.com` | NXDOMAIN / no resolution |
| DNS `assets.cardscanr.com` | NXDOMAIN |
| DNS `images.cardscanr.com` | NXDOMAIN |
| DNS `cdn.cardscanr.com` | NXDOMAIN |
| DNS `catalogue.cardscanr.com` | NXDOMAIN |
| Existing serving host | `https://pub-258b8de1c4964f538a8cb08022761430.r2.dev` |
| Legacy image worker | `https://cardscanr-images.andygore149.workers.dev` |
| Pages contract host | `https://cardscanr-cache.pages.dev` |

No CardScanR-owned public hostname was inventable or purchasable under the instruction “Do not invent or purchase a domain.”

## Required owner action

1. Provide a Cloudflare API token with **Zone:Read**, **Zone:DNS:Edit**, and **R2** custom-domain permissions for the CardScanR account/zone, **or**
2. Confirm the existing zone name already controlled by the owner (exact hostname), then grant access.

Once a real zone/hostname is available (for example `assets.cardscanr.com` if that zone exists under owner control), configure:

- R2 custom hostname → `cardscanr-catalog`
- Valid HTTPS certificate
- CORS for Flutter/web/dashboard
- Range requests (206)
- Immutable cache for versioned packs/images: `Cache-Control: public, max-age=31536000, immutable`
- Short/revalidated cache for active manifest
- Cache Rules / Smart Tiered Cache where available
- Disable public `r2.dev` for production after cutover (QA-only retention only if documented)

## Gate impact

Custom-domain verification (HEAD 200, Range 206, manifest/pack/image retrieval via CardScanR domain) **blocks production activation**. Pack architecture, mirror verification, and Flutter durability can proceed independently.
