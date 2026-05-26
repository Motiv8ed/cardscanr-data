#!/usr/bin/env python3
"""Report JP current price coverage against the JP app catalogue."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
JP_CATALOG_DIR = ROOT / "public" / "v1" / "catalog" / "pokemon" / "jp" / "cards"
JP_CURRENT_PRICE_DIR = ROOT / "public" / "v1" / "prices" / "current" / "pokemon" / "jp"
REPORT_JSON_PATH = ROOT / "reports" / "jp_price_coverage_latest.json"
REPORT_MD_PATH = ROOT / "reports" / "jp_price_coverage_latest.md"
IMPORT_REPORT_PATH = ROOT / "reports" / "pokewallet_price_import_latest.json"
WORKER_REPORT_PATH = ROOT / "reports" / "pokewallet_missing_price_worker_latest.json"


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def try_load_json(path: Path) -> Any:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


def format_percent(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.00%"
    return f"{(numerator / denominator) * 100:.2f}%"


def read_catalog_cards() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    cards: list[dict[str, Any]] = []
    cards_by_canonical: dict[str, dict[str, Any]] = {}
    cards_by_set: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not JP_CATALOG_DIR.exists():
        return cards, cards_by_canonical, cards_by_set

    for path in sorted(JP_CATALOG_DIR.glob("*.json"), key=lambda item: item.name.lower()):
        payload = try_load_json(path)
        if not isinstance(payload, dict):
            continue
        entries = payload.get("cards")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            canonical = str(entry.get("canonicalBaseId") or "").strip()
            set_id = str(entry.get("setId") or path.stem).strip()
            if not canonical or not set_id:
                continue
            card = {
                "canonicalBaseId": canonical,
                "setId": set_id,
                "setName": str(entry.get("setName") or "").strip(),
                "collectorNumber": str(entry.get("collectorNumber") or "").strip(),
                "displayName": str(entry.get("displayName") or entry.get("name") or "").strip(),
                "normalizedName": str(entry.get("normalizedName") or "").strip(),
                "pricingReferences": entry.get("pricingReferences") if isinstance(entry.get("pricingReferences"), dict) else {},
            }
            cards.append(card)
            cards_by_canonical[canonical] = card
            cards_by_set[set_id].append(card)
    return cards, cards_by_canonical, cards_by_set


def read_current_price_records() -> tuple[list[dict[str, Any]], Counter[str], Counter[str], Counter[str], Counter[str]]:
    records: list[dict[str, Any]] = []
    source_counts: Counter[str] = Counter()
    currency_counts: Counter[str] = Counter()
    variant_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    if not JP_CURRENT_PRICE_DIR.exists():
        return records, source_counts, currency_counts, variant_counts, status_counts

    for path in sorted(JP_CURRENT_PRICE_DIR.glob("*.json"), key=lambda item: item.name.lower()):
        if path.name == "status.json":
            continue
        payload = try_load_json(path)
        if not isinstance(payload, dict):
            continue
        prices = payload.get("prices")
        if not isinstance(prices, list):
            continue
        for entry in prices:
            if not isinstance(entry, dict):
                continue
            record = {
                "file": path.name,
                "canonicalCardId": str(entry.get("canonicalCardId") or "").strip(),
                "canonicalId": str(entry.get("canonicalId") or "").strip(),
                "setId": str(entry.get("setId") or payload.get("setId") or path.stem).strip(),
                "source": str(entry.get("source") or payload.get("source") or "unknown").strip(),
                "currency": str(entry.get("currency") or payload.get("currency") or "unknown").strip(),
                "variant": str(entry.get("variant") or "unknown").strip(),
                "status": str(entry.get("status") or payload.get("status") or "unknown").strip(),
            }
            records.append(record)
            source_counts[record["source"]] += 1
            currency_counts[record["currency"]] += 1
            variant_counts[record["variant"]] += 1
            status_counts[record["status"]] += 1
    return records, source_counts, currency_counts, variant_counts, status_counts


def build_set_rows(
    cards_by_set: dict[str, list[dict[str, Any]]],
    covered_card_ids: set[str],
    records_by_card: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for set_id, cards in sorted(cards_by_set.items(), key=lambda item: item[0]):
        total_cards = len(cards)
        covered_cards = sum(1 for card in cards if card["canonicalBaseId"] in covered_card_ids)
        missing_cards = total_cards - covered_cards
        rows.append(
            {
                "setId": set_id,
                "setName": cards[0].get("setName") if cards else set_id,
                "totalCards": total_cards,
                "coveredCards": covered_cards,
                "missingCards": missing_cards,
                "coveragePct": float((covered_cards / total_cards) * 100) if total_cards else 0.0,
                "coveredCardIds": sorted(card["canonicalBaseId"] for card in cards if card["canonicalBaseId"] in covered_card_ids),
                "missingCardIds": sorted(card["canonicalBaseId"] for card in cards if card["canonicalBaseId"] not in covered_card_ids),
                "multiplePriceRowCards": sum(1 for card in cards if len(records_by_card.get(card["canonicalBaseId"], [])) > 1),
            }
        )
    return rows


def summarize_duplicate_like_rows(records_by_card: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    multi_row_cards: list[dict[str, Any]] = []
    exact_duplicate_counts: Counter[str] = Counter()
    exact_duplicate_examples: list[dict[str, Any]] = []
    for card_id, rows in records_by_card.items():
        if len(rows) > 1:
            source_counts = Counter(row["source"] for row in rows)
            currency_counts = Counter(row["currency"] for row in rows)
            variant_counts = Counter(row["variant"] for row in rows)
            multi_row_cards.append(
                {
                    "canonicalCardId": card_id,
                    "rowCount": len(rows),
                    "sourceCounts": dict(sorted(source_counts.items())),
                    "currencyCounts": dict(sorted(currency_counts.items())),
                    "variantCounts": dict(sorted(variant_counts.items())),
                    "files": sorted({row["file"] for row in rows}),
                }
            )

        seen_ids: Counter[str] = Counter(row["canonicalId"] for row in rows if row.get("canonicalId"))
        for canonical_id, count in seen_ids.items():
            if count > 1:
                exact_duplicate_counts[canonical_id] += count - 1
                exact_duplicate_examples.append(
                    {
                        "canonicalCardId": card_id,
                        "canonicalId": canonical_id,
                        "duplicateRows": count,
                    }
                )

    multi_row_cards.sort(key=lambda item: (-int(item["rowCount"]), item["canonicalCardId"]))
    exact_duplicate_examples.sort(key=lambda item: (-int(item["duplicateRows"]), item["canonicalCardId"]))
    return {
        "cardsWithMultipleCurrentPriceRows": len(multi_row_cards),
        "multiRowCardExamples": multi_row_cards[:10],
        "exactDuplicatePriceRowCount": sum(exact_duplicate_counts.values()),
        "exactDuplicatePriceRowExamples": exact_duplicate_examples[:10],
    }


def load_supporting_reports() -> dict[str, Any]:
    import_report = try_load_json(IMPORT_REPORT_PATH)
    worker_report = try_load_json(WORKER_REPORT_PATH)
    return {
        "importReport": import_report if isinstance(import_report, dict) else None,
        "workerReport": worker_report if isinstance(worker_report, dict) else None,
    }


def build_report() -> dict[str, Any]:
    cards, cards_by_canonical, cards_by_set = read_catalog_cards()
    price_records, source_counts, currency_counts, variant_counts, status_counts = read_current_price_records()
    records_by_card: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in price_records:
        canonical_card_id = record.get("canonicalCardId")
        if canonical_card_id:
            records_by_card[canonical_card_id].append(record)

    covered_card_ids = set(records_by_card.keys()) & set(cards_by_canonical.keys())
    uncovered_card_ids = sorted(set(cards_by_canonical.keys()) - covered_card_ids)
    orphan_records = [record for record in price_records if record.get("canonicalCardId") not in cards_by_canonical]
    current_price_rows_without_canonical_id = sum(1 for record in price_records if not record.get("canonicalCardId"))

    set_rows = build_set_rows(cards_by_set, covered_card_ids, records_by_card)
    worst_sets = sorted(set_rows, key=lambda item: (item["coveragePct"], -item["missingCards"], item["setId"]))[:10]
    best_sets = sorted(set_rows, key=lambda item: (-item["coveragePct"], item["missingCards"], item["setId"]))[:10]

    duplicate_summary = summarize_duplicate_like_rows(records_by_card)
    supporting_reports = load_supporting_reports()
    import_report = supporting_reports["importReport"]
    worker_report = supporting_reports["workerReport"]

    import_unmatched = int(import_report.get("unmatchedRecords") or 0) if import_report else None
    import_unusable = int(import_report.get("unusableRecords") or 0) if import_report else None
    import_validation = str(import_report.get("validationResult") or "") if import_report else ""
    worker_complete = bool(worker_report and worker_report.get("status") == "complete")
    missing_sets_selected = int(import_report.get("missingPriceSetsSelected") or 0) if import_report else None

    coverage_pct = float((len(covered_card_ids) / len(cards)) * 100) if cards else 0.0
    coverage_state = "complete" if len(uncovered_card_ids) == 0 else "partial"

    app_readiness_notes: list[str] = []
    if coverage_state == "complete":
        app_readiness_notes.append("JP catalogue cards have at least one current price entry.")
    else:
        app_readiness_notes.append("JP catalogue cards still have uncovered current-price gaps.")
    if orphan_records:
        app_readiness_notes.append(f"{len(orphan_records):,} current price rows do not map to JP app cards and should be reviewed.")
    if duplicate_summary["exactDuplicatePriceRowCount"]:
        app_readiness_notes.append("Exact duplicate price rows were detected in current price files.")
    if worker_complete and missing_sets_selected == 0:
        app_readiness_notes.append("Missing-set JP price import is complete; move to non-price audits.")
    else:
        app_readiness_notes.append("JP missing-set import still needs attention before treating coverage as final.")

    readiness_status = "ready_for_app_validation" if coverage_state == "complete" and not orphan_records and not duplicate_summary["exactDuplicatePriceRowCount"] else "needs_review"

    return {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ledgerPath": "data/pokewallet_price_request_ledger.json",
        "catalogue": {
            "totalJpAppCatalogueCards": len(cards),
            "coveredJpAppCatalogueCards": len(covered_card_ids),
            "uncoveredJpAppCatalogueCards": len(uncovered_card_ids),
            "coveragePct": coverage_pct,
            "uncoveredCardIds": uncovered_card_ids,
        },
        "currentPriceFiles": {
            "fileCount": len([path for path in JP_CURRENT_PRICE_DIR.glob("*.json") if path.name != "status.json"]),
            "recordCount": len(price_records),
            "statusCounts": dict(sorted(status_counts.items())),
        },
        "setCoverage": {
            "worstMissingCoverageSets": worst_sets,
            "bestCoverageSets": best_sets,
            "allSets": set_rows,
        },
        "duplicateAndAmbiguous": {
            **duplicate_summary,
            "orphanCurrentPriceRows": len(orphan_records),
            "orphanCurrentPriceRowExamples": orphan_records[:10],
            "currentPriceRowsWithoutCanonicalCardId": current_price_rows_without_canonical_id,
        },
        "breakdowns": {
            "sourceCounts": dict(sorted(source_counts.items())),
            "currencyCounts": dict(sorted(currency_counts.items())),
            "variantCounts": dict(sorted(variant_counts.items())),
        },
        "supportingReports": {
            "latestImportReportAvailable": import_report is not None,
            "latestWorkerReportAvailable": worker_report is not None,
            "latestImportMissingPriceSetsSelected": missing_sets_selected,
            "latestImportUnmatchedRecords": import_unmatched,
            "latestImportUnusableRecords": import_unusable,
            "latestImportValidationResult": import_validation or None,
            "workerStatus": str(worker_report.get("status") or "") if worker_report else None,
        },
        "appReadinessSummary": {
            "status": readiness_status,
            "message": "JP current prices fully cover the JP app catalogue." if coverage_state == "complete" else "JP current prices still have uncovered app cards.",
            "nextStep": "Run unmatched/unusable audits and app integration validation." if coverage_state == "complete" else "Continue the missing-set import worker until coverage is complete.",
            "notes": app_readiness_notes,
            "coverageComplete": coverage_state == "complete",
            "workerComplete": worker_complete,
            "latestImportSelectedMissingSets": missing_sets_selected,
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    catalogue = report["catalogue"]
    current = report["currentPriceFiles"]
    coverage = report["setCoverage"]
    dupes = report["duplicateAndAmbiguous"]
    breakdowns = report["breakdowns"]
    readiness = report["appReadinessSummary"]
    support = report["supportingReports"]

    add("# JP Price Coverage Audit")
    add("")
    add(f"- generatedAtUtc: {report['generatedAtUtc']}")
    add(f"- ledgerPath: {report['ledgerPath']}")
    add("")
    add("## Coverage Summary")
    add("")
    add(f"- total JP app catalogue cards: {catalogue['totalJpAppCatalogueCards']:,}")
    add(f"- JP cards with at least one current price: {catalogue['coveredJpAppCatalogueCards']:,}")
    add(f"- JP cards without current price: {catalogue['uncoveredJpAppCatalogueCards']:,}")
    add(f"- JP card price coverage: {catalogue['coveragePct']:.2f}%")
    add(f"- current price files: {current['fileCount']:,}")
    add(f"- current price rows: {current['recordCount']:,}")
    add("")

    add("## Worst Coverage Sets")
    add("")
    add("| Set ID | Set Name | Total Cards | Covered | Missing | Coverage |")
    add("|---|---|---:|---:|---:|---:|")
    for item in coverage["worstMissingCoverageSets"]:
        add(
            f"| {item['setId']} | {item['setName']} | {item['totalCards']:,} | {item['coveredCards']:,} | {item['missingCards']:,} | {item['coveragePct']:.2f}% |"
        )
    add("")

    add("## Best Coverage Sets")
    add("")
    add("| Set ID | Set Name | Total Cards | Covered | Missing | Coverage |")
    add("|---|---|---:|---:|---:|---:|")
    for item in coverage["bestCoverageSets"]:
        add(
            f"| {item['setId']} | {item['setName']} | {item['totalCards']:,} | {item['coveredCards']:,} | {item['missingCards']:,} | {item['coveragePct']:.2f}% |"
        )
    add("")

    add("## Duplicate / Ambiguous Matches")
    add("")
    add(f"- cards with multiple current price rows: {dupes['cardsWithMultipleCurrentPriceRows']:,}")
    add(f"- exact duplicate price rows: {dupes['exactDuplicatePriceRowCount']:,}")
    add(f"- orphan current price rows not mapped to app cards: {dupes['orphanCurrentPriceRows']:,}")
    add(f"- current price rows missing canonicalCardId: {dupes['currentPriceRowsWithoutCanonicalCardId']:,}")
    if dupes["multiRowCardExamples"]:
        add("")
        add("### Multi-row examples")
        for item in dupes["multiRowCardExamples"][:5]:
            add(
                f"- {item['canonicalCardId']}: {item['rowCount']} rows (sources: {item['sourceCounts']}, currencies: {item['currencyCounts']}, variants: {item['variantCounts']})"
            )
    if dupes["exactDuplicatePriceRowExamples"]:
        add("")
        add("### Exact duplicate examples")
        for item in dupes["exactDuplicatePriceRowExamples"][:5]:
            add(f"- {item['canonicalCardId']}: {item['canonicalId']} duplicated {item['duplicateRows']}x")
    add("")

    add("## Unmatched / Unusable")
    add("")
    add(f"- latest import missingPriceSetsSelected: {support['latestImportMissingPriceSetsSelected']}")
    add(f"- latest import unmatched records: {support['latestImportUnmatchedRecords'] if support['latestImportUnmatchedRecords'] is not None else 'n/a'}")
    add(f"- latest import unusable records: {support['latestImportUnusableRecords'] if support['latestImportUnusableRecords'] is not None else 'n/a'}")
    if support.get("latestImportValidationResult"):
        add(f"- latest import validation result: {support['latestImportValidationResult']}")
    add("")

    add("## Breakdown")
    add("")
    add(f"- source counts: {breakdowns['sourceCounts']}")
    add(f"- currency counts: {breakdowns['currencyCounts']}")
    add(f"- variant counts: {breakdowns['variantCounts']}")
    add("")

    add("## App Readiness Summary")
    add("")
    add(f"- status: {readiness['status']}")
    add(f"- message: {readiness['message']}")
    add(f"- next step: {readiness['nextStep']}")
    for note in readiness.get("notes", []):
        add(f"- note: {note}")
    add("")
    add("## Supporting Reports")
    add("")
    add(f"- latest worker report available: {'yes' if support['latestWorkerReportAvailable'] else 'no'}")
    add(f"- latest import report available: {'yes' if support['latestImportReportAvailable'] else 'no'}")
    add("")
    return "\n".join(lines)


def main() -> int:
    report = build_report()
    write_json(REPORT_JSON_PATH, report)
    REPORT_MD_PATH.write_text(render_markdown(report), encoding="utf-8", newline="\n")

    print("JP price coverage audit")
    print(f"  total JP app catalogue cards: {report['catalogue']['totalJpAppCatalogueCards']}")
    print(f"  JP cards with at least one current price: {report['catalogue']['coveredJpAppCatalogueCards']}")
    print(f"  JP cards without current price: {report['catalogue']['uncoveredJpAppCatalogueCards']}")
    print(f"  JP card price coverage: {report['catalogue']['coveragePct']:.2f}%")
    worst = report['setCoverage']['worstMissingCoverageSets']
    best = report['setCoverage']['bestCoverageSets']
    if worst:
        print("  worst missing-coverage sets:")
        for item in worst[:5]:
            print(
                f"    - {item['setId']} {item['setName']}: {item['coveragePct']:.2f}% ({item['missingCards']} missing of {item['totalCards']})"
            )
    if best:
        print("  best coverage sets:")
        for item in best[:5]:
            print(
                f"    - {item['setId']} {item['setName']}: {item['coveragePct']:.2f}% ({item['missingCards']} missing of {item['totalCards']})"
            )
    dupes = report['duplicateAndAmbiguous']
    print(
        "  duplicate/ambiguous rows: "
        f"multi-row cards={dupes['cardsWithMultipleCurrentPriceRows']}, exact duplicates={dupes['exactDuplicatePriceRowCount']}"
    )
    print(
        "  unmatched/unusable summary: "
        f"orphan rows={dupes['orphanCurrentPriceRows']}, missing canonical ids={dupes['currentPriceRowsWithoutCanonicalCardId']}, "
        f"latest import unmatched={report['supportingReports']['latestImportUnmatchedRecords'] if report['supportingReports']['latestImportUnmatchedRecords'] is not None else 'n/a'}, "
        f"latest import unusable={report['supportingReports']['latestImportUnusableRecords'] if report['supportingReports']['latestImportUnusableRecords'] is not None else 'n/a'}"
    )
    print(f"  app readiness: {report['appReadinessSummary']['status']}")
    print(f"  wrote: {REPORT_JSON_PATH.relative_to(ROOT)}")
    print(f"  wrote: {REPORT_MD_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
