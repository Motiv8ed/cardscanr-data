# Security remediation migration reconciliation

Status: **DUPLICATE MIGRATIONS IDENTIFIED — DO NOT APPLY BOTH**

## Candidates

| ID | Path | Authority |
|---|---|---|
| A (canonical preferred) | `D:\cardscanr-data\supabase\migrations\20260727000000_security_advisor_remediation.sql` | `cardscanr-data` (image/pricing/storage) |
| B (duplicate / superseded) | `D:\CardScanR\supabase\migrations\20260727100000_security_advisor_remediation_views_and_grants.sql` (also mirrored under `D:\Card Scanner App\supabase\migrations\`) | App/beta repo copy |

**Do not delete B yet.** Mark as superseded after Window A uses A. Proposed removal only after A is applied and verified.

## Structured diff

### Views / `security_invoker`

| Topic | A `20260727000000` | B `20260727100000` |
|---|---|---|
| `card_image_manifests_current` | `DROP` + `CREATE ... WITH (security_invoker=true)` | `CREATE OR REPLACE ... WITH (security_invoker=true)` |
| Current view columns | **Narrow** client-safe set (no source hashes, `source_url`, `r2_bucket`, `r2_original_key`, `verification_reason`, …) | **Wide** nearly full row including source hashes, original keys, verification_reason |
| Current filter | `is_current AND verification_status='verified'` | same |
| Legacy view | `DROP` + recreate, `security_invoker=true`, full admin columns | `CREATE OR REPLACE`, `security_invoker=true`, full admin columns |
| Owners | unchanged (postgres) | unchanged |

### Grants

| Object | A | B | Required policy |
|---|---|---|---|
| `card_image_manifests_current` | SELECT anon+authenticated+service_role | SELECT anon+authenticated+service_role | Match both |
| `card_image_manifests_with_legacy_records` | **service_role only** | **anon+authenticated+service_role** | **A wins** (service_role only) |
| `card_image_manifests` base | revoke ALL then SELECT anon/auth; DML service_role | not touched | Prefer A cleanup |
| `get_market_price_bundle` | authenticated+service_role (**no anon**) | **anon+authenticated+service_role** | **A wins** (AuthGate / no proven anon caller) |
| `request_market_price_refresh` | authenticated+service_role | authenticated+service_role | Match |
| `get_or_create_market_price_key` | service_role only | service_role only | Match |
| `handle_new_user*` | revoke from public/anon/auth/**service_role**; no re-grant | revoke from public/anon/auth; **grant postgres+service_role** | Prefer A (trigger/internal; no client EXECUTE). service_role grant in B is unnecessary for triggers |
| `rls_auto_enable` | revoke all client/service EXECUTE | revoke client; grant postgres+service_role | Prefer A |

### Function bodies / search_path

| Topic | A | B |
|---|---|---|
| Redefines function bodies? | No | No |
| Sets `search_path` | Yes on listed functions | No |

### Storage

| Topic | A | B | Required policy |
|---|---|---|---|
| Drop `pokemon_card_images_public_read` | Yes | Yes | Yes |
| Replacement SELECT policies | **None** (rely on public bucket object URLs) | Creates authenticated + service_role listing policies | Prefer A for “no broad listing”; B still allows authenticated listing |

### Other

| Topic | A | B |
|---|---|---|
| Adds `quality_classification` if missing | Yes | No (assumes present) |
| Rollback comments | Yes | Minimal |
| Verification assumptions | Companion verify SQL under `cardscanr-data/supabase/verification/` | Companion under `CardScanR/supabase/scripts/security_advisor_remediation_verification.sql` |

## Canonical choice

**Use A only:** `D:\cardscanr-data\supabase\migrations\20260727000000_security_advisor_remediation.sql`

Reasons:
1. Preferred authority is `cardscanr-data`.
2. Matches owner least-privilege policy (no anon pricing; legacy view service_role only; narrowed public columns).
3. Aligns with audited Flutter dependency (MarketPriceService behind AuthGate).
4. B’s anon `get_market_price_bundle` and public legacy-view SELECT conflict with that policy.

## Marking B

- Treat `20260727100000_security_advisor_remediation_views_and_grants.sql` as **superseded duplicate**.
- Do not apply it.
- After Window A succeeds, remove or relocate B in a follow-up docs/cleanup PR (not during backup gate).

## Intentional residual Advisor warnings after A

- `authenticated` EXECUTE on SECURITY DEFINER `get_market_price_bundle` / `request_market_price_refresh` (required by signed-in app).
- Leaked-password protection remains until Auth dashboard toggle (owner step; not SQL).
