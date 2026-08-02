from cardscanr_worldwide.tcgdex_regional_probe import classify_roster


def test_classifies_exact_language_roster() -> None:
    assert classify_roster({"cardCount": {"official": 2}, "cards": [{"id": "a"}, {"id": "b"}]}, 2) == (
        "exact_roster", 2, 2,
    )


def test_classifies_empty_and_partial_rosters() -> None:
    assert classify_roster({"cardCount": {"official": 102}, "cards": []}, 102) == (
        "empty_roster", 0, 102,
    )
    assert classify_roster({"cardCount": {"official": 3}, "cards": [{"id": "a"}]}, 3) == (
        "partial_roster", 1, 3,
    )


def test_rejects_invalid_roster_payload() -> None:
    assert classify_roster([], 1) == ("invalid_payload", 0, None)
    assert classify_roster({}, 1) == ("invalid_payload", 0, None)
