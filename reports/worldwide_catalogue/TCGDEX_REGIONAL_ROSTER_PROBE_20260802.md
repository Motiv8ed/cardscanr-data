# TCGdex regional roster gap probe

- Exact language-specific rosters recovered: `0`
- Empty language-specific rosters: `98` sets / `5,471` expected printings
- Language-specific sets not found: `16` sets / `274` expected printings
- Derived records tested: `5,745`
- Fetch failures or invalid responses: `0`

| Language | Empty sets | Not found | Expected printings | API cards |
|---|---:|---:|---:|---:|
| de | 7 | 16 | 606 | 0 |
| es | 30 | 0 | 1,582 | 0 |
| it | 47 | 0 | 2,296 | 0 |
| nl | 3 | 0 | 228 | 0 |
| pl | 1 | 0 | 130 | 0 |
| pt | 8 | 0 | 718 | 0 |
| ru | 2 | 0 | 185 | 0 |

The probe queried the exact public `https://api.tcgdex.net/v2/{language}/sets/{set}` endpoint for every set release represented by `cardscanr-regional-roster-derivations`. Raw responses, hashes, request outcomes, and resumable state are retained outside Git under `D:\CardScanR_worldwide_runtime_20260802\regional\tcgdex-regional-rosters`.

This result does not disprove that the regional printings exist. It proves that the TCGdex language-specific API cannot supply exact card-level metadata for these derived gaps. The provisional identities remain preserved and unresolved; they must not be promoted as verified local metadata or exact variants based on the English reference roster.
