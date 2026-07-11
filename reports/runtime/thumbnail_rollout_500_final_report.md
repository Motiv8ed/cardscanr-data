# Thumbnail Rollout — 500-card Final Report

- Classification: **PARTIAL**
- Remaining 75: {'attempted': 75, 'skipped': 5, 'uploaded': 63, 'verified': 68, 'failed': 7, 'providerImageUnavailable': 7}
- Rate-limit pauses/wait: 0 / 15.4s
- Gate B complete verified: 93
- Full rollout verified: 493
- Combined contact sheet: `reports\runtime\thumbnail_rollout_500_combined_contact_sheet.png`
- Total Supabase thumbs: 591
- Provider breakdown: {'tcgdex': 20, 'pokewallet': 130, 'pokemon_tcg_api': 441}
- Sample total/avg thumb bytes: 6313792 / 12806
- Projected full-English storage: 299340250
- Catalogue patch: `None`
- imageCached=true changes: 0
- Canary search index: `None`
- Canary SHA-256: `None`
- Flutter compatibility: PARTIAL
- Cache/offline: {'deviceDiskCacheProven': False, 'offlineRenderProven': False, 'blocker': 'catalogue_wiring_and_canary_index_skipped_due_to_incomplete_500'}
- Catalogue wiring skipped: **True**
- Tests: {'passed': True, 'testsRun': 28, 'failures': 0, 'errors': 0}
- Display images imported: **False**
- Production catalogue published: **False**
- Production search index replaced: **False**
- Flutter modified: **False**
- Full English import: **False**

## Unresolved defects

- pokemon|en|me2pt5|123|gastly: provider_metadata_exists_but_image_cdn_unavailable (Gate A retained)
- pokemon|en|me2pt5|214|urbain: provider_metadata_exists_but_image_cdn_unavailable (Gate A retained)
- pokemon|en|me2pt5|253|mega_audino_ex: provider_metadata_exists_but_image_cdn_unavailable (Gate A retained)
- pokemon|en|me2pt5|256|boss_s_orders: provider_metadata_exists_but_image_cdn_unavailable (Gate A retained)
- pokemon|en|me2pt5|77|team_rocket_s_exeggcute: provider_metadata_exists_but_image_cdn_unavailable (Gate A retained)
- pokemon|en|me2pt5|92|rotom: provider_metadata_exists_but_image_cdn_unavailable (Gate A retained)
- pokemon|en|me3|19|dewgong: provider_metadata_exists_but_image_cdn_unavailable (Gate A retained)
- pokemon|en|me3|59|klefki: provider_metadata_exists_but_image_cdn_unavailable (Gate A retained)
- pokemon|en|me3|67|furfrou: provider_metadata_exists_but_image_cdn_unavailable (Gate A retained)
- pokemon|en|24326|034/086|galvantula_poke_ball_pattern: pokewallet_provider_image_cdn_unavailable (HTTP 404)
- pokemon|en|24326|053/086|mienshao_poke_ball_pattern: pokewallet_provider_image_cdn_unavailable (HTTP 404)
- pokemon|en|1528|076/131|snorlax_ex_prismatic_evolution_stamped: pokewallet_provider_image_cdn_unavailable (HTTP 404)
- pokemon|en|2069|2/30|lightning_energy_2: pokewallet_provider_image_cdn_unavailable (HTTP 404)
- pokemon|en|1533|7/30|metal_energy_7: pokewallet_provider_image_cdn_unavailable (HTTP 404)
- pokemon|en|1536|2/30|grass_energy_2: pokewallet_provider_image_cdn_unavailable (HTTP 404)
- pokemon|en|24326|041/086|gothita_poke_ball_pattern: pokewallet_provider_image_cdn_unavailable (HTTP 404)
- catalogue_wiring_and_canary_index_skipped_due_to_incomplete_500

Stopped after this report. Full English thumbnail import not started.
