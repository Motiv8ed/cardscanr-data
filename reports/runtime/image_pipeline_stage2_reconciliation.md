# Image Pipeline Stage 2 Reconciliation

- Classification: **APPROVAL_READY**
- Generated at (UTC): 2026-07-07T23:39:25Z

## Coverage reconciliation

- Total catalogue cards: 74578
- Unique chain matchable: 36542
- Unresolved: 38036
- Ambiguous: 7246
- Duplicate provider mappings: 0
- Matchable by language: {'en': 23375, 'jp': 13167}
- Chain-selected by provider: {'pokewallet': 9937, 'pokemon_tcg_api': 20359, 'tcgdex': 6246}
- Provider capability (exclusive): {'tcgdex': 6246, 'pokemon_tcg_api': 20359, 'pokewallet': 9937}

### Count discrepancy

- Prior Stage 2 figure: 36542
- Prior inspection-style figure: 52663
- Stage 2 explanation: Stage 2 used audit_catalogue_coverage()/resolve_provider_image(), counting unique canonicalBaseIds where the fallback chain TCGdex → Pokémon TCG API (EN) → PokeWallet returns the first validated match. Current recount: 36542 = 6246 TCGdex + 20359 Pokémon TCG API + 9937 PokeWallet (mutually exclusive provider capabilities, no double-count).
- Inspection explanation: The earlier inspection figure 52,663 equals all 46,417 EN catalogue cards plus all 6,246 JP imageSource=tcgdex cards, assuming universal EN TCGdex/Pokémon TCG API eligibility without PokeWallet validation. Provider-capability recount for TCGdex + Pokémon TCG API only: 26605 (EN capability 20359, JP tcgdex-source 6246).

## TCGdex-bucket fallback investigation

- Affected cards: 26
- By classification: {'url_generation_defect': 26}

## Verification rerun

- Sample verification passed: True
- Idempotent rerun passed: True
- Tests passed: True
- Full import run: **False**

## Unresolved defects

