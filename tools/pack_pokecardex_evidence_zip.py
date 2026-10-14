#!/usr/bin/env python3
"""Pack PokéCardex/global-acq evidence ZIP into the user's Downloads folder."""

from __future__ import annotations

import argparse
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition"
DEFAULT_OUT = Path(os.environ.get("USERPROFILE", r"C:\Users\andyg")) / "Downloads"


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"CardScanR_pokecardex_zh_hans_evidence_{utc_stamp()}.zip"
    include_globs = [
        "pokecardex/*.json",
        "pokecardex/*.md",
        "pokecardex/contact_sheets/*",
        "pokecardex/catalogue_review/*",
        "korean_image_sources_investigation.md",
        "korean_tcgdex_probe.json",
        "korean_acquisition_opportunity.json",
        "KOREAN_ACQUISITION_OPPORTUNITY.md",
        "before_coverage.json",
        "staging_acquire_summary.json",
    ]
    skip_names = {
        "zh_hans_cards.jsonl",
        "pokecardex_tcgdex_card_crosswalk.jsonl",
        "pokecardex_zh_hans_printings_review.jsonl",  # large; summary + manifest in zip
        "zh_hans_image_manifest.jsonl",
        "zh_hans_variant_links.jsonl",
        "tcgdex_sets_en.json",
        "tcgdex_sets_ja.json",
        "tcgdex_sets_ko.json",
        "_validate_stdout.txt",
        "watch_finish_status.json",
    }
    # Include slim jsonl samples? Prefer summaries. Also add RIGHTS pointer.
    written = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for pattern in include_globs:
            for path in REPORT.glob(pattern):
                if not path.is_file():
                    continue
                if path.name in skip_names:
                    continue
                arc = path.relative_to(REPORT).as_posix()
                zf.write(path, arcname=f"global_image_acquisition/{arc}")
                written += 1
        zf.writestr(
            "global_image_acquisition/RIGHTS_POINTER.txt",
            (
                "PokéCardex written permission evidence is stored privately at:\n"
                "D:\\CardScanR_Archive\\rights_evidence\\pokecardex\\"
                "POKECARDEX_WRITTEN_PERMISSION_2026-10-13.md\n"
                "Chinese images remain staged only; productionPromotionAllowed=false.\n"
                "TCGdex staging milestone preserved under "
                "D:\\CardScanR_Archive\\image_staging\\global_acq_20261013 (10,049 display.webp).\n"
                "Full review jsonl (not in this ZIP): "
                "reports/global_image_acquisition/pokecardex/catalogue_review/"
                "pokecardex_zh_hans_printings_review.jsonl\n"
            ),
        )
        # Also copy a second archive under CardScanR_Archive/Downloads for operator continuity
        alt = Path(r"D:\CardScanR_Archive\Downloads")
        alt.mkdir(parents=True, exist_ok=True)
    # Write primary then copy
    print(f"wrote {out} files={written}")
    alt_out = Path(r"D:\CardScanR_Archive\Downloads") / out.name
    alt_out.write_bytes(out.read_bytes())
    print(f"copied {alt_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
