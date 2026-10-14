#!/usr/bin/env python3
"""Cold-open + search latency/memory bench: RC 14923 pack vs baseline build98 zh-cn."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parent.parent
RC = (
    ROOT
    / "reports"
    / "global_image_acquisition"
    / "pokecardex"
    / "release_candidate"
    / "pack_install_layout"
    / "zh-cn"
    / "zh-cn.sqlite"
)
BASELINE = Path(r"D:\CardScanR\artifacts\build98_zhcn\zh-cn.sqlite")
OUT = (
    ROOT
    / "reports"
    / "global_image_acquisition"
    / "pokecardex"
    / "release_candidate"
    / "PERF_BENCH.json"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def cold_open_ms(path: Path, repeats: int = 5) -> dict:
    times: list[float] = []
    counts: list[int] = []
    for _ in range(repeats):
        # Drop OS cache is not reliable on Windows without admin; measure reopen cost.
        t0 = time.perf_counter()
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        n = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        con.execute("SELECT COUNT(*) FROM sets").fetchone()
        con.close()
        times.append((time.perf_counter() - t0) * 1000)
        counts.append(n)
    return {
        "repeats": repeats,
        "cardCount": counts[0],
        "coldOpenMsMean": round(mean(times), 2),
        "coldOpenMsMedian": round(median(times), 2),
        "coldOpenMsMin": round(min(times), 2),
        "coldOpenMsMax": round(max(times), 2),
    }


def search_latency_ms(path: Path, language: str, repeats: int = 20) -> dict:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    sample = con.execute(
        "SELECT set_code, printed_collector_number, native_card_name FROM cards "
        "WHERE set_code IS NOT NULL AND printed_collector_number != '' LIMIT 1"
    ).fetchone()
    set_code, printed, name = sample
    local = printed.split("/")[0]
    name_token = (name or "a").split()[0]

    def timed(fn, n=repeats):
        xs = []
        for _ in range(n):
            t0 = time.perf_counter()
            fn()
            xs.append((time.perf_counter() - t0) * 1000)
        return {"meanMs": round(mean(xs), 3), "medianMs": round(median(xs), 3), "p95Ms": round(sorted(xs)[int(0.95 * (n - 1))], 3)}

    by_collector = timed(
        lambda: con.execute(
            "SELECT COUNT(*) FROM cards WHERE language=? AND ("
            "printed_collector_number=? OR normalized_collector_number=?)",
            (language, printed, local),
        ).fetchone()
    )
    by_set = timed(
        lambda: con.execute(
            "SELECT COUNT(*) FROM cards WHERE language=? AND lower(set_code)=? "
            "AND (printed_collector_number=? OR normalized_collector_number=?)",
            (language, str(set_code).lower(), printed, local),
        ).fetchone()
    )
    # FTS may be unavailable naming; try MATCH on name column via cards_fts
    try:
        by_name = timed(
            lambda: con.execute(
                "SELECT COUNT(*) FROM cards_fts WHERE native_card_name MATCH ?",
                (name_token,),
            ).fetchone()
        )
    except sqlite3.Error as exc:
        by_name = {"error": str(exc)}
    con.close()
    return {
        "sample": {"set_code": set_code, "printed": printed, "name": name},
        "collectorLookup": by_collector,
        "setPlusCollector": by_set,
        "nameFts": by_name,
    }


def memory_probe(path: Path) -> dict:
    tracemalloc.start()
    t0 = time.perf_counter()
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    # Simulate binder-ish scan: page through 500 rows + distinct sets
    rows = con.execute(
        "SELECT canonical_printing_id, printed_collector_number, set_code FROM cards LIMIT 500"
    ).fetchall()
    sets = con.execute("SELECT set_id, set_name FROM sets").fetchall()
    con.close()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "rowsSampled": len(rows),
        "setsLoaded": len(sets),
        "elapsedMs": round((time.perf_counter() - t0) * 1000, 2),
        "tracemallocCurrentKb": round(current / 1024, 1),
        "tracemallocPeakKb": round(peak / 1024, 1),
        "sqliteFileMb": round(path.stat().st_size / (1024 * 1024), 2),
        "note": "Python process tracemalloc; not Flutter RSS. Useful relative RC vs baseline.",
    }


def bench_one(label: str, path: Path, language: str) -> dict:
    if not path.exists():
        return {"label": label, "missing": True, "path": str(path)}
    return {
        "label": label,
        "path": str(path),
        "bytes": path.stat().st_size,
        "coldOpen": cold_open_ms(path),
        "search": search_latency_ms(path, language),
        "memory": memory_probe(path),
    }


def main() -> int:
    report = {
        "generatedAtUtc": utc_now(),
        "host": os.environ.get("COMPUTERNAME", ""),
        "rc": bench_one("rc_14923", RC, "zh-cn"),
        "baseline_build98": bench_one("baseline_build98", BASELINE, "zh-cn"),
    }
    # Relative deltas when both present
    if not report["rc"].get("missing") and not report["baseline_build98"].get("missing"):
        rc_open = report["rc"]["coldOpen"]["coldOpenMsMedian"]
        base_open = report["baseline_build98"]["coldOpen"]["coldOpenMsMedian"]
        rc_mem = report["rc"]["memory"]["tracemallocPeakKb"]
        base_mem = report["baseline_build98"]["memory"]["tracemallocPeakKb"]
        report["deltas"] = {
            "coldOpenMedianMs_rc_minus_baseline": round(rc_open - base_open, 2),
            "fileMb_rc_minus_baseline": round(
                report["rc"]["memory"]["sqliteFileMb"] - report["baseline_build98"]["memory"]["sqliteFileMb"],
                2,
            ),
            "tracemallocPeakKb_rc_minus_baseline": round(rc_mem - base_mem, 1),
            "cardCount_rc": report["rc"]["coldOpen"]["cardCount"],
            "cardCount_baseline": report["baseline_build98"]["coldOpen"]["cardCount"],
        }
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report.get("deltas") or report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
