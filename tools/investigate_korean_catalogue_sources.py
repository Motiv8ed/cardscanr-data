#!/usr/bin/env python3
"""Phase-3 Korean catalogue/image source investigation (no unrestricted scraping).

Builds a source + missing-card inventory. Acquires nothing unless an explicit
allowlisted metadata probe is already public (TCGdex API sets list).
"""

from __future__ import annotations

import json
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition"
LIVE = Path(r"D:\CardScanR_Data\cardscanr-data")
UA = "CardScanR-CatalogueSync/1.0 (+ko-investigation-metadata-only)"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_catalogue_ko() -> list[dict[str, Any]]:
    path = LIVE / "data" / "global" / "catalogue" / "cards.jsonl"
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if str(row.get("language") or "") == "ko":
                rows.append(row)
    return rows


def fetch_tcgdex_ko_sets() -> list[dict[str, Any]]:
    cache = REPORT / "pokecardex" / "tcgdex_sets_ko.json"
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            return data
    req = urllib.request.Request("https://api.tcgdex.net/v2/ko/sets", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as resp:
        raw = resp.read()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(raw)
    data = json.loads(raw)
    return data if isinstance(data, list) else []


def official_count(set_row: dict[str, Any]) -> int:
    cc = set_row.get("cardCount")
    if isinstance(cc, dict):
        return int(cc.get("official") or cc.get("total") or 0)
    try:
        return int(cc or 0)
    except (TypeError, ValueError):
        return 0


def main() -> int:
    REPORT.mkdir(parents=True, exist_ok=True)
    ko_cat = load_catalogue_ko()
    tcg_sets = fetch_tcgdex_ko_sets()
    set_official = sum(official_count(s) for s in tcg_sets)
    with_count = sum(1 for s in tcg_sets if official_count(s) > 0)

    cat_by_set: Counter[str] = Counter()
    cat_with_images = 0
    for row in ko_cat:
        prov = row.get("providerSetIds") or {}
        if isinstance(prov, dict):
            for v in prov.values():
                if v:
                    cat_by_set[str(v)] += 1
        if row.get("imageProvenance"):
            cat_with_images += 1

    # Expected coverage gap (metadata universe proxy = TCGdex official counts)
    expected_missing_vs_tcgdex = max(0, set_official - len(ko_cat))

    sources = [
        {
            "source": "TCGdex API (ko)",
            "type": "public_http_api",
            "access": "metadata_list_ok_rate_limited",
            "imageRights": "third_party_hosts_require_per_url_review_mit_metadata_neq_pokemon_ip",
            "sets": len(tcg_sets),
            "approxOfficialCards": set_official,
            "acquireAllowedNow": "metadata_probe_only",
            "priority": 1,
            "notes": "Primary technical candidate for KO metadata expansion; gate any image staging.",
        },
        {
            "source": "CardScanR live catalogue (ko)",
            "type": "internal",
            "access": "already_in_repo",
            "imageRights": "n/a",
            "printings": len(ko_cat),
            "withImageProvenance": cat_with_images,
            "acquireAllowedNow": "n/a",
            "priority": 0,
            "notes": "Baseline; currently image-poor.",
        },
        {
            "source": "Pokémon TCG API (pokemontcg.io)",
            "type": "public_api",
            "access": "terms_apply",
            "imageRights": "typically_publisher_assets_review_required",
            "acquireAllowedNow": "no_bulk_mirror",
            "priority": 2,
            "notes": "Monitor KO completeness; do not bulk-mirror.",
        },
        {
            "source": "Official Pokémon Korea / TPCi pages",
            "type": "official_web",
            "access": "public_pages",
            "imageRights": "highest_legitimacy_when_press_assets_offered",
            "acquireAllowedNow": "promo_only_if_explicitly_offered",
            "priority": 3,
            "notes": "Prefer for promos; not a full-set pipeline.",
        },
        {
            "source": "Collector contributions",
            "type": "user_upload",
            "access": "requires_contribution_licence",
            "imageRights": "contributor_grant_plus_pokemon_ip_still_pending_for_production",
            "acquireAllowedNow": "design_intake_only",
            "priority": 4,
            "notes": "Useful for rare KO gaps with signed attestation.",
        },
        {
            "source": "PokéCardex",
            "type": "fan_catalogue",
            "access": "written_permission_cn_only",
            "imageRights": "no_ko_region",
            "acquireAllowedNow": "out_of_scope_for_ko",
            "priority": 99,
            "notes": "No KO zone; CN permission does not extend here.",
        },
        {
            "source": "Marketplace / scan-blog scrape",
            "type": "unauthorised_scrape",
            "access": "rejected",
            "imageRights": "high_legal_risk",
            "acquireAllowedNow": "no",
            "priority": 100,
            "notes": "Rejected.",
        },
    ]

    payload = {
        "generatedAtUtc": utc_now(),
        "phase": "korean_expansion_investigation",
        "bytesDownloaded": 0,
        "subscriptionsPurchased": 0,
        "catalogueKoPrintings": len(ko_cat),
        "catalogueKoWithImageProvenance": cat_with_images,
        "catalogueKoProviderSetCodes": len(cat_by_set),
        "catalogueKoTopSets": cat_by_set.most_common(15),
        "tcgdexKoSets": len(tcg_sets),
        "tcgdexKoSetsWithOfficialCount": with_count,
        "tcgdexKoApproxOfficialCards": set_official,
        "expectedMetadataGapVsTcgdexOfficial": expected_missing_vs_tcgdex,
        "expectedCoverage": {
            "ifTcgdexMetadataIngested": {
                "approxPrintings": set_official,
                "vsCurrentCatalogue": len(ko_cat),
                "approxAdditiveMetadata": expected_missing_vs_tcgdex,
                "imagesProductionCleared": 0,
                "note": (
                    "TCGdex official cardCount is a proxy, not a proof of downloadable artwork. "
                    "Image hosts must be classified before any staging."
                ),
            }
        },
        "sources": sources,
        "recommendedNextSteps": [
            "Inventory TCGdex ko card image URL hosts (metadata-only sample per set) and classify rights.",
            "Draft KO metadata ingest using cardscanr_global_catalogue contracts (provider=tcgdex, language=ko).",
            "Keep productionPromotionAllowed=false for any future KO image staging.",
            "Design collector contribution attestation path for rare gaps.",
            "Do not purchase subscriptions or scrape marketplaces.",
        ],
        "sampleTcgdexSetIds": [s.get("id") for s in tcg_sets[:20]],
    }
    (REPORT / "korean_acquisition_opportunity.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    md = [
        "# Korean acquisition opportunity report",
        "",
        f"Generated: {payload['generatedAtUtc']}",
        "",
        "## Current CardScanR KO catalogue",
        "",
        f"- Printings: **{len(ko_cat)}**",
        f"- With imageProvenance: **{cat_with_images}**",
        f"- Provider set codes: **{len(cat_by_set)}**",
        "",
        "## TCGdex KO metadata proxy",
        "",
        f"- Sets: **{len(tcg_sets)}** ({with_count} with official counts)",
        f"- Approx official cards: **{set_official}**",
        f"- Approx additive metadata vs catalogue: **{expected_missing_vs_tcgdex}**",
        "",
        "## Priority sources",
        "",
    ]
    for s in sorted(sources, key=lambda r: r["priority"]):
        md.append(
            f"- **{s['source']}** (priority {s['priority']}): {s.get('notes')} "
            f"— acquire now: `{s.get('acquireAllowedNow')}`"
        )
    md.extend(["", "## Policy", "", "- No KO image bytes downloaded this run.", "- No purchases / unrestricted scraping.", ""])
    (REPORT / "KOREAN_ACQUISITION_OPPORTUNITY.md").write_text("\n".join(md), encoding="utf-8")
    # Refresh the earlier investigation pointer
    (REPORT / "korean_tcgdex_probe.json").write_text(
        json.dumps(
            {
                "generatedAtUtc": utc_now(),
                "sets": len(tcg_sets),
                "setsWithOfficialCount": with_count,
                "approxOfficialCards": set_official,
                "sampleIds": payload["sampleTcgdexSetIds"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False)[:3500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
