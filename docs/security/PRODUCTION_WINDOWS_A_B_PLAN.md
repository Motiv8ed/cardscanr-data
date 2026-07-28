# Proposed production windows (NOT EXECUTED)

## Hard rules

- Do **not** use `supabase db push`.
- Do **not** apply A and B security migrations together (B is superseded).
- Do **not** combine Window A and Window B.
- Record migration-history rows only after the corresponding SQL file succeeds.
- Inserts must be idempotent.

## Remote history gap (current)

Remote `supabase_migrations.schema_migrations` currently has only image-pipeline rows:

1. `20260707204553` pokemon_card_image_pipeline  
2. `20260707205727` pokemon_card_image_records_grants  
3. `20260710063309` pokemon_card_image_provider_unavailable_status  
4. `20260723215548` card_image_manifests_build46  
5. `20260724004833` card_image_manifests_quality_classification_build47  

These are **not** present as identically named files in the beta repo (`D:\CardScanR\supabase\migrations`). Market-price migrations exist in `cardscanr-data` but are **not** in remote history (schema exists; history incomplete).

Therefore ordered `psql -f` + explicit history inserts is required.

---

## WINDOW A — Canonical security remediation only

Prereq: verified logical backup + owner approval for apply.

```powershell
# Process-local only — do not echo
# $env:CARDSCANR_DATABASE_URL = '<postgres uri for qstcdlczasmvexpgbpjk>'

$PG = 'D:\CardScanR_Supabase_Backups\_tools\pgsql\pgsql\bin'
$env:PATH = "$PG;$env:PATH"

# 1) Apply canonical SQL only
psql $env:CARDSCANR_DATABASE_URL -v ON_ERROR_STOP=1 `
  -f "D:\cardscanr-data\supabase\migrations\20260727000000_security_advisor_remediation.sql"

# 2) Only after success: idempotent history insert
psql $env:CARDSCANR_DATABASE_URL -v ON_ERROR_STOP=1 -c @"
insert into supabase_migrations.schema_migrations (version, name)
values ('20260727000000', 'security_advisor_remediation')
on conflict (version) do nothing;
"@

# 3) Verification
psql $env:CARDSCANR_DATABASE_URL -v ON_ERROR_STOP=1 `
  -f "D:\cardscanr-data\supabase\verification\20260727000000_security_advisor_remediation_verify.sql"
```

Do **not** apply `20260727100000_security_advisor_remediation_views_and_grants.sql`.

Owner Auth step (non-SQL): enable leaked-password protection per `docs/security/LEAKED_PASSWORD_PROTECTION.md`.

---

## WINDOW B — Four beta migrations only (after Window A passes + fresh backup)

```powershell
$files = @(
  'D:\CardScanR\supabase\migrations\20260726090000_beta_program_core.sql',
  'D:\CardScanR\supabase\migrations\20260726090100_beta_installations_and_telemetry.sql',
  'D:\CardScanR\supabase\migrations\20260726090200_beta_feedback.sql',
  'D:\CardScanR\supabase\migrations\20260726090300_beta_admin_dashboard_functions.sql'
)

$versions = @(
  @{ version='20260726090000'; name='beta_program_core' },
  @{ version='20260726090100'; name='beta_installations_and_telemetry' },
  @{ version='20260726090200'; name='beta_feedback' },
  @{ version='20260726090300'; name='beta_admin_dashboard_functions' }
)

for ($i=0; $i -lt $files.Count; $i++) {
  psql $env:CARDSCANR_DATABASE_URL -v ON_ERROR_STOP=1 -f $files[$i]
  if ($LASTEXITCODE -ne 0) { throw "Window B stopped at $($files[$i])" }
  $v = $versions[$i].version
  $n = $versions[$i].name
  psql $env:CARDSCANR_DATABASE_URL -v ON_ERROR_STOP=1 -c "insert into supabase_migrations.schema_migrations (version, name) values ('$v', '$n') on conflict (version) do nothing;"
}
```

---

## Migration-history recording plan

| After successful file | Insert |
|---|---|
| `20260727000000_security_advisor_remediation.sql` | `('20260727000000','security_advisor_remediation') ON CONFLICT DO NOTHING` |
| each Window B file | matching version/name above, `ON CONFLICT DO NOTHING` |

Never insert history for a file that failed. Never mark B’s superseded security file as applied.

## Rollback (high level)

- Window A: reverse grants/views/storage policy from Build 46/47 + prior function grants (see migration header).
- Window B: drop beta objects/functions/bucket only with an explicit owner-approved reverse script (not prepared here).
- Prefer restore from the logical backup taken immediately before the window.
