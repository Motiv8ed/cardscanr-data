#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_image_pipeline.config import ImagePipelineConfig
from cardscanr_image_pipeline.stage2_runner import Stage2Runner, build_contact_sheet, write_json_report
from cardscanr_image_pipeline.thumbnail_execute import (
    APPROVED_MANIFEST_PATH,
    APPROVED_MANIFEST_SHA256,
    build_tcgdex_diagnostic,
    count_supabase_thumbs,
    enrich_skipped_public_urls,
    filter_manifest_entries,
    gate_a_dry_run_checks,
    gate_b_authenticated_probe,
    load_approved_manifest,
    pokewallet_credential_status,
    reconcile_against_supabase,
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


def run_unit_tests() -> dict[str, object]:
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromName("tests.test_thumbnail_rollout"))
    suite.addTests(loader.loadTestsFromName("tests.test_image_pipeline"))
    result = unittest.TextTestRunner(stream=open(__import__("os").devnull, "w"), verbosity=0).run(suite)
    return {
        "passed": result.wasSuccessful(),
        "testsRun": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Controlled English thumbnail execution (Gate A / Gate B)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--output-dir", default=str(RUNTIME_DIR))
        subparser.add_argument("--manifest", default=str(APPROVED_MANIFEST_PATH))

    for name, help_text in (
        ("gate-a", "Reconcile, dry-run, execute, verify Pokémon TCG API 400-card batch"),
        ("gate-b", "PokeWallet canary then remaining 75 only if canary PASS"),
        ("tcgdex-diagnostic", "Write TCGdex diagnostic report (non-blocking)"),
        ("final-report", "Assemble final execution report from gate artefacts"),
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
    manifest = load_approved_manifest(Path(args.manifest))

    if args.command == "tcgdex-diagnostic":
        payload = build_tcgdex_diagnostic()
        path = write_json_report(payload, output_dir / "thumbnail_rollout_tcgdex_diagnostic.json")
        md = output_dir / "thumbnail_rollout_tcgdex_diagnostic.md"
        md.write_text(
            "\n".join(
                [
                    "# TCGdex Diagnostic (non-blocking)",
                    "",
                    f"- Generated at (UTC): {payload['generatedAtUtc']}",
                    f"- Normalization candidates sampled: {payload['normalizationCandidateCountObserved']}",
                    f"- Live sample 404s: {payload['liveSample404Count']}",
                    f"- Blocks Gate A: {payload['blocksGateA']}",
                    "",
                    payload["note"],
                    "",
                ]
            ),
            encoding="utf-8",
        )
        print(f"Wrote {path}")
        print(f"Wrote {md}")
        return 0

    if args.command == "gate-a":
        gate_a_manifest = filter_manifest_entries(manifest, provider="pokemon_tcg_api")
        if gate_a_manifest["cardCount"] != 400:
            raise RuntimeError(f"Expected 400 Pokémon TCG API cards, got {gate_a_manifest['cardCount']}")
        write_json_report(gate_a_manifest, output_dir / "thumbnail_rollout_gate_a_manifest.json")

        dry_runner = Stage2Runner(thumb_config(execute=False))
        reconcile = reconcile_against_supabase(dry_runner, gate_a_manifest)
        write_json_report(reconcile, output_dir / "thumbnail_rollout_gate_a_reconcile.json")
        print(
            f"Gate A reconcile imported={reconcile['alreadyImportedCount']} "
            f"verified={reconcile['alreadyVerifiedCount']} pending={reconcile['pendingCount']} "
            f"conflicts={reconcile['conflictCount']} dupPaths={reconcile['duplicateImmutableTargetCount']}"
        )
        if reconcile["conflictCount"] or reconcile["duplicateImmutableTargetCount"]:
            print("STOP: reconcile conflicts or duplicate immutable targets")
            return 1

        dry = gate_a_dry_run_checks(dry_runner, gate_a_manifest)
        write_json_report(dry, output_dir / "thumbnail_rollout_gate_a_dry_run.json")
        print(f"Gate A dry-run cards={dry['cardCount']} stop={dry['shouldStop']}")
        if dry["shouldStop"]:
            print("STOP reasons:", dry["stopReasons"][:20])
            return 1

        exec_runner = Stage2Runner(thumb_config(execute=True))
        execution = exec_runner.execute_manifest(gate_a_manifest)
        execution["cards"] = enrich_skipped_public_urls(exec_runner, execution.get("cards") or [])
        execution["summary"] = summarize_execution(execution["cards"])
        write_json_report(execution, output_dir / "thumbnail_rollout_gate_a_execute.json")
        print(
            f"Gate A execute attempted={execution['summary']['attempted']} "
            f"uploaded={execution['summary']['uploaded']} skipped={execution['summary']['skipped']} "
            f"failed={execution['summary']['failed']}"
        )
        if execution["summary"]["failed"] or execution["summary"]["ambiguous"] or execution["summary"]["displayPathsPresent"]:
            print("STOP: Gate A execution failures/ambiguous/display paths")
            return 1

        verification = verify_successful_cards(exec_runner, gate_a_manifest, execution.get("cards") or [])
        write_json_report(verification, output_dir / "thumbnail_rollout_gate_a_verify.json")
        print(f"Gate A verify verified={verification['verifiedCount']} issues={verification['issueCount']}")
        expected_successes = execution["summary"]["uploaded"] + execution["summary"]["skipped"]
        if not verification.get("passed") or int(verification.get("verifiedCount") or 0) != expected_successes:
            print("STOP: Gate A verification failed")
            return 1

        contact = output_dir / "thumbnail_rollout_gate_a_contact_sheet.png"
        build_contact_sheet(execution.get("cards") or [], output_path=contact, columns=20)
        print(f"Gate A contact sheet {contact}")

        idempotent = exec_runner.verify_idempotent_rerun(gate_a_manifest)
        write_json_report(idempotent, output_dir / "thumbnail_rollout_gate_a_idempotent.json")
        print(f"Gate A idempotent passed={idempotent.get('passed')}")
        if not idempotent.get("passed"):
            return 1

        report = {
            "gate": "A",
            "classification": "PASS",
            "generatedAtUtc": utc_now_iso(),
            "reconcile": {
                "alreadyImportedCount": reconcile["alreadyImportedCount"],
                "alreadyVerifiedCount": reconcile["alreadyVerifiedCount"],
                "pendingCount": reconcile["pendingCount"],
            },
            "execution": execution["summary"],
            "verification": {
                "passed": verification.get("passed"),
                "verifiedCount": verification.get("verifiedCount"),
                "issueCount": verification.get("issueCount"),
                "expectedSuccesses": expected_successes,
            },
            "idempotent": idempotent,
            "contactSheetPath": str(contact),
            "importDisplay": False,
        }
        write_json_report(report, output_dir / "thumbnail_rollout_gate_a_report.json")
        return 0

    if args.command == "gate-b":
        gate_a_report_path = output_dir / "thumbnail_rollout_gate_a_report.json"
        if not gate_a_report_path.exists():
            print("Gate A report missing; refuse Gate B")
            return 2
        gate_a_report = json.loads(gate_a_report_path.read_text(encoding="utf-8"))
        if gate_a_report.get("classification") != "PASS":
            print("Gate A is not PASS; refuse Gate B")
            return 2

        cred = pokewallet_credential_status()
        write_json_report(cred, output_dir / "thumbnail_rollout_gate_b_credential_status.json")
        print(f"PokeWallet credential availability={cred['availability']}")
        if cred["availability"] != "present":
            report = {
                "gate": "B",
                "classification": "FAIL",
                "reason": "pokewallet_credential_absent",
                "credentialAvailability": "absent",
            }
            write_json_report(report, output_dir / "thumbnail_rollout_gate_b_report.json")
            return 1

        pw_manifest = filter_manifest_entries(manifest, provider="pokewallet")
        dry_runner = Stage2Runner(thumb_config(execute=False))
        reconcile_all_pw = reconcile_against_supabase(dry_runner, pw_manifest)
        canary = select_pokewallet_canary(
            manifest,
            existing_ids=set(reconcile_all_pw.get("alreadyImported") or []),
        )
        write_json_report(canary, output_dir / "thumbnail_rollout_gate_b_canary_manifest.json")
        print(f"Gate B canary cards={canary['cardCount']}")

        probe = gate_b_authenticated_probe(canary)
        write_json_report(probe, output_dir / "thumbnail_rollout_gate_b_canary_probe.json")
        print(f"Gate B canary probe usable={probe['usableCount']} stop={probe['shouldStop']}")
        if probe["shouldStop"]:
            report = {
                "gate": "B",
                "classification": "FAIL",
                "phase": "canary_probe",
                "credentialAvailability": "present",
                "stopReasons": probe["stopReasons"],
                "remaining75Attempted": False,
            }
            write_json_report(report, output_dir / "thumbnail_rollout_gate_b_report.json")
            return 1

        canary_dry = dry_runner.dry_run_manifest(canary)
        canary_dry_stop = list(canary_dry.get("stopReasons") or [])
        if canary_dry.get("ambiguousCount"):
            canary_dry_stop.append("ambiguous_count_gt_0")
        for card in canary_dry.get("cards") or []:
            if card.get("display_storage_path"):
                canary_dry_stop.append(f"display_webp_planned:{card.get('canonical_base_id')}")
        canary_dry["stopReasons"] = canary_dry_stop
        canary_dry["shouldStop"] = bool(canary_dry_stop)
        write_json_report(canary_dry, output_dir / "thumbnail_rollout_gate_b_canary_dry_run.json")
        if canary_dry["shouldStop"]:
            report = {
                "gate": "B",
                "classification": "FAIL",
                "phase": "canary_dry_run",
                "credentialAvailability": "present",
                "stopReasons": canary_dry_stop,
                "remaining75Attempted": False,
            }
            write_json_report(report, output_dir / "thumbnail_rollout_gate_b_report.json")
            return 1

        exec_runner = Stage2Runner(thumb_config(execute=True))
        canary_exec = exec_runner.execute_manifest(canary)
        canary_exec["cards"] = enrich_skipped_public_urls(exec_runner, canary_exec.get("cards") or [])
        canary_exec["summary"] = summarize_execution(canary_exec["cards"])
        write_json_report(canary_exec, output_dir / "thumbnail_rollout_gate_b_canary_execute.json")
        print(
            f"Gate B canary execute uploaded={canary_exec['summary']['uploaded']} "
            f"skipped={canary_exec['summary']['skipped']} failed={canary_exec['summary']['failed']}"
        )
        if (
            canary_exec["summary"]["failed"]
            or canary_exec["summary"]["ambiguous"]
            or canary_exec["summary"]["displayPathsPresent"]
        ):
            report = {
                "gate": "B",
                "classification": "FAIL",
                "phase": "canary_execute",
                "credentialAvailability": "present",
                "execution": canary_exec["summary"],
                "remaining75Attempted": False,
            }
            write_json_report(report, output_dir / "thumbnail_rollout_gate_b_report.json")
            return 1

        canary_verify = verify_successful_cards(exec_runner, canary, canary_exec.get("cards") or [])
        write_json_report(canary_verify, output_dir / "thumbnail_rollout_gate_b_canary_verify.json")
        canary_contact = output_dir / "thumbnail_rollout_gate_b_canary_contact_sheet.png"
        build_contact_sheet(canary_exec.get("cards") or [], output_path=canary_contact, columns=5)
        canary_expected = canary_exec["summary"]["uploaded"] + canary_exec["summary"]["skipped"]
        if not canary_verify.get("passed") or int(canary_verify.get("verifiedCount") or 0) != canary_expected:
            report = {
                "gate": "B",
                "classification": "FAIL",
                "phase": "canary_verify",
                "credentialAvailability": "present",
                "verification": canary_verify,
                "contactSheetPath": str(canary_contact),
                "remaining75Attempted": False,
            }
            write_json_report(report, output_dir / "thumbnail_rollout_gate_b_report.json")
            return 1

        # Remaining 75 only after canary PASS.
        canary_ids = {entry["canonicalBaseId"] for entry in canary.get("entries") or []}
        remaining_ids = {
            entry["canonicalBaseId"]
            for entry in pw_manifest.get("entries") or []
            if entry["canonicalBaseId"] not in canary_ids
        }
        remaining = filter_manifest_entries(manifest, provider="pokewallet", canonical_ids=remaining_ids)
        write_json_report(remaining, output_dir / "thumbnail_rollout_gate_b_remaining75_manifest.json")
        remaining_exec = exec_runner.execute_manifest(remaining)
        remaining_exec["cards"] = enrich_skipped_public_urls(exec_runner, remaining_exec.get("cards") or [])
        remaining_exec["summary"] = summarize_execution(remaining_exec["cards"])
        write_json_report(remaining_exec, output_dir / "thumbnail_rollout_gate_b_remaining75_execute.json")
        print(
            f"Gate B remaining75 uploaded={remaining_exec['summary']['uploaded']} "
            f"skipped={remaining_exec['summary']['skipped']} failed={remaining_exec['summary']['failed']}"
        )
        if (
            remaining_exec["summary"]["failed"]
            or remaining_exec["summary"]["ambiguous"]
            or remaining_exec["summary"]["displayPathsPresent"]
        ):
            report = {
                "gate": "B",
                "classification": "PARTIAL",
                "phase": "remaining75_execute",
                "credentialAvailability": "present",
                "canary": canary_exec["summary"],
                "remaining75": remaining_exec["summary"],
                "remaining75Attempted": True,
                "canaryContactSheetPath": str(canary_contact),
            }
            write_json_report(report, output_dir / "thumbnail_rollout_gate_b_report.json")
            return 1

        remaining_verify = verify_successful_cards(exec_runner, remaining, remaining_exec.get("cards") or [])
        write_json_report(remaining_verify, output_dir / "thumbnail_rollout_gate_b_remaining75_verify.json")
        remaining_expected = remaining_exec["summary"]["uploaded"] + remaining_exec["summary"]["skipped"]
        combined_cards = list(canary_exec.get("cards") or []) + list(remaining_exec.get("cards") or [])
        combined_contact = output_dir / "thumbnail_rollout_gate_b_contact_sheet.png"
        build_contact_sheet(combined_cards, output_path=combined_contact, columns=10)
        classification = (
            "PASS"
            if remaining_verify.get("passed") and int(remaining_verify.get("verifiedCount") or 0) == remaining_expected
            else "FAIL"
        )
        report = {
            "gate": "B",
            "classification": classification,
            "credentialAvailability": "present",
            "canary": {
                "attempted": canary_exec["summary"]["attempted"],
                "uploaded": canary_exec["summary"]["uploaded"],
                "skipped": canary_exec["summary"]["skipped"],
                "failed": canary_exec["summary"]["failed"],
                "verified": canary_verify.get("verifiedCount"),
            },
            "remaining75": {
                "attempted": remaining_exec["summary"]["attempted"],
                "uploaded": remaining_exec["summary"]["uploaded"],
                "skipped": remaining_exec["summary"]["skipped"],
                "failed": remaining_exec["summary"]["failed"],
                "verified": remaining_verify.get("verifiedCount"),
                "verifyPassed": remaining_verify.get("passed"),
            },
            "remaining75Attempted": True,
            "canaryContactSheetPath": str(canary_contact),
            "contactSheetPath": str(combined_contact),
            "importDisplay": False,
        }
        write_json_report(report, output_dir / "thumbnail_rollout_gate_b_report.json")
        return 0 if classification == "PASS" else 1

    if args.command == "final-report":
        gate_a = json.loads((output_dir / "thumbnail_rollout_gate_a_report.json").read_text(encoding="utf-8"))
        gate_b_path = output_dir / "thumbnail_rollout_gate_b_report.json"
        gate_b = json.loads(gate_b_path.read_text(encoding="utf-8")) if gate_b_path.exists() else {}
        reconcile = json.loads((output_dir / "thumbnail_rollout_gate_a_reconcile.json").read_text(encoding="utf-8"))
        gate_a_exec = json.loads((output_dir / "thumbnail_rollout_gate_a_execute.json").read_text(encoding="utf-8"))
        tests = run_unit_tests()
        runner = Stage2Runner(thumb_config(execute=False))
        supabase_counts = count_supabase_thumbs(runner)
        uploaded_thumbs = int((gate_a_exec.get("summary") or {}).get("totalThumbBytes") or 0)
        uploaded_count = int((gate_a_exec.get("summary") or {}).get("uploaded") or 0)
        avg_thumb = (uploaded_thumbs // uploaded_count) if uploaded_count else 0
        projected_en = avg_thumb * 23375 if avg_thumb else None

        gate_a_pass = gate_a.get("classification") == "PASS"
        gate_b_pass = gate_b.get("classification") == "PASS"
        if gate_a_pass and gate_b_pass and tests.get("passed"):
            classification = "PASS"
        elif gate_a_pass:
            classification = "PARTIAL"
        else:
            classification = "FAIL"

        failures: list[str] = []
        if not gate_a_pass:
            failures.append("gate_a_not_pass")
        if gate_b and not gate_b_pass:
            failures.append(f"gate_b_{gate_b.get('classification', 'unknown')}:{gate_b.get('phase') or gate_b.get('reason')}")
        if not tests.get("passed"):
            failures.append("unit_tests_failed")

        report = {
            "classification": classification,
            "generatedAtUtc": utc_now_iso(),
            "originalManifestPath": str(Path(args.manifest)),
            "originalManifestSha256": APPROVED_MANIFEST_SHA256,
            "gateA": {
                "attempted": (gate_a.get("execution") or {}).get("attempted"),
                "skipped": (gate_a.get("execution") or {}).get("skipped"),
                "uploaded": (gate_a.get("execution") or {}).get("uploaded"),
                "verified": (gate_a.get("verification") or {}).get("verifiedCount"),
                "failed": (gate_a.get("execution") or {}).get("failed"),
                "existingSupabaseOverlapCount": reconcile.get("alreadyImportedCount"),
                "totalSourceBytes": (gate_a.get("execution") or {}).get("totalSourceBytes"),
                "storedThumbnailBytes": (gate_a.get("execution") or {}).get("totalThumbBytes"),
                "contactSheetPath": gate_a.get("contactSheetPath"),
            },
            "pokewalletCredentialAvailability": (gate_b.get("credentialAvailability") or pokewallet_credential_status()["availability"]),
            "gateB": {
                "canary": gate_b.get("canary"),
                "remaining75": gate_b.get("remaining75"),
                "remaining75Attempted": gate_b.get("remaining75Attempted"),
                "contactSheetPath": gate_b.get("contactSheetPath") or gate_b.get("canaryContactSheetPath"),
                "classification": gate_b.get("classification"),
            },
            "combinedVerifiedThumbnailCountInSupabase": supabase_counts.get("combinedVerifiedOrCompletedWithThumb"),
            "providerBreakdown": supabase_counts.get("providerBreakdown"),
            "exactFailuresAndReasons": failures,
            "unresolvedEnglishCards": 46417 - 23375,
            "projectedEnglishThumbnailStorageBytes": projected_en,
            "actualBatchAverageThumbBytes": avg_thumb,
            "tests": tests,
            "jsonReportPath": str(output_dir / "thumbnail_rollout_execution_report.json"),
            "markdownReportPath": str(output_dir / "thumbnail_rollout_execution_report.md"),
            "fullDisplayImageImportRun": False,
            "publicCatalogueUrlsChanged": False,
            "searchIndexRebuilt": False,
            "flutterModified": False,
            "fullEnglishImportRun": False,
            "tcgdexIncluded": False,
        }
        json_path = write_json_report(report, output_dir / "thumbnail_rollout_execution_report.json")
        md_lines = [
            "# Thumbnail Rollout — Controlled English Execution Report",
            "",
            f"- Classification: **{classification}**",
            f"- Original manifest: `{report['originalManifestPath']}`",
            f"- Manifest SHA-256: `{report['originalManifestSha256']}`",
            f"- Gate A attempted/skipped/uploaded/verified/failed: "
            f"{report['gateA']['attempted']}/{report['gateA']['skipped']}/{report['gateA']['uploaded']}/"
            f"{report['gateA']['verified']}/{report['gateA']['failed']}",
            f"- Existing Supabase overlap: {report['gateA']['existingSupabaseOverlapCount']}",
            f"- Gate A source bytes: {report['gateA']['totalSourceBytes']}",
            f"- Gate A stored thumb bytes: {report['gateA']['storedThumbnailBytes']}",
            f"- Gate A contact sheet: `{report['gateA']['contactSheetPath']}`",
            f"- PokeWallet credential availability: **{report['pokewalletCredentialAvailability']}**",
            f"- Gate B canary: {report['gateB'].get('canary')}",
            f"- Gate B remaining 75: {report['gateB'].get('remaining75')}",
            f"- Gate B contact sheet: `{report['gateB'].get('contactSheetPath')}`",
            f"- Combined verified/completed thumbs in Supabase: {report['combinedVerifiedThumbnailCountInSupabase']}",
            f"- Provider breakdown: {report['providerBreakdown']}",
            f"- Exact failures: {failures or 'none'}",
            f"- Unresolved English cards: {report['unresolvedEnglishCards']}",
            f"- Projected English thumb storage (actual avg): {projected_en}",
            f"- Tests: {tests}",
            f"- Display images imported: **False**",
            f"- Public catalogue URLs changed: **False**",
            f"- Search index rebuilt: **False**",
            f"- Flutter modified: **False**",
            f"- Full English import run: **False**",
            "",
        ]
        md_path = output_dir / "thumbnail_rollout_execution_report.md"
        md_path.write_text("\n".join(md_lines), encoding="utf-8")
        print(f"Classification={classification}")
        print(f"Wrote {json_path}")
        print(f"Wrote {md_path}")
        return 0 if classification != "FAIL" else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
