# U.S. official product archive reconciliation

- Legitimate official page identities indexed: `579`
- Official archive pages parsed: `575`
- Navigation placeholders excluded: `1` (`undefinedregions`)
- Official page shells with no recoverable detail body: `4`
- Corroborated product/style records recovered from those shells: `7`
- Remaining unclassified U.S. archive gaps: `0`

The official product-gallery collector checked both `www.pokemon.com` and `pokemon.com` archive
captures and retained every distinct digest. Four official page identities had no parseable detail body,
but their product identity, style, date, and contents remained recoverable from official announcements
and attributed public historical references. These records use the separate
`pokemon-us-product-gap-evidence` provider and `corroborated` verification status; they are not
misrepresented as parsed official pages.

| Official page identity | Recovered records |
|---|---:|
| `collector-chest-summer-2023-sprigatito-fuecoco-quaxly` | 1 |
| `kangaskhan-ex-battle-deck-greninja-ex-battle-deck` | 2 |
| `scarlet-violet-paradox-rift-elite-trainer-box` | 2 |
| `sun-and-moon-elite-trainer-box` | 2 |

The versioned evidence, exact sources, dates, contents, and style distinctions are retained in
`config/us_product_gap_evidence.json`. The staging import created `575` direct official products,
`7` corroborated products, `3,438` product-content rows, and `1,410` official image candidates.
