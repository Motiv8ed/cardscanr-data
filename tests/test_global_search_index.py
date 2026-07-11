import json
from pathlib import Path

from cardscanr_search_index.global_builder import build_global_search_index, verify_global_search_index


def test_global_index_excludes_authenticated_urls_and_preserves_region(tmp_path: Path) -> None:
    cards = tmp_path / "cards.jsonl"
    images = tmp_path / "images.jsonl"
    output = tmp_path / "global.sqlite"
    card = {"canonicalPrintingId":"p1","canonicalSetId":"s1","language":"es-419","region":"LATAM","nativeCardName":"Carta","englishCardName":"Card","nativeSetName":"Set Nativo","englishSetName":"Set","printedCollectorNumber":"001","normalizedCollectorNumber":"1","providerCardIds":{"pokewallet":"secret-card"},"providerSetIds":{"pokewallet":"set1"},"searchAliases":["alias"],"designations":[],"rarity":"rare","regulationMark":"G","releaseDate":"2026-01-01"}
    image = {"canonicalPrintingId":"p1","provider":"pokewallet","authenticationRequirement":"X-API-Key","directUseTechnicalStatus":"auth_server_only","normalizedThumbnailUrl":"https://api.pokewallet.io/images/secret-card?size=low","normalizedDisplayUrl":"https://api.pokewallet.io/images/secret-card?size=high","mirrorPermissionStatus":"pending"}
    cards.write_text(json.dumps(card)+"\n",encoding="utf-8")
    images.write_text(json.dumps(image)+"\n",encoding="utf-8")
    build_global_search_index(cards_path=cards,direct_images_path=images,output_path=output)
    result=verify_global_search_index(output)
    assert result["classification"]=="PASS"
    assert result["perLanguageCounts"]=={"es-419":1}
    assert result["authenticatedUrls"]==0
