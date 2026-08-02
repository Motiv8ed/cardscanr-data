from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.global_builder import build_global_search_index, verify_global_search_index


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-dir", type=Path, default=ROOT / "data" / "global" / "catalogue")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "reports" / "global_rollout" / "artifacts" / "global_catalogue_canary_v2.sqlite")
    args = parser.parse_args()
    output = args.output
    bundle = args.bundle_dir
    kwargs = {"cards_path": bundle / "cards.jsonl", "direct_images_path": bundle / "direct_images.jsonl",
              "products_path": bundle / "products.jsonl", "product_contents_path": bundle / "product_contents.jsonl",
              "direct_product_images_path": bundle / "direct_product_images.jsonl", "output_path": output}
    first = build_global_search_index(**kwargs)
    first_bytes = output.read_bytes()
    second = build_global_search_index(**kwargs)
    deterministic = first["sha256"] == second["sha256"] and first_bytes == output.read_bytes()
    verification = verify_global_search_index(output)
    report = {
        "schemaVersion": "2.0.0",
        "classification": "PASS" if deterministic and verification["classification"] == "PASS" else "FAIL",
        **second,
        "deterministicRebuild": deterministic,
        "verification": verification,
        "productionManifestReplaced": False,
        "productionIndexPreserved": True,
        "rollbackManifest": {"action": "delete_non_production_canary_only", "productionChangeRequired": False},
    }
    report_path = ROOT / "reports" / "global_rollout" / "global_search_index.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["classification"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
