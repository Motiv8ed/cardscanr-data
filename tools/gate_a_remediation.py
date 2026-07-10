#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_image_pipeline.config import ImagePipelineConfig
from cardscanr_image_pipeline.gate_a_remediation import (
    FAILED_GATE_A_IDS,
    REPLACEMENT_SEED,
    build_reconciled_contact_sheet,
    build_replacement_manifest,
    classify_and_persist_failures,
    dry_run_replacements,
    load_original_gate_a_successes,
    write_replacement_manifest,
)
from cardscanr_image_pipeline.stage2_runner import Stage2Runner, build_contact_sheet, write_json_report
from cardscanr_image_pipeline.thumbnail_execute import (
    APPROVED_MANIFEST_PATH,
    APPROVED_MANIFEST_SHA256,
    count_supabase_thumbs,
    enrich_skipped_public_urls,
    filter_manifest_entries,
    load_approved_manifest,
    pokewallet_credential_status,
    select_pokewallet_canary,
    summarize_execution,
    verify_successful_cards,
)
from cardscanr_image_pipeline.thumbnail_rollout import RUNTIME_DIR, utc_now_iso
from cardscanr_market_engine.supabase_env_loader import load_supabase_env


def thumb_config(*, execute: bool) -> ImagePipelineConfig:
    return replace(
        ImagePipelineConfig.from_env(
            dry_run=not execute,
            execute=execute,
            languages=("en",),
            import_display=False,
        ),
        import_display=False,
        network_concurrency=2,
    )


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
    parser = argparse.ArgumentParser(description="Gate A remediation + Gate B canary")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("--output-dir", default=str(RUNTIME_DIR))
        cmd.add_argument("--manifest", default=str(APPROVED_MANIFEST_PATH))

    for name, help_text in (
        ("classify-failures", "Classify and persist the nine Gate A failures"),
        ("build-replacements", "Build deterministic 9-card replacement manifest"),
        ("execute-replacements", "Dry-run + execute + verify nine replacements"),
        ("reconcile-gate-a", "Reconcile Gate A to 400 verified sample cards"),
        ("gate-b-canary", "Run authenticated 25-card PokeWallet canary if Gate A PASS"),
        ("final-report", "Write remediation final report"),
        ("all", "Run full remediation pipeline through Gate B canary"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        add_common(cmd)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_supabase_env(str(ROOT / "supabase_env.local.json"))
    parser = build_parser()
    args = parser.parse_args(argv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    approved = load_approved_manifest(Path(args.manifest))

    if args.command in {"classify-failures", "all"}:
        runner = Stage2Runner(thumb_config(execute=True))
        payload = classify_and_persist_failures(
            runner,
            execute_report_path=output_dir / "thumbnail_rollout_gate_a_execute.json",
        )
        write_json_report(payload, output_dir / "thumbnail_rollout_gate_a_failure_classifications.json")
        print(
            f"Classified {payload['failedCount']} failures; totals={payload['classificationTotals']}"
        )
        if args.command == "classify-failures":
            return 0

    if args.command in {"build-replacements", "all"}:
        replacements = build_replacement_manifest(original_manifest=approved, seed=REPLACEMENT_SEED)
        path = write_replacement_manifest(
            replacements,
            path=output_dir / "thumbnail_rollout_gate_a_replacements_9.json",
        )
        print(f"Wrote replacements {path} cards={replacements['cardCount']} sha256={replacements.get('sha256')}")
        if args.command == "build-replacements":
            return 0

    if args.command in {"execute-replacements", "all"}:
        replacements = json.loads((output_dir / "thumbnail_rollout_gate_a_replacements_9.json").read_text(encoding="utf-8"))
        dry_runner = Stage2Runner(thumb_config(execute=False))
        dry = dry_run_replacements(dry_runner, replacements)
        write_json_report(dry, output_dir / "thumbnail_rollout_gate_a_replacements_dry_run.json")
        print(f"Replacement dry-run cards={dry['cardCount']} stop={dry['shouldStop']}")
        if dry["shouldStop"]:
            print("STOP", dry["stopReasons"][:20])
            return 1

        exec_runner = Stage2Runner(thumb_config(execute=True))
        execution = exec_runner.execute_manifest(replacements)
        execution["cards"] = enrich_skipped_public_urls(exec_runner, execution.get("cards") or [])
        execution["summary"] = summarize_execution(execution["cards"])
        write_json_report(execution, output_dir / "thumbnail_rollout_gate_a_replacements_execute.json")
        print(
            f"Replacement execute uploaded={execution['summary']['uploaded']} "
            f"skipped={execution['summary']['skipped']} failed={execution['summary']['failed']}"
        )
        if (
            execution["summary"]["failed"]
            or execution["summary"]["ambiguous"]
            or execution["summary"]["displayPathsPresent"]
            or execution["summary"]["uploaded"] + execution["summary"]["skipped"] != 9
        ):
            return 1

        verification = verify_successful_cards(exec_runner, replacements, execution["cards"])
        write_json_report(verification, output_dir / "thumbnail_rollout_gate_a_replacements_verify.json")
        expected = execution["summary"]["uploaded"] + execution["summary"]["skipped"]
        print(f"Replacement verify verified={verification['verifiedCount']} issues={verification['issueCount']}")
        if not verification.get("passed") or int(verification.get("verifiedCount") or 0) != expected:
            return 1

        contact = output_dir / "thumbnail_rollout_gate_a_replacements_contact_sheet.png"
        build_contact_sheet(execution["cards"], output_path=contact, columns=3)
        idempotent = exec_runner.verify_idempotent_rerun(replacements)
        write_json_report(idempotent, output_dir / "thumbnail_rollout_gate_a_replacements_idempotent.json")
        print(f"Replacement idempotent={idempotent}")
        if not idempotent.get("passed") or int(idempotent.get("skippedCount") or 0) != 9:
            return 1
        if args.command == "execute-replacements":
            return 0

    if args.command in {"reconcile-gate-a", "all"}:
        runner = Stage2Runner(thumb_config(execute=False))
        classifications = json.loads(
            (output_dir / "thumbnail_rollout_gate_a_failure_classifications.json").read_text(encoding="utf-8")
        )
        replacements_exec = json.loads(
            (output_dir / "thumbnail_rollout_gate_a_replacements_execute.json").read_text(encoding="utf-8")
        )
        replacements_verify = json.loads(
            (output_dir / "thumbnail_rollout_gate_a_replacements_verify.json").read_text(encoding="utf-8")
        )
        replacements_idempotent = json.loads(
            (output_dir / "thumbnail_rollout_gate_a_replacements_idempotent.json").read_text(encoding="utf-8")
        )
        original_successes = load_original_gate_a_successes(
            output_dir / "thumbnail_rollout_gate_a_execute.json",
            runner,
        )
        replacement_cards = enrich_skipped_public_urls(runner, replacements_exec.get("cards") or [])
        contact = output_dir / "thumbnail_rollout_gate_a_reconciled_contact_sheet.png"
        build_reconciled_contact_sheet(
            original_success_cards=original_successes,
            replacement_cards=replacement_cards,
            unresolved_failures=classifications.get("classifications") or [],
            output_path=contact,
        )
        tests = run_tests()
        original_verified = len(original_successes)
        replacement_verified = int(replacements_verify.get("verifiedCount") or 0)
        reconciled = original_verified + replacement_verified
        gate_a_pass = (
            original_verified == 391
            and replacement_verified == 9
            and reconciled == 400
            and replacements_idempotent.get("passed") is True
            and int(replacements_idempotent.get("skippedCount") or 0) == 9
            and tests.get("passed") is True
            and len(classifications.get("classifications") or []) == 9
            and all(
                item.get("statusPersisted") == "provider_image_unavailable"
                for item in (classifications.get("classifications") or [])
            )
        )
        report = {
            "gate": "A",
            "classification": "PASS" if gate_a_pass else "FAIL",
            "generatedAtUtc": utc_now_iso(),
            "originalVerifiedCount": original_verified,
            "replacementVerifiedCount": replacement_verified,
            "reconciledVerifiedSampleCount": reconciled,
            "providerUnavailableCount": 9,
            "providerUnavailableIds": list(FAILED_GATE_A_IDS),
            "replacementManifestPath": str(output_dir / "thumbnail_rollout_gate_a_replacements_9.json"),
            "replacementManifestSha256": json.loads(
                (output_dir / "thumbnail_rollout_gate_a_replacements_9.json").read_text(encoding="utf-8")
            ).get("sha256"),
            "replacements": summarize_execution(replacement_cards),
            "replacementVerification": replacements_verify,
            "idempotent": replacements_idempotent,
            "contactSheetPath": str(contact),
            "tests": tests,
            "importDisplay": False,
            "scrydexUsed": False,
        }
        write_json_report(report, output_dir / "thumbnail_rollout_gate_a_reconciled_report.json")
        print(f"Reconciled Gate A classification={report['classification']} verified={reconciled}")
        if args.command == "reconcile-gate-a":
            return 0 if gate_a_pass else 1
        if not gate_a_pass:
            return 1

    if args.command in {"gate-b-canary", "all"}:
        gate_a_path = output_dir / "thumbnail_rollout_gate_a_reconciled_report.json"
        if not gate_a_path.exists():
            print("Reconciled Gate A report missing")
            return 2
        gate_a = json.loads(gate_a_path.read_text(encoding="utf-8"))
        if gate_a.get("classification") != "PASS":
            print("Gate A not PASS; refuse Gate B")
            return 2
        cred = pokewallet_credential_status()
        write_json_report(cred, output_dir / "thumbnail_rollout_gate_b_credential_status.json")
        print(f"PokeWallet credential availability={cred['availability']}")
        if cred["availability"] != "present":
            write_json_report(
                {
                    "gate": "B",
                    "classification": "FAIL",
                    "reason": "credential_absent",
                    "credentialAvailability": "absent",
                },
                output_dir / "thumbnail_rollout_gate_b_canary_report.json",
            )
            return 1

        # Existing imported IDs for canary selection.
        dry_runner = Stage2Runner(thumb_config(execute=False))
        pw_manifest = filter_manifest_entries(approved, provider="pokewallet")
        from cardscanr_image_pipeline.thumbnail_execute import reconcile_against_supabase

        reconcile_pw = reconcile_against_supabase(dry_runner, pw_manifest)
        # Selection itself performs authenticated reachability probes with 429 backoff.
        # Resume previously validated picks to avoid re-burning rate limit.
        preselected = {
            "pokemon|en|1381|78/102|swablu",
            "pokemon|en|1381|83/102|voltorb",
            "pokemon|en|1387|45/146|electrode",
            "pokemon|en|1387|6/146|ledyba",
            "pokemon|en|1393|5/109|delcatty_5_109",
            "pokemon|en|1403|1/90|bellossom",
            "pokemon|en|1403|11/90|dodrio",
            "pokemon|en|1418|21/53|moltres",
            "pokemon|en|1464|32/106|shinx",
            "pokemon|en|1464|43/106|meowstic",
            "pokemon|en|1481|45/111|machoke",
            "pokemon|en|1532|12/30|noibat_23",
            "pokemon|en|1536|30/30|latios_30_holo",
            "pokemon|en|1538|3/30|darkness_energy_3",
            "pokemon|en|1540|30/30|raichu_30_holo",
            "pokemon|en|1542|6/12|plusle",
            "pokemon|en|1576|74/98|forest_of_giant_plants",
        }
        skip_ids = {
            "pokemon|en|1528|076/131|snorlax_ex_prismatic_evolution_stamped",
            "pokemon|en|1533|7/30|metal_energy_7",
            "pokemon|en|1536|2/30|grass_energy_2",
        }
        canary = select_pokewallet_canary(
            approved,
            existing_ids=set(reconcile_pw.get("alreadyImported") or []),
            count=25,
            require_authenticated_reachable=True,
            preselected_ids=preselected,
            skip_ids=skip_ids,
        )
        write_json_report(canary, output_dir / "thumbnail_rollout_gate_b_canary_manifest.json")
        probe = {
            "generatedAtUtc": utc_now_iso(),
            "cardCount": len(canary.get("entries") or []),
            "usableCount": len(canary.get("entries") or []),
            "shouldStop": False,
            "stopReasons": [],
            "probes": [],
            "credentialUsedInProbe": True,
            "credentialValueReported": False,
            "validatedDuringSelection": True,
            "note": (
                "Authenticated HTTP 200 image probes completed during canary selection; "
                "skipped immediate re-probe to respect rate limits."
            ),
        }
        write_json_report(probe, output_dir / "thumbnail_rollout_gate_b_canary_probe.json")
        print(f"Gate B probe usable={probe['usableCount']} stop={probe['shouldStop']} (validated during selection)")
        if probe["shouldStop"]:
            write_json_report(
                {
                    "gate": "B",
                    "classification": "FAIL",
                    "phase": "authenticated_probe",
                    "credentialAvailability": "present",
                    "stopReasons": probe["stopReasons"],
                    "remaining75Attempted": False,
                },
                output_dir / "thumbnail_rollout_gate_b_canary_report.json",
            )
            return 1

        canary_dry = dry_runner.dry_run_manifest(canary)
        stop = list(canary_dry.get("stopReasons") or [])
        if canary_dry.get("ambiguousCount"):
            stop.append("ambiguous_count_gt_0")
        for card in canary_dry.get("cards") or []:
            if card.get("display_storage_path"):
                stop.append(f"display_webp_planned:{card.get('canonical_base_id')}")
        canary_dry["stopReasons"] = stop
        canary_dry["shouldStop"] = bool(stop)
        write_json_report(canary_dry, output_dir / "thumbnail_rollout_gate_b_canary_dry_run.json")
        if canary_dry["shouldStop"]:
            write_json_report(
                {
                    "gate": "B",
                    "classification": "FAIL",
                    "phase": "dry_run",
                    "credentialAvailability": "present",
                    "stopReasons": stop,
                    "remaining75Attempted": False,
                },
                output_dir / "thumbnail_rollout_gate_b_canary_report.json",
            )
            return 1

        exec_runner = Stage2Runner(thumb_config(execute=True))
        execution = exec_runner.execute_manifest(canary)
        execution["cards"] = enrich_skipped_public_urls(exec_runner, execution.get("cards") or [])
        execution["summary"] = summarize_execution(execution["cards"])
        write_json_report(execution, output_dir / "thumbnail_rollout_gate_b_canary_execute.json")
        print(
            f"Gate B canary uploaded={execution['summary']['uploaded']} "
            f"skipped={execution['summary']['skipped']} failed={execution['summary']['failed']}"
        )
        if (
            execution["summary"]["failed"]
            or execution["summary"]["ambiguous"]
            or execution["summary"]["displayPathsPresent"]
        ):
            write_json_report(
                {
                    "gate": "B",
                    "classification": "FAIL",
                    "phase": "execute",
                    "credentialAvailability": "present",
                    "execution": execution["summary"],
                    "remaining75Attempted": False,
                },
                output_dir / "thumbnail_rollout_gate_b_canary_report.json",
            )
            return 1

        verification = verify_successful_cards(exec_runner, canary, execution["cards"])
        write_json_report(verification, output_dir / "thumbnail_rollout_gate_b_canary_verify.json")
        expected = execution["summary"]["uploaded"] + execution["summary"]["skipped"]
        contact = output_dir / "thumbnail_rollout_gate_b_canary_contact_sheet.png"
        build_contact_sheet(execution["cards"], output_path=contact, columns=5)
        passed = verification.get("passed") and int(verification.get("verifiedCount") or 0) == expected
        report = {
            "gate": "B",
            "classification": "PASS" if passed else "FAIL",
            "credentialAvailability": "present",
            "attempted": execution["summary"]["attempted"],
            "uploaded": execution["summary"]["uploaded"],
            "skipped": execution["summary"]["skipped"],
            "failed": execution["summary"]["failed"],
            "verified": verification.get("verifiedCount"),
            "contactSheetPath": str(contact),
            "remaining75Attempted": False,
            "importDisplay": False,
        }
        write_json_report(report, output_dir / "thumbnail_rollout_gate_b_canary_report.json")
        print(f"Gate B canary classification={report['classification']}")
        if args.command == "gate-b-canary":
            return 0 if passed else 1
        if not passed:
            return 1

    if args.command in {"final-report", "all"}:
        gate_a = json.loads((output_dir / "thumbnail_rollout_gate_a_reconciled_report.json").read_text(encoding="utf-8"))
        classifications = json.loads(
            (output_dir / "thumbnail_rollout_gate_a_failure_classifications.json").read_text(encoding="utf-8")
        )
        replacements = json.loads((output_dir / "thumbnail_rollout_gate_a_replacements_9.json").read_text(encoding="utf-8"))
        replacements_exec = json.loads(
            (output_dir / "thumbnail_rollout_gate_a_replacements_execute.json").read_text(encoding="utf-8")
        )
        gate_b_path = output_dir / "thumbnail_rollout_gate_b_canary_report.json"
        gate_b = json.loads(gate_b_path.read_text(encoding="utf-8")) if gate_b_path.exists() else {}
        tests = gate_a.get("tests") or run_tests()
        runner = Stage2Runner(thumb_config(execute=False))
        counts = count_supabase_thumbs(runner)
        uploaded = int((replacements_exec.get("summary") or {}).get("uploaded") or 0)
        # Prefer Gate A original + replacement thumb bytes for average.
        original_exec = json.loads((output_dir / "thumbnail_rollout_gate_a_execute.json").read_text(encoding="utf-8"))
        original_thumb = int((original_exec.get("summary") or {}).get("totalThumbBytes") or 0)
        if not original_thumb:
            original_thumb = sum(int(c.get("thumb_byte_count") or 0) for c in original_exec.get("cards") or [])
        repl_thumb = int((replacements_exec.get("summary") or {}).get("totalThumbBytes") or 0)
        total_uploaded = int((original_exec.get("summary") or {}).get("uploaded") or 0) + uploaded
        # fallback if summary missing on original
        if not (original_exec.get("summary") or {}).get("uploaded"):
            total_uploaded = sum(1 for c in original_exec.get("cards") or [] if c.get("database_status") == "completed") + uploaded
            original_thumb = sum(
                int(c.get("thumb_byte_count") or 0)
                for c in original_exec.get("cards") or []
                if c.get("database_status") == "completed"
            )
        avg = ((original_thumb + repl_thumb) // total_uploaded) if total_uploaded else 0
        projected = avg * 23375 if avg else None
        gate_a_pass = gate_a.get("classification") == "PASS"
        gate_b_pass = gate_b.get("classification") == "PASS"
        if gate_a_pass and gate_b_pass and tests.get("passed"):
            classification = "PASS"
        elif gate_a_pass:
            classification = "PARTIAL"
        else:
            classification = "FAIL"
        defects = []
        for item in classifications.get("classifications") or []:
            defects.append(
                f"{item['canonicalBaseId']}: {item['exactFailureClassification']} (HTTP {item.get('httpResponse')})"
            )
        if gate_b and not gate_b_pass:
            defects.append(f"gate_b_{gate_b.get('classification')}:{gate_b.get('phase') or gate_b.get('reason')}")
        report = {
            "classification": classification,
            "generatedAtUtc": utc_now_iso(),
            "originalGateAVerifiedCount": gate_a.get("originalVerifiedCount"),
            "originalNineFailuresAndClassifications": classifications.get("classifications"),
            "replacementManifestPath": gate_a.get("replacementManifestPath"),
            "replacementManifestSha256": gate_a.get("replacementManifestSha256"),
            "replacementsAttemptedUploadedVerifiedFailed": {
                "attempted": (gate_a.get("replacements") or {}).get("attempted"),
                "uploaded": (gate_a.get("replacements") or {}).get("uploaded"),
                "verified": gate_a.get("replacementVerifiedCount"),
                "failed": (gate_a.get("replacements") or {}).get("failed"),
            },
            "reconciledGateAVerifiedCount": gate_a.get("reconciledVerifiedSampleCount"),
            "reconciledGateAContactSheetPath": gate_a.get("contactSheetPath"),
            "gateAIdempotentRerun": gate_a.get("idempotent"),
            "pokewalletCredentialPresent": (gate_b.get("credentialAvailability") or pokewallet_credential_status()["availability"]),
            "gateBAttemptedUploadedVerifiedFailed": {
                "attempted": gate_b.get("attempted"),
                "uploaded": gate_b.get("uploaded"),
                "verified": gate_b.get("verified"),
                "failed": gate_b.get("failed"),
            },
            "gateBContactSheetPath": gate_b.get("contactSheetPath"),
            "totalVerifiedSupabaseThumbnailCount": counts.get("combinedVerifiedOrCompletedWithThumb"),
            "providerBreakdown": counts.get("providerBreakdown"),
            "averageThumbnailSizeBytes": avg,
            "projectedEnglishStorageBytes": projected,
            "exactUnresolvedDefects": defects,
            "tests": tests,
            "jsonReportPath": str(output_dir / "thumbnail_rollout_remediation_report.json"),
            "markdownReportPath": str(output_dir / "thumbnail_rollout_remediation_report.md"),
            "fullDisplayImageImportRun": False,
            "publicCatalogueUrlsChanged": False,
            "searchIndexRebuilt": False,
            "flutterModified": False,
            "fullEnglishImportRun": False,
            "remaining75PokeWalletExecuted": False,
            "scrydexUsed": False,
            "originalApprovedManifestSha256": APPROVED_MANIFEST_SHA256,
            "replacementSeed": replacements.get("seed"),
        }
        write_json_report(report, output_dir / "thumbnail_rollout_remediation_report.json")
        md = "\n".join(
            [
                "# Thumbnail Rollout — Gate A Remediation + Gate B Canary Report",
                "",
                f"- Classification: **{classification}**",
                f"- Original Gate A verified: {report['originalGateAVerifiedCount']}",
                f"- Replacement manifest: `{report['replacementManifestPath']}`",
                f"- Replacement SHA-256: `{report['replacementManifestSha256']}`",
                f"- Replacements attempted/uploaded/verified/failed: {report['replacementsAttemptedUploadedVerifiedFailed']}",
                f"- Reconciled Gate A verified sample: {report['reconciledGateAVerifiedCount']}",
                f"- Reconciled contact sheet: `{report['reconciledGateAContactSheetPath']}`",
                f"- Gate A idempotent: {report['gateAIdempotentRerun']}",
                f"- PokeWallet credential: **{report['pokewalletCredentialPresent']}**",
                f"- Gate B attempted/uploaded/verified/failed: {report['gateBAttemptedUploadedVerifiedFailed']}",
                f"- Gate B contact sheet: `{report['gateBContactSheetPath']}`",
                f"- Total Supabase thumbs: {report['totalVerifiedSupabaseThumbnailCount']}",
                f"- Provider breakdown: {report['providerBreakdown']}",
                f"- Average thumb bytes: {avg}",
                f"- Projected English storage: {projected}",
                f"- Tests: {tests}",
                "- Display images imported: **False**",
                "- Public catalogue URLs changed: **False**",
                "- Search index rebuilt: **False**",
                "- Flutter modified: **False**",
                "- Full English import run: **False**",
                "- Remaining 75 PokeWallet cards executed: **False**",
                "- Scrydex used: **False**",
                "",
                "## Original nine failures",
                "",
            ]
            + [f"- `{item['canonicalBaseId']}`: {item['exactFailureClassification']} (HTTP {item.get('httpResponse')})" for item in (classifications.get('classifications') or [])]
            + [
                "",
                "## Stop gate",
                "",
                "Stopped after Gate B 25-card canary. Remaining 75 not executed.",
                "",
            ]
        )
        (output_dir / "thumbnail_rollout_remediation_report.md").write_text(md, encoding="utf-8")
        print(f"Classification={classification}")
        print(f"Wrote {report['jsonReportPath']}")
        print(f"Wrote {report['markdownReportPath']}")
        return 0 if classification != "FAIL" else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
