# Supabase Security Advisor remediation (owner gate)

Status: **READY FOR OWNER APPROVAL — DO NOT DEPLOY until Andrew approves.**

Marker: `SUPABASE_SECURITY_REMEDIATION_READY_FOR_OWNER_APPROVAL`

## Scope

- Repo: `D:\cardscanr-data` (authoritative migrations)
- Project: `qstcdlczasmvexpgbpjk`
- Migration: `supabase/migrations/20260727000000_security_advisor_remediation.sql`
- Verification: `supabase/verification/20260727000000_security_advisor_remediation_verify.sql`
- Auth dashboard (non-SQL): `docs/security/LEAKED_PASSWORD_PROTECTION.md`

## App dependencies verified

| Object | Client dependency |
|---|---|
| `card_image_manifests_current` | Flutter `SupabaseCardImageManifestLookup` |
| `card_image_manifests_with_legacy_records` | None in Flutter/Edge/publisher tooling |
| `get_market_price_bundle` | Flutter `MarketPriceService` (signed-in) |
| `request_market_price_refresh` | Flutter `MarketPriceService` (signed-in) |
| `get_or_create_market_price_key` | Backend worker / internal RPC only |
| `handle_new_user*` | `auth.users` triggers only |
| `rls_auto_enable` | Event-trigger internal only |
| `pokemon-card-images` listing | Not required; public object/CDN URLs used |

## Deploy after approval

1. Apply migration to project `qstcdlczasmvexpgbpjk`.
2. Run verification SQL.
3. Enable leaked-password protection in Auth dashboard.
4. Re-run Security Advisor.
5. Smoke: signed-app image load, signup profile/collection triggers, pricing RPC.

---

## History commit note (2026-07-28)

Window A applied this migration successfully. The Git commit on
`chore/security-advisor-remediation-history` records canonical history only.
**Do not reapply.** See `SECURITY_ADVISOR_REMEDIATION_HISTORY_RECORD.md`.
