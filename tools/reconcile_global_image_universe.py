#!/usr/bin/env python3
"""Reconcile global printings vs URL candidates vs EN/JA master vs staging.

Distinguishes:
- unique catalogue printings
- printings with a URL candidate (not verified coverage)
- unique missing non-EN/JA printings eligible for staging
- attempted URL outcomes (ok / fail / skip / quarantine)
- already-covered EN/JA production assets (separate identity system)
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LIVE = Path(r"D:\CardScanR_Data\cardscanr-data")
STAGING = Path(r"D:\CardScanR_Archive\image_staging\global_acq_20261013")
REPORT = ROOT / "reports" / "global_image_acquisition"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    REPORT.mkdir(parents=True, exist_ok=True)
    cards = LIVE / "data" / "global" / "catalogue" / "cards.jsonl"
    direct = LIVE / "data" / "global" / "catalogue" / "direct_images.jsonl"
    indep = LIVE / "reports" / "image_independence" / "image_independence_summary.json"

    printings_by_lang: Counter[str] = Counter()
    printing_ids: set[str] = set()
    for line in cards.open(encoding="utf-8"):
        if not line.strip():
            continue
        row = json.loads(line)
        lang = str(row.get("language") or "?")
        printings_by_lang[lang] += 1
        cid = str(row.get("canonicalPrintingId") or "")
        if cid:
            printing_ids.add(cid)

    candidate_ids: set[str] = set()
    candidates_by_lang: Counter[str] = Counter()
    candidate_hosts: Counter[str] = Counter()
    for line in direct.open(encoding="utf-8"):
        if not line.strip():
            continue
        row = json.loads(line)
        cid = str(row.get("canonicalPrintingId") or "")
        lang = str(row.get("language") or "?")
        url = str(row.get("normalizedDisplayUrl") or "")
        if cid:
            candidate_ids.add(cid)
            candidates_by_lang[lang] += 1
        if url.startswith("http"):
            host = url.split("/")[2].lower()
            candidate_hosts[host] += 1

    no_candidate_ids = printing_ids - candidate_ids
    no_candidate_by_lang: Counter[str] = Counter()
    # approximate by language from printings - candidates
    for lang, n in printings_by_lang.items():
        no_candidate_by_lang[lang] = max(0, n - candidates_by_lang.get(lang, 0))

    staging_ids: set[str] = set()
    staging_validated: Counter[str] = Counter()
    staging_quarantine = 0
    if STAGING.exists():
        for asset in STAGING.rglob("asset.json"):
            if "_quarantine" in asset.parts:
                staging_quarantine += 1
                continue
            if not (asset.parent / "display.webp").exists():
                continue
            meta = json.loads(asset.read_text(encoding="utf-8"))
            cid = str(meta.get("canonicalPrintingId") or "")
            if not cid:
                continue
            staging_ids.add(cid)
            if meta.get("validationStatus") == "quarantine_aspect":
                continue
            staging_validated[str(meta.get("language") or "?")] += 1

    progress_status: Counter[str] = Counter()
    attempted_ids: set[str] = set()
    prog = STAGING / "progress.jsonl"
    if prog.exists():
        for line in prog.open(encoding="utf-8"):
            if not line.strip():
                continue
            row = json.loads(line)
            cid = str(row.get("canonicalPrintingId") or "")
            if cid:
                attempted_ids.add(cid)
            progress_status[str(row.get("status") or "?")] += 1

    non_en_ja_candidates = {
        cid
        for cid in candidate_ids
        if not cid.startswith("pokemon|en|") and not cid.startswith("pokemon|ja|")
    }
    # Prefer language field from candidate file for accuracy
    non_en_ja_candidate_count = sum(
        n for lang, n in candidates_by_lang.items() if lang not in {"en", "ja"}
    )
    remaining_unique = non_en_ja_candidate_count - sum(staging_validated.values())

    indep_data: dict[str, Any] = {}
    if indep.exists():
        indep_data = json.loads(indep.read_text(encoding="utf-8"))

    payload = {
        "schemaVersion": "1.1.0",
        "generatedAtUtc": utc_now(),
        "definitions": {
            "printing": "unique canonicalPrintingId in global cards.jsonl",
            "urlCandidate": "row in direct_images.jsonl with a provider image URL (not verified coverage)",
            "verifiedStaging": "on-disk display.webp + asset.json under isolated staging",
            "enJaProduction": "separate EN/JP product catalogue identity; 68,011 local master assets",
            "attemptedUrl": "progress.jsonl row (ok/fail/skip/quarantine) — not equal to unique printings",
        },
        "totals": {
            "globalPrintings": len(printing_ids) or sum(printings_by_lang.values()),
            "globalPrintingsByLanguage": dict(sorted(printings_by_lang.items())),
            "urlCandidates": len(candidate_ids),
            "urlCandidatesByLanguage": dict(sorted(candidates_by_lang.items())),
            "urlCandidateHosts": dict(candidate_hosts),
            "printingsWithoutUrlCandidate": len(no_candidate_ids) or sum(no_candidate_by_lang.values()),
            "printingsWithoutUrlCandidateByLanguage": dict(sorted(no_candidate_by_lang.items())),
            "enJaProductionVerifiedFiles": {
                "en": (indep_data.get("en") or {}).get("localMaster"),
                "ja": (indep_data.get("ja") or {}).get("localMaster"),
                "total": indep_data.get("masterRows"),
            },
            "stagingUniqueValidatedByLanguage": dict(sorted(staging_validated.items())),
            "stagingUniqueValidatedTotal": sum(staging_validated.values()),
            "stagingQuarantineSidecars": staging_quarantine,
            "progressAttemptLinesByStatus": dict(progress_status),
            "uniquePrintingsAttemptedInProgress": len(attempted_ids),
            "nonEnJaUrlCandidateUniverse": non_en_ja_candidate_count,
            "remainingNonEnJaUrlCandidatesNotYetStagedApprox": max(0, remaining_unique),
        },
        "reconciliationNotes": [
            "92,817 URL candidates include EN+JA TCGdex URLs; do not subtract from 68,011 EN/JA master 1:1 (different ID systems).",
            "Unique missing for this staging programme ≈ non-EN/JA URL candidates minus validated staging files.",
            "Progress line counts can exceed unique printings (retries) or lag behind disk after path moves.",
            "24,848 printings without URL candidates require other permitted sources or permission outreach.",
        ],
    }
    out = REPORT / "universe_reconciliation.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    md = [
        "# Global image universe reconciliation",
        "",
        f"Generated: `{payload['generatedAtUtc']}`",
        "",
        "| Metric | Count |",
        "| --- | ---: |",
        f"| Global printings | {payload['totals']['globalPrintings']} |",
        f"| URL candidates (not verified) | {payload['totals']['urlCandidates']} |",
        f"| Printings with no URL candidate | {payload['totals']['printingsWithoutUrlCandidate']} |",
        f"| EN+JA production verified files | {payload['totals']['enJaProductionVerifiedFiles'].get('total')} |",
        f"| Staging unique validated (non-EN/JA programme) | {payload['totals']['stagingUniqueValidatedTotal']} |",
        f"| Remaining non-EN/JA URL candidates unstaged (approx) | {payload['totals']['remainingNonEnJaUrlCandidatesNotYetStagedApprox']} |",
        f"| Progress attempt lines | {sum(progress_status.values())} |",
        f"| Unique printings seen in progress | {len(attempted_ids)} |",
        "",
        "URL candidates ≠ verified coverage. MIT metadata ≠ artwork rights.",
        "",
    ]
    (REPORT / "universe_reconciliation.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(payload["totals"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
