# CardScanR Image Cache Strategy

Card images are static reference assets for scanner review, match confirmation, card detail screens, manual correction, and collection browsing. Prices, trends, and history should refresh often; card images should be fetched once and cached long term unless there is a concrete reason to re-check them.

## Identity Rule

Images must be cached by exact card identity, not by card name alone. A name such as `charizard` appears across many sets, languages, printings, and variants, so it is not a safe cache key.

Recommended cache key:

```text
{game}|{language}|{setId}|{collectorNumber}|{normalizedName}|{variant}
```

Examples:

```text
pokemon|en|base1|4|charizard|holo
pokemon|jp|SV10|006/098|charizard|normal
pokemon|zh|someSet|001/100|pikachu|normal
```

The app should resolve provider image IDs to CardScanR's canonical card identity before using an image as a stable cached asset. Provider IDs remain useful references, but they are not the main CardScanR identity.

## Provider References

Provider catalogue records may include image endpoints such as low and high resolution provider image references. These references help locate source images and confirm availability, but CardScanR should attach any cached binary image to the canonical card identity key above.

When the provider mapping is not fully confirmed, provider catalogue files should expose candidate keys, for example:

- `providerCanonicalImageKey`
- `cardScanRImageCacheCandidateKey`
- `imageCacheIdentityBasis`

These are match-assistance fields, not final canonical IDs.

## Refresh Policy

Images are mostly static. The default policy is:

- Fetch once when needed.
- Cache locally on device for a long period.
- Re-check only when there is a clear trigger.

Recommended re-check triggers:

- Image is missing.
- Image failed to load.
- Provider image ID changed.
- Card metadata changed.
- Cache entry is extremely old.
- Manual refresh requested.

Suggested defaults:

- Local app cache: 365 days.
- Metadata re-check: after 180 days, or earlier only for the triggers above.

## Storage

Do not bulk-store card image binaries in this repository. Public cache JSON should store deterministic metadata and references only.

Future binary storage options:

- Cloudflare R2
- Supabase Storage
- Firebase Storage

The Flutter app should cache images locally on device where allowed by provider terms and app policy. Future CardScanR CDN image URLs can be added beside provider references after a dedicated image ingestion pipeline exists.

## Time Handling

Public timestamps in image metadata are UTC. Flutter should convert display times locally when needed.
