# Ranked next acquisition opportunities

Generated during `task/global-image-acquisition-20261013`.

| Rank | Opportunity | Expected yield | Cost | Blocker |
| ---: | --- | --- | --- | --- |
| 1 | Resume TCGdex staging acquire for remaining non-EN/JA `direct_images.jsonl` candidates | ~65k−current staged | $0 | Time / rate limits only |
| 2 | Production-promotion rights decision for staged TCGdex assets | All verified staging files → CDN | $0 legal/ops | **Human permission / accepted risk** |
| 3 | Official Korea catalogue permission (TCGdex KO has sets/cards but **0** image URLs) | Hundreds–thousands KO printings | Unknown | **External permission** |
| 4 | Official CN / zh-Hans artwork permission (TCGdex zh-cn cards similarly **0** images) | Hundreds–thousands zh-Hans | Unknown | **External permission** |
| 5 | Official Asia trainers (id/th/zh-Hant) permission for residual no-candidate printings | Fills large missing-candidate tails | Unknown | Terms currently prohibit; **permission** |
| 6 | PokéWallet written rehost clarification (no paid upgrade) | Auth-gated EU/CJK lacking public hosts | $0 unless plan change | **External permission**; no spend |
| 7 | Collector photo intake + contributor licence | Long-tail variants / promos | Ops tooling | Product/legal intake design |
| 8 | Paid Scrydex API expansion beyond EN/JA | Limited (Scrydex EN/JA focused) | Paid credits | **Financial approval** — not started |

## Next recommended batch (immediate)

```powershell
cd D:\CardScanR_Data_worktrees\global-image-acq-20261013
$env:CARDSCANR_IMAGE_STAGING_ROOT = 'D:\CardScanR_Archive\image_staging\global_acq_20261013'
python -u tools\global_image_staging_acquire.py --execute --exclude-languages en,ja --concurrency 8 --min-interval 0.05
python tools\build_global_image_acquisition_report_bundle.py
```

Do **not** promote staging → production CDN until rank-2 decision.
