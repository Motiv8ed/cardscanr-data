#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_image_pipeline.config import ImagePipelineConfig
from cardscanr_image_pipeline.stage2_runner import write_json_report
from cardscanr_image_pipeline.thumbnail_rollout import (
    ENGLISH_BATCH_SIZE,
    RUNTIME_DIR,
    build_english_thumbnail_batch_manifest,
    classify_catalogue_image_state,
    dry_run_english_thumbnail_batch,
    estimate_thumbnail_storage,
    render_stage1_markdown,
    write_english_batch_manifest,
)
from cardscanr_market_engine.supabase_env_loader import load_supabase_env

# Authoritative Supabase count from Stage 2 sample (queried live at rollout start).
DEFAULT_SUPABASE_STORED = 100


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CardScanR thumbnail-only import rollout")
    sub = parser.add_subparsers(dest="command", required=True)

    stage1 = sub.add_parser("stage1", help="Inspect current image state with live HTTP samples")
    stage1.add_argument("--output-dir", default=str(RUNTIME_DIR))
    stage1.add_argument("--supabase-stored", type=int, default=DEFAULT_SUPABASE_STORED)
    stage1.add_argument("--http-sample", type=int, default=25)

    manifest_cmd = sub.add_parser("build-en-500", help="Build deterministic English 500-card thumbnail batch manifest")
    manifest_cmd.add_argument("--output-dir", default=str(RUNTIME_DIR))
    manifest_cmd.add_argument(
        "--require-reachability",
        action="store_true",
        help="Live-probe each candidate URL while filling the batch (slow). Default skips probes.",
    )

    dry = sub.add_parser("dry-run-en-500", help="Dry-run English 500-card thumbnail batch (no uploads)")
    dry.add_argument("--output-dir", default=str(RUNTIME_DIR))
    dry.add_argument("--manifest", default=None)

    report = sub.add_parser("first-report", help="Write planning + dry-run first report (stop gate)")
    report.add_argument("--output-dir", default=str(RUNTIME_DIR))
    return parser


def main(argv: list[str] | None = None) -> int:
    load_supabase_env(str(ROOT / "supabase_env.local.json"))
    parser = build_parser()
    args = parser.parse_args(argv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.command == "stage1":
        classification = classify_catalogue_image_state(
            supabase_stored_completed=args.supabase_stored,
            http_sample_per_provider=args.http_sample,
        )
        payload = classification.to_dict()
        json_path = write_json_report(payload, output_dir / "thumbnail_rollout_stage1.json")
        md_path = output_dir / "thumbnail_rollout_stage1.md"
        md_path.write_text(render_stage1_markdown(payload), encoding="utf-8")
        print(f"total={payload['total_cards']} matchable={payload['matchable_chain_total']} unresolved={payload['unresolved']}")
        print(f"Wrote {json_path}")
        print(f"Wrote {md_path}")
        return 0

    if args.command == "build-en-500":
        manifest = build_english_thumbnail_batch_manifest(require_reachable=bool(args.require_reachability))
        path = write_english_batch_manifest(manifest, path=output_dir / "thumbnail_rollout_en_500_manifest.json")
        print(f"Wrote English 500 manifest {path} cards={manifest['cardCount']} sha256={manifest.get('sha256')}")
        return 0

    if args.command == "dry-run-en-500":
        manifest_path = Path(args.manifest) if args.manifest else output_dir / "thumbnail_rollout_en_500_manifest.json"
        if not manifest_path.exists():
            print(f"Manifest missing: {manifest_path}. Run build-en-500 first.")
            return 2
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        config = replace(
            ImagePipelineConfig.from_env(dry_run=True, execute=False, languages=("en",), import_display=False),
            import_display=False,
        )
        payload = dry_run_english_thumbnail_batch(config=config, manifest=manifest)
        path = write_json_report(payload, output_dir / "thumbnail_rollout_en_500_dry_run.json")
        print(
            f"Dry-run cards={payload['cardCount']} ambiguous={payload['ambiguousCount']} "
            f"stop={payload['shouldStop']} importDisplay=False"
        )
        print(f"Wrote {path}")
        return 1 if payload["shouldStop"] else 0

    if args.command == "first-report":
        stage1_path = output_dir / "thumbnail_rollout_stage1.json"
        dry_path = output_dir / "thumbnail_rollout_en_500_dry_run.json"
        manifest_path = output_dir / "thumbnail_rollout_en_500_manifest.json"
        if not stage1_path.exists() or not dry_path.exists() or not manifest_path.exists():
            print("Missing stage1 / dry-run / manifest artefacts. Run stage1, build-en-500, dry-run-en-500 first.")
            return 2
        stage1 = json.loads(stage1_path.read_text(encoding="utf-8"))
        dry = json.loads(dry_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        matchable = int(stage1.get("matchable_chain_total") or 0)
        matchable_en = int((stage1.get("matchable_by_language") or {}).get("en") or 0)
        usable = int((stage1.get("usable_public_url_estimate") or {}).get("estimatedUsableMatchableTotal") or 0)
        storage = estimate_thumbnail_storage(matchable_en=matchable_en, matchable_all=matchable)
        stop = bool(dry.get("shouldStop"))
        dry_ok = (not stop) and int(dry.get("cardCount") or 0) == ENGLISH_BATCH_SIZE
        # PARTIAL if planning+dry-run succeeded but usable coverage is incomplete / auth failures remain.
        if dry_ok and matchable > 0:
            classification = "PARTIAL" if usable < matchable else "PASS"
        else:
            classification = "FAIL"
        risks = [
            "PokeWallet catalogue URLs require authentication; Manual Add still shows placeholders until Supabase thumbs are wired.",
            "38,036 unresolved cards remain outside the validated provider chain.",
            "Do not auto-proceed from 500-card batch to full English import.",
            "Catalogue wiring, search-index rebuild, and Flutter checks are deferred until English thumbnail batch approval.",
        ]
        if stage1.get("ambiguous"):
            risks.append(f"Ambiguous PokeWallet identities: {stage1.get('ambiguous')}")
        report = {
            "classification": classification,
            "validatedMatchableTotal": matchable,
            "usablePublicUrlTotalEstimate": usable,
            "providerFailureTotals": stage1.get("provider_failure_totals") or {},
            "proposed500CardEnglishBatchManifest": str(manifest_path),
            "proposed500CardEnglishBatchSha256": manifest.get("sha256"),
            "estimatedThumbnailStorage": storage,
            "dryRun": {
                "cardCount": dry.get("cardCount"),
                "ambiguousCount": dry.get("ambiguousCount"),
                "shouldStop": dry.get("shouldStop"),
                "stopReasons": dry.get("stopReasons"),
                "providerTotals": dry.get("providerTotals"),
                "importDisplay": False,
            },
            "filesChanged": [
                "cardscanr_image_pipeline/config.py",
                "cardscanr_image_pipeline/models.py",
                "cardscanr_image_pipeline/paths.py",
                "cardscanr_image_pipeline/processing.py",
                "cardscanr_image_pipeline/database.py",
                "cardscanr_image_pipeline/pipeline.py",
                "cardscanr_image_pipeline/stage2_runner.py",
                "cardscanr_image_pipeline/thumbnail_rollout.py",
                "tools/thumbnail_rollout.py",
                "tests/test_thumbnail_rollout.py",
            ],
            "tests": "tests/test_thumbnail_rollout.py + existing tests/test_image_pipeline.py",
            "exactUnresolvedRisks": risks,
            "fullDisplayImageImportRun": False,
            "flutterModified": False,
            "fullEnglishImportRun": False,
            "catalogueWiringRun": False,
            "searchIndexRebuildRun": False,
            "stopGate": "Stopped after planning and 500-card dry run. Awaiting approval before execute.",
        }
        json_path = write_json_report(report, output_dir / "thumbnail_rollout_first_report.json")
        md_lines = [
            "# Thumbnail Rollout — First Report",
            "",
            f"- Classification: **{classification}**",
            f"- Validated matchable total: {matchable}",
            f"- Usable public URL total (estimate): {usable}",
            f"- Provider failure totals (live sample): {report['providerFailureTotals']}",
            f"- Proposed 500-card English batch: `{manifest_path}`",
            f"- Batch SHA-256: `{manifest.get('sha256')}`",
            f"- Estimated 500-batch thumb storage: {storage['estimated500BatchBytes']} bytes",
            f"- Estimated full English matchable thumb storage: {storage['estimatedFullEnglishMatchableBytes']} bytes",
            f"- Estimated full catalogue matchable thumb storage: {storage['estimatedFullCatalogueMatchableBytes']} bytes",
            f"- Dry-run stop: {dry.get('shouldStop')}",
            f"- Full display-image import run: **False**",
            f"- Flutter modified: **False**",
            f"- Full English import run: **False**",
            "",
            "## Stop gate",
            "",
            report["stopGate"],
            "",
            "## Unresolved risks",
            "",
        ]
        for risk in risks:
            md_lines.append(f"- {risk}")
        md_lines.append("")
        md_path = output_dir / "thumbnail_rollout_first_report.md"
        md_path.write_text("\n".join(md_lines), encoding="utf-8")
        print(f"Classification={classification}")
        print(f"Wrote {json_path}")
        print(f"Wrote {md_path}")
        return 0 if classification != "FAIL" else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
