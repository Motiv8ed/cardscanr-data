# Asia product gallery gaps

- Generated during continuation after usage-limit interruption
- Official source family: `https://asia.pokemon-card.com/{locale}/products/`

## Local sealed-product galleries collected

| Locale | Detail pages | Products collected | Unparsed pages | Notes |
|---|---:|---:|---:|---|
| id | 77 | 102 | 4 | Expanded with archive/card, article-detail, WordPress, SPA, title/og parsers |
| th | 83 | 98 | 10 | Same parser family as Indonesia |
| hk | 114 | 187 | 12 | Expanded with current parser family |
| tw | 112 | 186 | 11 | Expanded with current parser family |

## English Asia locales without local sealed-product galleries

| Locale | `/products/` behavior | Local sealed products collected | Blocks |
|---|---|---:|---|
| sg | Index links to US `tcg.pokemon.com/en-us/expansions/*` only | 0 | Local sealed-product catalogue / pack art |
| my | Same US expansion redirect pattern | 0 | Local sealed-product catalogue / pack art |
| ph | Same US expansion redirect pattern | 0 | Local sealed-product catalogue / pack art |

These locales still have complete official card inventories and hydrated/parsed card details. The missing surface is the local sealed-product gallery, not card identity.

## Remaining unparsed special pages

Remaining unparsed pages are predominantly featured-card microsites or SPA marketing pages that do not expose a parseable sealed-product block and do not meet the conservative title+og:image product rule. They are retained as collected page evidence and counted as `unparsed_pages`; they are not invented into sealed products.

## Owner actions

1. Confirm whether SG/MY/PH sealed products are intentionally US-shared SKUs for those markets.
2. If local packaging/contents differ, supply an authorized local product catalogue or owner-visible export.
3. For residual SPA microsites, provide owner-visible HTML/JSON captures if exact local SKUs must be represented beyond title/og evidence.
