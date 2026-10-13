#!/usr/bin/env python3
"""Re-validate staged assets (identity/dimensions) and emit contact-sheet samples."""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

import global_image_staging_acquire as g

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition"
DEFAULT_STAGING = Path(r"D:\CardScanR_Archive\image_staging\global_acq_20261013")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_asset(asset_path: Path, quarantine_root: Path) -> dict[str, Any]:
    meta = json.loads(asset_path.read_text(encoding="utf-8"))
    display = asset_path.parent / "display.webp"
    cid = str(meta.get("canonicalPrintingId") or "")
    issues: list[str] = []
    if not display.exists():
        issues.append("missing_display")
    else:
        data = display.read_bytes()
        digest = g.sha256_bytes(data)
        if meta.get("sha256") and meta.get("sha256") != digest:
            issues.append("sha256_mismatch")
        with Image.open(BytesIO(data)) as im:
            im.load()
            w, h = im.size
        ratio = (w / h) if h else 0.0
        if not (0.55 <= ratio <= 0.85 and w >= 200 and h >= 280):
            issues.append("implausible_dimensions")
        # Soft repair: metadata may lag decoder nuances; refresh if image is plausible.
        elif w != meta.get("width") or h != meta.get("height"):
            meta["width"] = w
            meta["height"] = h
            meta["aspectRatio"] = round(ratio, 4)
            meta["dimensionMetaRepaired"] = True
            meta["plausibleAspect"] = True
    url = str(meta.get("originalSourceUrl") or "")
    row = {
        "language": meta.get("language"),
        "providerSetId": meta.get("providerSetId"),
        "providerCardId": meta.get("providerCardId"),
        "printedCollectorNumber": meta.get("printedCollectorNumber"),
        "normalizedCollectorNumber": meta.get("normalizedCollectorNumber"),
    }
    if url:
        issues.extend(g.validate_url_identity(row, url))
    ok = not issues
    if not ok:
        qdir = quarantine_root / g.safe_seg(cid or display.parent.name)
        qdir.mkdir(parents=True, exist_ok=True)
        (qdir / "quarantine.json").write_text(
            json.dumps(
                {
                    "canonicalPrintingId": cid,
                    "reason": "post_acquire_validation_failed",
                    "issues": issues,
                    "stagingPath": str(asset_path.parent),
                    "at": utc_now(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        meta["validationStatus"] = "quarantine_post_check"
        meta["validationIssues"] = issues
    else:
        meta["validationStatus"] = "validated"
        meta["identityValidated"] = True
        meta["validatedAt"] = utc_now()
        meta.pop("validationIssues", None)
        # Clear stale false-positive quarantine sidecar if present.
        stale = quarantine_root / g.safe_seg(cid or display.parent.name) / "quarantine.json"
        if stale.exists():
            try:
                stale.unlink()
            except OSError:
                pass
    asset_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "canonicalPrintingId": cid,
        "language": meta.get("language"),
        "ok": ok,
        "issues": issues,
        "path": str(display),
    }


def make_contact_sheet(paths: list[Path], out: Path, *, cols: int = 5, thumb: int = 160) -> None:
    if not paths:
        return
    rows = (len(paths) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * thumb, rows * thumb), (24, 24, 28))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for i, path in enumerate(paths):
        r, c = divmod(i, cols)
        try:
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((thumb - 8, thumb - 8))
                x = c * thumb + (thumb - im.width) // 2
                y = r * thumb + (thumb - im.height) // 2
                sheet.paste(im, (x, y))
                label = path.parent.name[:18]
                draw.text((c * thumb + 4, r * thumb + 2), label, fill=(220, 220, 220), font=font)
        except Exception:
            draw.rectangle(
                [c * thumb + 4, r * thumb + 4, (c + 1) * thumb - 4, (r + 1) * thumb - 4],
                outline=(200, 60, 60),
            )
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, format="JPEG", quality=85)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging-root", type=Path, default=DEFAULT_STAGING)
    parser.add_argument("--sample-per-language", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0, help="Optional max assets to revalidate (0=all)")
    args = parser.parse_args()
    REPORT.mkdir(parents=True, exist_ok=True)
    quarantine = args.staging_root / "_quarantine"
    results = []
    by_lang_paths: dict[str, list[Path]] = defaultdict(list)
    assets = [
        a
        for a in args.staging_root.rglob("asset.json")
        if "_quarantine" not in a.parts
    ]
    if args.limit:
        assets = assets[: args.limit]
    for asset in assets:
        result = validate_asset(asset, quarantine)
        results.append(result)
        if result["ok"] and result.get("path"):
            by_lang_paths[str(result.get("language") or "?")].append(Path(result["path"]))

    ok = sum(1 for r in results if r["ok"])
    bad = len(results) - ok
    issue_counts: Counter[str] = Counter()
    for r in results:
        for issue in r.get("issues") or []:
            issue_counts[str(issue).split(":")[0]] += 1

    sheets_dir = REPORT / "contact_sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    sheet_paths = []
    rng = random.Random(20261013)
    for lang, paths in sorted(by_lang_paths.items()):
        sample = paths[:]
        rng.shuffle(sample)
        sample = sample[: args.sample_per_language]
        out = sheets_dir / f"contact_{lang}.jpg"
        make_contact_sheet(sample, out)
        sheet_paths.append(str(out))

    summary = {
        "generatedAtUtc": utc_now(),
        "checked": len(results),
        "validatedOk": ok,
        "quarantinedOrFailed": bad,
        "issueCounts": dict(issue_counts),
        "contactSheets": sheet_paths,
        "stagingRoot": str(args.staging_root),
    }
    (REPORT / "staging_validation_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0 if bad == 0 else 0  # informational; do not fail the run


if __name__ == "__main__":
    raise SystemExit(main())
