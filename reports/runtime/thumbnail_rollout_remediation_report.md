# Thumbnail Rollout — Gate A Remediation + Gate B Canary Report

- Classification: **PASS**
- Original Gate A verified: 391
- Replacement manifest: `reports\runtime\thumbnail_rollout_gate_a_replacements_9.json`
- Replacement SHA-256: `5780595eff5722342dd5066d99ebd327806f8f835d1cf957bed5bad982843e6e`
- Replacements attempted/uploaded/verified/failed: {'attempted': 9, 'uploaded': 9, 'verified': 9, 'failed': 0}
- Reconciled Gate A verified sample: 400
- Reconciled contact sheet: `reports\runtime\thumbnail_rollout_gate_a_reconciled_contact_sheet.png`
- Gate A idempotent: {'passed': True, 'downloadedCount': 0, 'uploadedCount': 0, 'skippedCount': 9, 'attemptedCount': 9}
- PokeWallet credential: **present**
- Gate B attempted/uploaded/verified/failed: {'attempted': 25, 'uploaded': 4, 'verified': 25, 'failed': 0}
- Gate B contact sheet: `reports\runtime\thumbnail_rollout_gate_b_canary_contact_sheet.png`
- Total Supabase thumbs: 528
- Provider breakdown: {'tcgdex': 20, 'pokewallet': 67, 'pokemon_tcg_api': 441}
- Average thumb bytes: 12952
- Projected English storage: 302753000
- Tests: {'passed': True, 'testsRun': 28, 'failures': 0, 'errors': 0}
- Display images imported: **False**
- Public catalogue URLs changed: **False**
- Search index rebuilt: **False**
- Flutter modified: **False**
- Full English import run: **False**
- Remaining 75 PokeWallet cards executed: **False**
- Scrydex used: **False**

## Original nine failures

- `pokemon|en|me3|19|dewgong`: provider_metadata_exists_but_image_cdn_unavailable (HTTP 404)
- `pokemon|en|me2pt5|256|boss_s_orders`: provider_metadata_exists_but_image_cdn_unavailable (HTTP 404)
- `pokemon|en|me2pt5|92|rotom`: provider_metadata_exists_but_image_cdn_unavailable (HTTP 404)
- `pokemon|en|me2pt5|214|urbain`: provider_metadata_exists_but_image_cdn_unavailable (HTTP 404)
- `pokemon|en|me2pt5|123|gastly`: provider_metadata_exists_but_image_cdn_unavailable (HTTP 404)
- `pokemon|en|me3|59|klefki`: provider_metadata_exists_but_image_cdn_unavailable (HTTP 404)
- `pokemon|en|me3|67|furfrou`: provider_metadata_exists_but_image_cdn_unavailable (HTTP 404)
- `pokemon|en|me2pt5|253|mega_audino_ex`: provider_metadata_exists_but_image_cdn_unavailable (HTTP 404)
- `pokemon|en|me2pt5|77|team_rocket_s_exeggcute`: provider_metadata_exists_but_image_cdn_unavailable (HTTP 404)

## Stop gate

Stopped after Gate B 25-card canary. Remaining 75 not executed.
