#!/usr/bin/env python3
"""Pack PokéCardex/global-acq evidence ZIP (reports + sample contact sheets; no full image dump)."""

from __future__ import annotations

import argparse
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition"
DEFAULT_OUT = Path(r"D:\CardScanR_Archive\Downloads")


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
        "korean_image_sources_investigation.md",
        "korean_tcgdex_probe.json",
        "before_coverage.json",
        "staging_acquire_summary.json",
    ]
    # Prefer slim summaries; skip huge card crosswalk jsonl in ZIP unless small
    skip_names = {
        "zh_hans_cards.jsonl",
        "pokecardex_tcgdex_card_crosswalk.jsonl",
        "tcgdex_sets_en.json",
        "tcgdex_sets_ja.json",
        "tcgdex_sets_ko.json",
    }
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
        # Tiny provenance pointer (not the private email body)
        zf.writestr(
            "global_image_acquisition/RIGHTS_POINTER.txt",
            (
                "PokéCardex written permission evidence is stored privately at:\n"
                "D:\\CardScanR_Archive\\rights_evidence\\pokecardex\\"
                "POKECARDEX_WRITTEN_PERMISSION_2026-10-13.md\n"
                "Chinese images remain staged only; productionPromotionAllowed=false.\n"
                "TCGdex staging milestone preserved separately under "
                "D:\\CardScanR_Archive\\image_staging\\global_acq_20261013 (10,049 display.webp).\n"
            ),
        )
    print(f"wrote {out} files={written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
