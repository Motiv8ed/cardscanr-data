# Worldwide publication canary 3 (Cloudflare-native images)

Immutable R2 canary after Supabase catalogue removal and CardScanR-only image URL rewrite.
**Not** a production release. Active manifest was not overwritten.

## Search index

- Path: `D:\CardScanR_worldwide_runtime_20260802\publication\search_20260803_canary3\catalogue_canary3.sqlite`
- Bytes: `1,179,602,944`
- SHA-256: `95f4acb70ff30f19a3d18d21435d21711fda557ea003f9f131319ff5db540425`
- Records: `296,463` (FTS parity PASS)
- Products: `4,618` / contents `49,492`
- `THIRD_PARTY_RUNTIME_IMAGE_URLS = 0`
- Authenticated URLs: `0`
- Verify classification: **PASS**

## Image rewrite

- Source: canary2 (`89c07376…`)
- Card URLs rewritten to mirrored R2: `4,231` (at rewrite time; mirror still growing)
- Card URLs set to CardScanR placeholder: `90,911`
- Product URLs set to CardScanR placeholder: `3,696` (URL-string join incomplete; China/local bytes still uploading)
- Placeholder: `v2/catalog/pokemon/placeholders/card_missing.webp`

## R2 immutable objects

- Database key: `v2/catalog/pokemon/search/versions/95f4acb70ff30f19a3d18d21435d21711fda557ea003f9f131319ff5db540425/catalogue.sqlite`
- Manifest key: `v2/catalog/pokemon/search/versions/95f4acb70ff30f19a3d18d21435d21711fda557ea003f9f131319ff5db540425/manifest.json`
- Public verification: PASS (HEAD 200, Range 206)
- `activeManifestKey`: `null`
- Rollback retained: canary2 `89c07376…`

## Follow-ups before activation

1. Finish local→R2 image mirror (`75,666` unique SHAs).
2. Re-rewrite + publish canary4 with higher real-image coverage / fewer placeholders.
3. Flutter emulator QA against canary3/canary4 manifest URL.
4. Only then atomically write `catalogue.manifest.json`.
