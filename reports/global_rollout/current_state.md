# CardScanR Global Rollout — Current State

- Branch: `main`
- HEAD: `1aec8965c644c41eb17713d675f0b7ec00ecc1a0`
- Upstream: `origin/main`
- Working tree clean: **False**
- Python: `3.14.3 (tags/v3.14.3:323c59a, Feb  3 2026, 16:04:56) [MSC v.1944 64 bit (AMD64)]`
- Search index rows: 74578
- Search languages: {'en': 46417, 'jp': 28161}
- R2 accessible: True
- R2 objects/bytes: 6/465414622
- R2 Pokémon image objects: 0
- Supabase image records: 607
- Supabase status counts: {'completed': 591, 'provider_image_unavailable': 16}
- Supabase provider counts: {'pokemon_tcg_api': 450, 'pokewallet': 130, 'tcgdex': 20, 'unknown': 7}

## Current catalogue

- `en`: 173 sets, 46417 cards, 388 card files
- `jp`: 570 sets, 28161 cards, 473 card files
- `zh`: 58 sets, 6439 cards, 58 card files

## Working tree

No existing change was discarded. The audit captured the following status:

- `M .gitignore`
- ` M cardscanr_image_pipeline/config.py`
- ` M cardscanr_image_pipeline/database.py`
- ` M cardscanr_image_pipeline/models.py`
- ` M cardscanr_image_pipeline/paths.py`
- ` M cardscanr_image_pipeline/pipeline.py`
- ` M cardscanr_image_pipeline/processing.py`
- ` M cardscanr_image_pipeline/providers/pokemon_tcg_api.py`
- ` M cardscanr_image_pipeline/retry.py`
- ` M cardscanr_image_pipeline/stage2_runner.py`
- ` M requirements.txt`
- ` M tests/test_image_pipeline.py`
- `?? cardscanr_global_catalogue/`
- `?? cardscanr_image_pipeline/gate_a_remediation.py`
- `?? cardscanr_image_pipeline/gate_b_full_rollout.py`
- `?? cardscanr_image_pipeline/pokewallet_limiter.py`
- `?? cardscanr_image_pipeline/thumbnail_execute.py`
- `?? cardscanr_image_pipeline/thumbnail_rollout.py`
- `?? config/`
- `?? data/contracts/`
- `?? docs/global_catalogue_identity_contract.md`
- `?? docs/global_language_region_contract.md`
- `?? reports/global_rollout/`
- `?? reports/runtime/thumbnail_rollout_500_combined_contact_sheet.png`
- `?? reports/runtime/thumbnail_rollout_500_final_report.json`
- `?? reports/runtime/thumbnail_rollout_500_final_report.md`
- `?? reports/runtime/thumbnail_rollout_500_reconciled_report.json`
- `?? reports/runtime/thumbnail_rollout_500_url_map.json`
- `?? reports/runtime/thumbnail_rollout_en_500_dry_run.json`
- `?? reports/runtime/thumbnail_rollout_en_500_manifest.json`
- `?? reports/runtime/thumbnail_rollout_execution_report.json`
- `?? reports/runtime/thumbnail_rollout_execution_report.md`
- `?? reports/runtime/thumbnail_rollout_first_report.json`
- `?? reports/runtime/thumbnail_rollout_first_report.md`
- `?? reports/runtime/thumbnail_rollout_gate_a_contact_sheet.png`
- `?? reports/runtime/thumbnail_rollout_gate_a_dry_run.json`
- `?? reports/runtime/thumbnail_rollout_gate_a_execute.json`
- `?? reports/runtime/thumbnail_rollout_gate_a_failure_classifications.json`
- `?? reports/runtime/thumbnail_rollout_gate_a_idempotent.json`
- `?? reports/runtime/thumbnail_rollout_gate_a_manifest.json`
- `?? reports/runtime/thumbnail_rollout_gate_a_reconcile.json`
- `?? reports/runtime/thumbnail_rollout_gate_a_reconciled_contact_sheet.png`
- `?? reports/runtime/thumbnail_rollout_gate_a_reconciled_report.json`
- `?? reports/runtime/thumbnail_rollout_gate_a_replacements_9.json`
- `?? reports/runtime/thumbnail_rollout_gate_a_replacements_contact_sheet.png`
- `?? reports/runtime/thumbnail_rollout_gate_a_replacements_dry_run.json`
- `?? reports/runtime/thumbnail_rollout_gate_a_replacements_execute.json`
- `?? reports/runtime/thumbnail_rollout_gate_a_replacements_idempotent.json`
- `?? reports/runtime/thumbnail_rollout_gate_a_replacements_verify.json`
- `?? reports/runtime/thumbnail_rollout_gate_a_report.json`
- `?? reports/runtime/thumbnail_rollout_gate_a_verify.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_canary_contact_sheet.png`
- `?? reports/runtime/thumbnail_rollout_gate_b_canary_dry_run.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_canary_execute.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_canary_manifest.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_canary_probe.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_canary_report.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_canary_verify.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_credential_status.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_full_contact_sheet.png`
- `?? reports/runtime/thumbnail_rollout_gate_b_rate_limit_events.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_remaining75_checkpoint.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_remaining75_dry_run.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_remaining75_execute.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_remaining75_idempotent.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_remaining75_manifest.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_remaining75_report.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_remaining75_report.md`
- `?? reports/runtime/thumbnail_rollout_gate_b_remaining75_verify.json`
- `?? reports/runtime/thumbnail_rollout_gate_b_report.json`
- `?? reports/runtime/thumbnail_rollout_remediation_report.json`
- `?? reports/runtime/thumbnail_rollout_remediation_report.md`
- `?? reports/runtime/thumbnail_rollout_stage1.json`
- `?? reports/runtime/thumbnail_rollout_stage1.md`
- `?? reports/runtime/thumbnail_rollout_tcgdex_diagnostic.json`
- `?? reports/runtime/thumbnail_rollout_tcgdex_diagnostic.md`
- `?? reports/runtime/thumbnail_rollout_visual_preflight.json`
- `?? reports/runtime/thumbnail_rollout_visual_review_checklist.md`
- `?? supabase/migrations/20260710000000_pokemon_card_image_provider_unavailable_status.sql`
- `?? tests/test_global_rollout.py`
- `?? tests/test_thumbnail_rollout.py`
- `?? tools/gate_a_remediation.py`
- `?? tools/gate_b_full_rollout.py`
- `?? tools/global_rollout.py`
- `?? tools/thumbnail_execute.py`
- `?? tools/thumbnail_rollout.py`
- `?? tools/write_thumbnail_execution_report.py`

## Artifact inventory

- `reports`: 106 relevant files, 164478598 bytes
- `reports_runtime`: 75 relevant files, 163436174 bytes
- `data`: 15 relevant files, 104074056 bytes
- `public`: 1782 relevant files, 682470206 bytes
- `docs`: 9 relevant files, 44177 bytes

The complete per-file inventory is embedded in `current_state.json`.

## Safety state

- Production catalogue published: **false**
- Production search index replaced: **false**
- Flutter repository modified: **false**
- Existing Supabase or R2 assets deleted: **false**
