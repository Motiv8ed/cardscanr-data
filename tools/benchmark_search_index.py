#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.benchmark import benchmark_search_index, write_benchmark_report
from cardscanr_search_index.constants import SEARCH_OUTPUT_DIR


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark catalogue search index")
    parser.add_argument("--output-dir", default=str(SEARCH_OUTPUT_DIR))
    parser.add_argument("--report", default=str(ROOT / "reports" / "runtime" / "catalog_search_index_benchmark.json"))
    args = parser.parse_args()
    result = benchmark_search_index(output_dir=Path(args.output_dir))
    path = write_benchmark_report(result, Path(args.report))
    print(json.dumps(asdict(result), indent=2))
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
