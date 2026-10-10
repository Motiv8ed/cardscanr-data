# CardScanR EN/JA Pass 7 — Final 67 accounting

Generated: `2026-10-10T05:21:44Z`

## Outcome

- Targets: **67**
- Recovered & hosted this pass (identity-confirmed): **0**
- Candidate recoveries attempted then quarantined (identity fail-closed): **13**
- Still missing after pass: **67**
- Catalogue independent hosted after quarantine re-report: **67944 / 68011 (99.90%)**

## Identity audit

Post-apply review found that all 13 provisional pass7 acquisitions failed exact-identity requirements (wrong set/number, wrong card, unproven stamp/foil/signature, or non-kit-exact trainer reuse).
Assets were moved to `data/images/independence/pass7_quarantine/` and catalogue CDN bindings were reverted.

## Remaining blockers

- `ja_constructed_deck_kit_no_permitted_image`: **60**
- `stamped_or_exclusive_printing_no_permitted_public_image`: **5**
- `promo_or_match_exclusive_no_permitted_exact_image`: **2**

## Permission candidates

No fan-site or provisional API matches are approved for rehost after identity audit.
Pass6 pkmncards shared-thumb false positives remain rejected.
Provisional pass7 matches are recorded in the permission CSV with disposition `rejected_identity_mismatch_or_unproven_variant`.

Permission CSV: `en_jp_pass7_permission_candidates.csv`

## Collector scan request

Cards requiring new exact scans: **67**
CSV: `en_jp_collector_scan_request_final67.csv`

## Per-card ledger

CSV: `en_jp_pass7_final67_ledger.csv`

### Quarantined provisional recoveries (not hosted in catalogue)

- `pokemon|en|2282|077/167|iron_thorns_ex_077_167_2024_fernando_cifuentes_gold_signature` — `signature_stamp_not_proven_in_match`
- `pokemon|en|2374|110/193|larvitar_110_193_cosmo_foil` — `cosmo_foil_variant_not_proven`
- `pokemon|en|2374|112/203|dialga_112_203_cosmos_holo` — `cosmos_holo_variant_not_proven`
- `pokemon|en|pkmtch|S-P 163|cyndaquil` — `wrong_collector_number_matched_55_95`
- `pokemon|en|pkmtch|S-P 164|oshawott` — `wrong_set_pokemontcg_mcd11_4`
- `pokemon|jp|24045|009/014|super_scoop_up` — `kit_exact_printing_not_proven_shared_asset`
- `pokemon|jp|24046|009/014|energy_switch` — `wrong_set_pokemontcg_pop5_9_en`
- `pokemon|jp|24082|009/016|energy_search` — `wrong_set_latias_9_10`
- `pokemon|jp|24083|007/016|treecko_delta_species` — `wrong_set_pop_tournament`
- `pokemon|jp|24083|008/016|grovyle_delta_species` — `wrong_set_adv_p_promo`
- `pokemon|jp|24083|012/016|poke_ball` — `wrong_card_victini_poke_ball_pattern`
- `pokemon|jp|24086|008/015|dual_ball` — `wrong_set_number_008_014_vs_008_015`
- `pokemon|jp|24087|009/015|super_scoop_up` — `kit_exact_printing_not_proven_shared_asset`

### Still missing (individual)

- `pokemon|en|2282|068/195|kirlia_2023_tord_reklev` — Kirlia 2023 Tord Reklev [en 2282 #068/195] — blocker=`stamped_or_exclusive_printing_no_permitted_public_image`
- `pokemon|en|2282|077/167|iron_thorns_ex_077_167_2024_fernando_cifuentes_gold_signature` — Iron Thorns ex 077 167 2024 Fernando Cifuentes Gold Signature [en 2282 #077/167] — blocker=`stamped_or_exclusive_printing_no_permitted_public_image`
- `pokemon|en|2374|SWSH029|rayquaza_swsh029_pixel_cosmos_holo` — Rayquaza SWSH029 Pixel Cosmos Holo [en 2374 #SWSH029] — blocker=`stamped_or_exclusive_printing_no_permitted_public_image`
- `pokemon|en|2374|110/193|larvitar_110_193_cosmo_foil` — Larvitar 110 193 Cosmo Foil [en 2374 #110/193] — blocker=`stamped_or_exclusive_printing_no_permitted_public_image`
- `pokemon|en|2374|112/203|dialga_112_203_cosmos_holo` — Dialga 112 203 Cosmos Holo [en 2374 #112/203] — blocker=`stamped_or_exclusive_printing_no_permitted_public_image`
- `pokemon|en|pkmtch|S-P 163|cyndaquil` — Cyndaquil [en pkmtch #S-P 163] — blocker=`promo_or_match_exclusive_no_permitted_exact_image`
- `pokemon|en|pkmtch|S-P 164|oshawott` — Oshawott [en pkmtch #S-P 164] — blocker=`promo_or_match_exclusive_no_permitted_exact_image`
- `pokemon|jp|24045|009/014|super_scoop_up` — Super Scoop Up [ja 24045 #009/014] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24045|010/014|double_full_heal` — Double Full Heal [ja 24045 #010/014] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24045|014/014|conductive_quarry` — Conductive Quarry [ja 24045 #014/014] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24046|009/014|energy_switch` — Energy Switch [ja 24046 #009/014] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24046|010/014|double_full_heal` — Double Full Heal [ja 24046 #010/014] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24046|013/014|moms_kindness` — Moms Kindness [ja 24046 #013/014] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24046|014/014|miasma_valley` — Miasma Valley [ja 24046 #014/014] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24082|009/016|energy_search` — Energy Search [ja 24082 #009/016] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24082|013/016|pokenav` — PokeNav [ja 24082 #013/016] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24082|014/016|celios_network` — Celios Network [ja 24082 #014/016] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24083|001/016|torchic` — Torchic [ja 24083 #001/016] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24083|003/016|squirtle` — Squirtle [ja 24083 #003/016] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24083|007/016|treecko_delta_species` — Treecko Delta Species [ja 24083 #007/016] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24083|008/016|grovyle_delta_species` — Grovyle Delta Species [ja 24083 #008/016] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24083|011/016|pokenav` — PokeNav [ja 24083 #011/016] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24083|012/016|poke_ball` — Poke Ball [ja 24083 #012/016] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24083|014/016|celios_network` — Celios Network [ja 24083 #014/016] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24083|015/016|bills_maintenance` — Bills Maintenance [ja 24083 #015/016] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24083|016/016|multi_energy` — Multi Energy [ja 24083 #016/016] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24086|001/015|ponyta` — Ponyta [ja 24086 #001/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24086|002/015|rapidash` — Rapidash [ja 24086 #002/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24086|004/015|flareon_ex` — Flareon ex [ja 24086 #004/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24086|005/015|bagon_delta_species` — Bagon Delta Species [ja 24086 #005/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24086|006/015|shelgon_delta_species` — Shelgon Delta Species [ja 24086 #006/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24086|008/015|dual_ball` — Dual Ball [ja 24086 #008/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24086|009/015|switch` — Switch [ja 24086 #009/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24086|013/015|basic_fire_energy_013_015` — Basic Fire Energy 013 015 [ja 24086 #013/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24087|001/015|magnemite` — Magnemite [ja 24087 #001/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24087|005/015|dratini_delta_species` — Dratini Delta Species [ja 24087 #005/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24087|006/015|dragonair_delta_species` — Dragonair Delta Species [ja 24087 #006/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24087|007/015|eevee` — Eevee [ja 24087 #007/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24087|009/015|super_scoop_up` — Super Scoop Up [ja 24087 #009/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24087|010/015|holon_scientist` — Holon Scientist [ja 24087 #010/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24087|013/015|basic_lightning_energy_013_015` — Basic Lightning Energy 013 015 [ja 24087 #013/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24088|001/015|staryu` — Staryu [ja 24088 #001/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24088|002/015|ditto` — Ditto [ja 24088 #002/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24088|003/015|vaporeon_ex` — Vaporeon ex [ja 24088 #003/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24088|006/015|azumarill_delta_species` — Azumarill Delta Species [ja 24088 #006/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24088|008/015|potion` — Potion [ja 24088 #008/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24088|010/015|holon_scientist` — Holon Scientist [ja 24088 #010/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24088|015/015|holon_energy_wp` — Holon Energy WP [ja 24088 #015/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24091|001/024|jynx_delta_species` — Jynx Delta Species [ja 24091 #001/024] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24091|002/024|smoochum_delta_species` — Smoochum Delta Species [ja 24091 #002/024] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24091|003/024|ralts_delta_species` — Ralts Delta Species [ja 24091 #003/024] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24091|004/024|kirlia_delta_species` — Kirlia Delta Species [ja 24091 #004/024] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24091|005/024|gardevoir_ex_delta_species` — Gardevoir ex Delta Species [ja 24091 #005/024] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24091|006/024|smeargle_delta_species` — Smeargle Delta Species [ja 24091 #006/024] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24091|007/024|ralts` — Ralts [ja 24091 #007/024] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24091|009/024|trapinch_delta_species` — Trapinch Delta Species [ja 24091 #009/024] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24091|010/024|vibrava_delta_species` — Vibrava Delta Species [ja 24091 #010/024] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24091|012/024|dual_ball` — Dual Ball [ja 24091 #012/024] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24091|013/024|old_rod` — Old Rod [ja 24091 #013/024] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24091|018/024|island_hermit` — Island Hermit [ja 24091 #018/024] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24091|020/024|celios_network` — Celios Network [ja 24091 #020/024] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24091|023/024|buffer_piece` — Buffer Piece [ja 24091 #023/024] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24112|009/015|mr_stones_project` — Mr Stones Project [ja 24112 #009/015] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24159|004/012|sableye` — Sableye [ja 24159 #004/012] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24159|007/012|professor_oaks_research` — Professor Oaks Research [ja 24159 #007/012] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24159|008/012|oran_berry` — Oran Berry [ja 24159 #008/012] — blocker=`ja_constructed_deck_kit_no_permitted_image`
- `pokemon|jp|24159|009/012|multi_technical_machine_01` — Multi Technical Machine 01 [ja 24159 #009/012] — blocker=`ja_constructed_deck_kit_no_permitted_image`
