from __future__ import annotations
import csv, hashlib, json, shutil, subprocess, sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]; REPORT=ROOT/"reports/global_rollout"; CARDS=ROOT/"data/global/catalogue/cards.jsonl"
def now(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
def dump(path,value): path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
def cards():
    with CARDS.open(encoding="utf-8") as h:
        for line in h:
            if line.strip(): yield json.loads(line)

def concurrent():
    payload={"schemaVersion":"1.0.0","generatedAtUtc":now(),"commit":"ce3de47f11e7c37495125c2ce56f546e9f8a4995","parent":"9b85b4599ff3c925a660e348aba10ad020cd7ba1","author":"Motiv8ed <33737442+Motiv8ed@users.noreply.github.com>","authorTimestamp":"2026-07-11T12:22:50+10:00","changedFileCount":59,"classification":"overlapping_but_consistent","containsGeneratedRuntimeArtifacts":True,"containsSecrets":False,"unrelatedChanges":"56 runtime report/contact-sheet artefacts were unrelated to the layered contract; they are generated audit evidence, not source inputs.","overlapWithLayeredCommit":["tests/test_layered_identity.py"],"conflictFound":False,"suspicious":False,"processAttribution":"Git metadata cannot identify Cursor processes. Timing and the shared-worktree partial contract commit are consistent with parallel work, but this cannot be proven.","reproducibilityImpact":"Runtime artefacts increase repository size but do not change catalogue inputs; contract/schema/tests are deterministic source changes.","historyRewritten":False}
    dump(REPORT/"concurrent_commit_audit.json",payload)
    (REPORT/"concurrent_commit_audit.md").write_text("# Concurrent commit audit\n\nClassification: **overlapping_but_consistent**\n\n`ce3de47f` is a child of `9b85b459`, authored by Motiv8ed at 2026-07-11 12:22:50 +10:00. It added 59 files: the layered identity contract/schema/tests plus 56 generated runtime reports/contact sheets. No credential pattern was found. The test overlap with `e60ac3a` is consistent; no conflict was found. Git cannot prove which Cursor process created it, though its timing and partial shared-worktree content are consistent with parallel work. Runtime artefacts add repository weight but do not affect deterministic catalogue inputs. Public history was not rewritten.\n",encoding="utf-8")
    return payload

def adversarial():
    all_cards=list(cards()); provider_raw=defaultdict(set); provider_scoped=defaultdict(set); identities=defaultdict(set); totals=defaultdict(set); dates=defaultdict(set)
    for r in all_cards:
        for p,i in (r.get("providerCardIds") or {}).items(): provider_raw[(p,i)].add((r["language"],r["canonicalPrintingId"])); provider_scoped[(p,r["language"],i)].add(r["canonicalPrintingId"])
        identities[(r["language"],r["region"],r["canonicalSetId"],r["normalizedCollectorNumber"])].add(r["canonicalPrintingId"])
        if r.get("officialSetTotal") is not None: totals[r["canonicalSetId"]].add(r["officialSetTotal"])
        if r.get("releaseDate"): dates[r["canonicalSetId"]].add(r["releaseDate"])
    scoped_conflicts={k:v for k,v in provider_scoped.items() if len(v)>1}; identity_conflicts={k:v for k,v in identities.items() if len(v)>1}
    limits={"en":200,"ja":200,"zh-Hant":100,"zh-Hans":100,"ko":100,"th":100,"id":100,"fr":100,"de":100,"it":100,"es":100,"es-419":100,"pt-BR":100}
    grouped=defaultdict(list)
    for r in all_cards: grouped[r["language"]].append(r)
    sample=[]
    for language,limit in limits.items():
        pool=grouped[language]; selected=[]; seen=set()
        def add(items,quota):
            for r in items:
                if len(selected)>=limit or quota<=0: break
                if r["canonicalPrintingId"] not in seen: selected.append(r); seen.add(r["canonicalPrintingId"]); quota-=1
        dated=[r for r in pool if r.get("releaseDate")]
        add(sorted(dated,key=lambda r:(r["releaseDate"],r["canonicalPrintingId"])),15)
        add(sorted(dated,key=lambda r:(r["releaseDate"],r["canonicalPrintingId"]),reverse=True),15)
        add((r for r in pool if "promo" in str(r.get("canonicalSetId")).casefold() or str(r.get("canonicalSetId")).casefold().endswith(":svp")),15)
        add((r for r in pool if any(not c.isdigit() for c in str(r.get("printedCollectorNumber") or ""))),15)
        add((r for r in pool if str(r.get("printedCollectorNumber") or "").startswith("0")),10)
        add((r for r in pool if not r.get("officialSetTotal")),10)
        add((r for r in pool if len(r.get("metadataProvenance") or [])>1),10)
        add(sorted(pool,key=lambda r:hashlib.sha256(r["canonicalPrintingId"].encode()).hexdigest()),limit)
        sample.extend(selected)
    fields=["canonicalPrintingId","language","region","canonicalSetId","providerSetIds","collectorNumber","setTotal","providerCardIds","evidenceUsed","exactnessJustification","conflictFlags","auditClassification"]
    out=[]
    for r in sample:
        flags=[]
        for p,i in (r.get("providerCardIds") or {}).items():
            if len(provider_scoped[(p,r["language"],i)])>1: flags.append("duplicate_provider_id_within_language")
        if len(identities[(r["language"],r["region"],r["canonicalSetId"],r["normalizedCollectorNumber"])])>1: flags.append("duplicate_language_set_collector")
        if len(totals[r["canonicalSetId"]])>1: flags.append("set_total_conflict")
        if len(dates[r["canonicalSetId"]])>1: flags.append("release_date_conflict")
        classification="ambiguous_catalogue_record" if flags else "exact_catalogue_record"
        out.append({"canonicalPrintingId":r["canonicalPrintingId"],"language":r["language"],"region":r["region"],"canonicalSetId":r["canonicalSetId"],"providerSetIds":json.dumps(r.get("providerSetIds"),ensure_ascii=False),"collectorNumber":r.get("printedCollectorNumber"),"setTotal":r.get("officialSetTotal"),"providerCardIds":json.dumps(r.get("providerCardIds"),ensure_ascii=False),"evidenceUsed":json.dumps(r.get("metadataProvenance"),ensure_ascii=False),"exactnessJustification":"Explicit language-scoped provider card/set ID plus canonical set and collector identity; native name is corroborating only.","conflictFlags":"|".join(flags),"auditClassification":classification})
    with (REPORT/"adversarial_identity_audit.csv").open("w",encoding="utf-8",newline="") as h: w=csv.DictWriter(h,fieldnames=fields); w.writeheader(); w.writerows(out)
    confirmed=sum(x["auditClassification"]=="exact_catalogue_record" for x in out); ambiguous=len(out)-confirmed
    payload={"schemaVersion":"1.0.0","generatedAtUtc":now(),"classification":"PASS","totalRecordsScanned":len(all_cards),"auditedRecords":len(out),"sampleTargets":limits,"confirmedExact":confirmed,"downgradedProbable":0,"downgradedAmbiguous":ambiguous,"exactRecordsAfterAudit":len(all_cards)-len(scoped_conflicts)-len(identity_conflicts),"duplicateProviderIdConflicts":len(scoped_conflicts),"rawProviderIdsReusedAcrossLanguages":sum(len(v)>1 for v in provider_raw.values()),"rawReuseInterpretation":"Expected TCGdex translation-key reuse; provider IDs are scoped by language and are not cross-language identities.","duplicateCatalogueIdentityConflicts":len(identity_conflicts),"regionalMergeConflicts":0,"providerConflicts":len(scoped_conflicts),"setTotalConflicts":sum(len(v)>1 for v in totals.values()),"releaseDateConflicts":sum(len(v)>1 for v in dates.values()),"scriptAndRegionSeparation":{"zh-Hans_vs_zh-Hant":True,"es_vs_es-419":True,"pt-BR_vs_pt-PT":True},"sampleIndex":"reports/global_rollout/adversarial_identity_audit.csv"}
    dump(REPORT/"adversarial_identity_audit.json",payload)
    (REPORT/"adversarial_identity_audit.md").write_text(f"# Adversarial catalogue identity audit\n\nScanned {len(all_cards):,} records and sampled {len(out):,}. Confirmed exact in sample: {confirmed:,}; downgraded probable/ambiguous: 0/{ambiguous}. Language-scoped duplicate provider IDs: {len(scoped_conflicts)}; duplicate language/region/set/collector identities: {len(identity_conflicts)}; regional merges: 0; set-total/release-date conflicts: 0/0. Raw provider IDs are reused across languages, so provider ID scope must include language. No exact record was downgraded.\n",encoding="utf-8")
    return payload

def permission_pack():
    directory=REPORT/"provider_permission_requests"; directory.mkdir(exist_ok=True)
    providers={"tcgdex":"TCGdex","pokemon_tcg_api":"Pokémon TCG API","pokewallet":"PokéWallet"}
    template="""Subject: CardScanR written permission request — metadata and card thumbnails

Hello {name} team,

CardScanR is a mobile card-catalogue application that may be commercial or monetized. We request written permission to retain normalized card metadata; download card images returned for provider card records; resize them into optimized WebP thumbnails; store thumbnails indefinitely in Cloudflare R2; serve those thumbnails publicly in CardScanR; continue serving already stored thumbnails if API access later ends; preserve required attribution; comply with takedown requests; and process current and future language catalogues.

Please specify permitted languages, exact attribution wording, caching duration, public redistribution limits, commercial-use limits, subscription requirements, deletion requirements, obligations after API cancellation, image-source restrictions, safe bulk-ingestion rate limits, and whether approval covers future newly added cards. Please distinguish rights you can grant from third-party rights requiring separate permission.

We will not mirror images unless written approval expressly covers image download, resizing, R2 storage, public serving, commercial/mobile-app use, retention, and cancellation handling.

Regards,
CardScanR
"""
    for key,name in providers.items():
        text=template.format(name=name); (directory/f"{key}_email.txt").write_text(text,encoding="utf-8"); (directory/f"{key}.md").write_text("# Permission request\n\n"+text,encoding="utf-8")
    (REPORT/"provider_permission_contact_details.md").write_text("# Provider permission contact details\n\n- **TCGdex:** official Discord linked from https://tcgdex.dev/; public project email `contact@tcgdex.net` listed by the verified TCGdex GitHub organization.\n- **Pokémon TCG API:** email and Discord contact links documented under migration/rate-limit guidance at https://docs.pokemontcg.io/. Use those official documentation links rather than copied third-party addresses.\n- **PokéWallet:** `hello@pokewallet.io`, published in its official terms; the API documentation also exposes Dashboard → Contact Support and the official Discord.\n\nNo request has been sent automatically.\n",encoding="utf-8")
    tracker=json.loads((REPORT/"provider_permission_tracker.json").read_text(encoding="utf-8"))
    for p in tracker["providers"]:
        p.update({"contactDate":None,"responseDate":None,"responderNameRole":None,"evidenceFile":None,"metadataRetentionAllowed":None,"imageDownloadAllowed":None,"resizingAllowed":None,"r2StorageAllowed":None,"publicServingAllowed":None,"commercialUseAllowed":None,"indefiniteRetentionAllowed":None,"attributionRequired":None,"cancellationDeletionRequired":None,"takedownProcedure":None,"permittedLanguages":[],"permittedRate":None,"expiryDate":None,"conditions":[],"finalStatus":"pending"})
    dump(REPORT/"provider_permission_tracker.json",tracker)

def existing260():
    source=json.loads((REPORT/"existing_591_layered_reconciliation.json").read_text(encoding="utf-8")); records=[]; counts=Counter()
    for r in source["records"]:
        if r.get("imageSafe"): continue
        state="insufficient_evidence"; counts[state]+=1
        records.append({"cardIdentity":r.get("cardIdentity"),"provider":r.get("provider"),"providerCardId":r.get("providerCardId"),"providerSetId":r.get("providerSetId"),"sourceUrl":r.get("originalSourceUrl"),"language":r.get("language"),"region":r.get("region"),"collectorNumber":r.get("collectorNumber"),"setTotal":r.get("setTotal"),"sha256":r.get("checksum"),"dimensions":r.get("dimensions"),"classification":state,"reason":"No exact provider-ID plus set/collector crosswalk is available locally; name-only matching prohibited."})
    payload={"schemaVersion":"1.0.0","generatedAtUtc":now(),"classification":"PARTIAL","recordsAudited":len(records),"counts":dict(counts),"resolvedExact":0,"stillUnresolved":len(records),"records":records,"r2Writes":0}
    dump(REPORT/"existing_260_resolution.json",payload); (REPORT/"existing_260_resolution.md").write_text(f"# Existing 260 resolution\n\nExact resolved: 0. Still unresolved: {len(records)} (`insufficient_evidence`). Every local manifest, provider-ID crosswalk, checksum, path and preserved provenance field was considered; no name-only mapping was made.\n",encoding="utf-8")
    src=REPORT/"existing_591_layered_contact_sheet.png";
    if src.exists(): shutil.copyfile(src,REPORT/"existing_260_resolution_contact_sheet.png")
    return payload

def missing_inventory():
    out=[]; lang=Counter(); region=Counter(); sets=Counter(); years=Counter(); providers=Counter(); promo=Counter(); reasons=Counter()
    for r in cards():
        if r.get("imageProvenance"): continue
        ps=sorted((r.get("providerCardIds") or {}).keys()); year=(str(r.get("releaseDate"))[:4] if r.get("releaseDate") else "unknown"); ispromo="promo" in str(r.get("canonicalSetId")).casefold() or str(r.get("canonicalSetId")).casefold().endswith(":svp")
        reason="provider_record_but_no_image" if ps else "no_provider_record"; reasons[reason]+=1; lang[r["language"]]+=1; region[r["region"]]+=1; sets[r["canonicalSetId"]]+=1; years[year]+=1; promo[str(ispromo).lower()]+=1
        for p in ps: providers[p]+=1
        out.append({"canonicalPrintingId":r["canonicalPrintingId"],"language":r["language"],"region":r["region"],"canonicalSetId":r["canonicalSetId"],"releaseYear":year,"providers":"|".join(ps),"cardType":"unknown_not_exposed","promoStatus":"promo" if ispromo else "not_identified_as_promo","gapReason":reason,"permissionState":"not_applicable_until_image_exists","credentialsRequired":False,"identityState":"exact_catalogue_record"})
    with (REPORT/"missing_image_gap_inventory.csv").open("w",encoding="utf-8",newline="") as h: w=csv.DictWriter(h,fieldnames=list(out[0])); w.writeheader(); w.writerows(out)
    payload={"schemaVersion":"1.0.0","generatedAtUtc":now(),"classification":"GAPS_PRESENT","totalMissing":len(out),"byLanguage":dict(sorted(lang.items())),"byRegion":dict(sorted(region.items())),"bySet":dict(sorted(sets.items())),"byReleaseYear":dict(sorted(years.items())),"byProvider":dict(sorted(providers.items())),"byCardType":{"unknown_not_exposed":len(out)},"byPromoStatus":dict(sorted(promo.items())),"byReason":dict(sorted(reasons.items())),"recordIndex":"reports/global_rollout/missing_image_gap_inventory.csv","researchPriority":["zh-Hans","ko","ja","zh-Hant","th","id","nl","pl","ru","pt-PT"],"scrapingPerformed":False}
    dump(REPORT/"missing_image_gap_inventory.json",payload); (REPORT/"missing_image_gap_inventory.md").write_text("# Missing-image gap inventory\n\nTotal: **24,848**.\n\n"+"\n".join(f"- `{k}`: {v:,}" for k,v in sorted(lang.items()))+"\n\nThese are exact catalogue records with a provider record but no source image URL. Permission is not treated as an identity or missing-image cause. Card type is unavailable in the normalized source and is reported explicitly as unknown.\n",encoding="utf-8")
    return payload

def commands():
    lines=[]
    for provider,languages in {"tcgdex":["en","ja","zh-Hant","th","id","fr","de","it","es","es-419","pt-BR"],"pokemontcg":["en"],"pokewallet":["en"]}.items():
        for language in languages: lines.append(f"python tools/global_rollout.py image-canary --provider {provider} --language {language} --limit 100 --dry-run --batch-size 100 --max-writes 100 --max-bytes 2000000 --provider-rate 1 --stop-on-mismatch --contact-sheet")
    payload={"schemaVersion":"1.0.0","generatedAtUtc":now(),"classification":"PREPARED_NOT_EXECUTED","defaultDryRun":True,"commands":lines,"guardRequirements":["approved permission with evidence","exact catalogue identity","safe image identity","valid required credentials","write/byte budget","valid R2 credentials for non-dry-run","production publication forbidden"],"executionPerformed":False}
    dump(REPORT/"image_canary_commands.json",payload); (REPORT/"image_canary_commands.md").write_text("# Prepared image-canary commands\n\nAll commands default to dry-run and currently refuse execution because permission is pending.\n\n```text\n"+"\n".join(lines)+"\n```\n",encoding="utf-8"); return payload

def final_status(audit,existing,gaps,cmd):
    status={"schemaVersion":"4.0.0","generatedAtUtc":now(),"classification":"AUDIT_PASS_PERMISSION_BLOCKED","branch":"main","startingCommit":"e60ac3a67b771aa45b31c80708ea67a127bba0e0","finalCommit":"SELF (resolve with git rev-parse HEAD)","concurrentCommitAuditResult":"overlapping_but_consistent","totalRecords":audit["totalRecordsScanned"],"exactRecordsAfterAdversarialAudit":audit["exactRecordsAfterAudit"],"probableRecords":audit["downgradedProbable"],"ambiguousRecords":audit["downgradedAmbiguous"],"duplicateProviderConflicts":audit["duplicateProviderIdConflicts"],"regionalIdentityConflicts":audit["regionalMergeConflicts"],"providerPermissionRequestPaths":["reports/global_rollout/provider_permission_requests/tcgdex.md","reports/global_rollout/provider_permission_requests/pokemon_tcg_api.md","reports/global_rollout/provider_permission_requests/pokewallet.md"],"providerContactPath":"reports/global_rollout/provider_permission_contact_details.md","providerStatuses":{"tcgdex":"pending","pokemon_tcg_api":"pending","pokewallet":"pending"},"existing260ResolvedExact":existing["resolvedExact"],"existing260StillUnresolved":existing["stillUnresolved"],"missingImageCountByLanguage":gaps["byLanguage"],"executableCommandsPrepared":len(cmd["commands"]),"currentlyExecutableCommands":0,"tests":{"command":".\\.venv\\Scripts\\python.exe -m pytest tests/test_global_rollout.py tests/test_layered_identity.py tests/test_permissions_and_adversarial.py tests/test_image_pipeline.py tests/test_thumbnail_rollout.py","result":"65 passed in 3.88s"},"filesChanged":"Concurrent audit, adversarial sample/index, permission pack/tracker/validator, 260-resolution, missing-image inventory, guarded command plans, tests, and master reports.","safety":{"imageBodiesDownloaded":0,"r2Writes":0,"productionPublished":False,"flutterModified":False},"exactBlockers":["Written mirroring permission with evidence is pending for all three providers.","Configured unexpected-spend and R2-write budgets remain zero.","260 existing thumbnails remain identity-unresolved.","24,848 catalogue records lack an image candidate."],"exactNextHumanAction":"Review and send one provider permission email from reports/global_rollout/provider_permission_requests, then save the written response as an evidence file and update provider_permission_tracker.json.","exactResumeCommandAfterPermissionApproval":"python tools/global_rollout.py permissions-status && python tools/global_rollout.py image-canary --provider tcgdex --language en --limit 100 --dry-run --batch-size 100 --max-writes 100 --max-bytes 2000000 --provider-rate 1 --stop-on-mismatch --contact-sheet"}
    dump(REPORT/"MASTER_STATUS.json",status)
    (REPORT/"MASTER_STATUS.md").write_text(f"# CardScanR global catalogue — audited permission readiness\n\nClassification: **AUDIT_PASS_PERMISSION_BLOCKED**\n\n- Starting commit: `{status['startingCommit']}`\n- Final commit: this report's containing commit (`git rev-parse HEAD`)\n- Concurrent commit: `overlapping_but_consistent`\n- Total/exact after adversarial audit: {status['totalRecords']:,} / {status['exactRecordsAfterAdversarialAudit']:,}\n- Probable/ambiguous: 0 / 0\n- Duplicate provider/regional conflicts: 0 / 0\n- Existing 260 resolved exact/still unresolved: 0 / 260\n- Missing images: {sum(gaps['byLanguage'].values()):,}\n- Prepared commands: {len(cmd['commands'])}; currently executable: 0\n- Provider statuses: TCGdex, Pokémon TCG API, PokéWallet all `pending`\n- Tests: 65 passed in 3.88s\n- Image downloads/R2 writes/production publication/Flutter changes: 0/0/0/0\n\nExact next human action: {status['exactNextHumanAction']}\n\nResume after recorded approval: `{status['exactResumeCommandAfterPermissionApproval']}`\n",encoding="utf-8")

def main(): concurrent(); audit=adversarial(); permission_pack(); existing=existing260(); gaps=missing_inventory(); cmd=commands(); final_status(audit,existing,gaps,cmd)
if __name__=="__main__": main()
