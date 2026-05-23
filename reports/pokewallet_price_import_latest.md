# PokeWallet Price Import

- startedAtUtc: 2026-05-23T21:43:38Z
- finishedAtUtc: 2026-05-23T21:43:45Z
- mode: dry-run
- languages: en, jp
- source: both
- API requests used: 6
- endpoint success/failure: 6 / 0
- price records received: 1295
- matched records: 759
- imported records: 0
- would import records: 1163
- skipped existing better records: 0
- ambiguous records: 58
- unmatched records: 279
- unusable records: 137
- validation result: not_run
- next recommended action: Dry-run found usable mapped records. Re-run with --write for the same bounded set sample.

## Counts

- before: {'en': {'recordCount': 31211, 'fileCount': 159, 'sourceCounts': {'pokemon_tcg_api': 31211}, 'statusCounts': {'priced': 31211}, 'currencyCounts': {'USD': 31211}}, 'jp': {'recordCount': 0, 'fileCount': 0, 'sourceCounts': {}, 'statusCounts': {}, 'currencyCounts': {}}}
- after: {'en': {'recordCount': 31211, 'fileCount': 159, 'sourceCounts': {'pokemon_tcg_api': 31211}, 'statusCounts': {'priced': 31211}, 'currencyCounts': {'USD': 31211}}, 'jp': {'recordCount': 0, 'fileCount': 0, 'sourceCounts': {}, 'statusCounts': {}, 'currencyCounts': {}}}

## By Language
- en: 353
- jp: 810

## By Source
- cardmarket: 405
- tcgplayer: 758

## By Currency
- EUR: 405
- USD: 758

## By Variant
- holo: 613
- normal: 443
- reverse: 107

## Sets

| Language | Set | HTTP | Rows | Price records | Matched | Imported | Skipped existing | Ambiguous | Unmatched | Unusable |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| en | 604 / 604 | 200 | 104 | 203 | 102 | 0 | 0 | 0 | 1 | 103 |
| en | 1400 / 1400 | 200 | 222 | 222 | 222 | 0 | 0 | 0 | 0 | 0 |
| en | 1538 / 1538 | 200 | 60 | 60 | 30 | 0 | 0 | 0 | 30 | 30 |
| jp | 23599 / 23599 | 200 | 519 | 420 | 210 | 0 | 0 | 58 | 248 | 3 |
| jp | 23598 / 23598 | 200 | 104 | 206 | 103 | 0 | 0 | 0 | 0 | 1 |
| jp | 23600 / 23600 | 200 | 92 | 184 | 92 | 0 | 0 | 0 | 0 | 0 |
