"""Unit checks for pass5 discovery helpers."""

from __future__ import annotations

import image_independence_pass5_discovery as p5


def test_build_queries_include_lang_and_aliases() -> None:
    card = p5.CardTarget(
        canonical="x",
        language="en",
        set_id="1455",
        set_name="Best of Promos",
        collector="004/009",
        name="Rockets Scizor 4 Winner",
        failure_reason="auth_only",
    )
    qs = p5.build_queries(card)
    assert any("Best of Promos" in q for q in qs)
    assert any("Rockets Scizor" in q for q in qs)


def test_classify_permitted_and_wayback() -> None:
    rights, auto = p5.classify_url("https://images.pokemontcg.io/ex10/1.png")
    assert auto and rights == "approved_independence_ingestion"
    rights, auto = p5.classify_url(
        "https://web.archive.org/web/20250101000000id_/https://images.pokemontcg.io/ex10/1.png"
    )
    assert auto and rights == "wayback_of_permitted_host"
    rights, auto = p5.classify_url(
        "https://web.archive.org/web/20250101000000id_/https://api.pokewallet.io/images/pk_abc"
    )
    assert not auto and "pokewallet" in rights
    rights, auto = p5.classify_url("https://www.serebii.net/card/x/001.shtml")
    assert not auto


def test_dedupe_candidates() -> None:
    cands = [
        p5.Candidate("https://a/x.png", "s", None, "r", "e", True),
        p5.Candidate("https://a/x.png?1", "s", None, "r", "e", True),
        p5.Candidate("https://b/y.png", "s", None, "r", "e", False),
    ]
    out = p5.dedupe_candidates(cands)
    assert len(out) == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all_pass")
