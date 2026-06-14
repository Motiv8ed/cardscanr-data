# JP Image URL Audit

- Generated at: 2026-06-15T06:48:09
- Write aliases: True

## Summary

- jp_tcgdex_app_catalogue: total=28,043, with imageUrl=28,043, missing imageUrl=0
- jp_pokewallet_provider_catalogue: total=24,150, with imageUrl=24,150, missing imageUrl=0
- en_app_catalogue: total=39,763, with imageUrl=39,763, missing imageUrl=0
- en_pokewallet_provider_catalogue: total=31,515, with imageUrl=31,515, missing imageUrl=0

## Provider Counts

### jp_tcgdex_app_catalogue
- pokewallet: 21,797
- tcgdex: 6,246

### jp_pokewallet_provider_catalogue
- pokewallet_api_image_endpoint: 24,150

### en_app_catalogue
- pokemon_tcg_api: 20,359
- pokewallet: 19,404

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

- public/v1/catalog/pokemon/en/cards/1375.json
- public/v1/catalog/pokemon/en/cards/1381.json
- public/v1/catalog/pokemon/en/cards/1387.json
- public/v1/catalog/pokemon/en/cards/1393.json
- public/v1/catalog/pokemon/en/cards/1399.json
- public/v1/catalog/pokemon/en/cards/1400.json
- public/v1/catalog/pokemon/en/cards/1401.json
- public/v1/catalog/pokemon/en/cards/1403.json
- public/v1/catalog/pokemon/en/cards/1407.json
- public/v1/catalog/pokemon/en/cards/1418.json
- public/v1/catalog/pokemon/en/cards/1421.json
- public/v1/catalog/pokemon/en/cards/1423.json
- public/v1/catalog/pokemon/en/cards/1427.json
- public/v1/catalog/pokemon/en/cards/1430.json
- public/v1/catalog/pokemon/en/cards/1433.json
- public/v1/catalog/pokemon/en/cards/1451.json
- public/v1/catalog/pokemon/en/cards/1453.json
- public/v1/catalog/pokemon/en/cards/1455.json
- public/v1/catalog/pokemon/en/cards/1464.json
- public/v1/catalog/pokemon/en/cards/1465.json
- public/v1/catalog/pokemon/en/cards/1481.json
- public/v1/catalog/pokemon/en/cards/1494.json
- public/v1/catalog/pokemon/en/cards/1509.json
- public/v1/catalog/pokemon/en/cards/1528.json
- public/v1/catalog/pokemon/en/cards/1532.json
- public/v1/catalog/pokemon/en/cards/1533.json
- public/v1/catalog/pokemon/en/cards/1534.json
- public/v1/catalog/pokemon/en/cards/1536.json
- public/v1/catalog/pokemon/en/cards/1538.json
- public/v1/catalog/pokemon/en/cards/1539.json
- public/v1/catalog/pokemon/en/cards/1540.json
- public/v1/catalog/pokemon/en/cards/1541.json
- public/v1/catalog/pokemon/en/cards/1542.json
- public/v1/catalog/pokemon/en/cards/1543.json
- public/v1/catalog/pokemon/en/cards/1576.json
- public/v1/catalog/pokemon/en/cards/1661.json
- public/v1/catalog/pokemon/en/cards/1663.json
- public/v1/catalog/pokemon/en/cards/1692.json
- public/v1/catalog/pokemon/en/cards/1694.json
- public/v1/catalog/pokemon/en/cards/1701.json
- public/v1/catalog/pokemon/en/cards/1729.json
- public/v1/catalog/pokemon/en/cards/17674.json
- public/v1/catalog/pokemon/en/cards/1780.json
- public/v1/catalog/pokemon/en/cards/1796.json
- public/v1/catalog/pokemon/en/cards/1815.json
- public/v1/catalog/pokemon/en/cards/1840.json
- public/v1/catalog/pokemon/en/cards/1842.json
- public/v1/catalog/pokemon/en/cards/1853.json
- public/v1/catalog/pokemon/en/cards/1861.json
- public/v1/catalog/pokemon/en/cards/1863.json
- public/v1/catalog/pokemon/en/cards/1919.json
- public/v1/catalog/pokemon/en/cards/1938.json
- public/v1/catalog/pokemon/en/cards/1957.json
- public/v1/catalog/pokemon/en/cards/2069.json
- public/v1/catalog/pokemon/en/cards/2071.json
- public/v1/catalog/pokemon/en/cards/2148.json
- public/v1/catalog/pokemon/en/cards/2155.json
- public/v1/catalog/pokemon/en/cards/2178.json
- public/v1/catalog/pokemon/en/cards/2205.json
- public/v1/catalog/pokemon/en/cards/2208.json
- public/v1/catalog/pokemon/en/cards/2209.json
- public/v1/catalog/pokemon/en/cards/2214.json
- public/v1/catalog/pokemon/en/cards/2278.json
- public/v1/catalog/pokemon/en/cards/2282.json
- public/v1/catalog/pokemon/en/cards/22872.json
- public/v1/catalog/pokemon/en/cards/22873.json
- public/v1/catalog/pokemon/en/cards/22880.json
- public/v1/catalog/pokemon/en/cards/2289.json
- public/v1/catalog/pokemon/en/cards/23095.json
- public/v1/catalog/pokemon/en/cards/23120.json
- public/v1/catalog/pokemon/en/cards/23228.json
- public/v1/catalog/pokemon/en/cards/23237.json
- public/v1/catalog/pokemon/en/cards/23266.json
- public/v1/catalog/pokemon/en/cards/2328.json
- public/v1/catalog/pokemon/en/cards/23286.json
- public/v1/catalog/pokemon/en/cards/23306.json
- public/v1/catalog/pokemon/en/cards/2332.json
- public/v1/catalog/pokemon/en/cards/23323.json
- public/v1/catalog/pokemon/en/cards/23353.json
- public/v1/catalog/pokemon/en/cards/23381.json
- public/v1/catalog/pokemon/en/cards/23473.json
- public/v1/catalog/pokemon/en/cards/23520.json
- public/v1/catalog/pokemon/en/cards/23529.json
- public/v1/catalog/pokemon/en/cards/23537.json
- public/v1/catalog/pokemon/en/cards/23561.json
- public/v1/catalog/pokemon/en/cards/2364.json
- public/v1/catalog/pokemon/en/cards/23651.json
- public/v1/catalog/pokemon/en/cards/2374.json
- public/v1/catalog/pokemon/en/cards/2377.json
- public/v1/catalog/pokemon/en/cards/23821.json
- public/v1/catalog/pokemon/en/cards/24053.json
- public/v1/catalog/pokemon/en/cards/24073.json
- public/v1/catalog/pokemon/en/cards/24152.json
- public/v1/catalog/pokemon/en/cards/24155.json
- public/v1/catalog/pokemon/en/cards/24163.json
- public/v1/catalog/pokemon/en/cards/2420.json
- public/v1/catalog/pokemon/en/cards/24269.json
- public/v1/catalog/pokemon/en/cards/24325.json
- public/v1/catalog/pokemon/en/cards/24326.json
- public/v1/catalog/pokemon/en/cards/24380.json
- public/v1/catalog/pokemon/en/cards/24382.json
- public/v1/catalog/pokemon/en/cards/24448.json
- public/v1/catalog/pokemon/en/cards/24461.json
- public/v1/catalog/pokemon/en/cards/2464.json
- public/v1/catalog/pokemon/en/cards/2534.json
- public/v1/catalog/pokemon/en/cards/2545.json
- public/v1/catalog/pokemon/en/cards/2555.json
- public/v1/catalog/pokemon/en/cards/2585.json
- public/v1/catalog/pokemon/en/cards/2626.json
- public/v1/catalog/pokemon/en/cards/2675.json
- public/v1/catalog/pokemon/en/cards/2686.json
- public/v1/catalog/pokemon/en/cards/2701.json
- public/v1/catalog/pokemon/en/cards/2765.json
- public/v1/catalog/pokemon/en/cards/2776.json
- public/v1/catalog/pokemon/en/cards/2782.json
- public/v1/catalog/pokemon/en/cards/2807.json
- public/v1/catalog/pokemon/en/cards/2848.json
- public/v1/catalog/pokemon/en/cards/2906.json
- public/v1/catalog/pokemon/en/cards/2948.json
- public/v1/catalog/pokemon/en/cards/3020.json
- public/v1/catalog/pokemon/en/cards/3040.json
- public/v1/catalog/pokemon/en/cards/3051.json
- public/v1/catalog/pokemon/en/cards/3064.json
- public/v1/catalog/pokemon/en/cards/3068.json
- public/v1/catalog/pokemon/en/cards/3087.json
- public/v1/catalog/pokemon/en/cards/3118.json
- public/v1/catalog/pokemon/en/cards/3150.json
- public/v1/catalog/pokemon/en/cards/3170.json
- public/v1/catalog/pokemon/en/cards/3172.json
- public/v1/catalog/pokemon/en/cards/3179.json
- public/v1/catalog/pokemon/en/cards/604.json
- public/v1/catalog/pokemon/en/cards/best.json
- public/v1/catalog/pokemon/en/cards/bkppr.json
- public/v1/catalog/pokemon/en/cards/bkpr.json
- public/v1/catalog/pokemon/en/cards/bxy.json
- public/v1/catalog/pokemon/en/cards/clc.json
- public/v1/catalog/pokemon/en/cards/clk.json
- public/v1/catalog/pokemon/en/cards/cs0l.json
- public/v1/catalog/pokemon/en/cards/dhd.json
- public/v1/catalog/pokemon/en/cards/drs.json
- public/v1/catalog/pokemon/en/cards/ec1.json
- public/v1/catalog/pokemon/en/cards/ec2.json
- public/v1/catalog/pokemon/en/cards/ec3.json
- public/v1/catalog/pokemon/en/cards/ec4.json
- public/v1/catalog/pokemon/en/cards/ec5.json
- public/v1/catalog/pokemon/en/cards/erb.json
- public/v1/catalog/pokemon/en/cards/gbml.json
- public/v1/catalog/pokemon/en/cards/gh.json
- public/v1/catalog/pokemon/en/cards/ghd.json
- public/v1/catalog/pokemon/en/cards/hgss.json
- public/v1/catalog/pokemon/en/cards/hsbw.json
- public/v1/catalog/pokemon/en/cards/hsz.json
- public/v1/catalog/pokemon/en/cards/ifds.json
- public/v1/catalog/pokemon/en/cards/ipb.json
- public/v1/catalog/pokemon/en/cards/ipnc.json
- public/v1/catalog/pokemon/en/cards/ipnt.json
- public/v1/catalog/pokemon/en/cards/ips.json
- public/v1/catalog/pokemon/en/cards/l1ss.json
- public/v1/catalog/pokemon/en/cards/l2s.json
- public/v1/catalog/pokemon/en/cards/l2t.json
- public/v1/catalog/pokemon/en/cards/maa.json
- public/v1/catalog/pokemon/en/cards/mal.json
- public/v1/catalog/pokemon/en/cards/mcd13.json
- public/v1/catalog/pokemon/en/cards/mcdp.json
- public/v1/catalog/pokemon/en/cards/mcrp.json
- public/v1/catalog/pokemon/en/cards/mcvs.json
- public/v1/catalog/pokemon/en/cards/me03.json
- public/v1/catalog/pokemon/en/cards/me1.json
- public/v1/catalog/pokemon/en/cards/me2.json
- public/v1/catalog/pokemon/en/cards/me2pt5.json
- public/v1/catalog/pokemon/en/cards/me3.json
- public/v1/catalog/pokemon/en/cards/me4.json
- public/v1/catalog/pokemon/en/cards/med.json
- public/v1/catalog/pokemon/en/cards/mep.json
- public/v1/catalog/pokemon/en/cards/mps.json
- public/v1/catalog/pokemon/en/cards/msd.json
- public/v1/catalog/pokemon/en/cards/nde.json
- public/v1/catalog/pokemon/en/cards/ndi.json
- public/v1/catalog/pokemon/en/cards/ng.json
- public/v1/catalog/pokemon/en/cards/nr.json
- public/v1/catalog/pokemon/en/cards/pccp.json
- public/v1/catalog/pokemon/en/cards/pkmsm.json
- public/v1/catalog/pokemon/en/cards/pkmtch.json
- public/v1/catalog/pokemon/en/cards/ppb.json
- public/v1/catalog/pokemon/en/cards/pps1.json
- public/v1/catalog/pokemon/en/cards/pps2.json
- public/v1/catalog/pokemon/en/cards/pps3.json
- public/v1/catalog/pokemon/en/cards/pps4.json
- public/v1/catalog/pokemon/en/cards/pps5.json
- public/v1/catalog/pokemon/en/cards/pps6.json
- public/v1/catalog/pokemon/en/cards/pps7.json
- public/v1/catalog/pokemon/en/cards/rsv10pt5.json
- public/v1/catalog/pokemon/en/cards/sea.json
- public/v1/catalog/pokemon/en/cards/shc.json
- public/v1/catalog/pokemon/en/cards/si100.json
- public/v1/catalog/pokemon/en/cards/skv.json
- public/v1/catalog/pokemon/en/cards/stf.json
- public/v1/catalog/pokemon/en/cards/sv10.json
- public/v1/catalog/pokemon/en/cards/sv8.json
- public/v1/catalog/pokemon/en/cards/sv8pt5.json
- ... 1,277 more
