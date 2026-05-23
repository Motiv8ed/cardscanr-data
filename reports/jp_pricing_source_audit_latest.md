# JP Pricing Source Audit

Generated at UTC: 2026-05-23T07:20:40Z

## Key Answers

- Pokewallet JP provider files have usable numeric price fields: **False**
- TCGdex JP catalogue files have usable numeric price fields: **False**
- Any local JP price-like values exist: **True**
- Any local JP JPY records exist: **False**
- JP unavailable due to missing source data in local non-eBay files: **True**
- Main builder skip is explicit data-path decision: **True**

## Evidence Summary

### Pokewallet JP provider catalog
- Set files: 451
- Cards scanned: 24032
- hasPriceFields=true count: 24032
- Numeric price-like values found: 0

### TCGdex JP catalog files
- Set files: 472
- Cards scanned: 28043
- pricingReferences.cardmarketAvailable=true count: 18703
- pricingReferences.tcgplayerAvailable=true count: 0
- Numeric price-like values found: 0

### Existing JP local price-like records
- JP records across local files: 4
- Currency counts: {'AUD': 4}
- Source counts: {'manual_seed': 4}
- Files with records: ['public/v1/history/daily/2026-05-12/pokemon/jp/tracked.json', 'public/v1/history/daily/2026-05-13/pokemon/jp/tracked.json', 'public/v1/history/daily/2026-05-14/pokemon/jp/tracked.json', 'public/v1/prices/pokemon/jp/sample.json']

### Builder behavior
- Main builder explicit skip status token present: True
- Dedicated Pokewallet JP builder exists: True
- Dedicated builder is wired into main build pipeline: False

## JP Current Price Count

- Before: 0
- After: 0

## Recommendation

- Keep JP as pricing unavailable in main pipeline. Do not fabricate prices. Use dedicated Pokewallet JP builder only when API-backed priced fields are actually returned.
