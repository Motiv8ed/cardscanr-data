#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_image_pipeline.catalogue import DEFAULT_CATALOGUE_ROOT
from cardscanr_image_pipeline.gate_a_remediation import FAILED_GATE_A_IDS
from cardscanr_image_pipeline.gate_b_full_rollout import (
    automated_visual_preflight,
    build_canary_search_index,
    collect_verified_url_map,
    execute_remaining_75_rate_limited,
    flutter_compatibility_evidence,
    remaining_pokewallet_manifest,
    stage_catalogue_wiring,
    thumb_config,
    write_visual_review_checklist,
)
from cardscanr_image_pipeline.stage2_runner import Stage2Runner, build_contact_sheet, write_json_report
from cardscanr_image_pipeline.thumbnail_execute import (
    APPROVED_MANIFEST_PATH,
    count_supabase_thumbs,
    enrich_skipped_public_urls,
    filter_manifest_entries,
    load_approved_manifest,
    summarize_execution,
    verify_successful_cards,
)
from cardscanr_image_pipeline.thumbnail_rollout import RUNTIME_DIR, utc_now_iso
from cardscanr_market_engine.supabase_env_loader import load_supabase_env


def run_tests() -> dict[str, object]:
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromName("tests.test_thumbnail_rollout"))
    suite.addTests(loader.loadTestsFromName("tests.test_image_pipeline"))
    result = unittest.TextTestRunner(stream=open(os.devnull, "w"), verbosity=0).run(suite)
    return {
        "passed": result.wasSuccessful(),
        "testsRun": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gate B remaining 75 + 500 wiring canary")
    parser.add_argument(
        "command",
        choices=["all", "visual-gate", "remaining-75", "reconcile-500", "wire-catalogue", "canary-index", "final-report"],
    )
    parser.add_argument("--output-dir", default=str(RUNTIME_DIR))
    parser.add_argument("--manifest", default=str(APPROVED_MANIFEST_PATH))
    return parser


def main(argv: list[str] | None = None) -> int:
    load_supabase_env(str(ROOT / "supabase_env.local.json"))
    args = build_parser().parse_args(argv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    approved = load_approved_manifest(Path(args.manifest))

    if args.command in {"visual-gate", "all"}:
        checklist = write_visual_review_checklist(output_dir)
        preflight = automated_visual_preflight(output_dir)
        write_json_report(preflight, output_dir / "thumbnail_rollout_visual_preflight.json")
        print(f"Visual checklist: {checklist}")
        print(f"Human visual approval: {preflight['humanVisualApproval']}")
        print(f"Automated mismatch stop={preflight['shouldStop']} issues={preflight['issues']}")
        if preflight["shouldStop"]:
            return 2

    if args.command in {"remaining-75", "all"}:
        canary_path = output_dir / "thumbnail_rollout_gate_b_canary_manifest.json"
        if not canary_path.exists():
            print("Gate B canary manifest missing")
            return 2
        canary = json.loads(canary_path.read_text(encoding="utf-8"))
        remaining = remaining_pokewallet_manifest(approved, canary)
        write_json_report(remaining, output_dir / "thumbnail_rollout_gate_b_remaining75_manifest.json")
        print(f"Remaining PokeWallet cards: {remaining['cardCount']}")

        dry_runner = Stage2Runner(thumb_config(execute=False))
        dry = dry_runner.dry_run_manifest(remaining)
        stop = list(dry.get("stopReasons") or [])
        if dry.get("ambiguousCount"):
            stop.append("ambiguous_count_gt_0")
        for card in dry.get("cards") or []:
            if card.get("display_storage_path"):
                stop.append(f"display_webp_planned:{card.get('canonical_base_id')}")
        dry["stopReasons"] = stop
        dry["shouldStop"] = bool(stop)
        write_json_report(dry, output_dir / "thumbnail_rollout_gate_b_remaining75_dry_run.json")
        if dry["shouldStop"]:
            print(f"Dry-run stop: {stop}")
            return 1

        exec_runner = Stage2Runner(thumb_config(execute=True))
        execution = execute_remaining_75_rate_limited(exec_runner, remaining, output_dir=output_dir)
        execution["cards"] = enrich_skipped_public_urls(exec_runner, execution.get("cards") or [])
        execution["summary"] = summarize_execution(execution["cards"])
        unavailable = sum(
            1 for c in execution["cards"] if c.get("database_status") == "provider_image_unavailable"
        )
        execution["summary"]["providerImageUnavailable"] = unavailable
        # Treat CDN-unavailable as failed for the strict 75 PASS gate.
        execution["summary"]["failed"] = execution["summary"]["failed"] + unavailable
        write_json_report(execution, output_dir / "thumbnail_rollout_gate_b_remaining75_execute.json")
        print(
            f"Remaining75 uploaded={execution['summary']['uploaded']} "
            f"skipped={execution['summary']['skipped']} failed={execution['summary']['failed']} "
            f"unavailable={unavailable}"
        )
        if execution["summary"]["ambiguous"] or execution["summary"]["displayPathsPresent"]:
            write_json_report(
                {
                    "gate": "B_remaining75",
                    "classification": "FAIL",
                    "execution": execution["summary"],
                },
                output_dir / "thumbnail_rollout_gate_b_remaining75_report.json",
            )
            return 1

        success_cards = [
            c
            for c in execution["cards"]
            if c.get("database_status") in {"completed", "skipped"}
        ]
        success_ids = {c["canonical_base_id"] for c in success_cards}
        success_manifest = filter_manifest_entries(remaining, canonical_ids=success_ids)
        verification = verify_successful_cards(exec_runner, success_manifest, success_cards)
        write_json_report(verification, output_dir / "thumbnail_rollout_gate_b_remaining75_verify.json")
        expected = len(success_cards)
        passed_verify = bool(verification.get("passed")) and int(verification.get("verifiedCount") or 0) == expected

        # Idempotent rerun via Stage2Runner for success cards only.
        idem_raw = exec_runner.execute_manifest(success_manifest) if success_ids else {"cards": []}
        idem_cards = enrich_skipped_public_urls(exec_runner, idem_raw.get("cards") or [])
        idem_summary = summarize_execution(idem_cards)
        idem = {"cards": idem_cards, "summary": idem_summary, "raw": idem_raw}
        write_json_report(idem, output_dir / "thumbnail_rollout_gate_b_remaining75_idempotent.json")
        idem_ok = (
            not success_ids
            or (
                idem_summary["uploaded"] == 0
                and idem_summary["failed"] == 0
                and idem_summary["skipped"] == len(success_ids)
                and idem_summary["totalSourceBytes"] == 0
            )
        )

        # Full Gate B contact sheet = canary + remaining successes
        canary_exec = json.loads((output_dir / "thumbnail_rollout_gate_b_canary_execute.json").read_text(encoding="utf-8"))
        all_pw = filter_manifest_entries(approved, provider="pokewallet")
        all_cards = list(canary_exec.get("cards") or []) + list(success_cards)
        by_id = {c.get("canonical_base_id"): c for c in all_cards}
        ordered = []
        for entry in all_pw.get("entries") or []:
            card = by_id.get(entry["canonicalBaseId"])
            if card:
                ordered.append(card)
        contact = output_dir / "thumbnail_rollout_gate_b_full_contact_sheet.png"
        build_contact_sheet(ordered, output_path=contact, columns=10)

        classification = "PASS"
        if unavailable:
            classification = "PARTIAL"
        if not passed_verify or not idem_ok:
            classification = "FAIL"

        report = {
            "gate": "B_remaining75",
            "classification": classification,
            "attempted": execution["summary"]["attempted"],
            "uploaded": execution["summary"]["uploaded"],
            "skipped": execution["summary"]["skipped"],
            "failed": execution["summary"]["failed"],
            "providerImageUnavailable": unavailable,
            "verified": verification.get("verifiedCount"),
            "exhaustedIds": execution.get("exhaustedIds") or [],
            "idempotent": {
                "passed": idem_ok,
                "uploaded": idem_summary["uploaded"],
                "skipped": idem_summary["skipped"],
                "totalSourceBytes": idem_summary["totalSourceBytes"],
            },
            "rateLimit": execution.get("rateLimit"),
            "contactSheetPath": str(contact),
            "importDisplay": False,
        }
        write_json_report(report, output_dir / "thumbnail_rollout_gate_b_remaining75_report.json")
        md = output_dir / "thumbnail_rollout_gate_b_remaining75_report.md"
        md.write_text(
            "\n".join(
                [
                    "# Gate B Remaining 75 Report",
                    "",
                    f"- Classification: **{report['classification']}**",
                    f"- Attempted/uploaded/skipped/failed/verified: "
                    f"{report['attempted']}/{report['uploaded']}/{report['skipped']}/{report['failed']}/{report['verified']}",
                    f"- Provider image unavailable: {unavailable}",
                    f"- Exhausted IDs: {report['exhaustedIds']}",
                    f"- Idempotent: {report['idempotent']}",
                    f"- Rate-limit events: {(report.get('rateLimit') or {}).get('eventCount')}",
                    f"- Total wait seconds: {(report.get('rateLimit') or {}).get('totalWaitSeconds')}",
                    f"- Contact sheet: `{contact}`",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        print(f"Remaining75 classification={report['classification']}")
        if classification == "FAIL":
            return 1
        # PARTIAL continues to reconcile with honest counts; wiring requires full 500 PASS.
        if args.command == "remaining-75":
            return 0 if classification in {"PASS", "PARTIAL"} else 1

    if args.command in {"reconcile-500", "all"}:
        rem_report = json.loads((output_dir / "thumbnail_rollout_gate_b_remaining75_report.json").read_text(encoding="utf-8"))
        if rem_report.get("classification") not in {"PASS", "PARTIAL"}:
            print("Remaining 75 not PASS/PARTIAL; refuse reconcile")
            return 2
        runner = Stage2Runner(thumb_config(execute=False))
        replacements = json.loads((output_dir / "thumbnail_rollout_gate_a_replacements_9.json").read_text(encoding="utf-8"))
        replacement_ids = {e["canonicalBaseId"] for e in (replacements.get("entries") or [])}
        approved_ids = {e["canonicalBaseId"] for e in approved["entries"]}
        # Intended 500 = approved minus 9 Gate A CDN failures plus 9 replacements.
        intended_ids = (approved_ids - set(FAILED_GATE_A_IDS)) | replacement_ids
        url_map = collect_verified_url_map(runner, canonical_ids=intended_ids)
        verified_ids = set(url_map)
        pw_unavailable = list(rem_report.get("exhaustedIds") or [])
        missing = sorted(intended_ids - verified_ids)

        cards_for_sheet = []
        pw_ids = {e["canonicalBaseId"] for e in filter_manifest_entries(approved, provider="pokewallet")["entries"]}
        for cid in sorted(verified_ids):
            parts = cid.split("|")
            cards_for_sheet.append(
                {
                    "canonical_base_id": cid,
                    "language": parts[1] if len(parts) > 1 else "en",
                    "set_id": parts[2] if len(parts) > 2 else "?",
                    "collector_number": parts[3] if len(parts) > 3 else "?",
                    "provider": "pokewallet" if cid in pw_ids else "pokemon_tcg_api",
                    "database_status": "completed",
                    "thumb_public_url": url_map[cid],
                }
            )
        combined = output_dir / "thumbnail_rollout_500_combined_contact_sheet.png"
        build_contact_sheet(cards_for_sheet, output_path=combined, columns=20)

        counts = count_supabase_thumbs(runner)
        total_bytes = 0
        for cid in verified_ids:
            rec = runner.db.get_record(cid)
            total_bytes += int((rec or {}).get("thumb_bytes") or 0)
        avg_bytes = int(total_bytes / len(verified_ids)) if verified_ids else 0

        gate_b_verified = sum(1 for cid in verified_ids if cid in pw_ids)
        gate_a_verified = len(verified_ids) - gate_b_verified
        classification = "PASS" if len(verified_ids) == 500 and not missing else "PARTIAL"

        reconcile = {
            "generatedAtUtc": utc_now_iso(),
            "classification": classification,
            "gateAVerifiedSample": gate_a_verified,
            "gateBVerifiedSample": gate_b_verified,
            "totalVerifiedRolloutSample": len(verified_ids),
            "intendedSampleCount": 500,
            "providerUnavailableOriginals": sorted(FAILED_GATE_A_IDS),
            "pokewalletProviderUnavailable": pw_unavailable,
            "missingVerifiedIds": missing,
            "ambiguousExecutions": 0,
            "wrongPrintFindingsAutomated": 0,
            "providerBreakdownSample": {
                "pokemon_tcg_api": gate_a_verified,
                "pokewallet": gate_b_verified,
            },
            "supabaseTotals": counts,
            "totalStoredThumbnailBytesSample": total_bytes,
            "averageThumbnailBytesSample": avg_bytes,
            "combinedContactSheetPath": str(combined),
            "verifiedCanonicalBaseIds": sorted(verified_ids),
            "urlMapPath": str(output_dir / "thumbnail_rollout_500_url_map.json"),
            "remaining75Classification": rem_report.get("classification"),
        }
        write_json_report({"generatedAtUtc": utc_now_iso(), "urls": url_map}, output_dir / "thumbnail_rollout_500_url_map.json")
        write_json_report(reconcile, output_dir / "thumbnail_rollout_500_reconciled_report.json")
        print(f"Reconciled sample={len(verified_ids)} classification={classification}; contact sheet={combined}")
        if args.command == "reconcile-500":
            return 0 if classification in {"PASS", "PARTIAL"} else 1

    if args.command in {"wire-catalogue", "all"}:
        reconcile = json.loads((output_dir / "thumbnail_rollout_500_reconciled_report.json").read_text(encoding="utf-8"))
        if reconcile.get("classification") != "PASS" or int(reconcile.get("totalVerifiedRolloutSample") or 0) != 500:
            print(
                "Full 500-card technical PASS required for catalogue wiring; "
                f"got classification={reconcile.get('classification')} "
                f"verified={reconcile.get('totalVerifiedRolloutSample')}. Skipping wiring/index."
            )
            # Still write a final PARTIAL report path marker.
            if args.command == "wire-catalogue":
                return 2
            # Fall through to final-report with skipped wiring.
        else:
            url_map = json.loads((output_dir / "thumbnail_rollout_500_url_map.json").read_text(encoding="utf-8"))["urls"]
            staged_root = output_dir / "thumbnail_rollout_staged_catalogue" / "v1"
            wiring = stage_catalogue_wiring(
                catalogue_root=DEFAULT_CATALOGUE_ROOT,
                staged_root=staged_root,
                url_map=url_map,
            )
            write_json_report(wiring, output_dir / "thumbnail_rollout_500_catalogue_wiring_report.json")
            write_json_report(
                {
                    "generatedAtUtc": utc_now_iso(),
                    "modifiedCardCount": wiring["modifiedCardCount"],
                    "imageCachedTrueCount": wiring["imageCachedTrueCount"],
                    "changes": wiring["changes"],
                    "productionCataloguePublished": False,
                },
                output_dir / "thumbnail_rollout_500_catalogue_change_report.json",
            )
            print(f"Catalogue wiring staged: {wiring['modifiedCardCount']} cards at {staged_root}")
            if args.command == "wire-catalogue":
                return 0

    if args.command in {"canary-index", "all"}:
        wiring_path = output_dir / "thumbnail_rollout_500_catalogue_wiring_report.json"
        if not wiring_path.exists():
            print("Catalogue wiring skipped; skipping canary index and Flutter evidence")
            if args.command == "canary-index":
                return 2
        else:
            staged_root = output_dir / "thumbnail_rollout_staged_catalogue" / "v1"
            canary_dir = output_dir / "thumbnail_rollout_search_canary"
            canary = build_canary_search_index(staged_catalogue_root=staged_root, output_dir=canary_dir)
            write_json_report(canary, output_dir / "thumbnail_rollout_search_canary_report.json")
            print(f"Canary search index sha256={canary.get('sha256')}")

            url_map = json.loads((output_dir / "thumbnail_rollout_500_url_map.json").read_text(encoding="utf-8"))["urls"]
            flutter = flutter_compatibility_evidence(url_map=url_map, canary_index=canary, output_dir=output_dir)
            print(f"Flutter compatibility={flutter['classification']} blocker={flutter.get('blocker')}")
            if args.command == "canary-index":
                return 0 if canary.get("verify") else 1

    if args.command in {"final-report", "all"}:
        rem = json.loads((output_dir / "thumbnail_rollout_gate_b_remaining75_report.json").read_text(encoding="utf-8"))
        reconcile = json.loads((output_dir / "thumbnail_rollout_500_reconciled_report.json").read_text(encoding="utf-8"))
        wiring_path = output_dir / "thumbnail_rollout_500_catalogue_wiring_report.json"
        wiring = json.loads(wiring_path.read_text(encoding="utf-8")) if wiring_path.exists() else {}
        canary_path = output_dir / "thumbnail_rollout_search_canary_report.json"
        canary = json.loads(canary_path.read_text(encoding="utf-8")) if canary_path.exists() else {}
        flutter_path = output_dir / "thumbnail_rollout_flutter_compatibility_evidence.json"
        flutter = json.loads(flutter_path.read_text(encoding="utf-8")) if flutter_path.exists() else {
            "classification": "PARTIAL",
            "blockers": ["catalogue_wiring_and_canary_index_skipped_due_to_incomplete_500"],
            "deviceDiskCacheProven": False,
            "offlineRenderProven": False,
            "blocker": "catalogue_wiring_and_canary_index_skipped_due_to_incomplete_500",
        }
        tests = run_tests()
        runner = Stage2Runner(thumb_config(execute=False))
        totals = count_supabase_thumbs(runner)

        defects = [f"{cid}: provider_metadata_exists_but_image_cdn_unavailable (Gate A retained)" for cid in sorted(FAILED_GATE_A_IDS)]
        for cid in rem.get("exhaustedIds") or []:
            defects.append(f"{cid}: pokewallet_provider_image_cdn_unavailable (HTTP 404)")
        defects.extend(flutter.get("blockers") or [])

        classification = "PASS"
        if rem.get("classification") == "FAIL" or reconcile.get("classification") == "FAIL" or not tests.get("passed"):
            classification = "FAIL"
        elif (
            rem.get("classification") == "PARTIAL"
            or reconcile.get("classification") == "PARTIAL"
            or flutter.get("classification") == "PARTIAL"
            or not wiring
        ):
            classification = "PARTIAL"

        en_chain = 23375
        avg = reconcile.get("averageThumbnailBytesSample") or 12952
        projected = int(avg) * en_chain

        report = {
            "classification": classification,
            "generatedAtUtc": utc_now_iso(),
            "remainingPokeWallet75": {
                "attempted": rem.get("attempted"),
                "skipped": rem.get("skipped"),
                "uploaded": rem.get("uploaded"),
                "verified": rem.get("verified"),
                "failed": rem.get("failed"),
                "providerImageUnavailable": rem.get("providerImageUnavailable"),
            },
            "rateLimitPauses": (rem.get("rateLimit") or {}).get("eventCount"),
            "rateLimitTotalWaitSeconds": (rem.get("rateLimit") or {}).get("totalWaitSeconds"),
            "gateBCompleteVerifiedCount": reconcile.get("gateBVerifiedSample"),
            "fullRolloutVerifiedCount": reconcile.get("totalVerifiedRolloutSample"),
            "combinedContactSheetPath": reconcile.get("combinedContactSheetPath"),
            "totalSupabaseThumbnails": totals.get("combinedVerifiedOrCompletedWithThumb"),
            "providerBreakdown": totals.get("providerBreakdown"),
            "totalThumbnailBytesSample": reconcile.get("totalStoredThumbnailBytesSample"),
            "averageThumbnailBytes": reconcile.get("averageThumbnailBytesSample"),
            "projectedFullEnglishStorageBytes": projected,
            "cataloguePatchPath": wiring.get("stagedRoot"),
            "imageCachedTrueChanges": wiring.get("imageCachedTrueCount") or 0,
            "canarySearchIndexPath": canary.get("sqlitePath"),
            "canarySearchIndexSha256": canary.get("sha256"),
            "flutterCompatibilityResult": flutter.get("classification"),
            "cacheOfflineResult": {
                "deviceDiskCacheProven": flutter.get("deviceDiskCacheProven"),
                "offlineRenderProven": flutter.get("offlineRenderProven"),
                "blocker": flutter.get("blocker"),
            },
            "exactUnresolvedDefects": defects,
            "tests": tests,
            "fullDisplayImageImportRun": False,
            "productionCataloguePublished": False,
            "productionSearchIndexReplaced": False,
            "flutterModified": False,
            "fullEnglishImportRun": False,
            "catalogueWiringSkipped": not bool(wiring),
            "jsonReportPath": str(output_dir / "thumbnail_rollout_500_final_report.json"),
            "markdownReportPath": str(output_dir / "thumbnail_rollout_500_final_report.md"),
        }
        write_json_report(report, output_dir / "thumbnail_rollout_500_final_report.json")
        md_lines = [
            "# Thumbnail Rollout — 500-card Final Report",
            "",
            f"- Classification: **{classification}**",
            f"- Remaining 75: {report['remainingPokeWallet75']}",
            f"- Rate-limit pauses/wait: {report['rateLimitPauses']} / {report['rateLimitTotalWaitSeconds']}s",
            f"- Gate B complete verified: {report['gateBCompleteVerifiedCount']}",
            f"- Full rollout verified: {report['fullRolloutVerifiedCount']}",
            f"- Combined contact sheet: `{report['combinedContactSheetPath']}`",
            f"- Total Supabase thumbs: {report['totalSupabaseThumbnails']}",
            f"- Provider breakdown: {report['providerBreakdown']}",
            f"- Sample total/avg thumb bytes: {report['totalThumbnailBytesSample']} / {report['averageThumbnailBytes']}",
            f"- Projected full-English storage: {report['projectedFullEnglishStorageBytes']}",
            f"- Catalogue patch: `{report['cataloguePatchPath']}`",
            f"- imageCached=true changes: {report['imageCachedTrueChanges']}",
            f"- Canary search index: `{report['canarySearchIndexPath']}`",
            f"- Canary SHA-256: `{report['canarySearchIndexSha256']}`",
            f"- Flutter compatibility: {report['flutterCompatibilityResult']}",
            f"- Cache/offline: {report['cacheOfflineResult']}",
            f"- Catalogue wiring skipped: **{report['catalogueWiringSkipped']}**",
            f"- Tests: {report['tests']}",
            f"- Display images imported: **False**",
            f"- Production catalogue published: **False**",
            f"- Production search index replaced: **False**",
            f"- Flutter modified: **False**",
            f"- Full English import: **False**",
            "",
            "## Unresolved defects",
            "",
        ]
        for d in report["exactUnresolvedDefects"]:
            md_lines.append(f"- {d}")
        md_lines.append("")
        md_lines.append("Stopped after this report. Full English thumbnail import not started.")
        (output_dir / "thumbnail_rollout_500_final_report.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        print(f"Classification={classification}")
        print(f"Wrote {output_dir / 'thumbnail_rollout_500_final_report.json'}")
        return 0 if classification in {"PASS", "PARTIAL"} else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
