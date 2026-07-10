#!/usr/bin/env python3
from __future__ import annotations

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
from cardscanr_image_pipeline.stage2_runner import Stage2Runner, write_json_report
from cardscanr_image_pipeline.thumbnail_execute import (
    APPROVED_MANIFEST_SHA256,
    count_supabase_thumbs,
    pokewallet_credential_status,
)
from cardscanr_image_pipeline.thumbnail_rollout import utc_now_iso
from cardscanr_market_engine.supabase_env_loader import load_supabase_env

OUT = ROOT / "reports" / "runtime"


def main() -> int:
    load_supabase_env(str(ROOT / "supabase_env.local.json"))
    gate_a = json.loads((OUT / "thumbnail_rollout_gate_a_report.json").read_text(encoding="utf-8"))
    reconcile = json.loads((OUT / "thumbnail_rollout_gate_a_reconcile.json").read_text(encoding="utf-8"))

    gate_b = {
        "gate": "B",
        "classification": "BLOCKED",
        "reason": "gate_a_not_pass",
        "credentialAvailability": pokewallet_credential_status()["availability"],
        "canary": None,
        "remaining75Attempted": False,
        "contactSheetPath": None,
    }
    write_json_report(gate_b, OUT / "thumbnail_rollout_gate_b_report.json")

    runner = Stage2Runner(
        replace(
            ImagePipelineConfig.from_env(execute=False, dry_run=True, languages=("en",), import_display=False),
            import_display=False,
        )
    )
    counts = count_supabase_thumbs(runner)

    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromName("tests.test_thumbnail_rollout"))
    suite.addTests(loader.loadTestsFromName("tests.test_image_pipeline"))
    result = unittest.TextTestRunner(stream=open(os.devnull, "w"), verbosity=0).run(suite)
    tests = {
        "passed": result.wasSuccessful(),
        "testsRun": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
    }

    uploaded = int(gate_a["execution"]["uploaded"])
    thumb_bytes = int(gate_a.get("storedThumbnailBytesSuccessful") or gate_a["execution"]["totalThumbBytes"])
    avg = (thumb_bytes // uploaded) if uploaded else 0
    projected = avg * 23375 if avg else None

    failures = [
        "gate_a_partial_9_pokemontcg_io_404_on_me3_me2pt5",
        "gate_b_blocked_because_gate_a_not_pass",
    ]
    for card in gate_a.get("failedCards") or []:
        failures.append(f"{card['canonicalBaseId']}: {card.get('failureReason')}")

    report = {
        "classification": "PARTIAL",
        "generatedAtUtc": utc_now_iso(),
        "originalManifestPath": "reports/runtime/thumbnail_rollout_en_500_manifest.json",
        "originalManifestSha256": APPROVED_MANIFEST_SHA256,
        "gateA": {
            "attempted": gate_a["execution"]["attempted"],
            "skipped": gate_a["execution"]["skipped"],
            "uploaded": gate_a["execution"]["uploaded"],
            "verified": gate_a["verification"]["verifiedCount"],
            "failed": gate_a["execution"]["failed"],
            "existingSupabaseOverlapCount": reconcile.get("alreadyImportedCount"),
            "totalSourceBytes": gate_a["execution"]["totalSourceBytes"],
            "storedThumbnailBytes": thumb_bytes,
            "contactSheetPath": gate_a.get("contactSheetPath"),
            "idempotentPassed": (gate_a.get("idempotent") or {}).get("passed"),
            "classification": "PARTIAL",
        },
        "pokewalletCredentialAvailability": gate_b["credentialAvailability"],
        "gateB": {
            "canary": None,
            "canaryAttempted": False,
            "canaryUploaded": 0,
            "canaryVerified": 0,
            "canaryFailed": 0,
            "remaining75": None,
            "remaining75Attempted": False,
            "contactSheetPath": None,
            "classification": "BLOCKED",
            "reason": "Gate A was PARTIAL (9 failures); Gate B not started per stop gate.",
        },
        "combinedVerifiedThumbnailCountInSupabase": counts.get("combinedVerifiedOrCompletedWithThumb"),
        "providerBreakdown": counts.get("providerBreakdown"),
        "exactFailuresAndReasons": failures,
        "unresolvedEnglishCards": 46417 - 23375,
        "projectedEnglishThumbnailStorageBytes": projected,
        "actualBatchAverageThumbBytes": avg,
        "tests": tests,
        "jsonReportPath": str(OUT / "thumbnail_rollout_execution_report.json"),
        "markdownReportPath": str(OUT / "thumbnail_rollout_execution_report.md"),
        "tcgdexDiagnosticPath": str(OUT / "thumbnail_rollout_tcgdex_diagnostic.json"),
        "fullDisplayImageImportRun": False,
        "publicCatalogueUrlsChanged": False,
        "searchIndexRebuilt": False,
        "flutterModified": False,
        "fullEnglishImportRun": False,
        "tcgdexIncluded": False,
    }
    write_json_report(report, OUT / "thumbnail_rollout_execution_report.json")
    md = "\n".join(
        [
            "# Thumbnail Rollout — Controlled English Execution Report",
            "",
            "- Classification: **PARTIAL**",
            f"- Original manifest: `{report['originalManifestPath']}`",
            f"- Manifest SHA-256: `{report['originalManifestSha256']}`",
            (
                "- Gate A attempted/skipped/uploaded/verified/failed: "
                f"{report['gateA']['attempted']}/{report['gateA']['skipped']}/"
                f"{report['gateA']['uploaded']}/{report['gateA']['verified']}/{report['gateA']['failed']}"
            ),
            f"- Existing Supabase overlap: {report['gateA']['existingSupabaseOverlapCount']}",
            f"- Gate A source bytes: {report['gateA']['totalSourceBytes']}",
            f"- Gate A stored thumb bytes: {report['gateA']['storedThumbnailBytes']}",
            f"- Gate A contact sheet: `{report['gateA']['contactSheetPath']}`",
            f"- Gate A idempotent: {report['gateA']['idempotentPassed']}",
            f"- PokeWallet credential availability: **{report['pokewalletCredentialAvailability']}**",
            "- Gate B canary attempted/uploaded/verified/failed: 0/0/0/0 (BLOCKED)",
            "- Remaining PokeWallet 75: not attempted",
            "- PokeWallet contact sheet: none",
            f"- Combined verified/completed thumbs in Supabase: {report['combinedVerifiedThumbnailCountInSupabase']}",
            f"- Provider breakdown: {report['providerBreakdown']}",
            f"- Unresolved English cards: {report['unresolvedEnglishCards']}",
            f"- Actual batch avg thumb bytes: {avg}",
            f"- Projected English thumb storage: {projected}",
            f"- Tests: {tests}",
            f"- TCGdex diagnostic: `{report['tcgdexDiagnosticPath']}`",
            "- Display images imported: **False**",
            "- Public catalogue URLs changed: **False**",
            "- Search index rebuilt: **False**",
            "- Flutter modified: **False**",
            "- Full English import run: **False**",
            "",
            "## Exact failures",
            "",
            "- 9 Mega Evolution cards (me3 / me2pt5) returned HTTP 404 on images.pokemontcg.io",
            "- Catalogue previously used images.scrydex.com mirrors; those hosts are not allowed for Gate A",
            "- Gate B blocked because Gate A is not PASS",
            "",
            "## Stop gate",
            "",
            "Stopped after Gate A PARTIAL report. No catalogue wiring. No search-index rebuild.",
            "",
        ]
    )
    (OUT / "thumbnail_rollout_execution_report.md").write_text(md, encoding="utf-8")
    print("Classification=PARTIAL")
    print("combined", counts.get("combinedVerifiedOrCompletedWithThumb"))
    print("providers", counts.get("providerBreakdown"))
    print("avg", avg, "projected", projected)
    print("tests", tests)
    print("cred", gate_b["credentialAvailability"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
