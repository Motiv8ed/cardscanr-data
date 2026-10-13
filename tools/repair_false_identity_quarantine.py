#!/usr/bin/env python3
"""Clear false identity quarantines after hyphenated set-id parsing fix.

Reads `_quarantine/*/quarantine.json` (which retain URLs), re-validates with
current identity rules, removes clearable sidecars, and strips matching
`status=quarantine` lines from progress JSONL so rows can be re-acquired.
Does not delete successfully staged displays.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import global_image_staging_acquire as g

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition"
DEFAULT_STAGING = Path(r"D:\CardScanR_Archive\image_staging\global_acq_20261013")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reconstruct_row(q: dict) -> dict | None:
    url = str(q.get("url") or "")
    parsed = g.parse_tcgdex_asset_url(url) if url else None
    issues = list(q.get("issues") or [])
    provider_card = ""
    for issue in issues:
        if issue.startswith("provider_card_set_mismatch:") or issue.startswith(
            "provider_card_num_mismatch:"
        ):
            provider_card = issue.split(":", 1)[1]
            break
    if not parsed:
        return None
    lang = str(q.get("language") or parsed["urlLanguage"])
    # Prefer language from canonical id when present: pokemon|de|...
    cid = str(q.get("canonicalPrintingId") or "")
    parts = cid.split("|")
    if len(parts) >= 2 and parts[1]:
        lang = parts[1]
    return {
        "language": lang,
        "providerSetId": parsed["urlSetId"],
        "providerCardId": provider_card or f"{parsed['urlSetId']}-{parsed['urlLocalId']}",
        "printedCollectorNumber": parsed["urlLocalId"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging-root", type=Path, default=DEFAULT_STAGING)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    qroot = args.staging_root / "_quarantine"
    clearable: dict[str, dict] = {}
    scanned_q = 0
    if qroot.exists():
        for qp in qroot.rglob("quarantine.json"):
            scanned_q += 1
            try:
                q = json.loads(qp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if q.get("reason") not in {"identity_mismatch", "post_acquire_validation_failed"}:
                continue
            url = str(q.get("url") or "")
            row = reconstruct_row(q)
            if not row or not url:
                continue
            if g.validate_url_identity(row, url):
                continue
            cid = str(q.get("canonicalPrintingId") or "")
            if cid:
                clearable[cid] = {"path": qp, "url": url, "issues": q.get("issues")}

    progress_paths = [
        args.staging_root / "progress.jsonl",
        REPORT / "progress.jsonl",
    ]
    removed_progress = 0
    removed_sidecars = 0

    if args.execute and clearable:
        for cid, info in clearable.items():
            qp: Path = info["path"]
            if qp.exists():
                qp.unlink()
                removed_sidecars += 1
            # orphan empty dirs left alone

        for path in progress_paths:
            if not path.exists():
                continue
            kept: list[str] = []
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        kept.append(line if line.endswith("\n") else line + "\n")
                        continue
                    cid = str(row.get("canonicalPrintingId") or "")
                    if row.get("status") == "quarantine" and cid in clearable:
                        removed_progress += 1
                        continue
                    kept.append(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            bak = path.with_suffix(path.suffix + f".bak_{utc_now().replace(':', '')}")
            path.replace(bak)
            path.write_text("".join(kept), encoding="utf-8", newline="\n")

    summary = {
        "generatedAtUtc": utc_now(),
        "scannedQuarantineSidecars": scanned_q,
        "clearableFalseQuarantines": len(clearable),
        "sampleCleared": [
            {"canonicalPrintingId": cid, "issues": info.get("issues")}
            for cid, info in list(clearable.items())[:10]
        ],
        "removedProgressLines": removed_progress,
        "removedSidecars": removed_sidecars,
        "executed": bool(args.execute),
    }
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "false_quarantine_repair.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
