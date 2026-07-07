from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalogue import DEFAULT_CATALOGUE_ROOT, iter_catalogue_identities
from .matching import resolve_provider_image
from .pipeline import PipelineRunSummary


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def audit_catalogue_coverage(
    *,
    catalogue_root: Path = DEFAULT_CATALOGUE_ROOT,
    languages: tuple[str, ...] = ("en", "jp"),
    sample_limit: int | None = None,
) -> dict[str, Any]:
    total = 0
    with_provider_match = 0
    with_catalogue_urls = 0
    by_language: dict[str, dict[str, int]] = {}
    by_image_source: dict[str, int] = {}
    unmatched_examples: list[dict[str, str]] = []

    for identity in iter_catalogue_identities(
        catalogue_root,
        languages=languages,
        sample_limit=sample_limit,
    ):
        total += 1
        lang_stats = by_language.setdefault(identity.language, {"total": 0, "matchable": 0, "hasUrls": 0})
        lang_stats["total"] += 1
        if identity.catalogue_image_small and identity.catalogue_image_large:
            with_catalogue_urls += 1
            lang_stats["hasUrls"] += 1
        source = identity.image_source or "unknown"
        by_image_source[source] = by_image_source.get(source, 0) + 1
        candidate, _ = resolve_provider_image(identity)
        if candidate is not None:
            with_provider_match += 1
            lang_stats["matchable"] += 1
        elif len(unmatched_examples) < 25:
            unmatched_examples.append(
                {
                    "canonicalBaseId": identity.canonical_base_id,
                    "language": identity.language,
                    "setId": identity.set_id,
                    "collectorNumber": identity.collector_number,
                    "imageSource": identity.image_source or "",
                }
            )

    return {
        "generatedAtUtc": utc_now_iso(),
        "totalCards": total,
        "withCatalogueImageUrls": with_catalogue_urls,
        "withProviderMatch": with_provider_match,
        "withoutProviderMatch": total - with_provider_match,
        "matchRate": round((with_provider_match / total) * 100, 2) if total else 0.0,
        "byLanguage": by_language,
        "byImageSource": dict(sorted(by_image_source.items())),
        "unmatchedExamples": unmatched_examples,
    }


def write_coverage_reports(
    audit: dict[str, Any],
    *,
    output_dir: Path,
    prefix: str = "image_pipeline_coverage",
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{prefix}_latest.json"
    md_path = output_dir / f"{prefix}_latest.md"
    json_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_coverage_markdown(audit), encoding="utf-8")
    return json_path, md_path


def render_coverage_markdown(audit: dict[str, Any]) -> str:
    lines = [
        "# Pokémon Card Image Pipeline Coverage",
        "",
        f"- Generated at (UTC): {audit.get('generatedAtUtc')}",
        f"- Total catalogue cards scanned: {audit.get('totalCards')}",
        f"- Cards with catalogue image URLs: {audit.get('withCatalogueImageUrls')}",
        f"- Cards with provider match: {audit.get('withProviderMatch')}",
        f"- Cards without provider match: {audit.get('withoutProviderMatch')}",
        f"- Match rate: {audit.get('matchRate')}%",
        "",
        "## By language",
        "",
    ]
    by_language = audit.get("byLanguage") or {}
    for language, stats in sorted(by_language.items()):
        lines.append(
            f"- `{language}`: total={stats.get('total', 0)}, matchable={stats.get('matchable', 0)}, hasUrls={stats.get('hasUrls', 0)}"
        )
    lines.extend(["", "## By image source", ""])
    for source, count in (audit.get("byImageSource") or {}).items():
        lines.append(f"- `{source}`: {count}")
    examples = audit.get("unmatchedExamples") or []
    if examples:
        lines.extend(["", "## Unmatched examples", ""])
        for item in examples:
            lines.append(
                f"- `{item.get('canonicalBaseId')}` ({item.get('language')}, set={item.get('setId')}, #{item.get('collectorNumber')}, source={item.get('imageSource')})"
            )
    lines.append("")
    return "\n".join(lines)


def write_run_reports(
    summary: PipelineRunSummary,
    *,
    output_dir: Path,
    prefix: str,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = summary.to_dict()
    payload["generatedAtUtc"] = utc_now_iso()
    json_path = output_dir / f"{prefix}_latest.json"
    md_path = output_dir / f"{prefix}_latest.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_lines = [
        f"# {prefix.replace('_', ' ').title()}",
        "",
        f"- Generated at (UTC): {payload['generatedAtUtc']}",
        f"- Dry run: {payload['dryRun']}",
        f"- Total candidates: {payload['totalCandidates']}",
        f"- Completed: {payload['completed']}",
        f"- Failed: {payload['failed']}",
        f"- Skipped: {payload['skipped']}",
        f"- Resumed: {payload['resumed']}",
        "",
        "## Failures by reason",
        "",
    ]
    for reason, count in (payload.get("failuresByReason") or {}).items():
        md_lines.append(f"- `{reason}`: {count}")
    md_lines.append("")
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    return json_path, md_path
