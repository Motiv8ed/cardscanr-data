# Global Language and Region Contract

This contract uses canonical BCP-47-compatible language tags while storing release region separately.

## Rules

- `language` describes the printed text/script.
- `region` describes the release market only when the provider proves it.
- `releaseTerritories` lists known territories without claiming a single exact market.
- A provider language is never used as proof of region when that provider combines markets.
- Legacy IDs are retained in the provider crosswalk. Existing public IDs are not rewritten in place.
- `zh-Hant` from TCGdex remains `region=MULTI` with territories `TW` and `HK`; it must not be guessed as either market.
- `en` remains `region=GLOBAL` until stronger release evidence exists.

## Reversible aliases

- `jp` and `jap` map to `ja`.
- `zh-cn` and `chs` map to `zh-Hans`.
- `zh-tw` and `cht` map to `zh-Hant`.
- `kr` maps to `ko`.
- `es-mx` maps to `es-419`, the Unicode/BCP-47 Latin America macroregion.
- `pt-br` maps to `pt-BR`.
- `pt-pt` maps to `pt-PT`, even when the current provider exposes no set.

The values `zh`, `chn`, `chi`, `zho`, and `pt` are deliberately ambiguous and require source evidence.

## Region separation

Canonical set and printing identities include both language and region. Two records with the same translated name,
collector number, or artwork are not merged across regions. A later region split is represented as a reversible
crosswalk from the provisional `MULTI` identity; it is never a destructive rewrite.
