CARDSCANR WORLDWIDE CATALOGUE — READ-ONLY RECONCILIATION v2
18 November 2026

This portable kit contains a verified 480-record TCGdex metadata snapshot,
including 239 Korean entries. NO card images were downloaded.

1. Extract the complete ZIP on your Windows PC.
2. Double-click Run-CardScanR-Reconciliation.cmd
3. The launcher searches ONLY likely CardScanR data folders under D: to depth 4,
   max 1200 directories. It never scans the entire drive indiscriminately.
4. If ONE staging SQLite file exists, it is checked and read without writes.
   The review report is placed at:
       %LOCALAPPDATA%\CardScanR\CatalogueReconciliation\<timestamp>\verified-review\report.json
5. If NONE or MULTIPLE staging files are found, it stops safely and writes:
       %LOCALAPPDATA%\CardScanR\CatalogueReconciliation\<timestamp>\SAFE-Database-Locator.json
6. Upload ONLY the report.json or SAFE-Database-Locator.json to Atlas/ChatGPT.
   Do NOT upload staging database files, private backups or credentials.

If you already know the verified staging SQLite location, you can bypass discovery:
  PowerShell: .\Run-CardScanR-Reconciliation.ps1 -Database 'D:\correct\path\worldwide-catalogue.sqlite'

If the staging DB has an active -wal file, the launcher stops; use a
checkpointed copy to avoid an incomplete immutable snapshot.

Safety:
- Never modifies, deletes, installs or moves databases/files.
- Does not run against production Supabase or change app/website data.
- Preserves canonical IDs and physical variant distinctions.
- No image downloads, public uploads or app publication.
- A source not found in staging is NOT confirmed to be a newly printed card.
- Results reflect the particular dated staging snapshot, not necessarily
  the latest production catalogue.

Source evidence:
https://github.com/Motiv8ed/cardscanr-data/pull/51
https://github.com/Motiv8ed/cardscanr-data/actions/runs/44231007062
