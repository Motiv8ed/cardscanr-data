# PokéCardex ↔ TCGdex crosswalk

Generated: 2026-10-14T02:07:39Z

## Set crosswalk

- PokéCardex CHN sets: **59**
- Mapped to TCGdex zh-cn set id: **40**
- EN name bridge only: **2**
- Unmatched sets: **17**
- Methods: `{'unmatched': 17, 'en_name_bridge_only': 1, 'manual_alias_30th_promo': 1, 'exact_set_id': 31, 'half_set_transform': 9}`

## Card / version reconcile (all metadata rows)

- Metadata rows: **15039**
- Version kinds: `{'standard': 8931, 'reverse': 2936, 'reverse_masterball': 1823, 'special_alternate': 1340, 'rare_promo_style': 8, 'holo_or_special': 1}`
- Standard rows: **8931**
- Standard rows inside mapped TCGdex sets: **6750**
- Genuinely unmapped standard candidates: **2181**
- Genuinely unmapped non-standard candidates: **893**
- Status counts: `{'genuinely_unmapped_candidate': 3074, 'in_tcgdex_zh_cn_set_not_in_catalogue': 11965}`

## Interpretation

Rows in a mapped TCGdex zh-cn set are treated as known Chinese printings (often missing from the CardScanR catalogue). Genuinely unmapped candidates lack both a mapped TCGdex zh-cn set and a catalogue soft-match — these need human/visual review before calling them new printings. Non-standard version rows (reverse / masterball / special) are variants of the same collector number, not automatically additive printings.

TCGdex zh-cn set endpoints currently return cardCount but empty cards[]. Card IDs are therefore synthesized as {setId}-{normalizedCollector} for provenance linking; visual/OCR validation remains required.
