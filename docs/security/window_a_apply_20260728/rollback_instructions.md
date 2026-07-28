# Rollback readiness — Window A

Backup (primary):
D:\CardScanR_Supabase_Backups\pre_security_beta_20260728_061818

Backup (verified second copy):
D:\Google Drive\CardScanR Backups\Supabase\pre_security_beta_20260728_061818

Status: BACKUP_VERIFIED_AND_RECONCILIATION_READY (copy hash PASS)

Logical rollback options (owner-only; destructive; not executed):
1. Prefer targeted reverse of Window A objects:
   - Recreate views without security_invoker / prior Build 46+47 column sets
   - Re-grant prior function EXECUTE as needed
   - Recreate storage policy pokemon_card_images_public_read if listing required
2. Or restore public schema data/objects from the logical dumps with pg_restore
   (storage binary objects are NOT in these dumps; auth.users not dumped)

Do not use supabase db push for rollback.
