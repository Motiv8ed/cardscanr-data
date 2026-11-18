CARDSCANR — EXACT PROVIDER-ID AUDIT (READ ONLY)
18 November 2026

Purpose:
The 480-card metadata reconciliation verified overlapping set/language/number identities,
but 0 of the 480 upstream provider-card IDs was independently confirmed.
This tool checks the historical staging DB's actual stored provider_record_id
format so we can stop guessing about alias mappings.

This package uses the two files previously supplied by the owner:
- report.json
- reconciliation.jsonl

They should already be included in the complete downloadable kit.

Steps:
1. Extract the complete ZIP to any folder on Windows.
2. Double-click Run-CardScanR-Source-ID-Audit.cmd.
3. No administrator rights or installation needed. Python is already on the PC.
4. Upload ONLY the resulting SAFE-Provider-ID-Audit.json to ChatGPT.
5. Do not upload the 3GB SQLite database or secrets.

The script uses the verified local SQLite path:
D:\CardScanR_Data\worldwide_runtime\staging\worldwide-catalogue.sqlite

It uses SQLite mode=ro&immutable=1, checks the WAL state, runs 480 keyed SELECTs
and never writes to staging. Outputs under:
%LOCALAPPDATA%\CardScanR\CatalogueSourceAudit\<timestamp>

Provider source IDs in the report are catalogue metadata identifiers, not
customer data. The report includes at most two sample source IDs per language.

No physical printing variant or artwork publication approval results from
an ID match. This is a diagnostic only. No app, database, R2 or website changes.
