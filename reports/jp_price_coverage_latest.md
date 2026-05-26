# JP Price Coverage Audit

- generatedAtUtc: 2026-05-26T08:34:54Z
- ledgerPath: data/pokewallet_price_request_ledger.json

## Coverage Summary

- total JP app catalogue cards: 28,043
- JP cards with at least one current price: 19,942
- JP cards without current price: 8,101
- JP card price coverage: 71.11%
- current price files: 380
- current price rows: 40,012

## Worst Coverage Sets

| Set ID | Set Name | Total Cards | Covered | Missing | Coverage |
|---|---|---:|---:|---:|---:|
| SV4a | レイジングサーフ | 320 | 0 | 320 | 0.00% |
| S12a | VSTARユニバース | 254 | 0 | 254 | 0.00% |
| SV8a | テラスタルフェスex | 237 | 0 | 237 | 0.00% |
| S-P-CS | S-P/CS | 220 | 0 | 220 | 0.00% |
| SV-P-ID | SV-P/ID | 218 | 0 | 218 | 0.00% |
| SV2a | ポケモンカード151 | 210 | 0 | 210 | 0.00% |
| SV-P-TH | SV-P/TH | 182 | 0 | 182 | 0.00% |
| svM | svM | 175 | 0 | 175 | 0.00% |
| SV11B | ブラックボルト | 174 | 0 | 174 | 0.00% |
| SV11W | ホワイトフレア | 174 | 0 | 174 | 0.00% |

## Best Coverage Sets

| Set ID | Set Name | Total Cards | Covered | Missing | Coverage |
|---|---|---:|---:|---:|---:|
| 23598 | SV1a: Triplet Beat | 103 | 103 | 0 | 100.00% |
| 23599 | SV2a: Pokemon Card 151 | 210 | 210 | 0 | 100.00% |
| 23600 | SV3a: Raging Surf | 92 | 92 | 0 | 100.00% |
| 23601 | SV4a: Shiny Treasure ex | 360 | 360 | 0 | 100.00% |
| 23602 | SV5a: Crimson Haze | 96 | 96 | 0 | 100.00% |
| 23603 | SV6a: Night Wanderer | 94 | 94 | 0 | 100.00% |
| 23604 | SV7a: Paradise Dragona | 94 | 94 | 0 | 100.00% |
| 23605 | SV1S: Scarlet ex | 108 | 108 | 0 | 100.00% |
| 23606 | SV1V: Violet ex | 108 | 108 | 0 | 100.00% |
| 23607 | SV2P: Snow Hazard | 99 | 99 | 0 | 100.00% |

## Duplicate / Ambiguous Matches

- cards with multiple current price rows: 17,016
- exact duplicate price rows: 0
- orphan current price rows not mapped to app cards: 0
- current price rows missing canonicalCardId: 0

### Multi-row examples
- pokemon|jp|23857|009/024|sandaconda: 4 rows (sources: {'pokewallet': 4}, currencies: {'EUR': 2, 'USD': 2}, variants: {'holo': 2, 'normal': 2})
- pokemon|jp|23877|006/013|machop: 4 rows (sources: {'pokewallet': 4}, currencies: {'EUR': 2, 'USD': 2}, variants: {'holo': 2, 'normal': 2})
- pokemon|jp|23893|002/053|swadloon: 4 rows (sources: {'pokewallet': 4}, currencies: {'EUR': 2, 'USD': 2}, variants: {'first_edition': 2, 'unlimited': 2})
- pokemon|jp|23893|003/053|leavanny: 4 rows (sources: {'pokewallet': 4}, currencies: {'EUR': 2, 'USD': 2}, variants: {'first_edition': 2, 'unlimited': 2})
- pokemon|jp|23893|006/053|deerling: 4 rows (sources: {'pokewallet': 4}, currencies: {'EUR': 2, 'USD': 2}, variants: {'first_edition': 2, 'unlimited': 2})

## Unmatched / Unusable

- latest import missingPriceSetsSelected: 0
- latest import unmatched records: 0
- latest import unusable records: 0
- latest import validation result: not_run

## Breakdown

- source counts: {'pokewallet': 40012}
- currency counts: {'EUR': 18461, 'USD': 21551}
- variant counts: {'first_edition': 6107, 'first_edition_holo': 2114, 'holo': 13710, 'normal': 14232, 'unlimited': 2973, 'unlimited_holofoil': 876}

## App Readiness Summary

- status: needs_review
- message: JP current prices still have uncovered app cards.
- next step: Continue the missing-set import worker until coverage is complete.
- note: JP catalogue cards still have uncovered current-price gaps.
- note: Missing-set JP price import is complete; move to non-price audits.

## Supporting Reports

- latest worker report available: yes
- latest import report available: yes
