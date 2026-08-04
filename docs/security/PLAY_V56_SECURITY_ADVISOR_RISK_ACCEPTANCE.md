# Security Advisor Risk Acceptance — Play v56

## Before
Security WARN: 15 (2 search_path + 12 authenticated SECURITY DEFINER + 1 leaked password)

## After play_v56_security_hardening (+ legacy RLS initplan)
Security WARN expected: ~11 intentional DEFINER RPCs + leaked password (dashboard)
Performance auth_rls_initplan on customer_* fixed; legacy user_* optimized in follow-up migration.

## Intentional remaining SECURITY DEFINER + authenticated EXECUTE
These are the public mobile/portal RPC API surface. Each derives ownership from auth.uid(),
pins search_path, and has anon EXECUTE revoked:
- customer_upsert_collection_item / soft_delete_collection_item
- customer_upsert_binder / soft_delete_binder
- customer_upsert_binder_membership / soft_delete_binder_membership
- customer_purge_cloud_collection_data
- customer_request_collection_data_deletion
- get_market_price_bundle
- request_market_price_refresh

Internal helpers (begin/ack/require_auth_uid/payload_hash): EXECUTE revoked from authenticated.

## Leaked password protection
OWNER ACTION in Supabase Auth dashboard (HaveIBeenPwned). CardScanR production login is Google OAuth;
enable protection if email/password remains enabled, or disable unused password signup.
