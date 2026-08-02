# Worldwide open-dataset staging report

- Generated: `2026-08-02T00:03:25.080222+00:00`
- Database bytes: `967,208,960`
- Database SHA-256: `2b291ff0343dd5a5ed64b2e3000dc5d5cdc36b9b3794a08dee7a02dcea496382`
- SQLite integrity: `ok`
- Foreign-key failures: `0`

## Core counts

| Entity | Rows |
|---|---:|
| source_provider | 2 |
| import_run | 2 |
| source_snapshot | 2 |
| source_record | 61,999 |
| series | 54 |
| card_set | 728 |
| set_release | 1,994 |
| card_design | 61,035 |
| card_printing | 160,392 |
| card_variant | 221,329 |
| card_localisation | 160,392 |
| attack | 200,418 |
| ability | 29,648 |
| marketplace_mapping | 305,302 |
| provider_entity_mapping | 36,478 |
| card_image_candidate | 40,888 |
| sealed_product | 188 |
| sealed_product_variant | 188 |
| product_content | 3,976 |
| unresolved_item | 36 |

## Language and region coverage

| Language | Region | Sets | Printings | Variants | Unknown variants | Quarantined |
|---|---|---:|---:|---:|---:|---:|
| de | INTL | 187 | 20,259 | 32,377 | 7,021 | 0 |
| en | INTL | 393 | 43,949 | 56,619 | 29,537 | 0 |
| es | INTL | 158 | 15,438 | 21,752 | 7,040 | 0 |
| es-mx | MX | 14 | 1,827 | 3,568 | 29 | 0 |
| fr | INTL | 204 | 21,604 | 33,520 | 8,247 | 2 |
| id | ID | 83 | 2,788 | 3,094 | 2,581 | 0 |
| it | INTL | 194 | 15,521 | 22,142 | 7,021 | 0 |
| ja | JP | 221 | 10,729 | 13,053 | 3,353 | 5 |
| ko | KR | 143 | 1,363 | 1,363 | 1,363 | 0 |
| nl | INTL | 3 | 0 | 0 | 0 | 0 |
| pl | INTL | 2 | 0 | 0 | 0 | 0 |
| pt | INTL | 129 | 14,332 | 20,647 | 5,932 | 27 |
| pt-br | BR | 11 | 1,124 | 1,124 | 1,124 | 2 |
| ru | INTL | 9 | 0 | 0 | 0 | 0 |
| th | TH | 77 | 2,994 | 3,300 | 2,787 | 0 |
| zh-cn | CN | 57 | 877 | 877 | 829 | 0 |
| zh-tw | TW | 109 | 7,587 | 7,893 | 7,380 | 0 |

## Enumerated language matrix

| Language | Expected regions | Printings | Status |
|---|---|---:|---|
| en | US, CA, GB, AU, NZ, SG, MY, PH | 43,949 | present |
| ja | JP | 10,729 | present |
| ko | KR | 1,363 | present |
| zh-cn | CN | 877 | present |
| zh-tw | TW, HK | 7,587 | present |
| th | TH | 2,994 | present |
| id | ID | 2,788 | present |
| fr | FR, CA, BE, CH | 21,604 | present |
| de | DE, AT, CH | 20,259 | present |
| es | ES, LATAM | 15,438 | present |
| es-mx | MX | 1,827 | present |
| it | IT, CH | 15,521 | present |
| pt | BR, PT | 14,332 | present |
| pt-br | BR | 1,124 | present |
| pt-pt | PT | 0 | enumerated_zero_printings |
| nl | NL, BE | 0 | enumerated_zero_printings |
| pl | PL | 0 | enumerated_zero_printings |
| ru | RU | 0 | enumerated_zero_printings |

## Source records

| Provider | Type | Records | Errors |
|---|---|---:|---:|
| pokemontcg-data | card | 20,444 | 0 |
| pokemontcg-data | deck | 188 | 0 |
| pokemontcg-data | set | 174 | 0 |
| tcgdex-cards-database | card | 40,591 | 0 |
| tcgdex-cards-database | series | 39 | 0 |
| tcgdex-cards-database | set | 563 | 0 |

This is an acquisition-stage report, not a completion declaration. Candidate mappings and image URLs remain unverified until their dedicated gates pass.
