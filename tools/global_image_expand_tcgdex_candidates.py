#!/usr/bin/env python3
"""Expand TCGdex image candidates for languages sparse/missing in direct_images.jsonl.

Writes a NEW staging candidates JSONL only (does not mutate production catalogue).
Downloads are not performed here — feed output into global_image_acquisition_pipeline.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition"
UA = "CardScanR-GlobalImageAcquisition/1.0"
# Languages with weak/zero rows in existing direct_images.jsonl.
TARGET_LANGS = {
    "ko": "KR",
    "zh-cn": "CN",  # zh-Hans
    "nl": "NL",
    "pl": "PL",
    "ru": "RU",
    "pt": "PT",  # may overlap pt-BR catalogue rows
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def http_json(url: str) -> Any:
    last = None
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(2**i, 8))
    raise last  # type: ignore[misc]


def card_image_urls(lang: str, card: dict[str, Any]) -> tuple[str | None, str | None]:
    image = card.get("image")
    if isinstance(image, str) and image.strip():
        base = image.strip().rstrip("/")
        return f"{base}/high.webp", f"{base}/low.webp"
    # Some payloads omit image; construct from id when serie/set known — skip fail-closed.
    return None, None


def expand_lang(lang: str, region: str, *, max_sets: int | None) -> list[dict[str, Any]]:
    sets = http_json(f"https://api.tcgdex.net/v2/{lang}/sets")
    if not isinstance(sets, list):
        return []
    rows: list[dict[str, Any]] = []
    for i, s in enumerate(sets):
        if max_sets is not None and i >= max_sets:
            break
        sid = str(s.get("id") or "")
        if not sid:
            continue
        detail = http_json(f"https://api.tcgdex.net/v2/{lang}/sets/{urllib.parse.quote(sid)}")
        cards = detail.get("cards") if isinstance(detail, dict) else None
        if not isinstance(cards, list):
            time.sleep(0.25)
            continue
        for card in cards:
            if not isinstance(card, dict):
                continue
            pid = str(card.get("id") or "")
            local_id = str(card.get("localId") or "")
            high, low = card_image_urls(lang, card)
            if not high:
                # Fetch card detail only when list entry lacks image.
                try:
                    cdetail = http_json(
                        f"https://api.tcgdex.net/v2/{lang}/cards/{urllib.parse.quote(pid)}"
                    )
                    high, low = card_image_urls(lang, cdetail if isinstance(cdetail, dict) else {})
                    time.sleep(0.15)
                except Exception:
                    continue
            if not high:
                continue
            # Canonical id uses expanded lang code; map zh-cn -> zh-Hans for CardScanR.
            canon_lang = {"zh-cn": "zh-Hans", "zh-tw": "zh-Hant"}.get(lang, lang)
            rows.append(
                {
                    "schemaVersion": "1.0.0",
                    "canonicalPrintingId": (
                        f"pokemon|{canon_lang}|{region}|tcgdex:{sid}|{local_id}|unspecified"
                    ),
                    "canonicalSetId": f"pokemon|{canon_lang}|{region}|tcgdex:{sid}",
                    "language": canon_lang,
                    "region": region,
                    "provider": "tcgdex",
                    "providerCardId": pid,
                    "providerSetId": sid,
                    "printedCollectorNumber": local_id,
                    "normalizedCollectorNumber": local_id,
                    "normalizedDisplayUrl": high,
                    "normalizedThumbnailUrl": low or high,
                    "originalDocumentedImageUrl": high,
                    "mirrorPermissionStatus": "pending_human_review",
                    "directUseTechnicalStatus": "direct_ready_permission_to_mirror_pending",
                    "authenticationRequirement": "not_required",
                    "expansionSource": "tcgdex_api_expand_20261013",
                }
            )
        time.sleep(0.35)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--languages", default="ko,zh-cn", help="Comma-separated TCGdex lang codes")
    parser.add_argument("--max-sets", type=int, default=None, help="Limit sets per language (resume-friendly)")
    parser.add_argument(
        "--out",
        type=Path,
        default=REPORT / "expanded_tcgdex_candidates.jsonl",
    )
    args = parser.parse_args()
    REPORT.mkdir(parents=True, exist_ok=True)
    langs = [x.strip() for x in args.languages.split(",") if x.strip()]
    total = 0
    summary: dict[str, Any] = {"generatedAtUtc": utc_now(), "byLanguage": {}, "out": str(args.out)}
    with args.out.open("w", encoding="utf-8") as fh:
        for lang in langs:
            region = TARGET_LANGS.get(lang, "MULTI")
            rows = expand_lang(lang, region, max_sets=args.max_sets)
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            summary["byLanguage"][lang] = len(rows)
            total += len(rows)
            print(f"{lang}: {len(rows)} candidates", flush=True)
    summary["total"] = total
    (REPORT / "expanded_tcgdex_candidates_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
