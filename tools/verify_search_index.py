#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.constants import SEARCH_OUTPUT_DIR
from cardscanr_search_index.verify import verify_search_index


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify catalogue search index")
    parser.add_argument("--output-dir", default=str(SEARCH_OUTPUT_DIR))
    args = parser.parse_args()
    result = verify_search_index(output_dir=Path(args.output_dir))
    print(json.dumps(result.__dict__, indent=2))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
