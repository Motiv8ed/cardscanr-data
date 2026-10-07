#!/usr/bin/env python3
"""Generate a large offline HTML fixture that exercises Unicode capture hazards.

LOCAL ONLY — never contacts eBay. Deterministic UTF-8 bytes for SHA comparison.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tests" / "fixtures" / "unicode_capture" / "unicode_sold_fixture.html"
DEFAULT_META = ROOT / "tests" / "fixtures" / "unicode_capture" / "unicode_sold_fixture.meta.json"

# Markers that must survive capture exactly (literal code points, not escapes).
ZWNJ = "\u200c"  # ZERO WIDTH NON-JOINER — Ceruledge failure character
SMART_APOS = "\u2019"  # ’
EN_DASH = "\u2013"  # –
EM_DASH = "\u2014"  # —
NBSP = "\u00a0"  # non-breaking space
ACCENTED = "Pokémon Café"  # é
JAPANESE = "サーナイト"  # Gardevoir katakana marker
EMOJI = "🔥🃏"  # emoji markers
REPLACEMENT_INTENTIONAL = ""  # fixture intentionally has ZERO U+FFFD


def build_html(*, min_bytes: int = 5_500_000) -> str:
    head = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Ceruledge{ZWNJ} Phantasmal Flames 20 {ACCENTED} {JAPANESE} {EMOJI} Sold | eBay</title>
</head>
<body>
<h1>Sold items{NBSP}{SMART_APOS} listings</h1>
<p>Results for Ceruledge{ZWNJ} Phantasmal Flames 20 {EN_DASH} raw &amp; graded {EM_DASH} {ACCENTED}</p>
<p id="jp-marker">Japanese marker: {JAPANESE}</p>
<p id="emoji-marker">Emoji marker: {EMOJI}</p>
<p id="zwnj-marker">ZWNJ between words: Ceruledge{ZWNJ}Phantasmal</p>
<ul class="srp-results">
"""
    item_tpl = (
        '  <li class="s-item">'
        '<a class="s-item__link" href="https://www.ebay.com.au/itm/{itm}">'
        '<h3 class="s-item__title"><span role="heading">'
        "Ceruledge{zwnj} Phantasmal Flames 20 {accent} {jp} {emoji} "
        "PSA{nbsp}10 {apos}raw{apos} {en} sold"
        "</span></h3></a>"
        '<div class="s-item__price">AU $12.50</div>'
        '<div class="s-item__caption">Sold 1 Oct 2026</div>'
        '<div class="s-item__detail">{pad}</div>'
        "</li>\n"
    )
    parts = [head]
    i = 0
    pad_unit = ("PADDING_BLOCK_" + ("x" * 200) + "_") * 20
    while True:
        itm = 200000000000 + i
        parts.append(
            item_tpl.format(
                itm=itm,
                zwnj=ZWNJ,
                accent=ACCENTED,
                jp=JAPANESE,
                emoji=EMOJI,
                nbsp=NBSP,
                apos=SMART_APOS,
                en=EN_DASH,
                pad=pad_unit,
            )
        )
        i += 1
        if i % 20 == 0:
            blob = "".join(parts)
            if len(blob.encode("utf-8")) >= min_bytes:
                break
            if i > 5000:
                break
    parts.append("</ul>\n<p>Sold items footer {em} done</p>\n</body></html>\n".format(em=EM_DASH))
    return "".join(parts)


def character_counts(text: str) -> dict[str, int]:
    return {
        "u200c_zwnj": text.count(ZWNJ),
        "japanese_marker": text.count(JAPANESE),
        "emoji_marker": text.count(EMOJI),
        "smart_apos": text.count(SMART_APOS),
        "en_dash": text.count(EN_DASH),
        "em_dash": text.count(EM_DASH),
        "nbsp": text.count(NBSP),
        "accented_pokemon": text.count(ACCENTED),
        "replacement_fffd": text.count("\ufffd"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--meta", type=Path, default=DEFAULT_META)
    ap.add_argument("--min-bytes", type=int, default=5_500_000)
    args = ap.parse_args(argv)

    html = build_html(min_bytes=int(args.min_bytes))
    data = html.encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    counts = character_counts(html)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    meta = {
        "path": str(args.out),
        "encoding": "utf-8",
        "byteLength": len(data),
        "charLength": len(html),
        "sha256": sha,
        "characterCounts": counts,
        "markers": {
            "zwnj": "U+200C",
            "japanese": JAPANESE,
            "emoji": EMOJI,
            "smartApos": SMART_APOS,
        },
        "intentionalReplacementCharacters": 0,
        "note": "LOCAL fixture only. Serves under host-resolver MAP to ebay.com.au — zero live eBay.",
    }
    args.meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "sha256": sha, "byteLength": len(data), "counts": counts}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
