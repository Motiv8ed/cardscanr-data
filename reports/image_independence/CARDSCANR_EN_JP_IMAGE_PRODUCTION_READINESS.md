# CARDSCANR_EN_JP_IMAGE_PRODUCTION_READINESS

Generated: `2026-10-10T09:30:00Z`  
Branch: `task/en-jp-image-independence-20261007`  
Status: **ready for PR review / approval — do not merge or deploy until gates and owners approve**

## Verified baseline

| Metric | Value |
|--------|------:|
| Total EN+JA cards | 68,011 |
| Local master | 68,011 |
| CardScanR CDN hosted | 68,011 |
| Missing | 0 |
| SHA-256 mismatches | 0 |
| Duplicate canonical / CDN mappings | 0 |

Closeout commit tip (post-rebase onto `origin/main`): see branch HEAD.  
Companion app-repo reports commit: CardScanR `48d54e27` (or successor on same task branch).

## Production CDN configuration

- Catalogue EN/JA `imageSource=cardscanr_cdn` with absolute URLs on  
  `https://cardscanr-images.andygore149.workers.dev/...` (CardScanR Worker over R2).
- Flutter `CardImageResolver` prefers CardScanR hosts already on the candidate before any provider URL.
- `CardImageCloudEnvironment.isConfiguredCdnHost` accepts `cardscanr-images.*.workers.dev` and `*.cardscanr.com`.
- Release builds default key-construction base to `https://cards.cardscanr.com` when dart-defines are omitted; **EN/JA catalogue rows already carry absolute CardScanR CDN URLs**, so the app serves those CardScanR URLs rather than PokéWallet/TCGdex/Scrydex originals.
- Custom-domain cutover (`images.cardscanr.com` / `cards.cardscanr.com` as catalogue hostname) remains **deferred** and requires **no** mass re-upload (see app `scripts/card_image_cloud/ENABLE_PRODUCTION_DOMAIN_LATER.md`).

## Outstanding rights review (blocker for licence claims, not for CDN hosting)

Three seller-photo recoveries are hosted with attribution retained and **licence not inferred**:

- CSV: `reports/image_independence/en_jp_seller_photo_outstanding_rights_review.csv`
- `rightsStatus=seller_photo_attribution_retained_licence_not_inferred`
- `rightsReviewStatus=outstanding_rights_review`
- `licenceConfirmed=false`

Do not mark these as licence-confirmed without human legal/rights approval.

## Automated new-card handling

Gap-only operator entry (does not re-acquire hosted printings):

```powershell
cd D:\cardscanr-data
python tools/run_en_jp_image_independence_gap_pipeline.py --report-only
# When gaps exist and acquisition is approved:
python tools/run_en_jp_image_independence_gap_pipeline.py --upload --apply
```

Pipeline steps reuse existing tools:

1. Detect gaps (`verify_en_jp_image_independence_closeout.py`)
2. Resolve unresolved only (`image_independence_multisource_resolver.py --skip-hosted`)
3. Bind catalogue (`apply_image_independence_to_catalogue.py`)
4. Fail-closed regression (`tests/test_en_jp_image_independence_regression.py`)

Failed imports remain in `en_jp_unresolved_images.csv` / gap summary; resolvers are identity fail-closed and must not silently substitute another printing.

CI: `validate-cache` workflow runs the catalogue CDN regression gate on PRs (local master optional in CI).

## Backup / restore (rebuild CDN from local archive)

Backups (manifests + hashes; binaries remain in master root):

- `D:\CardScanR_Archive\backups\en_jp_image_independence_100pct_20261010T080450Z`
- `D:\CardScanR_Archive\backups\en_jp_image_independence_100pct_20261010T081442Z`

Live master: `D:\cardscanr-data\data\images\master` (gitignored).

### Restore CDN from local master (no deletes)

1. Confirm master inventory: every `asset.json` has `display.webp`, `sha256`, `hostedObjectKey`.
2. For each asset (or use `tools/repair_en_jp_cdn_alias_gaps.py` pattern / `image_independence_upload_local_master.py`):  
   upload content key + alias + thumb via existing `wrangler_put`.
3. Verify alias URL HTTP 200.
4. Run `python tools/apply_image_independence_to_catalogue.py` if catalogue bindings drifted.
5. Run `python tools/verify_en_jp_image_independence_closeout.py` until `allGatesPassed=true`.

Do **not** delete existing R2 objects during restore.

## Rollout plan (after approval)

1. Merge `cardscanr-data` PR → publish/sync catalogue that apps/workers already consume.  
2. Merge CardScanR PR (resolver preference + reports).  
3. Smoke: open EN + JA cards in app; confirm image hosts are CardScanR CDN, not provider hosts.  
4. Keep gap pipeline available for post-release set drops.

## Rollback

1. **Catalogue-only rollback:** revert the merged catalogue commit(s) on `cardscanr-data` main (or redeploy previous catalogue artifact). App will fall back to prior image fields; do not delete R2.  
2. **App-only rollback:** revert CardScanR resolver commit; absolute CardScanR URLs in catalogue still display if present.  
3. **CDN object rollback:** not required for catalogue revert; objects are additive/content-addressed.  
4. Re-run regression + closeout verifier after any rollback.

## Exact release actions (human)

- [ ] Approve seller-photo rights posture (hosting OK vs licence claims)  
- [ ] Approve both PRs  
- [ ] Merge data PR, then app PR  
- [ ] Post-merge smoke on Android (EN + JA image host check)  
- [ ] Do **not** force-push main; do **not** deploy unrelated pricing/worker changes in the same window unless separately approved
