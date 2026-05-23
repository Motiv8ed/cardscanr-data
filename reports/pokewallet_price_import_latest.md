# PokeWallet Price Import

- startedAtUtc: 2026-05-23T21:44:12Z
- finishedAtUtc: 2026-05-23T21:44:24Z
- mode: write
- languages: jp
- source: both
- API requests used: 3
- endpoint success/failure: 3 / 0
- price records received: 810
- matched records: 405
- imported records: 810
- would import records: 810
- skipped existing better records: 0
- ambiguous records: 58
- unmatched records: 248
- unusable records: 4
- validation result: passed
- next recommended action: Review the JP current price sample and run validation/export reports before expanding max sets.

## Counts

- before: {'en': {'recordCount': 31211, 'fileCount': 159, 'sourceCounts': {'pokemon_tcg_api': 31211}, 'statusCounts': {'priced': 31211}, 'currencyCounts': {'USD': 31211}}, 'jp': {'recordCount': 0, 'fileCount': 0, 'sourceCounts': {}, 'statusCounts': {}, 'currencyCounts': {}}}
- after: {'en': {'recordCount': 31211, 'fileCount': 159, 'sourceCounts': {'pokemon_tcg_api': 31211}, 'statusCounts': {'priced': 31211}, 'currencyCounts': {'USD': 31211}}, 'jp': {'recordCount': 810, 'fileCount': 3, 'sourceCounts': {'pokewallet': 810}, 'statusCounts': {'priced': 810}, 'currencyCounts': {'EUR': 405, 'USD': 405}}}

## By Language
- jp: 810

## By Source
- cardmarket: 405
- tcgplayer: 405

## By Currency
- EUR: 405
- USD: 405

## By Variant
- holo: 582
- normal: 228

## Sets

| Language | Set | HTTP | Rows | Price records | Matched | Imported | Skipped existing | Ambiguous | Unmatched | Unusable |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| jp | 23599 / 23599 | 200 | 519 | 420 | 210 | 420 | 0 | 58 | 248 | 3 |
| jp | 23598 / 23598 | 200 | 104 | 206 | 103 | 206 | 0 | 0 | 0 | 1 |
| jp | 23600 / 23600 | 200 | 92 | 184 | 92 | 184 | 0 | 0 | 0 | 0 |
