# JP Image URL Audit

- Generated at: 2026-06-15T06:49:54
- Write aliases: True

## Summary

- jp_tcgdex_app_catalogue: total=28,161, with imageUrl=28,161, missing imageUrl=0
- jp_pokewallet_provider_catalogue: total=24,150, with imageUrl=24,150, missing imageUrl=0
- en_app_catalogue: total=46,417, with imageUrl=46,417, missing imageUrl=0
- en_pokewallet_provider_catalogue: total=31,515, with imageUrl=31,515, missing imageUrl=0

## Provider Counts

### jp_tcgdex_app_catalogue
- pokewallet: 21,797
- pokewallet_api_image_endpoint: 118
- tcgdex: 6,246

### jp_pokewallet_provider_catalogue
- pokewallet_api_image_endpoint: 24,150

### en_app_catalogue
- pokemon_tcg_api: 20,359
- pokewallet: 18,988
- pokewallet_api_image_endpoint: 7,070

### en_pokewallet_provider_catalogue
- pokewallet_api_image_endpoint: 31,515

## Regression Checks

- EN sv10 Arrokuda 062/182: imageUrl=https://images.pokemontcg.io/sv10/62.png
- JP M3 Nihil Zero 032/080 Espurr: imageUrl=https://assets.tcgdex.net/ja/M/M3/032/low.webp
- JP M3 013/080: imageUrl=https://assets.tcgdex.net/ja/M/M3/013/low.webp
- JP M3 056/080: imageUrl=https://assets.tcgdex.net/ja/M/M3/056/low.webp
- JP 043/132 candidate path: imageUrl=https://assets.tcgdex.net/ja/SV/SV9/043/low.webp

## Samples With Image URL

### jp_tcgdex_app_catalogue
- public/v1/catalog/pokemon/jp/cards/23598.json 001/073 Tropius 001 073: https://api.pokewallet.io/images/pk_fa95f59686eb913c80b9bfe51aefbca03761932d17e5c8d181c09abaaeea07f9429181746d6fd41adcd6fb6ea5fb?size=low
- public/v1/catalog/pokemon/jp/cards/23598.json 002/073 Foongus: https://api.pokewallet.io/images/pk_dbecda6cbf590b0bc8fb16ad8cca44e1fe79cf01405e125a5378680d30de67e3aeeb76c0ac4d786cf4a77e65164d?size=low
- public/v1/catalog/pokemon/jp/cards/23598.json 003/073 Amoonguss: https://api.pokewallet.io/images/pk_56930c0e62d7f229a098e0730a11dec9f2f3f4177f7c48634fa48ee3b980f1bfab5dad3c4ef74c7e43016b1aafa5?size=low
- public/v1/catalog/pokemon/jp/cards/23598.json 004/073 Sprigatito 004 073: https://api.pokewallet.io/images/pk_419e4e8c7a8c1250b0cd3ed06e01c798087f2504b26683187531d11d17195cd59b8f39f58620413a919d6a32c11c?size=low
- public/v1/catalog/pokemon/jp/cards/23598.json 005/073 Sprigatito 005 073: https://api.pokewallet.io/images/pk_fc314e5586abea6c031fd0b2fb91977da8f627783ce0820469c731a77765be5ea8b1f579424f9b9450d1205ed22d?size=low

### jp_pokewallet_provider_catalogue
- public/v1/provider-catalog/pokewallet/cards/jp/-163.json 184 Absol (S-P/CS 184): https://api.pokewallet.io/images/be28ecf2746c79c64ab13b708d617d8fca95a420d504565e176f5dcb8c8e2a2e?size=low
- public/v1/provider-catalog/pokewallet/cards/jp/-163.json 140 Adaman (S-P/CS 140): https://api.pokewallet.io/images/0902108660246a22968b8c1844067db7e17cceebf59f28bf35bc4913d9ecf1ae?size=low
- public/v1/provider-catalog/pokewallet/cards/jp/-163.json 142 Adaman (S-P/CS 142): https://api.pokewallet.io/images/748dee79189c89d038f1d44d112ce4f7878a6489f13e4cfe92d4fafe4934c8ab?size=low
- public/v1/provider-catalog/pokewallet/cards/jp/-163.json 186 Aerodactyl (S-P/CS 186): https://api.pokewallet.io/images/3712067a8a474cd2a60141d33df583f14b481fc60e3d51d485eb3babe3d060c2?size=low
- public/v1/provider-catalog/pokewallet/cards/jp/-163.json 51 Applin (S-P/CS 051): https://api.pokewallet.io/images/048d136803d81c63c5a6ed79e47774d2ccb52fa4cc3d1addc0a65a97336759b8?size=low

### en_app_catalogue
- public/v1/catalog/pokemon/en/cards/1375.json 001/165 Alakazam 1: https://api.pokewallet.io/images/pk_272f179e790646f7fc72b425820a06aa28f5f74dfa820b42b8ee2a72377c5cf90f17b1f539462a76e94a2099?size=low
- public/v1/catalog/pokemon/en/cards/1375.json 002/165 Ampharos 2: https://api.pokewallet.io/images/pk_8e478bd90fe76e874c7708f0193272a95966405e1c0521288f7d92241343371950c7404975d3e7f48b4093ab?size=low
- public/v1/catalog/pokemon/en/cards/1375.json 003/165 Arbok 3: https://api.pokewallet.io/images/pk_ef8e54cc7647c46e337506a987e4b049a38a4f7fdf15171c9082eb63f9f8e31b9140900e3af70d437784083f?size=low
- public/v1/catalog/pokemon/en/cards/1375.json 004/165 Blastoise 4: https://api.pokewallet.io/images/pk_22fa2156d3fde9c8b48fa5e48f381ddd13cceac75bfe32135a6598b4f47520ff4b8245f898a31e529df6dbd3?size=low
- public/v1/catalog/pokemon/en/cards/1375.json 005/165 Butterfree 5: https://api.pokewallet.io/images/pk_06d1da1cbf0cbb1a2357afc65a4bfdb66193c03382f43438dffc641faa187bd802b68d3fc23bb2a68eaa0c59?size=low

### en_pokewallet_provider_catalogue
- public/v1/provider-catalog/pokewallet/cards/en/-10.json DP 76 Chimchar Lv.8 (BKPR DP 76): https://api.pokewallet.io/images/67cacf0736175f5e7d822bbc6b78abaa16091663bdd7c23b14e5d9745fdf9959?size=low
- public/v1/provider-catalog/pokewallet/cards/en/-10.json DP 49 Grotle Lv.21 (BKPR DP 49): https://api.pokewallet.io/images/aff3f39d48d3f898b31489bfd26ebe7989cbb7419025637728f7b98310523848?size=low
- public/v1/provider-catalog/pokewallet/cards/en/-10.json MT 52 Happiny Lv.8 (BKPR MT 52): https://api.pokewallet.io/images/e5f568f17530633fb2388a8c0435f48598107581548cd5114abc970d75747c29?size=low
- public/v1/provider-catalog/pokewallet/cards/en/-10.json DP 6 Lucario Lv.30 (BKPR DP 6): https://api.pokewallet.io/images/0b79b55c36536dafdcf4a4eb5baa6b090cd11541d3a6a7dbdcd05fcdf9152660?size=low
- public/v1/provider-catalog/pokewallet/cards/en/-10.json DP 9 Manaphy Lv.20 (BKPR DP 9): https://api.pokewallet.io/images/94fbb9fc077778be5d14327f0ce5d34f9f778cd482126eded621b485851c7cf3?size=low

## Samples Missing Image URL

### jp_tcgdex_app_catalogue
- None

### jp_pokewallet_provider_catalogue
- None

### en_app_catalogue
- None

### en_pokewallet_provider_catalogue
- None

## Changed Files

- None
