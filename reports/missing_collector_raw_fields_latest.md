# Missing Collector Raw Fields

- generatedAtUtc: 2026-05-23T07:40:02Z
- languages: en, jp
- includeZh: false
- missingCollectorNumberTotal: 3903

## Classification Counts
- likely_provider_parser_gap: 2011
- true_unnumbered_or_product: 1540
- unsafe_name_only_number: 352

## Safe Recovery
- safeRecoverableCount: 0
- remainingBlockedCount: 3903
- recommendation: No safe recovery candidate found in stored provider fields.

## Parser Gap Signals
- raw_payload_has_card_info_but_no_stored_number: 2011
- raw_payload_has_market_fields: 2011
- appears_product_or_unnumbered: 1540
- number_like_token_only_in_name_fields: 352

## Top Sets By Classification
- likely_provider_parser_gap:
  - en UNP (UNP): 186
  - en EXS (EXS): 121
  - jp 23974 (DP2: Secret of the Lakes): 112
  - jp 23975 (DP3: Shining Darkness): 112
  - jp 23973 (DP1: Space-Time Creation): 111
  - jp 23729 (Darkness, and to Light...): 99
  - jp 23726 (Challenge from the Darkness): 93
  - en EXP (EXP): 92
- true_unnumbered_or_product:
  - en PKM (PKM): 296
  - en PKMSV (PKMSV): 119
  - jp 23721 (Expansion Pack): 102
  - jp 23725 (Leaders' Stadium): 96
  - jp 24175 (City Gym Decks): 95
  - en 1539 (League & Championship Cards): 93
  - jp 23740 (Expansion Pack (No Rarity)): 46
  - en 23330 (My First Battle): 40
- unsafe_name_only_number:
  - en WCD24 (WCD24): 102
  - en EP08 (EP08): 42
  - en 2686 (Battle Academy): 36
  - en 3051 (Battle Academy 2022): 36
  - jp 24175 (City Gym Decks): 28
  - jp DP3d (DP3d): 19
  - jp DP3p (DP3p): 19
  - en PPS1 (PPS1): 17

## Samples
- likely_provider_parser_gap:
  - en GTG Blaine (GTG) explicit=0 unsafeTitle=0
  - en GTG Blaine's Charmander (GTG) explicit=0 unsafeTitle=0
  - en GTG Blaine's Dodrio (GTG) explicit=0 unsafeTitle=0
  - en GTG Blaine's Growlithe (GTG) explicit=0 unsafeTitle=0
  - en GTG Blaine's Growlithe (GTG) explicit=0 unsafeTitle=0
  - en GTG Blaine's Ponyta (GTG) explicit=0 unsafeTitle=0
  - en GTG Blaine's Vulpix (GTG) explicit=0 unsafeTitle=0
  - en GTG Fervor (GTG) explicit=0 unsafeTitle=0
- true_unnumbered_or_product:
  - en HSZ Fighting Energy (HSZ) explicit=0 unsafeTitle=0
  - en HSZ Fire Energy (HSZ) explicit=0 unsafeTitle=0
  - en HSZ Grass Energy (HSZ) explicit=0 unsafeTitle=0
  - en HSZ Lightning Energy (HSZ) explicit=0 unsafeTitle=0
  - en HSZ Psychic Energy (HSZ) explicit=0 unsafeTitle=0
  - en HSZ Water Energy (HSZ) explicit=0 unsafeTitle=0
  - en IPNC Energy Stadium (IPNC) explicit=0 unsafeTitle=0
  - en IPNC Metal Energy (IPNC) explicit=0 unsafeTitle=0
- unsafe_name_only_number:
  - en GTG Blaine's Quiz #1 (GTG) explicit=0 unsafeTitle=1
  - en GTG Blaine's Quiz #2 (GTG) explicit=0 unsafeTitle=1
  - en PKM Online Code Card (WCD 2018: Buzzroc) (PKM) explicit=0 unsafeTitle=1
  - en PKM Online Code Card (WCD 2018: Dragones y Sombras) (PKM) explicit=0 unsafeTitle=1
  - en PKM Online Code Card (WCD 2018: Garbanette) (PKM) explicit=0 unsafeTitle=1
  - en PKM Online Code Card (WCD 2018: Victory Map) (PKM) explicit=0 unsafeTitle=1
  - en PKMSV Live Code Card (Pokémon TCG 2023 Collector Chest) (PKMSV) explicit=0 unsafeTitle=1
  - en PPS1 Darkness Energy (PPS1) explicit=0 unsafeTitle=1
