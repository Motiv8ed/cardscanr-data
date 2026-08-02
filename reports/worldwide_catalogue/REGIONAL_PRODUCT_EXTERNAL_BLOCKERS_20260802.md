# Regional sealed-product external blockers

- Expected printed language/region pairs: `33`
- Pairs with an exact normalized product catalogue: `11`
- Pairs blocked on an authorized exact source: `22`

Each missing pair is stored in staging as `regional_sealed_product_catalogue_unavailable` with `blocked_external` status. Existing US, Japan, Korea, China, and Pokemon Asia catalogues are preserved; they are not projected onto regions whose packaging, contents, language, or release identity has not been verified.
