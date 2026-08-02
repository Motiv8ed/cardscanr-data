from cardscanr_worldwide.pikaqian_china_import import release_date


def test_pikaqian_release_date() -> None:
    assert release_date("2026/06/12") == "2026-06-12"
    assert release_date("2026-6-2") == "2026-06-02"
    assert release_date(None) is None
