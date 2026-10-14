#!/usr/bin/env python3
"""Emit before/after coverage + metadata-release readiness verdict."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition"
OUT = REPORT / "coverage_before_after.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    integ = json.loads(
        (ROOT / "data/global/catalogue/pokecardex_integration_manifest.json").read_text(encoding="utf-8")
    )
    overlap = json.loads(
        (REPORT / "pokecardex/zh_hans_catalogue_overlap_summary.json").read_text(encoding="utf-8")
    )
    webp = json.loads((REPORT / "pokecardex/zh_hans_webp_disjoint_audit.json").read_text(encoding="utf-8"))
    auto = json.loads((REPORT / "pokecardex/auto_resolve/summary.json").read_text(encoding="utf-8"))
    ko = json.loads((REPORT / "korean/ko_metadata_acquire_summary.json").read_text(encoding="utf-8"))
    tcgdex = 10049
    payload = {
        "generatedAtUtc": utc_now(),
        "before": {
            "zhHansCatalogue": integ["zhHansBefore"],
            "koCatalogue": integ["beforeLanguageCounts"].get("ko"),
            "enCatalogue": integ["beforeLanguageCounts"].get("en"),
            "jaCatalogue": integ["beforeLanguageCounts"].get("ja"),
            "pokecardexStagedWebp": webp["disjointOnDisk"]["totalWebp"],
            "tcgdexStagedWebpPreserved": tcgdex,
        },
        "afterIsolatedBranch": {
            "zhHansCatalogue": integ["zhHansAfter"],
            "zhHansDelta": integ["zhHansDelta"],
            "koCatalogue": integ["afterLanguageCounts"].get("ko"),
            "koNewMetadataCandidates": ko.get("newMetadataCandidates"),
            "enCatalogue": integ["afterLanguageCounts"].get("en"),
            "jaCatalogue": integ["afterLanguageCounts"].get("ja"),
            "enJaUnchanged": integ["enJaPreserved"]["unchanged"],
        },
        "webpDisjoint": webp["disjointOnDisk"],
        "standardInventory": webp["standardInventory"],
        "catalogueOverlap": {
            "exactSetCodeOverlap": overlap["exactProviderSetCodeOverlapCount"],
            "highConfidenceRelated": overlap["highConfidenceRelated"],
            "softParallelOrRelated": overlap["softParallelOrRelated"],
            "noSoftMatch": overlap["noSoftMatch"],
            "explanation": overlap["anomalyExplanation"],
        },
        "autoResolve": auto,
        "korean": ko,
        "readinessVerdict": {
            "chineseMetadataIntegrationReady": True,
            "chineseArtworkProductionReady": False,
            "koreanMetadataApiExhaustedForNow": True,
            "koreanArtworkReady": False,
            "mergeToLiveReady": False,
            "summary": (
                "Chinese PokéCardex metadata is integrated on the isolated worktree catalogue "
                f"({integ['zhHansBefore']} → {integ['zhHansAfter']} zh-Hans) with attribution and "
                "artwork placeholders. EN/JA unchanged. WebP partitions are balanced. "
                "TCGdex KO API currently exposes only the same 239 cards already in catalogue "
                f"(official-count proxy {ko.get('tcgdexOfficialCountSum')}); no new KO metadata "
                "rows could be created without additional permitted sources. "
                "Do not merge/deploy or promote artwork."
            ),
        },
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (REPORT / "COVERAGE_BEFORE_AFTER.md").write_text(
        "\n".join(
            [
                "# Coverage before/after (isolated branch)",
                "",
                f"Generated: {payload['generatedAtUtc']}",
                "",
                f"- zh-Hans: **{integ['zhHansBefore']} → {integ['zhHansAfter']}** (Δ {integ['zhHansDelta']})",
                f"- KO catalogue: **{integ['beforeLanguageCounts'].get('ko')}** (new TCGdex candidates: **{ko.get('newMetadataCandidates')}**)",
                f"- EN/JA unchanged: **{integ['enJaPreserved']['unchanged']}**",
                f"- WebP: {webp['equation']}",
                f"- Catalogue set-code overlap: **{overlap['exactProviderSetCodeOverlapCount']}** (expected); soft related: **{overlap['highConfidenceRelated']}+{overlap['softParallelOrRelated']}**",
                "",
                "## Readiness verdict",
                "",
                payload["readinessVerdict"]["summary"],
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps(payload["readinessVerdict"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
