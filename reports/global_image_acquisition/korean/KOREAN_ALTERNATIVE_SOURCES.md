# Korean alternative data sources (no new bulk acquisition)

TCGdex KO API currently returns the same **239** concrete card objects already in the live catalogue (official-count proxy ~7.5k–7.8k). Do **not** start another large acquisition until a viable source is proven.

## Viable / watchlist sources

| Source | Metadata | Images | Notes | Next gate |
|---|---|---|---|---|
| **TCGdex API (`ko`)** | Partial — set list 95; card payloads often empty | URLs may appear; classify hosts before staging | Exhausted for *new* card objects today | Re-probe when `cards[]` fills; metadata-only |
| **TCGdex cards-database (GitHub)** | Possible MIT DB metadata | Artwork **not** covered by MIT | Prefer over scraping; verify KO completeness | Licence + KO coverage audit only |
| **Official Pokémon Korea / TPCi** | Press/promo lists if published | Promo assets only if terms allow | Highest legitimacy; not a full-set pipeline | Written enquiry draft (not sent) |
| **Collector contribution programme** | Attested metadata | User photos under contributor licence | Still needs Pokémon IP before production art | Design intake; no bulk ingest |
| **pokemontcg.io** | Monitor KO fields | Do not bulk-mirror | Completeness unknown for KO | Watch only |

## Explicitly non-viable without new written permission

- Marketplace / blog / Discord / fan-wiki scrape
- Purchased subscription dumps without redistribute rights
- Hotlinking third-party scan CDNs
- **PokéCardex** — CN written permission only; **no KO region**

## Policy reminder

Metadata licence ≠ artwork rights. KO image bytes downloaded this programme: **0**.

## Smallest next action

1. Schedule a lightweight TCGdex `ko` re-probe (set detail `cards[]` non-empty?).
2. If still empty, draft one written enquiry to Pokémon Korea / a licensed partner for **metadata** clarification — no acquisition until reply.
