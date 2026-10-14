#!/usr/bin/env python3
"""Visual / OCR-lite validation for isolated PokéCardex zh-Hans staging.

Layers (kept distinct):
- metadata coverage (jsonl rows)
- acquired images (display.webp present)
- visually verified (this tool pass)
- production-cleared (never set here; Pokémon IP pending)

Quarantines uncertain identities under staging/_quarantine/.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import validate_staging_visual_similarity as vis

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition" / "pokecardex"
DEFAULT_STAGING = Path(r"D:\CardScanR_Archive\image_staging\pokecardex_zh_hans_20261014")
META = REPORT / "zh_hans_cards.jsonl"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iter_assets(staging: Path) -> list[Path]:
    return sorted(staging.glob("zh-Hans/*/*/asset.json"))


def build_contact_sheet(paths: list[Path], out: Path, cols: int = 6, cell: int = 140) -> None:
    if not paths:
        return
    rows = (len(paths) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * cell), (20, 22, 28))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None
    for i, p in enumerate(paths):
        display = p.parent / "display.webp"
        if not display.exists():
            continue
        with Image.open(display) as im:
            im = im.convert("RGB")
            im.thumbnail((cell - 8, cell - 24))
            x = (i % cols) * cell + (cell - im.width) // 2
            y = (i // cols) * cell + 4
            sheet.paste(im, (x, y))
            label = p.parent.name
            if font:
                draw.text((x, y + im.height + 2), label[:18], fill=(220, 220, 220), font=font)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "JPEG", quality=85)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging-root", type=Path, default=DEFAULT_STAGING)
    parser.add_argument("--sample-limit", type=int, default=0, help="0=all acquired assets")
    parser.add_argument("--seed", type=int, default=13)
    args = parser.parse_args()

    staging = args.staging_root
    quarantine_root = staging / "_quarantine"
    assets = iter_assets(staging)
    if args.sample_limit and len(assets) > args.sample_limit:
        rng = random.Random(args.seed)
        assets = rng.sample(assets, args.sample_limit)

    meta_std = 0
    if META.exists():
        with META.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if int(row.get("providerVersionId") or 1) in {0, 1}:
                    meta_std += 1

    status = Counter()
    issue_counts = Counter()
    by_set: dict[str, Counter] = defaultdict(Counter)
    hashes: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    verified_paths: list[Path] = []
    quarantine_paths: list[Path] = []

    for asset_path in assets:
        raw = asset_path.read_text(encoding="utf-8").strip()
        if not raw:
            status["empty_asset_json"] += 1
            issue_counts["empty_asset_json"] += 1
            quarantine_paths.append(asset_path)
            qdir = quarantine_root / asset_path.parent.parent.name / asset_path.parent.name
            qdir.mkdir(parents=True, exist_ok=True)
            (qdir / "quarantine.json").write_text(
                json.dumps(
                    {"reason": "empty_asset_json", "path": str(asset_path.parent), "at": utc_now()},
                    indent=2,
                ),
                encoding="utf-8",
            )
            continue
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError:
            status["corrupt_asset_json"] += 1
            issue_counts["corrupt_asset_json"] += 1
            continue
        display = asset_path.parent / "display.webp"
        set_id = str(meta.get("providerSetId") or asset_path.parent.parent.name)
        num = str(meta.get("normalizedCollectorNumber") or "")
        issues: list[str] = []
        if not display.exists():
            issues.append("missing_display")
        else:
            rgb = vis.load_rgb(display)
            if rgb is None:
                issues.append("undecodable")
            else:
                h, w = rgb.shape[:2]
                ratio = (w / h) if h else 0.0
                if not (0.55 <= ratio <= 0.85 and w >= 180 and h >= 250):
                    issues.append("implausible_dimensions")
                # OCR-lite collector template
                if num and num.isdigit():
                    score = vis.collector_template_score(rgb, num)
                    meta["collectorOcrLiteScore"] = round(float(score), 4)
                    if score < 0.12:
                        issues.append("collector_ocr_lite_weak")
                art = vis.art_crop(rgb)
                bits = vis.dhash_bits(art)
                pack = "".join("1" if b else "0" for b in bits.tolist())
                hashes[set_id].append((pack, asset_path))
                meta["width"] = w
                meta["height"] = h
                meta["aspectRatio"] = round(ratio, 4)

        if not issues:
            meta["validationStatus"] = "visually_verified"
            meta["visuallyVerifiedAt"] = utc_now()
            meta["productionPromotionAllowed"] = False
            status["visually_verified"] += 1
            by_set[set_id]["visually_verified"] += 1
            verified_paths.append(asset_path)
        else:
            meta["validationStatus"] = "quarantine_uncertain"
            meta["validationIssues"] = issues
            meta["productionPromotionAllowed"] = False
            status["quarantine_uncertain"] += 1
            by_set[set_id]["quarantine_uncertain"] += 1
            for i in issues:
                issue_counts[i] += 1
            qdir = quarantine_root / set_id / asset_path.parent.name
            qdir.mkdir(parents=True, exist_ok=True)
            (qdir / "quarantine.json").write_text(
                json.dumps(
                    {
                        "stagingKey": meta.get("stagingKey"),
                        "issues": issues,
                        "path": str(asset_path.parent),
                        "at": utc_now(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            quarantine_paths.append(asset_path)
        asset_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # Near-duplicate art across different collector numbers in same set
    dup_pairs = 0
    for set_id, items in hashes.items():
        for i in range(len(items)):
            for j in range(i + 1, min(i + 40, len(items))):
                a_bits = np.array([c == "1" for c in items[i][0]])
                b_bits = np.array([c == "1" for c in items[j][0]])
                if vis.hamming(a_bits, b_bits) <= 8:
                    # same art hash — check collector differs
                    ai = json.loads(items[i][1].read_text(encoding="utf-8"))
                    aj = json.loads(items[j][1].read_text(encoding="utf-8"))
                    if ai.get("normalizedCollectorNumber") != aj.get("normalizedCollectorNumber"):
                        dup_pairs += 1
                        for ap in (items[i][1], items[j][1]):
                            m = json.loads(ap.read_text(encoding="utf-8"))
                            issues = list(m.get("validationIssues") or [])
                            if "near_duplicate_art_diff_collector" not in issues:
                                issues.append("near_duplicate_art_diff_collector")
                                m["validationIssues"] = issues
                                m["validationStatus"] = "quarantine_uncertain"
                                m["productionPromotionAllowed"] = False
                                ap.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
                                issue_counts["near_duplicate_art_diff_collector"] += 1
                                if m.get("stagingKey") and status["visually_verified"] > 0:
                                    status["visually_verified"] -= 1
                                    status["quarantine_uncertain"] += 1
                                    set_code = str(m.get("providerSetId") or set_id)
                                    if by_set[set_code]["visually_verified"] > 0:
                                        by_set[set_code]["visually_verified"] -= 1
                                    by_set[set_code]["quarantine_uncertain"] += 1
                                if ap not in quarantine_paths:
                                    quarantine_paths.append(ap)
                                if ap in verified_paths:
                                    verified_paths.remove(ap)

    sheets_dir = REPORT / "contact_sheets"
    rng = random.Random(args.seed)
    build_contact_sheet(
        rng.sample(verified_paths, min(24, len(verified_paths))) if verified_paths else [],
        sheets_dir / "pokecardex_verified_sample.jpg",
    )
    build_contact_sheet(
        rng.sample(quarantine_paths, min(24, len(quarantine_paths))) if quarantine_paths else [],
        sheets_dir / "pokecardex_quarantine_sample.jpg",
    )

    acquired = sum(1 for _ in staging.rglob("display.webp"))
    tcgdex_staging = Path(r"D:\CardScanR_Archive\image_staging\global_acq_20261013")
    tcgdex_count = 0
    if tcgdex_staging.exists():
        for lang in tcgdex_staging.iterdir():
            if lang.is_dir() and not lang.name.startswith("_"):
                tcgdex_count += sum(1 for _ in lang.rglob("display.webp"))

    payload = {
        "generatedAtUtc": utc_now(),
        "layers": {
            "metadataStandardRows": meta_std,
            "acquiredImages": acquired,
            "visuallyVerified": status.get("visually_verified", 0),
            "quarantineUncertain": status.get("quarantine_uncertain", 0),
            "productionCleared": 0,
            "note": (
                "productionCleared remains 0 until separate Pokémon/IP rights review. "
                "Do not collapse these layers into one coverage percentage."
            ),
        },
        "assetsScanned": len(assets),
        "statusCounts": dict(status),
        "issueCounts": dict(issue_counts),
        "nearDuplicateArtDiffCollectorPairs": dup_pairs,
        "bySet": {k: dict(v) for k, v in sorted(by_set.items())},
        "contactSheets": {
            "verified": str(sheets_dir / "pokecardex_verified_sample.jpg"),
            "quarantine": str(sheets_dir / "pokecardex_quarantine_sample.jpg"),
        },
        "preservedTcgdexStagingDisplayWebp": tcgdex_count,
        "quarantineRoot": str(quarantine_root),
    }
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "zh_hans_visual_validation.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    md = [
        "# PokéCardex zh-Hans visual validation",
        "",
        f"Generated: {payload['generatedAtUtc']}",
        "",
        "## Layers",
        "",
        f"- Metadata (standard): **{meta_std}**",
        f"- Acquired images: **{acquired}**",
        f"- Visually verified: **{payload['layers']['visuallyVerified']}**",
        f"- Quarantine (uncertain): **{payload['layers']['quarantineUncertain']}**",
        f"- Production-cleared: **0** (Pokémon IP pending)",
        "",
        f"Issue counts: `{dict(issue_counts)}`",
        f"Near-duplicate art pairs (diff collector): **{dup_pairs}**",
        f"Preserved TCGdex staging: **{tcgdex_count}**",
        "",
    ]
    (REPORT / "ZH_HANS_VISUAL_VALIDATION.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False)[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
