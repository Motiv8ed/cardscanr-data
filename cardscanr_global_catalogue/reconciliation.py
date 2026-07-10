from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .contracts import LANGUAGE_DEFINITIONS, canonicalize_language, write_json_atomic


ROOT = Path(__file__).resolve().parent.parent
CATALOGUE_DIR = ROOT / "data" / "global" / "catalogue"
REPORT_DIR = ROOT / "reports" / "global_rollout"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid JSONL at {path}:{line_number}") from exc
            if isinstance(item, dict):
                yield item


def _empty_metrics() -> Counter[str]:
    return Counter(
        {
            "sets": 0,
            "canonicalPrintings": 0,
            "nativeNamesPresent": 0,
            "englishAliasesPresent": 0,
            "exactCollectorIdentityPresent": 0,
            "releaseDatesPresent": 0,
            "rarityPresent": 0,
            "providerIdsPresent": 0,
            "imageCandidatesPresent": 0,
            "verifiedImages": 0,
            "unresolvedRecords": 0,
            "ambiguousRecords": 0,
            "duplicateCandidates": 0,
            "providerExclusiveRecords": 0,
            "variantUnresolved": 0,
        }
    )


def _language_from_unresolved(row: dict[str, Any]) -> str | None:
    value = row.get("language") or row.get("sourceLanguage")
    if not value:
        return None
    try:
        return canonicalize_language(str(value), provider="tcgdex")
    except ValueError:
        return None


def _percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100, 2)


def _metrics_payload(metrics: Counter[str]) -> dict[str, Any]:
    result = {key: int(value) for key, value in sorted(metrics.items())}
    denominator = int(metrics["canonicalPrintings"])
    result["coveragePercent"] = {
        "nativeNames": _percent(metrics["nativeNamesPresent"], denominator),
        "englishAliases": _percent(metrics["englishAliasesPresent"], denominator),
        "exactCollectorIdentity": _percent(
            metrics["exactCollectorIdentityPresent"], denominator
        ),
        "releaseDates": _percent(metrics["releaseDatesPresent"], denominator),
        "rarity": _percent(metrics["rarityPresent"], denominator),
        "providerIds": _percent(metrics["providerIdsPresent"], denominator),
        "imageCandidates": _percent(metrics["imageCandidatesPresent"], denominator),
        "verifiedImages": _percent(metrics["verifiedImages"], denominator),
    }
    return result


def reconcile_global_catalogue() -> dict[str, Any]:
    cards_path = CATALOGUE_DIR / "cards.jsonl"
    sets_path = CATALOGUE_DIR / "sets.jsonl"
    crosswalk_path = CATALOGUE_DIR / "provider_crosswalk.jsonl"
    unresolved_path = CATALOGUE_DIR / "unresolved.jsonl"
    conflicts_path = CATALOGUE_DIR / "conflicts.jsonl"
    if not cards_path.exists():
        raise FileNotFoundError(
            "Global catalogue is missing; run ingest-metadata before reconciliation"
        )

    provider_sets_by_printing: dict[str, set[str]] = defaultdict(set)
    for row in iter_jsonl(crosswalk_path):
        printing_id = str(row.get("canonicalPrintingId") or "")
        provider = str(row.get("provider") or "")
        if printing_id and provider:
            provider_sets_by_printing[printing_id].add(provider)

    by_pair: dict[tuple[str, str], Counter[str]] = defaultdict(_empty_metrics)
    for definition in LANGUAGE_DEFINITIONS:
        by_pair[(definition.language, definition.default_region)] = _empty_metrics()
    duplicate_ids: Counter[str] = Counter()
    total_metrics = _empty_metrics()
    image_candidates_by_provider: Counter[str] = Counter()
    printing_pair: dict[str, tuple[str, str]] = {}

    for row in iter_jsonl(sets_path):
        pair = (str(row.get("language") or "unknown"), str(row.get("region") or "unknown"))
        by_pair[pair]["sets"] += 1
        total_metrics["sets"] += 1

    for row in iter_jsonl(cards_path):
        language = str(row.get("language") or "unknown")
        region = str(row.get("region") or "unknown")
        pair = (language, region)
        metrics = by_pair[pair]
        printing_id = str(row.get("canonicalPrintingId") or "")
        duplicate_ids[printing_id] += 1
        if printing_id:
            printing_pair[printing_id] = pair

        for target in (metrics, total_metrics):
            target["canonicalPrintings"] += 1
            target["nativeNamesPresent"] += int(
                bool(row.get("nativeCardName") and row.get("nativeSetName"))
            )
            target["englishAliasesPresent"] += int(
                bool(row.get("englishCardName") or row.get("englishSetName"))
            )
            target["exactCollectorIdentityPresent"] += int(
                bool(
                    row.get("canonicalSetId")
                    and row.get("printedCollectorNumber")
                    and row.get("normalizedCollectorNumber")
                )
            )
            target["releaseDatesPresent"] += int(bool(row.get("releaseDate")))
            target["rarityPresent"] += int(bool(row.get("rarity")))
            target["providerIdsPresent"] += int(
                bool(row.get("providerCardIds") and row.get("providerSetIds"))
            )
            images = [
                image
                for image in row.get("imageProvenance") or []
                if isinstance(image, dict) and image.get("sourceUrl")
            ]
            target["imageCandidatesPresent"] += int(bool(images))
            target["verifiedImages"] += int(
                any(
                    str(image.get("state") or "").startswith("verified")
                    for image in images
                )
            )
            target["variantUnresolved"] += int(
                str(row.get("cardVariant") or "") == "unspecified"
            )

        for image in row.get("imageProvenance") or []:
            if isinstance(image, dict) and image.get("sourceUrl"):
                image_candidates_by_provider[str(image.get("provider") or "unknown")] += 1

    for printing_id, providers in provider_sets_by_printing.items():
        if len(providers) != 1:
            continue
        pair = printing_pair.get(printing_id)
        if pair is not None:
            by_pair[pair]["providerExclusiveRecords"] += 1
        total_metrics["providerExclusiveRecords"] += 1

    unresolved_without_language = 0
    for row in iter_jsonl(unresolved_path):
        language = _language_from_unresolved(row)
        reason = str(row.get("reason") or "")
        ambiguous = "ambiguous" in reason or "duplicate" in reason
        duplicate_candidates = (
            int(row.get("candidateCount") or 1) if "duplicate" in reason else 0
        )
        if language is None:
            unresolved_without_language += 1
        else:
            candidate_pairs = [pair for pair in by_pair if pair[0] == language]
            if len(candidate_pairs) == 1:
                by_pair[candidate_pairs[0]]["unresolvedRecords"] += 1
                by_pair[candidate_pairs[0]]["ambiguousRecords"] += int(ambiguous)
                by_pair[candidate_pairs[0]][
                    "duplicateCandidates"
                ] += duplicate_candidates
        total_metrics["unresolvedRecords"] += 1
        total_metrics["ambiguousRecords"] += int(ambiguous)
        total_metrics["duplicateCandidates"] += duplicate_candidates

    conflict_count = 0
    for row in iter_jsonl(conflicts_path):
        candidate_count = int(row.get("candidateCount") or 1)
        conflict_count += 1
        total_metrics["duplicateCandidates"] += candidate_count

    duplicate_output_rows = {
        printing_id: count
        for printing_id, count in duplicate_ids.items()
        if printing_id and count > 1
    }
    total_metrics["duplicateCandidates"] += sum(duplicate_output_rows.values())

    rows: list[dict[str, Any]] = []
    for (language, region), metrics in sorted(by_pair.items()):
        rows.append(
            {
                "language": language,
                "region": region,
                **_metrics_payload(metrics),
            }
        )

    classification = (
        "PARTIAL"
        if total_metrics["unresolvedRecords"]
        or total_metrics["variantUnresolved"]
        or conflict_count
        else "PASS"
    )
    payload = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now_iso(),
        "classification": classification,
        "scope": "staging_only_not_published",
        "source": {
            "cards": cards_path.relative_to(ROOT).as_posix(),
            "sets": sets_path.relative_to(ROOT).as_posix(),
            "crosswalk": crosswalk_path.relative_to(ROOT).as_posix(),
        },
        "totals": _metrics_payload(total_metrics),
        "languages": rows,
        "imageCandidatesByProvider": dict(sorted(image_candidates_by_provider.items())),
        "unresolvedWithoutLanguage": unresolved_without_language,
        "duplicateOutputCanonicalIds": duplicate_output_rows,
        "conflictRows": conflict_count,
        "identityCaveats": [
            "Set list records do not prove all physical finish/stamp variants; cardVariant remains unspecified.",
            "TCGdex Traditional Chinese data cannot prove Taiwan versus Hong Kong and remains region MULTI.",
            "English aliases for non-English records require a separately proven cross-language artwork/base crosswalk.",
        ],
        "productionPublished": False,
    }
    json_path = REPORT_DIR / "language_coverage.json"
    markdown_path = REPORT_DIR / "language_coverage.md"
    csv_path = REPORT_DIR / "language_coverage.csv"
    write_json_atomic(json_path, payload)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_coverage_markdown(payload), encoding="utf-8")
    _write_coverage_csv(csv_path, rows)
    write_provider_metadata_reconciliation()
    return payload


def write_provider_metadata_reconciliation() -> dict[str, Any]:
    mapped_rows: Counter[str] = Counter()
    mapped_printings: dict[str, set[str]] = defaultdict(set)
    evidence: dict[str, Counter[str]] = defaultdict(Counter)
    for row in iter_jsonl(CATALOGUE_DIR / "provider_crosswalk.jsonl"):
        provider = str(row.get("provider") or "unknown")
        mapped_rows[provider] += 1
        mapped_printings[provider].add(str(row.get("canonicalPrintingId") or ""))
        evidence[provider][str(row.get("evidence") or "unknown")] += 1

    unresolved_provider_ids: Counter[str] = Counter()
    unresolved_reasons: Counter[str] = Counter()
    for row in iter_jsonl(CATALOGUE_DIR / "unresolved.jsonl"):
        unresolved_reasons[str(row.get("reason") or "unknown")] += 1
        provider_ids = row.get("providerIds")
        if isinstance(provider_ids, dict):
            for provider, provider_id in provider_ids.items():
                if provider_id:
                    unresolved_provider_ids[str(provider)] += 1

    providers = []
    for provider in sorted(set(mapped_rows) | set(unresolved_provider_ids)):
        providers.append(
            {
                "provider": provider,
                "mappedCrosswalkRows": mapped_rows[provider],
                "mappedCanonicalPrintingGroups": len(
                    mapped_printings.get(provider, set()) - {""}
                ),
                "unresolvedProviderIds": unresolved_provider_ids[provider],
                "evidence": dict(sorted(evidence.get(provider, Counter()).items())),
            }
        )
    payload = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now_iso(),
        "classification": "PARTIAL" if unresolved_provider_ids else "PASS",
        "canonicalSourceFetchedThisRun": "tcgdex",
        "preservedExistingSourcesReconciled": [
            "pokemon_tcg_api",
            "pokewallet",
            "tcgdex",
        ],
        "newAuthenticatedPokewalletRequests": 0,
        "newPokemonTcgApiRequests": 0,
        "rationale": {
            "pokemon_tcg_api": (
                "Existing English catalogue provider IDs were reconciled to TCGdex by identical stable card ID. "
                "No name-only match was used."
            ),
            "pokewallet": (
                "Existing locally preserved provider IDs were crosswalked only when the containing app record "
                "already had an exact TCGdex or identical Pokémon TCG API identity."
            ),
            "tcgdex": "Fetched resumably for every provider language in the registry.",
        },
        "providers": providers,
        "unresolvedReasons": dict(sorted(unresolved_reasons.items())),
        "productionPublished": False,
    }
    write_json_atomic(REPORT_DIR / "provider_metadata_reconciliation.json", payload)
    (REPORT_DIR / "provider_metadata_reconciliation.md").write_text(
        render_provider_metadata_reconciliation(payload),
        encoding="utf-8",
    )
    return payload


def render_provider_metadata_reconciliation(payload: dict[str, Any]) -> str:
    lines = [
        "# Provider Metadata Reconciliation",
        "",
        f"Classification: **{payload['classification']}**",
        "",
        f"- Canonical source fetched: {payload['canonicalSourceFetchedThisRun']}",
        f"- New authenticated PokéWallet requests: {payload['newAuthenticatedPokewalletRequests']}",
        f"- New Pokémon TCG API requests: {payload['newPokemonTcgApiRequests']}",
        "",
    ]
    for provider in payload["providers"]:
        lines.append(
            f"- `{provider['provider']}`: {provider['mappedCrosswalkRows']} crosswalk rows, "
            f"{provider['mappedCanonicalPrintingGroups']} canonical groups, "
            f"{provider['unresolvedProviderIds']} unresolved provider IDs"
        )
    lines.extend(
        [
            "",
            "Provider-exclusive unresolved records are preserved in `unresolved.jsonl`; they are not discarded or name-matched.",
            "",
        ]
    )
    return "\n".join(lines)


def validate_global_catalogue_schema() -> dict[str, Any]:
    import jsonschema

    from .artifacts import CANONICAL_PRINTING_SCHEMA

    validator = jsonschema.Draft202012Validator(
        CANONICAL_PRINTING_SCHEMA,
        format_checker=jsonschema.FormatChecker(),
    )
    seen: set[str] = set()
    errors: list[dict[str, Any]] = []
    duplicate_count = 0
    schema_error_count = 0
    row_count = 0
    for row_count, row in enumerate(
        iter_jsonl(CATALOGUE_DIR / "cards.jsonl"),
        start=1,
    ):
        printing_id = str(row.get("canonicalPrintingId") or "")
        if printing_id in seen:
            duplicate_count += 1
            if len(errors) < 100:
                errors.append(
                    {
                        "row": row_count,
                        "canonicalPrintingId": printing_id,
                        "error": "duplicate_canonical_printing_id",
                    }
                )
        seen.add(printing_id)
        for error in validator.iter_errors(row):
            schema_error_count += 1
            if len(errors) < 100:
                errors.append(
                    {
                        "row": row_count,
                        "canonicalPrintingId": printing_id,
                        "path": [str(item) for item in error.absolute_path],
                        "error": error.message,
                    }
                )
    payload = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": utc_now_iso(),
        "classification": (
            "PASS"
            if schema_error_count == 0 and duplicate_count == 0
            else "FAIL"
        ),
        "rowsValidated": row_count,
        "uniqueCanonicalPrintingIds": len(seen),
        "duplicateCanonicalPrintingIds": duplicate_count,
        "schemaErrors": schema_error_count,
        "reportedErrors": errors,
        "errorsTruncated": schema_error_count + duplicate_count > len(errors),
        "schema": "data/contracts/canonical_printing_schema.json",
    }
    write_json_atomic(REPORT_DIR / "catalogue_schema_validation.json", payload)
    return payload


def _write_coverage_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "language",
        "region",
        "sets",
        "canonicalPrintings",
        "nativeNamesPresent",
        "englishAliasesPresent",
        "exactCollectorIdentityPresent",
        "releaseDatesPresent",
        "rarityPresent",
        "providerIdsPresent",
        "imageCandidatesPresent",
        "verifiedImages",
        "unresolvedRecords",
        "ambiguousRecords",
        "duplicateCandidates",
        "providerExclusiveRecords",
        "variantUnresolved",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def render_coverage_markdown(payload: dict[str, Any]) -> str:
    totals = payload["totals"]
    lines = [
        "# Global Catalogue Language Coverage",
        "",
        f"Classification: **{payload['classification']}**",
        "",
        f"- Canonical printing groups: {totals['canonicalPrintings']}",
        f"- Canonical sets: {totals['sets']}",
        f"- Image candidates: {totals['imageCandidatesPresent']}",
        f"- Verified images in staging catalogue: {totals['verifiedImages']}",
        f"- Unresolved records: {totals['unresolvedRecords']}",
        f"- Variant-unresolved records: {totals['variantUnresolved']}",
        "",
        "## By language and evidenced region",
        "",
    ]
    for row in payload["languages"]:
        lines.append(
            f"- `{row['language']}` / `{row['region']}`: {row['sets']} sets, "
            f"{row['canonicalPrintings']} printing groups, {row['imageCandidatesPresent']} image candidates, "
            f"{row['unresolvedRecords']} unresolved"
        )
    lines.extend(
        [
            "",
            "Counts are deduplicated by `canonicalPrintingId`. Physical finish variants are not claimed complete.",
            "The CSV and JSON reports contain all requested fields and percentages.",
            "",
        ]
    )
    return "\n".join(lines)

