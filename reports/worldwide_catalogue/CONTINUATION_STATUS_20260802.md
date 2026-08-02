# Worldwide catalogue continuation status

- Generated: 2026-08-02T08:57:57.206901+00:00
- Worktree: D:\\CardScanR_worktrees\\worldwide_catalogue_products_20260802
- Branch: eature/worldwide-pokemon-catalogue-products-20260802
- Runtime: D:\CardScanR_worldwide_runtime_20260802

## Phase A secured

- Interrupted working tree was 5 files, not 166; later commits already preserved the bulk of prior work.
- Committed parser/report units: 1361aac, 7517055a, 465b3539, 1eadb5a, 27e7301f, 2dff1135.
- Staging PRAGMA quick_check = ok; foreign_key_check = 0.
- No production manifest activation performed.

## Active writers

- hk cards: 7609/14285 full=running
- tw cards: 13199/14276 full=running

## Completed Asia card acquisition

- id: 12325 parsed (full details)
- th: 9554 parsed (full details)
- ph: 7406 parsed (full details)
- sg: 7406 parsed (hydrated from PH identical inventory)
- my: 7406 parsed (hydrated from PH identical inventory)

## Product galleries

- id products=92 latest=('completed', '{"detail_pages": 77, "indexed_detail_pages": 77, "pages_without_products": 13, "products": 92}')
- th products=90 latest=('completed', '{"detail_pages": 83, "indexed_detail_pages": 83, "pages_without_products": 18, "products": 90}')
- hk products=102 latest=('completed', '{"detail_pages": 95, "indexed_detail_pages": 95, "pages_without_products": 62, "products": 102}')
- tw products=102 latest=('completed', '{"detail_pages": 94, "indexed_detail_pages": 94, "pages_without_products": 61, "products": 102}')
- sg products=0 latest=('completed', '{"detail_pages": 0, "indexed_detail_pages": 0, "pages_without_products": 0, "products": 0}')
- my products=0 latest=('completed', '{"detail_pages": 0, "indexed_detail_pages": 0, "pages_without_products": 0, "products": 0}')
- ph products=0 latest=('completed', '{"detail_pages": 0, "indexed_detail_pages": 0, "pages_without_products": 0, "products": 0}')

## Card image validation

- id assets={'pass': 12324}
- th assets={'fail': 1, 'pass': 1965, 'pending': 7580}
- ph assets={'pass': 2024, 'pending': 5382}
- ja assets={'pass': 23150}

## Staging product image gap

- variants=4267
- variants_without_pass_image=906

## Notes

- SG/MY/PH official /products/ pages link to US 	cg.pokemon.com expansions only; no local sealed-product gallery index is published.
- Remaining Asia special microsites without parseable sealed-product blocks are retained as unparsed pages / evidence, not invented products.
- Dual-writer risk on TW avoided by stopping sequential multi-locale runner and resuming HK alone.
