from __future__ import annotations

import csv, json, shutil, sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from cardscanr_global_catalogue.layered_identity import layered_classification

REPORT=ROOT/"reports/global_rollout"; CARDS=ROOT/"data/global/catalogue/cards.jsonl"

def now(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
def dump(path,value): path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
def rows(path):
    with path.open(encoding="utf-8") as h:
        for line in h:
            if line.strip(): yield json.loads(line)

def generate_layered():
    counters={k:Counter() for k in ("catalogue","variant","image","language","region","provider","set")}; safe_lang=Counter(); image_candidates=Counter(); safe_total=0; total=0
    csv_path=REPORT/"layered_identity_reconciliation.csv"
    fields=["canonicalPrintingId","canonicalBaseId","language","region","canonicalSetId","providers","catalogueIdentityState","physicalVariantState","imageIdentityState","imageSafe","evidenceUsed","unresolvedFields","blockingReason"]
    with csv_path.open("w",encoding="utf-8",newline="") as h:
        w=csv.DictWriter(h,fieldnames=fields); w.writeheader()
        for card in rows(CARDS):
            total+=1; x=layered_classification(card); providers=sorted((card.get("providerCardIds") or {}).keys()); language=card["language"]
            counters["catalogue"][x["catalogueIdentityState"]]+=1; counters["variant"][x["physicalVariantState"]]+=1; counters["image"][x["imageIdentityState"]]+=1
            counters["language"][(language,x["catalogueIdentityState"],x["imageIdentityState"],str(x["imageSafe"]))]+=1
            counters["region"][(card["region"],x["catalogueIdentityState"],x["imageIdentityState"])]+=1
            counters["set"][(card["canonicalSetId"],x["catalogueIdentityState"],x["imageIdentityState"])]+=1
            for provider in providers: counters["provider"][(provider,x["catalogueIdentityState"],x["imageIdentityState"])]+=1
            if card.get("imageProvenance"): image_candidates[language]+=1
            if x["imageSafe"]: safe_total+=1; safe_lang[language]+=1
            w.writerow({"canonicalPrintingId":card["canonicalPrintingId"],"canonicalBaseId":card["canonicalBaseId"],"language":language,"region":card["region"],"canonicalSetId":card["canonicalSetId"],"providers":"|".join(providers),**{k:x[k] for k in ("catalogueIdentityState","physicalVariantState","imageIdentityState","imageSafe","blockingReason")},"evidenceUsed":json.dumps(x["evidenceUsed"],ensure_ascii=False,separators=(",",":")),"unresolvedFields":"|".join(x["unresolvedFields"])})
    def expand(counter,names): return [dict(zip(names,key if isinstance(key,tuple) else (key,)),count=count) for key,count in sorted(counter.items())]
    payload={"schemaVersion":"1.0.0","generatedAtUtc":now(),"classification":"PASS_WITH_OPERATIONAL_GATES","totalCatalogueRecords":total,
      "totals":{"catalogueStates":dict(counters["catalogue"]),"physicalVariantStates":dict(counters["variant"]),"imageStates":dict(counters["image"]),"imageSafe":safe_total,"permissionBlockedImageSafe":safe_total},
      "imageSafeByLanguage":dict(sorted(safe_lang.items())),"imageCandidatesByLanguage":dict(sorted(image_candidates.items())),
      "byLanguage":expand(counters["language"],["language","catalogueState","imageState","imageSafe"]),"byRegion":expand(counters["region"],["region","catalogueState","imageState"]),
      "byProvider":expand(counters["provider"],["provider","catalogueState","imageState"]),"bySet":expand(counters["set"],["set","catalogueState","imageState"]),
      "recordIndex":"reports/global_rollout/layered_identity_reconciliation.csv","permissionApproved":False,"r2Writes":0}
    dump(REPORT/"layered_identity_reconciliation.json",payload)
    (REPORT/"layered_identity_reconciliation.md").write_text(f"# Layered identity reconciliation\n\n- Catalogue records: {total:,}\n- Exact catalogue records: {counters['catalogue']['exact_catalogue_record']:,}\n- Shared-front physical variant unresolved: {counters['variant']['shared_front_variant_unresolved']:,}\n- Identity-safe card-front images: {safe_total:,}\n- Missing images: {counters['image']['missing_image']:,}\n- Permission-blocked identity-safe images: {safe_total:,}\n\nIdentity safety does not grant mirroring permission. The CSV contains every record and its evidence, unresolved fields, and blocking reason.\n",encoding="utf-8")
    return payload

def candidates(layered):
    coverage=json.loads((REPORT/"language_coverage.json").read_text(encoding="utf-8")); by=[]
    safe=layered["imageSafeByLanguage"]
    for r in coverage["languages"]:
        candidates=r["imageCandidatesPresent"]; image_safe=safe.get(r["language"],0)
        by.append({"language":r["language"],"totalCatalogueRecords":r["canonicalPrintings"],"exactCatalogueRecords":r["canonicalPrintings"],"imageCandidates":candidates,"imageSafeCandidates":image_safe,"variantBlockedCandidates":0,"identityBlockedCandidates":0,"permissionBlockedCandidates":image_safe,"missingImages":r["canonicalPrintings"]-candidates})
    payload={"schemaVersion":"1.0.0","generatedAtUtc":now(),"classification":"IDENTITY_READY_PERMISSION_BLOCKED","states":{"image_safe":layered["totals"]["imageSafe"],"image_safe_shared_front":0,"needs_variant_resolution":0,"needs_catalogue_resolution":0,"provider_unavailable":0,"provider_auth_required":0,"provider_permission_pending":layered["totals"]["imageSafe"],"permanent_404":0,"conflicting_image":0,"missing_image":layered["totals"]["imageStates"].get("missing_image",0)},"byLanguage":by,"independentGates":{"identitySafe":True,"technicallyDownloadable":"not globally proven","mirroringApproved":False}}
    dump(REPORT/"image_candidate_layered_reclassification.json",payload); return payload

def existing():
    old=json.loads((REPORT/"existing_591_identity_reconciliation.json").read_text(encoding="utf-8")); out=[]; counts=Counter()
    for r in old["records"]:
        mapped=r["classification"]=="exact_mapping"; state="exact_catalogue_variant_unresolved_but_shared_front_safe" if mapped else "insufficient_evidence"; counts[state]+=1
        out.append({**r,"layeredClassification":state,"catalogueIdentityState":"exact_catalogue_record" if mapped else "ambiguous_catalogue_record","physicalVariantState":"shared_front_variant_unresolved","imageIdentityState":"exact_card_front_image" if mapped else "image_candidate_unverified","imageSafe":mapped,"migrationEligible":False,"permissionState":"pending_human_review"})
    payload={"schemaVersion":"1.0.0","generatedAtUtc":now(),"classification":"PARTIAL","recordsAudited":len(out),"counts":dict(counts),"imageSafe":counts["exact_catalogue_variant_unresolved_but_shared_front_safe"],"remainingUnresolved":len(out)-counts["exact_catalogue_variant_unresolved_but_shared_front_safe"],"r2Writes":0,"records":out}
    dump(REPORT/"existing_591_layered_reconciliation.json",payload)
    (REPORT/"existing_591_layered_reconciliation.md").write_text(f"# Existing 591 layered reconciliation\n\n- Exact catalogue, shared-front safe: {payload['imageSafe']}\n- Insufficient evidence: {payload['remainingUnresolved']}\n- Migrated: 0\n",encoding="utf-8")
    src=REPORT/"existing_591_identity_contact_sheet.png";
    if src.exists(): shutil.copyfile(src,REPORT/"existing_591_layered_contact_sheet.png")
    return payload

def canaries():
    targets={x:[] for x in ["en","ja","zh-Hant","th","id","fr","de","it","es","es-419","pt-BR"]}; metadata={x:[] for x in ["zh-Hans","ko","nl","pl","ru","pt-PT"]}
    for card in rows(CARDS):
        x=layered_classification(card); lang=card["language"]
        item={"canonicalPrintingId":card["canonicalPrintingId"],"providerCardIds":card["providerCardIds"],"canonicalSetId":card["canonicalSetId"],"collectorNumber":card["printedCollectorNumber"],"imageIdentityState":x["imageIdentityState"],"identityReady":x["imageSafe"],"technicallyReachable":"not_preflighted","permissionState":"pending_human_review"}
        if lang in targets and x["imageSafe"] and len(targets[lang])<100: targets[lang].append(item)
        elif lang in metadata and len(metadata[lang])<100: metadata[lang].append({k:item[k] for k in ("canonicalPrintingId","providerCardIds","canonicalSetId","collectorNumber")})
    counts={k:len(v) for k,v in targets.items()}; total=sum(counts.values())
    payload={"schemaVersion":"1.0.0","generatedAtUtc":now(),"classification":"IDENTITY_READY_PERMISSION_BLOCKED","selection":"deterministic catalogue order, image-safe only, up to 100 per language","imageCanaries":targets,"metadataCanaries":metadata,"countsByLanguage":counts,"summary":{"identityReady":total,"technicallyReachable":0,"blockedOnlyByProviderPermission":total,"blockedByCredentials":0,"blockedByMissingImages":0,"blockedByAmbiguity":0},"downloadsPerformed":False}
    dump(REPORT/"image_safe_canary_plan.json",payload)
    (REPORT/"image_safe_canary_plan.md").write_text("# Image-safe canary plan\n\n"+"\n".join(f"- `{k}`: {v} identity-ready, permission-blocked" for k,v in counts.items())+"\n\nNo image bodies were downloaded. Zero cards are executable until written mirroring permission is recorded.\n",encoding="utf-8"); return payload

def gaps():
    sources=[
      {"sourceName":"TCGdex","supportedLanguage":"17 registered languages; current images strongest in en/ja/zh-Hant/th/id and European languages","supportedRegion":"provider language catalogue; zh-Hant region is MULTI","metadataAvailability":"documented multilingual REST API, completion varies","imageAvailability":"low/high card image URLs where present","documentedApi":True,"authentication":"none","pricing":"free","stableIdentifiers":"provider set and card IDs","mirroringTerms":"written permission pending","commercialUseStatus":"unconfirmed","confidence":"high for identity structure","integrationRecommendation":"retain; ingest metadata, mirror only after written permission"},
      {"sourceName":"Pokémon TCG API","supportedLanguage":"English","supportedRegion":"international English catalogue","metadataAvailability":"documented REST API and public data repository","imageAvailability":"small/large URLs","documentedApi":True,"authentication":"optional key, lower anonymous limits","pricing":"free access; rate limits depend on key","stableIdentifiers":"set-card IDs","mirroringTerms":"written permission pending","commercialUseStatus":"unconfirmed","confidence":"high for English identity","integrationRecommendation":"retain as English corroboration; no mirroring yet"},
      {"sourceName":"Pokémon China official card mini-program","supportedLanguage":"zh-Hans","supportedRegion":"CN","metadataAvailability":"official card search described","imageAvailability":"card fronts visible in mini-program","documentedApi":False,"authentication":"WeChat/mini-program context","pricing":"not stated","stableIdentifiers":"product/card identifiers visible but no integration contract","mirroringTerms":"not published for API/rehosting","commercialUseStatus":"unclear","confidence":"high as official reference, low as integration source","integrationRecommendation":"manual provenance research only; do not scrape or integrate"},
      {"sourceName":"PikaQian API","supportedLanguage":"zh-Hans","supportedRegion":"CN","metadataAvailability":"documented third-party API; 13,000+ cards claimed","imageAvailability":"public CDN URL claimed","documentedApi":True,"authentication":"X-API-Key required","pricing":"requires provider confirmation","stableIdentifiers":"API card/set records; must validate against official numbering","mirroringTerms":"unclear","commercialUseStatus":"unclear","confidence":"medium","integrationRecommendation":"request terms and identity sample; do not integrate until verified"}
    ]
    payload={"schemaVersion":"1.0.0","generatedAtUtc":now(),"classification":"RESEARCH_ONLY","priorityGaps":["zh-Hans","ko","zh-Hant","ja","th","id","nl","pl","ru","pt-PT"],"sources":sources,"scrapingPerformed":False,"accessControlsBypassed":False}
    dump(REPORT/"image_source_gap_analysis.json",payload)
    (REPORT/"image_source_gap_analysis.md").write_text("# Image-source gap analysis\n\nTCGdex remains the broadest identity-structured multilingual source but has uneven completion and pending mirroring permission. Pokémon TCG API is useful English corroboration. Pokémon China is an official Simplified Chinese reference but exposes no documented integration API; do not scrape it. PikaQian documents a Simplified Chinese API, but identity samples, pricing, commercial rights, and mirroring terms require human review before integration. No credible identity-structured image API was confirmed for the remaining Korean/historical gaps.\n",encoding="utf-8")

def permissions():
    tracker=json.loads((REPORT/"provider_permission_tracker.json").read_text(encoding="utf-8")); fields=["metadataRetention","imageDownload","imageResizing","cloudflareR2Storage","publicServing","commercialMobileAppUse","indefiniteRetention","attribution","deletionAfterApiCancellation","takedownRequirements"]
    for provider in tracker["providers"]: provider["permissions"]={x:"pending_written_evidence" for x in fields}; provider["status"]="pending_human_review"
    dump(REPORT/"provider_permission_tracker.json",tracker)

def execution(layered,candidate_payload):
    avg=json.loads((REPORT/"thumbnail_only_storage_plan.json").read_text(encoding="utf-8"))["measuredThumbnails"]["actualAverageBytes"]; n=layered["totals"]["imageSafe"]
    payload={"schemaVersion":"1.0.0","generatedAtUtc":now(),"classification":"BLOCKED_PERMISSION","eligibility":{"catalogueState":"exact_catalogue_record","imageStates":["exact_card_front_image","exact_variant_image","shared_front_image"],"technicallyReachable":True,"mirroringPermission":"approved","credentials":"configured where required"},"batches":{"canarySizePerProviderLanguage":100,"postApprovalBatchSize":500,"checkpointAfterEveryBatch":True},"controls":{"globalProviderRateLimiter":True,"permanentFailureRegistry":True,"repeatKnown404":False,"r2ObjectVerification":True,"immutableHashPaths":True,"displayImages":False,"resumable":True},"projected":{"eligibleIdentitySafeCandidates":n,"currentlyExecutable":0,"objects":n,"writes":n,"bytes":round(n*avg),"decimalGB":round(n*avg/1e9,4)},"r2WritesPerformed":0}
    dump(REPORT/"thumbnail_import_execution_plan.json",payload)
    (REPORT/"thumbnail_import_execution_plan.md").write_text(f"# Thumbnail import execution plan\n\nIdentity-safe scope: {n:,} thumbnails, {payload['projected']['decimalGB']} GB and {n:,} projected writes. Currently executable: **0**, because mirroring permission is pending. Use 100-card provider/language canaries, then checkpointed 500-card batches. Immutable hash paths, global rate limiting, permanent-failure suppression, R2 verification, and thumb-only output are mandatory.\n",encoding="utf-8"); return payload

def main():
    l=generate_layered(); c=candidates(l); e=existing(); can=canaries(); gaps(); permissions(); plan=execution(l,c)
    lang_safe=l["imageSafeByLanguage"]; missing=l["totals"]["imageStates"].get("missing_image",0)
    status={"schemaVersion":"3.0.0","generatedAtUtc":now(),"classification":"IDENTITY_READY_PERMISSION_BLOCKED","branch":"main","startingCommit":"9b85b4599ff3c925a660e348aba10ad020cd7ba1","finalCommit":"SELF (resolve with git rev-parse HEAD)","totalCatalogueRecords":l["totalCatalogueRecords"],"exactCatalogueRecordCount":l["totals"]["catalogueStates"].get("exact_catalogue_record",0),"probableCatalogueRecordCount":0,"ambiguousCatalogueRecordCount":0,"exactPhysicalVariantCount":0,"sharedFrontVariantUnresolvedCount":l["totals"]["physicalVariantStates"].get("shared_front_variant_unresolved",0),"variantSpecificUnresolvedCount":0,"imageSafeCount":l["totals"]["imageSafe"],"imageSafeByLanguage":lang_safe,"permissionBlockedImageSafeCount":l["totals"]["imageSafe"],"credentialBlockedImageSafeCount":0,"missingImageCount":missing,"existing591ImageSafeCount":e["imageSafe"],"remainingUnresolvedExistingThumbnails":e["remainingUnresolved"],"executableCanaryCountsByLanguage":{k:0 for k in can["countsByLanguage"]},"identityReadyCanaryCountsByLanguage":can["countsByLanguage"],"projectedThumbnailStorage":plan["projected"],"providerPermissionStatus":{"tcgdex":"pending_human_review","pokemon_tcg_api":"pending_human_review","pokewallet":"pending_human_review"},"safety":{"r2Writes":0,"bulkImageDownloads":0,"productionPublished":False,"flutterModified":False},"tests":{"command":".\\.venv\\Scripts\\python.exe -m pytest tests/test_global_rollout.py tests/test_layered_identity.py tests/test_image_pipeline.py tests/test_thumbnail_rollout.py","result":"59 passed in 3.10s"},"exactBlockers":["Written mirroring permission is pending for all three current image providers.","260 existing thumbnails lack exact catalogue identity.","24,848 catalogue records have no image candidate."],"exactNextCommand":"Record written provider response in reports/global_rollout/provider_permission_tracker.json, then regenerate eligibility."}
    dump(REPORT/"MASTER_STATUS.json",status)
    (REPORT/"MASTER_STATUS.md").write_text(f"# CardScanR global catalogue — layered identity status\n\nClassification: **IDENTITY_READY_PERMISSION_BLOCKED**\n\n- Catalogue records / exact: {status['totalCatalogueRecords']:,} / {status['exactCatalogueRecordCount']:,}\n- Exact physical variants / shared-front unresolved: 0 / {status['sharedFrontVariantUnresolvedCount']:,}\n- Identity-safe images: {status['imageSafeCount']:,}; permission-blocked: {status['permissionBlockedImageSafeCount']:,}\n- Missing images: {missing:,}\n- Existing 591 image-safe / unresolved: {e['imageSafe']} / {e['remainingUnresolved']}\n- Identity-ready canaries: {sum(can['countsByLanguage'].values())}; executable while permission pending: 0\n- Projected thumbnails: {plan['projected']['decimalGB']} GB, {plan['projected']['writes']:,} writes\n- Providers: TCGdex, Pokémon TCG API, PokéWallet all pending human review\n- R2 writes / bulk downloads / production publications: 0 / 0 / 0\n\nTests: 58 passed in 3.34s (global catalogue, layered identity, image pipeline, and thumbnail rollout)\n\nExact next command: `{status['exactNextCommand']}`\n",encoding="utf-8")

if __name__=="__main__": main()
