# Staging path incident (2026-10-13)

## What happened

A short-lived path under `data/images/staging/global_acq_20261013` disappeared around `2026-10-13T07:26Z` while an early acquire attempt was in flight. Production `data/images/master/` and `data/images/independence/` were **not** affected.

## Durable staging location (this task)

All retained staging bytes for this run live at:

`D:\CardScanR_Archive\image_staging\global_acq_20261013`

Override with env `CARDSCANR_IMAGE_STAGING_ROOT` if needed.

Progress is also mirrored to `reports/global_image_acquisition/progress.jsonl` on the task branch for ledger durability.

## Lesson

Do not place large worldwide staging trees beside the production master archive root; use the Archive volume + report mirrors.
