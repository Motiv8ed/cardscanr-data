#!/usr/bin/env python3
"""Remove false permanent_404 progress rows so a later resume can retry.

Safe to run while acquire is idle (no lock). Does not start downloads.
Default: scrub CS1AC rows that 404'd due to CDN case sensitivity.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

DEFAULT_STAGING = Path(r"D:\CardScanR_Archive\image_staging\pokecardex_zh_hans_20261014")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging-root", type=Path, default=DEFAULT_STAGING)
    parser.add_argument(
        "--sets",
        default="CS1AC",
        help="Comma-separated PokéCardex set codes whose permanent_404 rows should be scrubbed",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    progress = args.staging_root / "progress.jsonl"
    lock = args.staging_root / "acquire.pid"
    if lock.exists():
        try:
            lock_pid = int(lock.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            lock_pid = 0
        alive = False
        if lock_pid > 0:
            if os.name == "nt":
                import ctypes

                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                handle = kernel32.OpenProcess(0x1000, False, lock_pid)
                if handle:
                    kernel32.CloseHandle(handle)
                    alive = True
            else:
                try:
                    os.kill(lock_pid, 0)
                    alive = True
                except OSError:
                    alive = False
        if alive or lock_pid == 0:
            raise SystemExit(
                f"acquire lock present ({lock} pid={lock_pid} alive={alive}); "
                "refuse scrub while downloader may be running"
            )
    if not progress.exists():
        print("no progress file")
        return 0
    targets = {s.strip() for s in args.sets.split(",") if s.strip()}
    kept: list[str] = []
    removed = 0
    for line in progress.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = str(row.get("stagingKey") or "")
        status = str(row.get("status") or "")
        parts = key.split("|")
        set_code = parts[2] if len(parts) > 2 else ""
        if status == "permanent_404" and set_code in targets:
            removed += 1
            continue
        kept.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
    print(f"would_remove={removed} keep={len(kept)} sets={sorted(targets)}")
    if args.dry_run:
        return 0
    bak = progress.with_suffix(".jsonl.bak_false404")
    bak.write_text(progress.read_text(encoding="utf-8"), encoding="utf-8")
    progress.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    print(f"wrote {progress}; backup {bak}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
