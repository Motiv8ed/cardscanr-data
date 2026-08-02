# Existing self-controlled R2 image reconciliation

The preserved Build 47 manifests were imported into worldwide staging without re-uploading or
duplicating image binaries.

- Import run: `b768c350-aaed-4a1e-803d-f9d9279475d0`
- Combined input SHA-256: `3faeab06488da3cb84a34526a0cb1e6516f3355ccb02927fd5b6bb3112ce613b`
- Unique manifest card IDs: `23,493`
- Exact staged mappings: `23,456`
  - English: `20,304`
  - Japanese: `3,152`
- Existing CardScanR canonical promo printings restored: `35`
- Previously verified assets: `23,452`
- Assets still pending technical verification: `4`
- Identity-review records retained: `37`
- Display candidates / thumbnail candidates: `23,456` / `23,456`
- Acquisition attempts recorded: `23,456`
- SQLite integrity / foreign keys: `PASS` / `0`

English mappings use exact PokémonTCG provider card IDs. Japanese mappings use exact language,
set code and collector number, with local-name corroboration; nineteen punctuation/gender-label
differences were accepted only where the exact set and collector number identify one printing.
Recent English promos absent from the current upstream PokémonTCG mirror were restored only from
their exact existing CardScanR canonical identities. The remaining 37 older pilot rows have no safe
staged identity and remain in `needs_review`; they were not matched by card name or hidden.

## Preserved source manifests

- `pilot/manifest_pending.json`: `3476357b7f9038aa9d14bd24a62b0cca1caa22f2130af2cad86b0844ccd5d1ad`
- `stage_a_public/manifest_pending.json`: `0e19ad55c1912032762d6a54190029f1f382fc9f7cb458616cd0caf8ddc7e0a4`
- `build47_mapped_repair/manifest_pending.json`: `58e7b1099393157a2724378506227965d8401236159abe3dac7db62bc06ee6eb`
