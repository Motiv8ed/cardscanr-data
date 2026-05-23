#!/usr/bin/env python3
"""Audit source and provenance coverage for app catalogue records."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from promote_provider_catalog_to_app_catalog import (
    APP_ROOT,
    ROOT,
    SCHEMA_VERSION,
    build_app_set_token_map,
    build_candidate,
    collector_identity_key,
    iter_provider_records,
    load_app_sets,
    load_json,
    make_position_key,
)


REPORT_JSON_PATH = ROOT / "reports" / "app_catalogue_source_audit_latest.json"
REPORT_MD_PATH = ROOT / "reports" / "app_catalogue_source_audit_latest.md"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_json_if_changed(path: Path, payload: Any) -> bool:
    encoded = json_bytes(payload)
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_bytes(encoded)
    tmp_path.replace(path)
    return True


def write_text_if_changed(path: Path, text: str) -> bool:
    encoded = text.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_bytes(encoded)
    tmp_path.replace(path)
    return True


def normalize_source(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in {"pokemon_tcg_api", "pokemontcgapi", "pokemonTcgApi".lower()}:
        return "pokemon_tcg_api"
    if text in {"tcgdex", "tcg_dex"}:
        return "tcgdex"
    if text in {"pokewallet", "pokewallet_provider_promotion"}:
        return "pokewallet"
    if text in {"manual", "seed", "manual_seed", "manual/seed"}:
        return "manual/seed"
    return text


def provider_ids(card: dict[str, Any]) -> dict[str, Any]:
    value = card.get("providerIds")
    return value if isinstance(value, dict) else {}


def external_ids(card: dict[str, Any]) -> dict[str, Any]:
    value = card.get("externalIds")
    return value if isinstance(value, dict) else {}


def source_evidence(card: dict[str, Any], *, file_source: Any) -> set[str]:
    evidence: set[str] = set()
    providers = provider_ids(card)
    external = external_ids(card)
    promotion = card.get("promotionMetadata") if isinstance(card.get("promotionMetadata"), dict) else {}

    if providers.get("pokewallet") or promotion.get("provider") == "pokewallet":
        evidence.add("pokewallet")
    if providers.get("pokemonTcgApi") or external.get("pokemonTcgApiId"):
        evidence.add("pokemon_tcg_api")
    if providers.get("tcgdex") or external.get("tcgdexCardId"):
        evidence.add("tcgdex")

    for field in (card.get("source"), card.get("imageSource"), promotion.get("source")):
        normalized = normalize_source(field)
        if normalized:
            evidence.add(normalized)

    if not evidence:
        normalized = normalize_source(file_source)
        if normalized:
            evidence.add(normalized)

    return evidence


def primary_source(evidence: set[str]) -> str:
    meaningful = {item for item in evidence if item not in {"unknown"}}
    if not meaningful:
        return "unknown"
    if len(meaningful) > 1:
        return "merged/multiple"
    return next(iter(meaningful))


def app_languages() -> list[str]:
    if not APP_ROOT.exists():
        return []
    return sorted(item.name for item in APP_ROOT.iterdir() if item.is_dir())


def iter_app_cards(languages: list[str] | None = None) -> list[dict[str, Any]]:
    wanted = set(languages or app_languages())
    rows: list[dict[str, Any]] = []
    for language in sorted(wanted):
        cards_dir = APP_ROOT / language / "cards"
        if not cards_dir.exists():
            continue
        for path in sorted(cards_dir.glob("*.json"), key=lambda item: item.name.lower()):
            payload = load_json(path)
            if not isinstance(payload, dict):
                continue
            cards = payload.get("cards")
            if not isinstance(cards, list):
                continue
            for card in cards:
                if not isinstance(card, dict):
                    continue
                rows.append(
                    {
                        "language": language,
                        "setId": str(card.get("setId") or payload.get("setId") or path.stem),
                        "setName": str(card.get("setName") or payload.get("setName") or path.stem),
                        "file": path.relative_to(ROOT).as_posix(),
                        "fileSource": payload.get("source"),
                        "card": card,
                    }
                )
    return rows


def app_identity_key(row: dict[str, Any]) -> str:
    card = row["card"]
    variant = card.get("availableVariants")
    if isinstance(variant, list):
        variant_key = ",".join(sorted(str(item).strip().lower() for item in variant if str(item).strip())) or "normal"
    elif isinstance(variant, str) and variant.strip():
        variant_key = variant.strip().lower()
    else:
        variant_key = "normal"
    collector = str(card.get("collectorNumber") or "").strip()
    normalized_name = str(card.get("normalizedName") or card.get("name") or "").strip()
    return "|".join([str(row["language"]), str(row["setId"]), collector_identity_key(collector), normalized_name, variant_key])


def provider_identity_sets(languages: list[str]) -> tuple[set[str], set[str]]:
    provider_identities: set[str] = set()
    provider_positions: set[str] = set()
    app_set_maps = {language: build_app_set_token_map(load_app_sets(language)) for language in sorted(set(languages))}
    for record in iter_provider_records(languages):
        candidate, reason = build_candidate(
            record,
            app_set_map=app_set_maps.get(record.language, {}),
            enabled_languages=set(languages),
        )
        if not candidate or reason != "promotable":
            continue
        provider_identities.add(candidate.identity_key)
        provider_positions.add(make_position_key(record.language, candidate.app_set_id, candidate.collector_number))
    return provider_identities, provider_positions


def build_report(languages: list[str] | None = None) -> dict[str, Any]:
    selected = sorted(languages or app_languages())
    provider_identities, provider_positions = provider_identity_sets(selected)

    total_by_language: Counter[str] = Counter()
    primary_by_language: dict[str, Counter[str]] = defaultdict(Counter)
    evidence_by_language: dict[str, Counter[str]] = defaultdict(Counter)
    by_set_source: dict[str, Counter[str]] = defaultdict(Counter)
    unknown_samples: list[dict[str, Any]] = []
    no_source_samples: list[dict[str, Any]] = []
    duplicate_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)

    pokewallet_provider_ids: Counter[str] = Counter()
    without_pokewallet_provider_ids: Counter[str] = Counter()
    multiple_provider_ids: Counter[str] = Counter()
    not_represented_by_pokewallet: Counter[str] = Counter()
    earlier_imports: Counter[str] = Counter()
    variant_count: Counter[str] = Counter()
    unknown_source_count: Counter[str] = Counter()

    for row in iter_app_cards(selected):
        language = str(row["language"])
        set_id = str(row["setId"])
        card = row["card"]
        total_by_language[language] += 1
        providers = provider_ids(card)
        provider_id_values = [value for value in providers.values() if isinstance(value, str) and value.strip()]
        if providers.get("pokewallet"):
            pokewallet_provider_ids[language] += 1
        else:
            without_pokewallet_provider_ids[language] += 1
        if len(provider_id_values) > 1:
            multiple_provider_ids[language] += 1

        evidence = source_evidence(card, file_source=row.get("fileSource"))
        primary = primary_source(evidence)
        primary_by_language[language][primary] += 1
        by_set_source[f"{language}|{set_id}"][primary] += 1
        for item in sorted(evidence):
            evidence_by_language[language][item] += 1

        if primary in {"pokemon_tcg_api", "tcgdex"}:
            earlier_imports[language] += 1
        if primary == "unknown":
            unknown_source_count[language] += 1
            if len(unknown_samples) < 100:
                unknown_samples.append(sample_card(row, primary, evidence))
        if not evidence:
            if len(no_source_samples) < 100:
                no_source_samples.append(sample_card(row, primary, evidence))

        identity_key = app_identity_key(row)
        duplicate_groups[identity_key].append(row)
        position_key = "|".join([language, set_id, collector_identity_key(card.get("collectorNumber"))])
        if not providers.get("pokewallet") and identity_key not in provider_identities and position_key not in provider_positions:
            not_represented_by_pokewallet[language] += 1
        variants = card.get("availableVariants")
        if isinstance(variants, list) and variants:
            variant_count[language] += 1

    duplicate_summaries = []
    for identity, rows in duplicate_groups.items():
        if len(rows) <= 1:
            continue
        first = rows[0]
        duplicate_summaries.append(
            {
                "identityKey": identity,
                "count": len(rows),
                "language": first["language"],
                "setId": first["setId"],
                "name": first["card"].get("name"),
                "collectorNumber": first["card"].get("collectorNumber"),
                "sources": sorted(primary_source(source_evidence(row["card"], file_source=row.get("fileSource"))) for row in rows),
            }
        )
    duplicate_summaries.sort(key=lambda item: (-int(item["count"]), str(item["identityKey"])))

    by_set_rows = []
    for key, counts in sorted(by_set_source.items()):
        language, set_id = key.split("|", 1)
        by_set_rows.append({"language": language, "setId": set_id, "sourceCounts": dict(sorted(counts.items()))})

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": now_utc(),
        "languagesProcessed": selected,
        "totalAppCatalogueByLanguage": dict(sorted(total_by_language.items())),
        "primarySourceCountsByLanguage": {
            language: dict(sorted(counter.items())) for language, counter in sorted(primary_by_language.items())
        },
        "sourceEvidenceCountsByLanguage": {
            language: dict(sorted(counter.items())) for language, counter in sorted(evidence_by_language.items())
        },
        "appRecordsWithPokewalletProviderIds": dict(sorted(pokewallet_provider_ids.items())),
        "appRecordsWithoutPokewalletProviderIds": dict(sorted(without_pokewallet_provider_ids.items())),
        "appRecordsNotRepresentedByPokewalletIdentityOrPosition": dict(sorted(not_represented_by_pokewallet.items())),
        "appRecordsFromEarlierImports": dict(sorted(earlier_imports.items())),
        "appRecordsWithMultipleProviderIds": dict(sorted(multiple_provider_ids.items())),
        "appRecordsWithUnknownSource": dict(sorted(unknown_source_count.items())),
        "uniqueCanonicalIdentityCount": len(duplicate_groups),
        "duplicateCanonicalIdentityGroupCount": len(duplicate_summaries),
        "duplicateCanonicalIdentityRecordCount": sum(int(item["count"]) for item in duplicate_summaries),
        "variantRecordCountByLanguage": dict(sorted(variant_count.items())),
        "sourceCountsBySet": by_set_rows,
        "unknownSourceSamples": unknown_samples,
        "cardsWithNoProviderOrSourceMetadataSamples": no_source_samples,
        "duplicateCanonicalIdentityGroups": duplicate_summaries[:100],
    }


def sample_card(row: dict[str, Any], primary: str, evidence: set[str]) -> dict[str, Any]:
    card = row["card"]
    return {
        "language": row["language"],
        "setId": row["setId"],
        "collectorNumber": card.get("collectorNumber"),
        "name": card.get("name"),
        "file": row["file"],
        "primarySource": primary,
        "sourceEvidence": sorted(evidence),
        "providerIds": provider_ids(card),
        "externalIds": external_ids(card),
        "imageSource": card.get("imageSource"),
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# App Catalogue Source Audit",
        "",
        f"- generatedAtUtc: {report['generatedAtUtc']}",
        f"- languages: {', '.join(report['languagesProcessed'])}",
        "",
        "## Totals",
    ]
    for language, count in sorted(report["totalAppCatalogueByLanguage"].items()):
        lines.append(f"- {language}: {count}")
    lines.extend(["", "## Primary Source Counts"])
    for language, counts in sorted(report["primarySourceCountsByLanguage"].items()):
        lines.append(f"- {language}: {counts}")
    lines.extend(["", "## Pokewallet Coverage"])
    lines.append(f"- with Pokewallet provider IDs: {report['appRecordsWithPokewalletProviderIds']}")
    lines.append(f"- without Pokewallet provider IDs: {report['appRecordsWithoutPokewalletProviderIds']}")
    lines.append(
        "- not represented by Pokewallet identity/position: "
        f"{report['appRecordsNotRepresentedByPokewalletIdentityOrPosition']}"
    )
    lines.extend(["", "## Source Gaps"])
    lines.append(f"- unknown source records: {report['appRecordsWithUnknownSource']}")
    lines.append(f"- multiple provider ID records: {report['appRecordsWithMultipleProviderIds']}")
    lines.append(f"- duplicate canonical identity groups: {report['duplicateCanonicalIdentityGroupCount']}")
    lines.append(f"- unique canonical identities: {report['uniqueCanonicalIdentityCount']}")
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit app catalogue card source/provenance.")
    parser.add_argument("--languages", default=None, help="Comma-separated languages to evaluate. Defaults to app languages.")
    parser.add_argument("--no-report", action="store_true", help="Print summary only; do not write reports.")
    return parser.parse_args()


def parse_languages(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def main() -> int:
    args = parse_args()
    report = build_report(parse_languages(args.languages))
    if not args.no_report:
        write_json_if_changed(REPORT_JSON_PATH, report)
        write_text_if_changed(REPORT_MD_PATH, markdown_report(report))
    summary_keys = [
        "generatedAtUtc",
        "languagesProcessed",
        "totalAppCatalogueByLanguage",
        "primarySourceCountsByLanguage",
        "sourceEvidenceCountsByLanguage",
        "appRecordsWithPokewalletProviderIds",
        "appRecordsWithoutPokewalletProviderIds",
        "appRecordsNotRepresentedByPokewalletIdentityOrPosition",
        "appRecordsFromEarlierImports",
        "appRecordsWithMultipleProviderIds",
        "appRecordsWithUnknownSource",
        "uniqueCanonicalIdentityCount",
        "duplicateCanonicalIdentityGroupCount",
        "duplicateCanonicalIdentityRecordCount",
        "variantRecordCountByLanguage",
    ]
    print(json.dumps({key: report[key] for key in summary_keys}, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
