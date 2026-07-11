# Thumbnail Rollout — Controlled English Execution Report

- Classification: **PARTIAL**
- Original manifest: `reports/runtime/thumbnail_rollout_en_500_manifest.json`
- Manifest SHA-256: `2e84741b1921388baac2346387040f65a7dbea0acf11924076809d7ec2438f5e`
- Gate A attempted/skipped/uploaded/verified/failed: 400/1/390/391/9
- Existing Supabase overlap: 1
- Gate A source bytes: 279882297
- Gate A stored thumb bytes: 5067414
- Gate A contact sheet: `reports\runtime\thumbnail_rollout_gate_a_contact_sheet.png`
- Gate A idempotent: True
- PokeWallet credential availability: **present**
- Gate B canary attempted/uploaded/verified/failed: 0/0/0/0 (BLOCKED)
- Remaining PokeWallet 75: not attempted
- PokeWallet contact sheet: none
- Combined verified/completed thumbs in Supabase: 490
- Provider breakdown: {'tcgdex': 20, 'pokewallet': 38, 'pokemon_tcg_api': 432}
- Unresolved English cards: 23042
- Actual batch avg thumb bytes: 12993
- Projected English thumb storage: 303711375
- Tests: {'passed': True, 'testsRun': 28, 'failures': 0, 'errors': 0}
- TCGdex diagnostic: `D:\cardscanr-data\reports\runtime\thumbnail_rollout_tcgdex_diagnostic.json`
- Display images imported: **False**
- Public catalogue URLs changed: **False**
- Search index rebuilt: **False**
- Flutter modified: **False**
- Full English import run: **False**

## Exact failures

- 9 Mega Evolution cards (me3 / me2pt5) returned HTTP 404 on images.pokemontcg.io
- Catalogue previously used images.scrydex.com mirrors; those hosts are not allowed for Gate A
- Gate B blocked because Gate A is not PASS

## Stop gate

Stopped after Gate A PARTIAL report. No catalogue wiring. No search-index rebuild.
