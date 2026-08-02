# Worldwide open-dataset staging report

- Generated: `2026-08-02T05:05:50.901103+00:00`
- Database bytes: `2,004,836,352`
- Database SHA-256: `97c3dabeeed3fbd6d01fee7bf2df9ca39ba0f5c90791a1131278b718da3370bb`
- SQLite integrity: `ok`
- Foreign-key failures: `0`

## Core counts

| Entity | Rows |
|---|---:|
| source_provider | 24 |
| import_run | 27 |
| source_snapshot | 27 |
| source_record | 155,253 |
| series | 67 |
| card_set | 1,753 |
| set_release | 3,019 |
| card_design | 114,458 |
| card_printing | 222,088 |
| card_variant | 359,173 |
| card_localisation | 216,343 |
| attack | 253,287 |
| ability | 33,461 |
| marketplace_mapping | 323,303 |
| provider_entity_mapping | 145,688 |
| card_image_candidate | 423,992 |
| sealed_product | 4,284 |
| sealed_product_variant | 4,284 |
| product_content | 109,860 |
| product_image_candidate | 9,218 |
| image_validation_result | 57,847 |
| image_acquisition_attempt | 34,391 |
| publication_run | 1 |
| publication_artifact | 9 |
| accessory | 1,649 |
| unresolved_item | 119,586 |

## Language and region coverage

| Language | Region | Sets | Printings | Variants | Unknown variants | Quarantined |
|---|---|---:|---:|---:|---:|---:|
| de | INTL | 187 | 20,865 | 46,221 | 20,865 | 0 |
| en | INTL | 393 | 43,984 | 71,066 | 43,949 | 0 |
| en | MY | 45 | 0 | 0 | 0 | 0 |
| en | PH | 45 | 0 | 0 | 0 | 0 |
| en | SG | 45 | 0 | 0 | 0 | 0 |
| es | INTL | 158 | 17,020 | 31,732 | 17,020 | 0 |
| es-mx | MX | 14 | 1,827 | 5,366 | 1,827 | 0 |
| fr | INTL | 204 | 21,604 | 46,877 | 21,604 | 2 |
| id | ID | 176 | 15,113 | 15,626 | 15,113 | 0 |
| it | INTL | 194 | 17,817 | 32,938 | 17,817 | 0 |
| ja | JP | 221 | 10,729 | 20,429 | 10,729 | 5 |
| ko | KR | 257 | 4,646 | 4,646 | 4,646 | 0 |
| nl | INTL | 3 | 228 | 228 | 228 | 0 |
| pl | INTL | 2 | 130 | 130 | 130 | 0 |
| pt | INTL | 129 | 15,050 | 29,765 | 15,050 | 27 |
| pt-br | BR | 11 | 1,124 | 1,124 | 1,124 | 2 |
| ru | INTL | 9 | 185 | 185 | 185 | 0 |
| th | TH | 159 | 2,994 | 3,507 | 2,994 | 0 |
| zh-cn | CN | 394 | 41,185 | 41,233 | 41,185 | 0 |
| zh-tw | HK | 132 | 0 | 0 | 0 | 0 |
| zh-tw | TW | 241 | 7,587 | 8,100 | 7,587 | 0 |

## Publication readiness

| Gate | Value |
|---|---:|
| collector_number_collision_groups | 10,753 |
| classified_collector_collision_groups | 10,466 |
| collector_collision_groups_needing_review | 0 |
| secret_bearing_card_image_urls | 0 |
| secret_bearing_product_image_urls | 0 |
| secret_bearing_source_payloads | 0 |
| secret_bearing_product_payloads | 0 |
| transient_only_product_image_candidates | 3,106 |
| open_unresolved_items | 74,272 |
| unclassified_unresolved_items | 74,361 |
| external_blocker_state_mismatches | 0 |
| external_blocker_items | 63 |
| running_import_runs | 0 |
| orphan_product_contents | 0 |
| missing_core_provenance | 0 |
| unexplained_official_release_shortfalls | 1,162 |
| unclassified_regional_variants | 5,745 |
| failed_image_validation_results | 59 |
| not_found_image_acquisition_attempts | 54 |
| active_publication_runs | 0 |
| officially_printed_languages_without_records | 1 |

### Card images

| Total printings | With candidate | Technically verified | App eligible |
|---:|---:|---:|---:|
| 222,088 | 216,343 | 26,771 | 26,771 |

### Product images

| Product variants | With candidate | Technically verified | App eligible |
|---:|---:|---:|---:|
| 4,284 | 3,484 | 1,382 | 1,189 |

## Enumerated language matrix

| Language | Expected regions | Printings | Status |
|---|---|---:|---|
| en | US, CA, GB, AU, NZ, SG, MY, PH | 43,984 | present |
| ja | JP | 10,729 | present |
| ko | KR | 4,646 | present |
| zh-cn | CN | 41,185 | present |
| zh-tw | TW, HK | 7,587 | present |
| th | TH | 2,994 | present |
| id | ID | 15,113 | present |
| fr | FR, CA, BE, CH | 21,604 | present |
| de | DE, AT, CH | 20,865 | present |
| es | ES, LATAM | 17,020 | present |
| es-mx | MX | 1,827 | present |
| it | IT, CH | 17,817 | present |
| pt | BR, PT | 15,050 | present |
| pt-br | BR | 1,124 | present |
| pt-pt | PT | 0 | enumerated_zero_printings |
| nl | NL, BE | 228 | present |
| pl | PL | 130 | present |
| ru | RU | 185 | present |

## Source records

| Provider | Type | Records | Errors |
|---|---|---:|---:|
| cardscanr-catalogue-corrections | normalization_correction | 1 | 0 |
| cardscanr-existing-r2-images | card_image | 23,493 | 0 |
| cardscanr-missing-image-registry | missing_image_record | 2,907 | 0 |
| cardscanr-regional-roster-derivations | derived_card_roster | 5,745 | 0 |
| pokellector-english-gap-evidence | card_image_evidence | 50 | 0 |
| pokemon-asia-hk-official | product | 132 | 0 |
| pokemon-asia-id-official | card | 12,325 | 0 |
| pokemon-asia-id-official | product | 93 | 0 |
| pokemon-asia-my-official | product | 45 | 0 |
| pokemon-asia-ph-official | product | 45 | 0 |
| pokemon-asia-sg-official | product | 45 | 0 |
| pokemon-asia-th-official | product | 82 | 0 |
| pokemon-asia-tw-official | product | 132 | 0 |
| pokemon-cn-official | sealed_product | 162 | 0 |
| pokemon-japan-products-official | sealed_product | 1,947 | 0 |
| pokemon-korea-official-archive | card | 3,570 | 0 |
| pokemon-korea-official-archive | set | 114 | 0 |
| pokemon-korea-products-official-archive | sealed_product | 1,139 | 0 |
| pokemon-tcg-kb-pikaqian | card | 18,001 | 0 |
| pokemon-tcg-kb-pikaqian | set | 120 | 0 |
| pokemon-us-product-gap-evidence | sealed_product | 7 | 0 |
| pokemon-us-products-official-archive | sealed_product | 575 | 0 |
| pokemontcg-data | card | 20,444 | 0 |
| pokemontcg-data | deck | 188 | 0 |
| pokemontcg-data | set | 174 | 0 |
| ptcg-chs-datasets | card | 22,307 | 0 |
| ptcg-chs-datasets | collection | 217 | 0 |
| tcgdex-cards-database | card | 40,591 | 0 |
| tcgdex-cards-database | series | 39 | 0 |
| tcgdex-cards-database | set | 563 | 0 |

This is an acquisition-stage report, not a completion declaration. Candidate mappings and image URLs remain unverified until their dedicated gates pass.
