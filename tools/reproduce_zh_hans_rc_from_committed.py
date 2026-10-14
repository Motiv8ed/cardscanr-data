#!/usr/bin/env python3
"""Reproduce zh-Hans RC catalogue + QA pack from committed inputs only.

Documented external dependency (not in git):
  LIVE_CARDS = D:\\CardScanR_Data\\cardscanr-data\\data\\global\\catalogue\\cards.jsonl
  (or --live-cards override)

Committed inputs required:
  - reports/.../catalogue_review/pokecardex_zh_hans_printings_review.jsonl
  - reports/.../auto_resolve/auto_resolved.jsonl
  - reports/.../zh_hans_catalogue_overlap_matches.jsonl

Outputs (gitignored regenerable):
  - data/global/catalogue/cards.jsonl
  - reports/.../release_candidate/qa_pack/zh-cn.sqlite
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED = [
    ROOT / "reports/global_image_acquisition/pokecardex/catalogue_review/pokecardex_zh_hans_printings_review.jsonl",
    ROOT / "reports/global_image_acquisition/pokecardex/auto_resolve/auto_resolved.jsonl",
    ROOT / "reports/global_image_acquisition/pokecardex/zh_hans_catalogue_overlap_matches.jsonl",
]
DEFAULT_LIVE = Path(r"D:\CardScanR_Data\cardscanr-data\data\global\catalogue\cards.jsonl")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-cards", type=Path, default=DEFAULT_LIVE)
    parser.add_argument("--skip-pack", action="store_true")
    args = parser.parse_args()

    missing = [str(p) for p in REQUIRED if not p.exists()]
    if missing:
        print(json.dumps({"ok": False, "missingCommittedInputs": missing}, indent=2))
        return 2
    if not args.live_cards.exists():
        print(json.dumps({"ok": False, "missingExternalLiveCards": str(args.live_cards)}, indent=2))
        return 2

    # Patch integrate live path via env is cleaner; temporarily invoke with cwd and
    # monkey by rewriting LIVE only if different — prefer subprocess with env.
    env_live = args.live_cards.resolve()
    integrate = ROOT / "tools" / "integrate_pokecardex_zh_hans_catalogue.py"
    # Ensure integrate uses the provided live path
    text = integrate.read_text(encoding="utf-8")
    if "LIVE_CARDS = Path(" not in text:
        print("integrate script missing LIVE_CARDS")
        return 1

    # Run integrate (uses hardcoded LIVE; verify it matches or fail)
    import integrate_pokecardex_zh_hans_catalogue as integ

    if Path(integ.LIVE_CARDS).resolve() != env_live:
        # Temporarily override module constant
        integ.LIVE_CARDS = env_live
    rc = integ.main()
    if rc != 0:
        return rc

    cards = ROOT / "data/global/catalogue/cards.jsonl"
    zh = 0
    with cards.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip() and json.loads(line).get("language") == "zh-Hans":
                zh += 1
    if zh != 14923:
        print(json.dumps({"ok": False, "zhHansCount": zh, "expected": 14923}, indent=2))
        return 1

    if not args.skip_pack:
        pack_rc = subprocess.call([sys.executable, str(ROOT / "tools/build_zh_hans_rc_qa_pack.py")], cwd=ROOT)
        if pack_rc != 0:
            return pack_rc
        layout_rc = subprocess.call(
            [sys.executable, str(ROOT / "tools/prepare_zh_hans_rc_pack_install_layout.py")], cwd=ROOT
        )
        if layout_rc != 0:
            return layout_rc

    out = {
        "ok": True,
        "zhHansCount": zh,
        "liveCards": str(env_live),
        "committedInputs": [str(p) for p in REQUIRED],
        "cardsOut": str(cards),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    # Allow importing sibling tools
    sys.path.insert(0, str(ROOT / "tools"))
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
