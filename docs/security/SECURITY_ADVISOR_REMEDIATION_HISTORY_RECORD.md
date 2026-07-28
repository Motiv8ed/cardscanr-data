# Security Advisor remediation — Git history record

**Status:** ALREADY APPLIED TO LIVE PROJECT — DO NOT REAPPLY

| Field | Value |
| --- | --- |
| Canonical migration | `supabase/migrations/20260727000000_security_advisor_remediation.sql` |
| Version | `20260727000000` |
| Name in `schema_migrations` | `security_advisor_remediation` |
| SHA-256 | `d30bbcb9af729018c92348e2da5ad94f8aa0436b19b6935f6be2ab6440a98bea` |
| Apply evidence | `docs/security/window_a_apply_20260728/` |
| Project | `qstcdlczasmvexpgbpjk` |
| Apply route | Supabase MCP `execute_sql` (Window A, 2026-07-28) |

## Purpose of this Git commit

This commit records the **canonical** remediation SQL and supporting verification
in the `cardscanr-data` repository so Git history matches live
`supabase_migrations.schema_migrations`.

It does **not** authorize or perform a second apply.

## Do not

* Re-run this migration against the live project
* Apply the divergent app-repo draft `20260727100000_security_advisor_remediation_views_and_grants.sql`
* Treat the app-repo draft as a second migration authority

## Verification

Use `supabase/verification/20260727000000_security_advisor_remediation_verify.sql`
(read-only checks) if needed. Prefer Window A evidence pack for historical proof.