# Continuation handoff

## Git

- Worktree: `D:\CardScanR_worktrees\worldwide_catalogue_products_20260802`
- Branch: `feature/worldwide-pokemon-catalogue-products-20260802`
- Runtime: `D:\CardScanR_worldwide_runtime_20260802`
- Do not recreate the worktree or reset runtime state.

## Active writer

- Hong Kong card collector PID should be the only Asia writer:
  `python -u tools/collect_pokemon_asia.py --locale hk --runtime-root D:\CardScanR_worldwide_runtime_20260802\regional --mode full --delay-seconds 0.35`
- Resume is safe: `collect_details` skips already-parsed cards.

## Completed card acquisition

| Locale | Cards | Status |
|---|---:|---|
| id | 12325 | complete + imported + images verified |
| th | 9554 | complete + imported; images paused mid-validation |
| ph | 7406 | complete + imported + images verified |
| sg | 7406 | hydrated from PH + imported; images paused |
| my | 7406 | hydrated from PH + imported; images paused |
| tw | 14276 | complete + imported; images paused |
| hk | in progress | collector running; import after completion |

## Paused image validators (resume after HK import)

```bat
C:\Python314\python.exe -u tools\validate_card_images.py --database D:\CardScanR_worldwide_runtime_20260802\staging\worldwide-catalogue.sqlite --runtime-root D:\CardScanR_worldwide_runtime_20260802\card_image_validation_asia_th --provider pokemon-asia-th-official --workers 4 --delay-seconds 0.2 --retry-failed
C:\Python314\python.exe -u tools\validate_card_images.py --database D:\CardScanR_worldwide_runtime_20260802\staging\worldwide-catalogue.sqlite --runtime-root D:\CardScanR_worldwide_runtime_20260802\card_image_validation_asia_sg --provider pokemon-asia-sg-official --workers 3 --delay-seconds 0.2
C:\Python314\python.exe -u tools\validate_card_images.py --database D:\CardScanR_worldwide_runtime_20260802\staging\worldwide-catalogue.sqlite --runtime-root D:\CardScanR_worldwide_runtime_20260802\card_image_validation_asia_my --provider pokemon-asia-my-official --workers 3 --delay-seconds 0.2
C:\Python314\python.exe -u tools\validate_card_images.py --database D:\CardScanR_worldwide_runtime_20260802\staging\worldwide-catalogue.sqlite --runtime-root D:\CardScanR_worldwide_runtime_20260802\card_image_validation_asia_tw --provider pokemon-asia-tw-official --workers 3 --delay-seconds 0.2
```

After HK card import:

```bat
C:\Python314\python.exe -u tools\import_pokemon_asia_checkpoint.py --database D:\CardScanR_worldwide_runtime_20260802\staging\worldwide-catalogue.sqlite --checkpoint D:\CardScanR_worldwide_runtime_20260802\regional\pokemon-asia\hk\checkpoint.sqlite --locale hk
C:\Python314\python.exe -u tools\validate_card_images.py --database D:\CardScanR_worldwide_runtime_20260802\staging\worldwide-catalogue.sqlite --runtime-root D:\CardScanR_worldwide_runtime_20260802\card_image_validation_asia_hk --provider pokemon-asia-hk-official --workers 4 --delay-seconds 0.2
```

## Product galleries

- id/th/hk/tw expanded and imported; most product images verified.
- sg/my/ph have no local sealed-product gallery index (US expansion links only). See `ASIA_PRODUCT_GALLERY_GAPS_20260802.md`.

## Production safety

- No worldwide manifest activation.
- No production Supabase write in this continuation.
- Existing v1 manifest retained.
