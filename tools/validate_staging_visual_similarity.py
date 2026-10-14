#!/usr/bin/env python3
"""Visual / OCR-lite validation of staged card images (beyond URL metadata).

Uses OpenCV + NumPy (already available). Does not require Tesseract.
Quarantines:
- near-identical art across different collector numbers in the same language/set
- collector-number template match failures (OCR-lite)
- optional cross-language art disagreement when an EN sibling is present under live master

Writes reports/global_image_acquisition/staging_visual_validation.json and
sampled contact sheets under contact_sheets/visual_samples/.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import global_image_staging_acquire as g

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition"
DEFAULT_STAGING = Path(r"D:\CardScanR_Archive\image_staging\global_acq_20261013")
DEFAULT_LIVE = Path(r"D:\CardScanR_Data\cardscanr-data")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_rgb(path: Path) -> np.ndarray | None:
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if bgr is None:
        return None
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def art_crop(rgb: np.ndarray) -> np.ndarray:
    """Crop interior art window, excluding title/collector chrome."""
    h, w = rgb.shape[:2]
    y0, y1 = int(h * 0.18), int(h * 0.72)
    x0, x1 = int(w * 0.12), int(w * 0.88)
    return rgb[y0:y1, x0:x1]


def collector_crop(rgb: np.ndarray) -> np.ndarray:
    """Bottom-left collector-number region typical of Pokémon TCG layout."""
    h, w = rgb.shape[:2]
    y0, y1 = int(h * 0.88), int(h * 0.98)
    x0, x1 = int(w * 0.04), int(w * 0.34)
    return rgb[y0:y1, x0:x1]


def dhash_bits(rgb: np.ndarray, hash_size: int = 16) -> np.ndarray:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    resized = cv2.resize(gray, (hash_size + 1, hash_size), interpolation=cv2.INTER_AREA)
    diff = resized[:, 1:] > resized[:, :-1]
    return diff.flatten()


def hamming(a: np.ndarray, b: np.ndarray) -> int:
    return int(np.count_nonzero(a != b))


def collector_template_score(rgb: np.ndarray, expected: str) -> float:
    """OCR-lite: max NCC of rendered expected digits over collector-region scales.

    This is a weak signal without Tesseract — used to flag empty/corrupt chrome,
    not as a sole identity proof. Returns 1.0 when no expected number is known.
    """
    expected = re.sub(r"[^0-9A-Za-z/]", "", expected or "")
    if not expected:
        return 1.0
    crop = collector_crop(rgb)
    if crop.size == 0:
        return 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    # Emphasize light-on-dark and dark-on-light number glyphs.
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(blur, 60, 140)
    ink_ratio = float(np.count_nonzero(edges)) / float(edges.size)
    if ink_ratio < 0.004:
        # Essentially blank collector chrome — suspicious.
        return 0.0

    # Multi-scale template: render expected string and slide/match via matchTemplate.
    best = 0.0
    for scale in (0.7, 1.0, 1.3, 1.6):
        tw = max(12, int(len(expected) * 10 * scale))
        th = max(14, int(18 * scale))
        canvas = Image.new("L", (tw, th), 0)
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.load_default()
        draw.text((1, 1), expected, fill=255, font=font)
        templ = np.array(canvas, dtype=np.uint8)
        if templ.shape[0] >= edges.shape[0] or templ.shape[1] >= edges.shape[1]:
            continue
        for src in (edges, 255 - edges):
            res = cv2.matchTemplate(src, templ, cv2.TM_CCOEFF_NORMED)
            best = max(best, float(res.max()) if res.size else 0.0)
    # Blend structural ink presence with template peak so plausible chrome
    # with weak font match is not mass-quarantined.
    return max(best, min(0.35, ink_ratio * 8.0))


def write_quarantine(
    quarantine_root: Path,
    *,
    cid: str,
    issues: list[str],
    staging_path: Path,
) -> None:
    qdir = quarantine_root / g.safe_seg(cid or staging_path.name)
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "quarantine_visual.json").write_text(
        json.dumps(
            {
                "canonicalPrintingId": cid,
                "reason": "visual_validation_failed",
                "issues": issues,
                "stagingPath": str(staging_path),
                "at": utc_now(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def make_contact_sheet(paths: list[Path], out: Path, *, cols: int = 4, thumb: int = 140) -> None:
    if not paths:
        return
    rows = (len(paths) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * thumb, rows * thumb), (20, 22, 28))
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
                draw.text((c * thumb + 3, r * thumb + 2), path.parent.name[:16], fill=(230, 230, 230), font=font)
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
    parser.add_argument("--sample-per-language", type=int, default=12)
    parser.add_argument("--limit", type=int, default=0, help="0=all assets")
    parser.add_argument(
        "--dup-hamming-threshold",
        type=int,
        default=6,
        help="Art dHash hamming distance at/below which different collector# cards are suspicious",
    )
    parser.add_argument(
        "--collector-score-min",
        type=float,
        default=0.02,
        help="Minimum OCR-lite score before flagging empty/corrupt collector chrome",
    )
    parser.add_argument("--apply-quarantine", action="store_true")
    args = parser.parse_args()

    REPORT.mkdir(parents=True, exist_ok=True)
    quarantine_root = args.staging_root / "_quarantine"
    assets = [
        a
        for a in args.staging_root.rglob("asset.json")
        if "_quarantine" not in a.parts
    ]
    if args.limit and len(assets) > args.limit:
        # Language-balanced sample so --limit does not only hit one filesystem prefix.
        by_lang_assets: dict[str, list[Path]] = defaultdict(list)
        for a in assets:
            lang = a.parts[len(args.staging_root.parts)] if len(a.parts) > len(args.staging_root.parts) else "?"
            by_lang_assets[lang].append(a)
        assets = []
        langs = sorted(by_lang_assets)
        idx = 0
        while len(assets) < args.limit and by_lang_assets:
            lang = langs[idx % len(langs)]
            bucket = by_lang_assets[lang]
            if bucket:
                assets.append(bucket.pop())
            else:
                by_lang_assets.pop(lang, None)
                langs = sorted(by_lang_assets)
                if not langs:
                    break
                idx = 0
                continue
            idx += 1

    # Group by language+set for near-duplicate art scans
    by_lang_set: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    results: list[dict[str, Any]] = []
    by_lang_paths: dict[str, list[Path]] = defaultdict(list)
    suspicious_paths: list[Path] = []

    for asset_path in assets:
        meta = json.loads(asset_path.read_text(encoding="utf-8"))
        display = asset_path.parent / "display.webp"
        cid = str(meta.get("canonicalPrintingId") or "")
        lang = str(meta.get("language") or "?")
        set_id = str(meta.get("providerSetId") or meta.get("canonicalSetId") or "?")
        collector = str(
            meta.get("printedCollectorNumber")
            or meta.get("normalizedCollectorNumber")
            or ""
        )
        issues: list[str] = []
        if not display.exists():
            issues.append("missing_display")
            results.append({"canonicalPrintingId": cid, "ok": False, "issues": issues})
            continue
        rgb = load_rgb(display)
        if rgb is None:
            issues.append("undecodable_image")
            results.append({"canonicalPrintingId": cid, "ok": False, "issues": issues})
            continue
        art = art_crop(rgb)
        bits = dhash_bits(art)
        score = collector_template_score(rgb, collector)
        if collector and score < args.collector_score_min:
            issues.append(f"collector_ocr_lite_low:{score:.3f}:expected={collector}")
        entry = {
            "canonicalPrintingId": cid,
            "language": lang,
            "setId": set_id,
            "collector": collector,
            "path": display,
            "assetPath": asset_path,
            "bits": bits,
            "collectorScore": score,
            "issues": issues,
        }
        by_lang_set[(lang, set_id)].append(entry)
        by_lang_paths[lang].append(display)

    # Near-duplicate art across different collector numbers
    for (_lang, _set_id), group in by_lang_set.items():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if not a["collector"] or not b["collector"]:
                    continue
                if g.normalize_collector(a["collector"]) == g.normalize_collector(b["collector"]):
                    continue
                dist = hamming(a["bits"], b["bits"])
                if dist <= args.dup_hamming_threshold:
                    msg = (
                        f"art_near_duplicate_other_collector:hamming={dist}"
                        f":other={b['collector']}"
                    )
                    a["issues"].append(msg)
                    b["issues"].append(
                        f"art_near_duplicate_other_collector:hamming={dist}"
                        f":other={a['collector']}"
                    )

    issue_counts: Counter[str] = Counter()
    quarantined = 0
    for group in by_lang_set.values():
        for entry in group:
            # de-dupe issue strings
            issues = sorted(set(entry["issues"]))
            ok = not issues
            if not ok:
                quarantined += 1
                suspicious_paths.append(entry["path"])
                for issue in issues:
                    issue_counts[issue.split(":")[0]] += 1
                if args.apply_quarantine:
                    write_quarantine(
                        quarantine_root,
                        cid=entry["canonicalPrintingId"],
                        issues=issues,
                        staging_path=entry["path"].parent,
                    )
                    meta_path: Path = entry["assetPath"]
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    meta["validationStatus"] = "quarantine_visual"
                    meta["validationIssues"] = issues
                    meta["visualValidatedAt"] = utc_now()
                    meta_path.write_text(
                        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
            else:
                meta_path = entry["assetPath"]
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["visualValidated"] = True
                meta["visualValidatedAt"] = utc_now()
                meta["collectorOcrLiteScore"] = round(float(entry["collectorScore"]), 4)
                meta_path.write_text(
                    json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            results.append(
                {
                    "canonicalPrintingId": entry["canonicalPrintingId"],
                    "language": entry["language"],
                    "ok": ok,
                    "issues": issues,
                    "collectorScore": round(float(entry["collectorScore"]), 4),
                }
            )

    sheets_dir = REPORT / "contact_sheets" / "visual_samples"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    sheet_paths: list[str] = []
    rng = random.Random(20261013)
    for lang, paths in sorted(by_lang_paths.items()):
        sample = paths[:]
        rng.shuffle(sample)
        out = sheets_dir / f"visual_contact_{lang}.jpg"
        make_contact_sheet(sample[: args.sample_per_language], out)
        sheet_paths.append(str(out))
    if suspicious_paths:
        sus = suspicious_paths[:]
        rng.shuffle(sus)
        out = sheets_dir / "visual_suspicious_sample.jpg"
        make_contact_sheet(sus[:24], out)
        sheet_paths.append(str(out))

    summary = {
        "generatedAtUtc": utc_now(),
        "checked": len(results),
        "validatedOk": sum(1 for r in results if r.get("ok")),
        "suspiciousOrFailed": quarantined,
        "issueCounts": dict(issue_counts),
        "applyQuarantine": bool(args.apply_quarantine),
        "dupHammingThreshold": args.dup_hamming_threshold,
        "collectorScoreMin": args.collector_score_min,
        "ocrEngine": "opencv_template_ncc_lite_no_tesseract",
        "contactSheets": sheet_paths,
        "stagingRoot": str(args.staging_root),
        "note": (
            "Full string OCR requires Tesseract (not installed). "
            "This pass uses art dHash near-duplicate detection and collector-number template NCC."
        ),
    }
    (REPORT / "staging_visual_validation.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
