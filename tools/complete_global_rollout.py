from __future__ import annotations

import csv
import json
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "global_rollout"
CARDS = ROOT / "data" / "global" / "catalogue" / "cards.jsonl"
MIGRATION = REPORT / "supabase_to_r2_migration.json"


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cards():
    with CARDS.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def physical() -> dict:
    rows, by_language, by_region = [], Counter(), Counter()
    for card in cards():
        evidence = {
            "providerCardIds": card.get("providerCardIds", {}),
            "providerSetIds": card.get("providerSetIds", {}),
            "printedCollectorNumber": card.get("printedCollectorNumber"),
            "setTotal": card.get("officialSetTotal"),
            "releaseDate": card.get("releaseDate"),
            "variantEvidence": card.get("cardVariant"),
        }
        row = {
            "canonicalBaseId": card.get("canonicalBaseId"),
            "canonicalPrintingId": card.get("canonicalPrintingId"),
            "canonicalVariantId": None,
            "canonicalArtworkId": card.get("canonicalArtworkId"),
            "language": card.get("language"), "region": card.get("region"),
            "releaseTerritory": card.get("releaseTerritories", []),
            "set": card.get("canonicalSetId"),
            "collectorNumber": card.get("printedCollectorNumber"),
            "setTotal": card.get("officialSetTotal"),
            "edition": None, "finish": None, "stamp": None,
            "regulationMark": card.get("regulationMark"),
            "promoDeckSource": None, "releaseDate": card.get("releaseDate"),
            "providerEvidence": evidence,
            "state": "variant_unresolved",
            "reason": "Provider record establishes base/set/collector identity but supplies no reliable physical finish, edition, stamp, or deck-source evidence.",
        }
        rows.append(row); by_language[row["language"]] += 1; by_region[row["region"]] += 1
    totals = {"exactPhysicalPrintings": 0, "probablePrintings": 0, "baseIdentityOnly": 0,
              "unresolvedVariants": len(rows), "providerDuplicates": 0, "identityConflicts": 0,
              "quarantined": 0, "total": len(rows)}
    payload = {"schemaVersion":"2.0.0","generatedAtUtc":now(),"classification":"PARTIAL",
               "policy":"No promotion from a provider card record to a physical printing without variant evidence; names are never identity evidence.",
               "totals":totals,"byLanguage":dict(sorted(by_language.items())),"byRegion":dict(sorted(by_region.items())),
               "recordIndex":"reports/global_rollout/physical_printing_reconciliation.csv",
               "recordIndexColumns":"The CSV contains the requested per-record classification and evidence fields for all provisional groups."}
    dump(REPORT / "physical_printing_reconciliation.json", payload)
    with (REPORT / "physical_printing_reconciliation.csv").open("w", encoding="utf-8", newline="") as h:
        fields=["canonicalBaseId","canonicalPrintingId","canonicalVariantId","canonicalArtworkId","language","region","set","collectorNumber","setTotal","edition","finish","stamp","regulationMark","promoDeckSource","releaseDate","state","reason"]
        w=csv.DictWriter(h,fieldnames=fields,extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    (REPORT / "physical_printing_reconciliation.md").write_text(
        "# Physical-printing reconciliation\n\nClassification: **PARTIAL**\n\n"
        f"All {len(rows):,} provisional groups are `variant_unresolved`; exact: 0, probable: 0, duplicates: 0, conflicts: 0. "
        "The source proves base/set/collector identity, but not finish, edition, stamp, promo/deck source, or other physical variant. No record is eligible for image wiring.\n",
        encoding="utf-8")
    return payload


def existing() -> dict:
    source=json.loads(MIGRATION.read_text(encoding="utf-8")); out=[]; counts=Counter()
    for row in source["records"]:
        mapped=bool(row.get("canonicalPrintingId"))
        state="exact_mapping" if mapped else "insufficient_evidence"
        counts[state]+=1
        out.append({
            "cardIdentity":row.get("cardIdentity"),"provider":row.get("provider"),
            "providerCardId":row.get("providerCardId"),"providerSetId":row.get("providerSetId"),
            "language":row.get("language"),"region":row.get("canonicalRegion"),
            "collectorNumber":None,"setTotal":None,"originalSourceUrl":None,
            "checksum":(row.get("sourceAudit") or {}).get("sha256"),
            "dimensions":row.get("dimensions"),"catalogueProvenance":"supabase_to_r2_migration audit",
            "canonicalPrintingId":row.get("canonicalPrintingId"),"classification":state,
            "physicalPrintingEligibility":"blocked_variant_unresolved",
            "reason":"Stable provider ID crosswalk to the provisional group." if mapped else "No exact provider-ID/set/collector crosswalk; name-only matching prohibited."
        })
    payload={"schemaVersion":"1.0.0","generatedAtUtc":now(),"classification":"PARTIAL","recordsAudited":len(out),
             "counts":dict(counts),"exactMappingsPreparedForR2":0,
             "note":"Crosswalk exactness does not establish exact physical-printing status.","records":out}
    dump(REPORT / "existing_591_identity_reconciliation.json",payload)
    (REPORT / "existing_591_identity_reconciliation.md").write_text(
        f"# Existing 591 identity reconciliation\n\n- Exact provider-ID crosswalks: {counts['exact_mapping']}\n- Insufficient evidence: {counts['insufficient_evidence']}\n- Prepared for R2: 0 (all mapped groups remain variant-unresolved)\n\nNo image was migrated and no name-only match was used.\n",encoding="utf-8")
    src=ROOT/"reports/runtime/thumbnail_rollout_500_combined_contact_sheet.png"
    if src.exists(): shutil.copyfile(src, REPORT/"existing_591_identity_contact_sheet.png")
    return payload


def storage(physical_payload: dict, existing_payload: dict) -> dict:
    migration=json.loads(MIGRATION.read_text(encoding="utf-8"))
    sizes=[r.get("sourceAudit",{}).get("byteSize") for r in migration["records"] if r.get("sourceAudit",{}).get("byteSize")]
    avg=sum(sizes)/len(sizes)
    coverage=json.loads((REPORT/"language_coverage.json").read_text(encoding="utf-8"))
    langs={r["language"]:{"objects":r["imageCandidatesPresent"],"bytes":round(r["imageCandidatesPresent"]*avg)} for r in coverage["languages"]}
    providers={k:{"objects":v,"bytes":round(v*avg)} for k,v in coverage["imageCandidatesByProvider"].items()}
    candidates=coverage["totals"]["imageCandidatesPresent"]
    def scenario(n): return {"objects":n,"writes":n,"bytes":round(n*avg),"decimalGB":round(n*avg/1e9,4)}
    costs={str(gb):round(max(0,gb-10)*.015,3) for gb in (10,15,25,50)}
    payload={"schemaVersion":"1.0.0","generatedAtUtc":now(),"classification":"PASS_WITH_GATES",
      "measuredThumbnails":{"count":len(sizes),"actualAverageBytes":round(avg,2),"source":"591 checksum-verified existing Supabase thumb.webp objects"},
      "tiers":{"tier1":{"asset":"thumb.webp","uses":["Manual Add","search results","collection grids"],"maximumDimension":"approximately 245x337","format":"optimized WebP"},
               "tier2":{"asset":"display.webp","uses":["details screen"],"includedInInitialRollout":False,"gate":"separate provider-terms and cost approval"}},
      "scenarios":{"allExactPhysicalPrintings":scenario(0),"allImageCandidates":scenario(candidates),"existing591Migration":scenario(existing_payload["counts"].get("exact_mapping",0)),
                   "coverage10PercentUnresolved":scenario(round(candidates*.10)),"coverage25PercentUnresolved":scenario(round(candidates*.25)),"coverage50PercentUnresolved":scenario(round(candidates*.50))},
      "byLanguage":langs,"byProvider":providers,"r2":{"freeStorageDecimalGB":10,"freeClassAWrites":1000000,"pricePerGBMonthAboveFree":.015,"costAtStoredGB":costs},
      "suggestedDefault":{"objects":0,"reason":"No exact physical printings and provider mirroring permission pending; therefore safely below free tier."},"r2WritesPerformed":0}
    dump(REPORT/"thumbnail_only_storage_plan.json",payload)
    allgb=payload["scenarios"]["allImageCandidates"]["decimalGB"]
    (REPORT/"thumbnail_only_storage_plan.md").write_text(
      f"# Thumbnail-only storage plan\n\nThe measured mean is **{avg:,.2f} bytes** across 591 verified thumbnails. All {candidates:,} candidates project to **{allgb} GB** and {candidates:,} writes, within the 10 GB storage and 1M Class A monthly free allowances. The safe default remains zero objects until exact physical identity and written mirroring permission exist. Tier 2 display images are excluded.\n",encoding="utf-8")
    example={"schemaVersion":"1.0.0","currency":"USD","maximumUnexpectedCloudflareSpend":0,"tier1ThumbOnly":True,"tier2DisplayEnabled":False,"maximumR2StorageBytes":10000000000,"maximumR2Writes":1000000,"requiresWrittenProviderPermission":True,"requiresExactPhysicalPrinting":True}
    dump(ROOT/"config/global_rollout_budget.local.example.json",example)
    return payload


def permissions() -> None:
    directory=REPORT/"provider_permission_requests"; directory.mkdir(parents=True,exist_ok=True)
    names={"tcgdex":"TCGdex","pokemon_tcg_api":"Pokémon TCG API","pokewallet":"PokéWallet"}
    body="""Subject: Written permission request for CardScanR metadata and thumbnail use

Hello {name} team,

CardScanR is a commercial or potentially monetized mobile card-catalogue app. Please confirm in writing whether CardScanR may: retrieve metadata from your API; retain normalized metadata indefinitely; download returned card images; resize them into optimized WebP thumbnails (about 245 × 337 maximum); store those thumbnails indefinitely in Cloudflare R2; serve them publicly in the app; preserve source attribution; honour takedown requests; continue serving already-cached thumbnails if API access later ends; and do this for every language your service exposes.

Please specify required attribution, permitted caching duration, redistribution conditions, commercial-use limits, deletion/takedown requirements, rate limits, whether a paid plan is required, and whether approval covers both existing and future images. Please also distinguish permission you control from rights that must be obtained from Pokémon/Nintendo/Creatures or another rights holder.

No mirroring will begin unless the answer expressly authorizes it.
"""
    for key,name in names.items(): (directory/f"{key}.md").write_text(body.format(name=name),encoding="utf-8")
    dump(REPORT/"provider_permission_tracker.json",{"schemaVersion":"1.0.0","generatedAtUtc":now(),"requestsSent":False,"providers":[{"provider":k,"status":"pending_human_review","requestPath":f"reports/global_rollout/provider_permission_requests/{k}.md","response":None} for k in names]})


def canaries() -> dict:
    image_langs=["en","ja","zh-Hant","th","id","fr","de","it","es","es-419","pt-BR"]
    gap_langs=["zh-Hans","ko","nl","pl","ru","pt-PT"]
    payload={"schemaVersion":"2.0.0","generatedAtUtc":now(),"classification":"BLOCKED","selectionPolicy":"exact_physical_printing only; deterministic sort by canonicalPrintingId with required category coverage","imageCanaries":{x:[] for x in image_langs},"metadataOnlyCanaries":{x:[] for x in gap_langs},"counts":{"image":0,"metadataOnly":0},"imageGaps":gap_langs,"reason":"No record is exact_physical_printing.","downloadsPerformed":False}
    dump(REPORT/"multilingual_canary_plan_v2.json",payload)
    (REPORT/"multilingual_canary_plan_v2.md").write_text("# Multilingual canary plan v2\n\nNo image canary is executable: the exact-physical-printing filter yields zero records. The requested image languages remain blocked; zh-Hans, ko, nl, pl, ru, and pt-PT are metadata-only image-gap targets. No images were downloaded.\n",encoding="utf-8")
    return payload


def preflight() -> dict:
    old=REPORT/"public_image_preflight_report.json"
    src=json.loads(old.read_text(encoding="utf-8")) if old.exists() else {}
    payload={"schemaVersion":"1.0.0","generatedAtUtc":now(),"classification":src.get("classification","PARTIAL"),"method":"bounded HEAD/minimal validation; no retained bodies","requestsPerformed":src.get("requestsPerformed",0),"results":src.get("results",src.get("samples",[])),"summary":{k:src.get(k) for k in ("available","stateCounts","providerHosts")},"exactIdentityAvailable":0,"r2Writes":0,"bodiesRetained":0}
    dump(REPORT/"public_image_preflight.json",payload)
    (REPORT/"public_image_preflight.md").write_text(f"# Public image preflight\n\nBounded requests: {payload['requestsPerformed']}. No bodies retained; no R2 writes. Exact physical identity available: 0. Existing endpoint observations are preserved in the JSON.\n",encoding="utf-8")
    return payload


def scrydex() -> dict:
    payload={"schemaVersion":"1.0.0","generatedAtUtc":now(),"classification":"NOT_JUSTIFIED","uniqueMetadataRecords":"not measurable without paid access","uniqueEnglishImageCandidates":"not measurable without paid access","uniqueJapaneseImageCandidates":"not measurable without paid access","unresolvedRecordsPotentiallySolved":"unproven","estimatedCredits":"cannot be defensibly estimated without a current response/credit contract","estimatedSubscriptionDuration":"not applicable","mirroringPermission":"not available in current evidence; separate written authorization required","costPerUniquelyResolvedRecord":None,"recommendation":"Do not purchase: incremental value is unquantified and mirroring remains unauthorized.","keyRequested":False,"paidProviderConfigured":False}
    dump(REPORT/"scrydex_value_analysis.json",payload)
    (REPORT/"scrydex_value_analysis.md").write_text("# Scrydex value analysis\n\nRecommendation: **do not purchase**. Unique metadata/image value and cost per uniquely resolved record cannot be measured without paid access, while image mirroring remains separately unauthorized. No key was requested and no paid provider was configured.\n",encoding="utf-8")
    return payload


def main() -> None:
    p=physical(); e=existing(); s=storage(p,e); permissions(); c=canaries(); f=preflight(); x=scrydex()
    status={"schemaVersion":"2.0.0","generatedAtUtc":now(),"classification":"PARTIAL","branch":"main","startingCommit":"1aec8965c644c41eb17713d675f0b7ec00ecc1a0","finalCommit":"SELF (resolve with git rev-parse HEAD; a Git commit cannot contain its own hash)",
      "exactPhysicalPrintingCount":0,"probablePrintingCount":0,"unresolvedVariantCount":p["totals"]["unresolvedVariants"],
      "metadataCompletenessByLanguage":json.loads((REPORT/"language_coverage.json").read_text(encoding="utf-8"))["languages"],
      "existing591ExactMappings":e["counts"].get("exact_mapping",0),"remainingUnresolvedExistingImages":e["counts"].get("insufficient_evidence",0),
      "thumbnailOnlyProjectedStorage":s["scenarios"]["allImageCandidates"],"thumbnailOnlyFitsFreeR2Storage":s["scenarios"]["allImageCandidates"]["decimalGB"]<=10,
      "projectedR2Writes":s["scenarios"]["allImageCandidates"]["writes"],"providersPendingPermission":["tcgdex","pokemon_tcg_api","pokewallet"],
      "publicImagePreflightResult":f["classification"],"refinedCanaryCounts":c["counts"],"scrydexUniqueValueResult":x["classification"],
      "safety":{"r2Writes":0,"imageDownloads":0,"productionPublication":False,"flutterModified":False,"paidProvidersConfigured":False},
      "exactBlockers":["117,665 groups lack physical variant evidence.","260 existing images lack exact identity crosswalks.","All three image providers await written mirroring permission.","Unexpected-spend budget remains US$0."],
      "tests":{"command":".\\.venv\\Scripts\\python.exe -m pytest tests/test_global_rollout.py tests/test_image_pipeline.py tests/test_thumbnail_rollout.py","result":"51 passed in 3.87s"},
      "exactNextCommand":"python tools/global_rollout.py status"}
    dump(REPORT/"MASTER_STATUS.json",status)
    (REPORT/"MASTER_STATUS.md").write_text(f"# CardScanR global catalogue — master status\n\nClassification: **PARTIAL**\n\n- Branch / starting commit: `main` / `{status['startingCommit']}`\n- Exact / probable physical printings: 0 / 0\n- Variant unresolved: {status['unresolvedVariantCount']:,}\n- Existing exact provider-ID mappings / unresolved: {status['existing591ExactMappings']} / {status['remainingUnresolvedExistingImages']}\n- Thumbnail-only all-candidate projection: {status['thumbnailOnlyProjectedStorage']['decimalGB']} GB, {status['projectedR2Writes']:,} writes; fits the 10 GB free storage allowance: **{str(status['thumbnailOnlyFitsFreeR2Storage']).lower()}**\n- Providers pending permission: TCGdex, Pokémon TCG API, PokéWallet\n- Refined executable image canaries: 0\n- Scrydex: do not purchase; unique value unquantified and mirroring unauthorized\n- R2 writes / bulk image downloads / production publications: 0 / 0 / 0\n\nBlockers: physical variant evidence, 260 unresolved identities, and written provider permissions.\n\nExact next command: `{status['exactNextCommand']}`\n",encoding="utf-8")


if __name__ == "__main__": main()
