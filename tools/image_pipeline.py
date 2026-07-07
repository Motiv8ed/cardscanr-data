#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_image_pipeline.catalogue import DEFAULT_CATALOGUE_ROOT
from cardscanr_image_pipeline.config import ImagePipelineConfig
from cardscanr_image_pipeline.pipeline import ImageIngestionPipeline
from cardscanr_image_pipeline.reports import audit_catalogue_coverage, write_coverage_reports, write_run_reports
from cardscanr_market_engine.supabase_env_loader import load_supabase_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CardScanR Pokémon card image pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser("audit", help="Audit catalogue image coverage without network writes")
    audit.add_argument("--languages", default="en,jp")
    audit.add_argument("--sample-limit", type=int, default=None)
    audit.add_argument("--output-dir", default=str(ROOT / "reports"))

    sample = subparsers.add_parser("sample", help="Dry-run a bounded sample import")
    _add_import_args(sample, default_sample=100)

    import_cmd = subparsers.add_parser("import", help="Run import (requires --execute)")
    _add_import_args(import_cmd, default_sample=None)

    verify = subparsers.add_parser("verify", help="Verify stored image records")
    verify.add_argument("--languages", default="en,jp")
    verify.add_argument("--sample-limit", type=int, default=100)
    verify.add_argument("--output-dir", default=str(ROOT / "reports"))
    verify.add_argument("--execute", action="store_true", help="Perform verification against Supabase")

    report = subparsers.add_parser("report", help="Generate coverage report only")
    report.add_argument("--languages", default="en,jp")
    report.add_argument("--sample-limit", type=int, default=None)
    report.add_argument("--output-dir", default=str(ROOT / "reports"))
    return parser


def _add_import_args(parser: argparse.ArgumentParser, *, default_sample: int | None) -> None:
    parser.add_argument("--languages", default="en,jp")
    parser.add_argument("--sample-limit", type=int, default=default_sample)
    parser.add_argument("--set-id", default=None)
    parser.add_argument("--output-dir", default=str(ROOT / "reports"))
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform network fetch, storage upload, and database writes. Default is dry-run.",
    )


def parse_languages(value: str) -> tuple[str, ...]:
    items = [part.strip().lower() for part in value.split(",") if part.strip()]
    return tuple(items or ("en", "jp"))


def main(argv: list[str] | None = None) -> int:
    load_supabase_env(str(ROOT / "supabase_env.local.json"))
    parser = build_parser()
    args = parser.parse_args(argv)
    languages = parse_languages(args.languages)

    if args.command == "audit":
        audit = audit_catalogue_coverage(
            catalogue_root=DEFAULT_CATALOGUE_ROOT,
            languages=languages,
            sample_limit=args.sample_limit,
        )
        write_coverage_reports(audit, output_dir=Path(args.output_dir))
        print(f"Audited {audit['totalCards']} cards; match rate {audit['matchRate']}%")
        return 0

    if args.command == "report":
        audit = audit_catalogue_coverage(
            catalogue_root=DEFAULT_CATALOGUE_ROOT,
            languages=languages,
            sample_limit=args.sample_limit,
        )
        json_path, md_path = write_coverage_reports(audit, output_dir=Path(args.output_dir))
        print(f"Wrote {json_path}")
        print(f"Wrote {md_path}")
        return 0

    if args.command == "verify":
        if not args.execute:
            print("Verify is dry-run only without --execute. No remote checks were performed.")
            return 0
        config = ImagePipelineConfig.from_env(execute=True, languages=languages, sample_limit=args.sample_limit)
        pipeline = ImageIngestionPipeline(config)
        verified = 0
        failed = 0
        for identity in pipeline.iter_identities():
            record = pipeline.db.get_record(identity.canonical_base_id)
            if not record or record.get("status") not in {"completed", "verified"}:
                failed += 1
                continue
            thumb_path = record.get("thumb_storage_path")
            display_path = record.get("display_storage_path")
            if not thumb_path or not display_path:
                failed += 1
                continue
            if pipeline.storage.verify_public_readable(thumb_path) and pipeline.storage.verify_public_readable(display_path):
                verified += 1
            else:
                failed += 1
        print(f"Verified {verified} records; failed {failed}")
        return 0 if failed == 0 else 1

    execute = bool(getattr(args, "execute", False))
    dry_run = not execute
    if args.command == "import" and not execute:
        print("Import requires --execute. Use `sample` for a bounded dry-run.")
        return 2
    config = ImagePipelineConfig.from_env(
        dry_run=dry_run,
        execute=execute,
        languages=languages,
        sample_limit=args.sample_limit,
    )
    pipeline = ImageIngestionPipeline(config)
    summary = pipeline.run(set_id=getattr(args, "set_id", None))
    prefix = "image_pipeline_sample" if args.command == "sample" else "image_pipeline_import"
    write_run_reports(summary, output_dir=Path(args.output_dir), prefix=prefix)
    print(
        f"{'Dry-run' if summary.dry_run else 'Executed'} {summary.total_candidates} cards: "
        f"completed={summary.completed}, failed={summary.failed}, skipped={summary.skipped}"
    )
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
