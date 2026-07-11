# CardScanR global catalogue — layered identity status

Classification: **IDENTITY_READY_PERMISSION_BLOCKED**

- Catalogue records / exact: 117,665 / 117,665
- Exact physical variants / shared-front unresolved: 0 / 117,665
- Identity-safe images: 92,817; permission-blocked: 92,817
- Missing images: 24,848
- Existing 591 image-safe / unresolved: 331 / 260
- Identity-ready canaries: 1100; executable while permission pending: 0
- Projected thumbnails: 1.1902 GB, 92,817 writes
- Providers: TCGdex, Pokémon TCG API, PokéWallet all pending human review
- R2 writes / bulk downloads / production publications: 0 / 0 / 0

Tests: 59 passed in 3.10s (global catalogue, layered identity, image pipeline, and thumbnail rollout)

Files changed: layered identity contract/schema/tests are in concurrent commit `ce3de47f`; 18 reconciliation, report, and execution-plan files are in this report's focused commit.

Exact next command: `Record written provider response in reports/global_rollout/provider_permission_tracker.json, then regenerate eligibility.`
