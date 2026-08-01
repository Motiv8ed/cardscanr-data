# Worldwide catalogue pre-change backup

- Classification: `VERIFIED_PRE_CHANGE_PRODUCTION_BACKUP`
- Repository starting commit: `94fe6a41a9c31fa7a66de83626acfca5614702e1`
- Backup directory: `D:\CardScanR_backups\worldwide_prechange_20260801T230701Z`
- Backup manifest SHA-256: `451fe86990ff6bf43f7e1f533bdf5047fa48acdf4220062b58dc2704e88e054f`
- Supabase resources: 22
- Supabase rows: 81,485
- R2 objects inventoried: 12
- R2 publication manifests copied: 3
- Local publication manifests copied: 6
- Payload files checksum-verified: 32
- Checksum failures: 0
- Credentials included: no

Five public tables did not initially grant `SELECT` to `service_role`. Their grants were captured, `SELECT` was granted temporarily for the local export, and then revoked. A post-export query verified that all five tables still have RLS enabled and have zero `service_role` `SELECT` grants, matching the initial state.

The backup payload is deliberately outside Git because it contains production data. Only counts, paths and checksums are tracked here.
