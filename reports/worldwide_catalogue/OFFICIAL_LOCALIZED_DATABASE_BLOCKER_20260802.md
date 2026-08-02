# Official localized Pokémon card database acquisition boundary

## Result

The official Pokémon card database is a materially stronger source for German, Spanish, Italian, and Brazilian Portuguese card names and images, but it is not available as a stable, unauthenticated bulk-data interface. The remaining regional skeletons were therefore **not** promoted from English-derived structure to verified localized card records.

## Verified ordinary-browser behavior

- `https://www.pokemon.com/de/karten` redirects to the official German card database and renders the localized set filters and card search normally in an ordinary in-app browser.
- A focused search for `Pikachu` returned server-rendered localized card links such as `/de/pokemon-sammelkartenspiel/pokemon-karten/series/svp/27/` and public static images such as `https://assets.pokemon.com/static-assets/content-assets/cms2-de-de/img/cards/web/SVP/SVP_DE_27.png`.
- The site JavaScript at `https://assets.pokemon.com/static2/_ui/js/card-database.js` submits the search form as a normal GET. Search results are server-rendered; no public card-roster JSON endpoint was exposed.
- The advertised autocomplete path `/us/api/pokemon-cards/lookups` returned HTTP 404 to a cookie-free request and is not a catalogue endpoint.
- Official indexed detail pages corroborate localized historical records under the German, Spanish, Italian, and Brazilian Portuguese routes.
- The official German sealed-product gallery for 2017 rendered 12 localized product records with official images and linked annual galleries from 2014 through 2026. It likewise exposed no stable bulk-data endpoint.

## Collector boundary

A single cookie-free request to the German filtered search route returned HTTP 200 and the complete 458,930-byte database page. Subsequent normal cookie-free requests across German, Spanish, Italian, and Brazilian Portuguese routes returned an Incapsula response titled `Pardon Our Interruption` (HTTP 200, 6,058 bytes) instead of card data.

Collection stopped at that boundary. CardScanR did not solve a CAPTCHA, replay browser cookies or tokens, imitate an authenticated session, alter browser storage, or bypass the access control. No localized card or sealed-product records or images were imported from this source.

## Consequence

The affected regional printing rows remain explicit provisional roster evidence only. Their local card metadata, exact physical finish or stamp variant, and exact image remain unresolved until Pokémon provides a stable authorized export/API or written permission and an approved acquisition method. Missing localized sealed-product catalogues are registered separately as explicit regional external blockers.
