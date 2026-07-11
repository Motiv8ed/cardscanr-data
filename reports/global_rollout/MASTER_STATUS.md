# CardScanR global catalogue — audited permission readiness

Classification: **AUDIT_PASS_PERMISSION_BLOCKED**

- Starting commit: `e60ac3a67b771aa45b31c80708ea67a127bba0e0`
- Final commit: this report's containing commit (`git rev-parse HEAD`)
- Concurrent commit: `overlapping_but_consistent`
- Total/exact after adversarial audit: 117,665 / 117,665
- Probable/ambiguous: 0 / 0
- Duplicate provider/regional conflicts: 0 / 0
- Existing 260 resolved exact/still unresolved: 0 / 260
- Missing images: 24,848
- Prepared commands: 13; currently executable: 0
- Provider statuses: TCGdex, Pokémon TCG API, PokéWallet all `pending`
- Tests: 65 passed in 3.88s
- Image downloads/R2 writes/production publication/Flutter changes: 0/0/0/0

Exact next human action: Review and send one provider permission email from reports/global_rollout/provider_permission_requests, then save the written response as an evidence file and update provider_permission_tracker.json.

Resume after recorded approval: `python tools/global_rollout.py permissions-status && python tools/global_rollout.py image-canary --provider tcgdex --language en --limit 100 --dry-run --batch-size 100 --max-writes 100 --max-bytes 2000000 --provider-rate 1 --stop-on-mismatch --contact-sheet`
