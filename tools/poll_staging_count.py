#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(r"D:\CardScanR_Archive\image_staging\global_acq_20261013")


def main() -> int:
    total = 0
    if ROOT.exists():
        for lang in ROOT.iterdir():
            if not lang.is_dir() or lang.name.startswith("_"):
                continue
            total += sum(1 for _ in lang.rglob("display.webp"))
    print(total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
