# Worldwide publication canary 2

Immutable R2 canary after Asia catalogue completion and release-gate acceptance.
This is validation evidence, **not** a production release. Active manifest was not overwritten.

## Search index

- Path: `D:\CardScanR_worldwide_runtime_20260802\publication\search_20260802_canary2\catalogue_canary2.sqlite`
- Bytes: `897,101,824`
- SHA-256: `89c07376b30e9b0edf8ee1ad74c8b53583dc12a11f5f3fb71ec5d8419db5428b`
- Records: `296,463` (FTS parity PASS)
- Products: `4,618` / contents `49,492`
- Card images in search DB: `118,594`
- Product images in search DB: `3,696`
- Deterministic rebuild: PASS
- Authenticated URLs: `0`

## Staging export bundle

- Version: `2026.08.02-canary2`
- Source staging SHA-256: `6f3026ace7857f77b81e274d4f888a95016800cbd20a8e5302330dc81a59c28b`
- Source staging bytes: `3,267,694,592`
- Classification: `STAGED_NOT_PUBLISHED` (bundle) / R2 canary upload PASS
- Publication-history registration: PASS (`canary`)

## R2 immutable objects

- Database key: `v2/catalog/pokemon/search/versions/89c07376b30e9b0edf8ee1ad74c8b53583dc12a11f5f3fb71ec5d8419db5428b/catalogue.sqlite`
- Manifest key: `v2/catalog/pokemon/search/versions/89c07376b30e9b0edf8ee1ad74c8b53583dc12a11f5f3fb71ec5d8419db5428b/manifest.json`
- Public verification: PASS (HEAD 200, Range 206, SQLite magic)
- `activeManifestKey`: `null` (production pointer unchanged)
- JSON evidence: `PUBLICATION_CANARY_20260802_CANARY2.json`
