#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import unittest
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_image_pipeline.config import ImagePipelineConfig
from cardscanr_image_pipeline.reconciliation import (
    ProviderCoverageStats,
    audit_provider_coverage,
    build_reconciliation_contact_sheet,
    investigate_tcgdex_bucket_fallbacks,
    render_reconciliation_markdown,
    utc_now_iso,
)
from cardscanr_image_pipeline.sample_manifest import (
    identities_for_manifest,
    load_sample_manifest,
    manifest_path,
    sha256_file,
)
from cardscanr_image_pipeline.stage2_runner import Stage2Runner, write_json_report
from cardscanr_image_pipeline.providers.tcgdex import TcgdexImageProvider
from cardscanr_image_pipeline.tcgdex_serie_cache import enrich_identity_serie_id
from cardscanr_market_engine.supabase_env_loader import load_supabase_env

RUNTIME_DIR = ROOT / "reports" / "runtime"


def run_unit_tests() -> dict[str, object]:
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_image_pipeline")
    result = unittest.TextTestRunner(stream=open("nul", "w"), verbosity=0).run(suite)
    return {
        "passed": result.wasSuccessful(),
        "testsRun": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
    }


def main(argv: list[str] | None = None) -> int:
    load_supabase_env(str(ROOT / "supabase_env.local.json"))
    parser = argparse.ArgumentParser(description="Stage 2 reconciliation and approval-gate audit")
    parser.add_argument("--output-dir", default=str(RUNTIME_DIR))
    parser.add_argument("--skip-coverage", action="store_true")
    args = parser.parse_args(argv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Auditing provider coverage across full catalogue...")
    prior_path = output_dir / "image_pipeline_stage2_reconciliation.json"
    if args.skip_coverage and prior_path.exists():
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
        coverage_dict = prior.get("coverage") or {}
        coverage = ProviderCoverageStats(
            total_catalogue_cards=int(coverage_dict.get("totalCatalogueCards") or 0),
            unique_chain_matchable=int(coverage_dict.get("uniqueChainMatchable") or 0),
            unresolved=int(coverage_dict.get("unresolved") or 0),
            ambiguous=int(coverage_dict.get("ambiguous") or 0),
            pokewallet_validation_rejects=int(coverage_dict.get("pokewalletValidationRejects") or 0),
            duplicate_provider_mappings=int(coverage_dict.get("duplicateProviderMappings") or 0),
            matchable_by_language=coverage_dict.get("matchableByLanguage") or {},
            chain_selected_by_provider=coverage_dict.get("chainSelectedByProvider") or {},
            provider_capability_exclusive=coverage_dict.get("providerCapabilityExclusive") or {},
            inspection_style_tcgdex_pokemon_tcg_api=int(coverage_dict.get("inspectionStyleTcgdexPokemonTcgApi") or 0),
            prior_stage2_matchable_figure=int(coverage_dict.get("priorStage2MatchableFigure") or 36542),
            prior_stage2_figure_explanation=str(coverage_dict.get("priorStage2FigureExplanation") or ""),
            inspection_figure_explanation=str(coverage_dict.get("inspectionFigureExplanation") or ""),
        )
    else:
        coverage = audit_provider_coverage()
        coverage_dict = asdict(coverage)

    manifest = load_sample_manifest(manifest_path())
    identities = identities_for_manifest(manifest)
    identities_by_id = {identity.canonical_base_id: identity for identity in identities}
    execute_path = output_dir / "image_pipeline_stage2_execute.json"

    print("Investigating TCGdex-bucket fallback cards...")
    fallback_cases = investigate_tcgdex_bucket_fallbacks(
        execute_report_path=execute_path,
        manifest=manifest,
        identities_by_id=identities_by_id,
    )
    fallback_totals: dict[str, int] = {}
    for case in fallback_cases:
        key = case["classification"]
        fallback_totals[key] = fallback_totals.get(key, 0) + 1

    url_generation_defects = [case for case in fallback_cases if case["classification"] == "url_generation_defect"]
    remaining_url_defects: list[dict[str, Any]] = []
    tcg = TcgdexImageProvider()
    for case in fallback_cases:
        identity = identities_by_id[case["canonicalBaseId"]]
        enriched = enrich_identity_serie_id(identity)
        if (
            identity.image_source == "pokemon_tcg_api"
            and not identity.provider_ids.get("tcgdex")
            and tcg.resolve(enriched) is not None
        ):
            remaining_url_defects.append(case)

    print("Building reconciliation contact sheet...")
    contact_path = output_dir / "image_pipeline_stage2_reconciliation_contact_sheet.png"
    build_reconciliation_contact_sheet(
        execute_report_path=execute_path,
        manifest=manifest,
        fallback_cases=fallback_cases,
        output_path=contact_path,
    )

    print("Re-running sample verification and idempotent check...")
    config = ImagePipelineConfig.from_env(execute=False, dry_run=True)
    runner = Stage2Runner(config)
    execution = json.loads(execute_path.read_text(encoding="utf-8"))
    verification = runner.verify_manifest(manifest, execution_cards=execution.get("cards") or [])
    idempotent = runner.verify_idempotent_rerun(manifest)
    test_result = run_unit_tests()

    defects: list[str] = []
    if remaining_url_defects:
        defects.append(f"{len(remaining_url_defects)} TCGdex URL-generation defects remain unresolved")
    sample_ambiguous = int(execution.get("ambiguousCount") or 0)
    if sample_ambiguous > 0:
        defects.append(f"{sample_ambiguous} ambiguous cards in sample execution")
    if coverage.duplicate_provider_mappings > 0:
        defects.append(f"{coverage.duplicate_provider_mappings} duplicate provider identity mappings found")
    if not verification.get("passed"):
        defects.append("independent sample verification failed")
    if not idempotent.get("passed"):
        defects.append("idempotent rerun failed")
    if not test_result.get("passed"):
        defects.append("unit tests failed")

    approval_ready = not defects
    report: dict[str, object] = {
        "classification": "APPROVAL_READY" if approval_ready else "NOT_APPROVAL_READY",
        "generatedAtUtc": utc_now_iso(),
        "coverage": {
            "totalCatalogueCards": coverage.total_catalogue_cards,
            "uniqueChainMatchable": coverage.unique_chain_matchable,
            "unresolved": coverage.unresolved,
            "ambiguous": coverage.ambiguous,
            "pokewalletValidationRejects": coverage.pokewallet_validation_rejects,
            "duplicateProviderMappings": coverage.duplicate_provider_mappings,
            "matchableByLanguage": coverage.matchable_by_language,
            "chainSelectedByProvider": coverage.chain_selected_by_provider,
            "providerCapabilityExclusive": coverage.provider_capability_exclusive,
            "inspectionStyleTcgdexPokemonTcgApi": coverage.inspection_style_tcgdex_pokemon_tcg_api,
            "inspectionStyleTcgdexPokemonTcgApiCapabilityRecount": coverage.provider_capability_exclusive["tcgdex"]
            + coverage.provider_capability_exclusive["pokemon_tcg_api"],
            "priorStage2MatchableFigure": coverage.prior_stage2_matchable_figure,
            "priorStage2FigureExplanation": coverage.prior_stage2_figure_explanation,
            "inspectionFigureExplanation": coverage.inspection_figure_explanation,
        },
        "fallbackInvestigationCount": len(fallback_cases),
        "fallbackClassificationTotals": fallback_totals,
        "fallbackCases": fallback_cases,
        "verificationPassed": verification.get("passed"),
        "verificationSummary": {
            "verifiedCount": verification.get("verifiedCount"),
            "issueCount": verification.get("issueCount"),
        },
        "idempotentRerunPassed": idempotent.get("passed"),
        "idempotentRerun": idempotent,
        "testsPassed": test_result.get("passed"),
        "testResult": test_result,
        "sampleManifestPath": str(manifest_path()),
        "sampleManifestSha256": sha256_file(manifest_path()),
        "contactSheetPath": str(contact_path),
        "fullImportRun": False,
        "unresolvedDefects": defects,
        "fixesApplied": [
            "TCGdex provider skips pokemon_tcg_api imageSource without explicit tcgdex provider ID",
            "classify_sample_bucket respects catalogue imageSource for EN bucket assignment",
            "_fast_bucket no longer routes pokemon_tcg_api cards into en_tcgdex based on set membership alone",
        ],
    }

    json_path = output_dir / "image_pipeline_stage2_reconciliation.json"
    md_path = output_dir / "image_pipeline_stage2_reconciliation.md"
    write_json_report(report, json_path)
    md_path.write_text(render_reconciliation_markdown(report), encoding="utf-8")

    print(f"Classification={report['classification']}")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(f"Wrote {contact_path}")
    return 0 if approval_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
