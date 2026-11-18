CARDSCANR WORLDWIDE CATALOGUE - READ ONLY RECONCILIATION
18 November 2026

This archive packages a real 480-card-metadata candidate batch, including
239 Korean metadata records. NO card images were downloaded.

On your home Windows PC:
1. Right-click the ZIP and choose Extract All.
2. Double-click Run-CardScanR-Reconciliation.cmd in the extracted folder.
3. The launcher reads the existing SQLite staging DB at:
   D:\CardScanR_worldwide_runtime_20260802\staging\worldwide-catalogue.sqlite
4. The result is under:
   %LOCALAPPDATA%\CardScanR\CatalogueReconciliation\<timestamp>\report.json
5. Share ONLY report.json with ChatGPT.

If your staging DB was moved, run PowerShell with:
  .\Run-CardScanR-Reconciliation.ps1 -Database "D:\path\to\worldwide-catalogue.sqlite"

The program refuses active SQLite WAL files, unexpected archive members,
altered source hashes and already-populated output directories. It never
changes staging, Supabase, cloud, website or Android data. All print variants
remain unverified, regardless of exact provider/set/number matches.

This is NOT an installable catalogue pack and is NOT proof artwork rights
or production publication. Korean matches still require real CardScanR
staging comparison and variant-level review. Do not send private DB files.

Provenance:
https://github.com/Motiv8ed/cardscanr-data/pull/51
https://github.com/Motiv8ed/cardscanr-data/actions/runs/44231007062
