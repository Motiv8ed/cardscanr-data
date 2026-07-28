# Commands executed (Window A apply 2026-07-28)

1. robocopy verified backup -> Google Drive second copy
2. SHA-256 verify each manifest-listed file in the copy
3. Supabase MCP list_projects / execute_sql / get_advisors (project qstcdlczasmvexpgbpjk)
4. Pre-apply counts + advisor snapshot
5. execute_sql: full contents of 20260727000000_security_advisor_remediation.sql
6. execute_sql: idempotent schema_migrations insert for 20260727000000 / security_advisor_remediation
7. Post-apply validation queries (views, grants, privileges, role SET ROLE tests, counts)
8. get_advisors security rerun
9. Evidence pack written under this directory

Not executed:
- supabase db push
- Window B / beta migrations
- superseded 20260727100000_* migration
- Auth/OAuth configuration changes
- Stage A stop/restart/duplicate
- Physical phone access
