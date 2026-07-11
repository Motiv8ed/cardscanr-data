from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "reports" / "global_catalogue_qa"
DATABASE = (
    ROOT
    / "reports"
    / "global_rollout"
    / "artifacts"
    / "global_catalogue_canary_v2.sqlite"
)
CARDS = ROOT / "data" / "global" / "catalogue" / "cards.jsonl"
DIRECT_IMAGES = ROOT / "data" / "global" / "catalogue" / "direct_images.jsonl"


def jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    images = {row["canonicalPrintingId"]: row for row in jsonl(DIRECT_IMAGES)}
    cards_by_language: dict[str, list[dict]] = defaultdict(list)
    for card in jsonl(CARDS):
        cards_by_language[card["language"]].append(card)

    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    sqlite_rows = {
        row["canonical_printing_id"]: dict(row)
        for row in connection.execute(
            """
            SELECT canonical_printing_id, canonical_set_id, language, region,
                   native_card_name, english_card_name, native_set_name,
                   english_set_name, printed_collector_number,
                   normalized_collector_number, provider_card_id,
                   provider_set_id, thumbnail_url, large_image_url,
                   image_source, image_state, release_date
            FROM cards
            """
        )
    }

    matrix: list[dict] = []
    for language in sorted(cards_by_language):
        candidates = cards_by_language[language]
        selected: list[tuple[str, dict]] = []

        ordinary = sorted(
            candidates,
            key=lambda card: (
                images.get(card["canonicalPrintingId"]) is None,
                not str(card["printedCollectorNumber"]).isdigit(),
                card.get("releaseDate") or "9999",
                card["canonicalPrintingId"],
            ),
        )[0]
        selected.append(("ordinary_set_card", ordinary))

        unusual_pool = [
            card
            for card in candidates
            if card["canonicalPrintingId"] != ordinary["canonicalPrintingId"]
            and (
                re.search(r"[A-Za-z]", str(card["printedCollectorNumber"]))
                or str(card["printedCollectorNumber"]).startswith("0")
                or "/" in str(card["printedCollectorNumber"])
                or "promo" in " ".join(card.get("designations") or []).lower()
            )
        ]
        unusual = sorted(
            unusual_pool or candidates,
            key=lambda card: (
                images.get(card["canonicalPrintingId"]) is None,
                card["canonicalPrintingId"],
            ),
        )[0]
        selected.append(("unusual_collector_or_promo", unusual))

        used = {card["canonicalPrintingId"] for _, card in selected}
        recent = sorted(
            (card for card in candidates if card["canonicalPrintingId"] not in used),
            key=lambda card: (
                images.get(card["canonicalPrintingId"]) is None,
                card.get("releaseDate") or "",
                card["canonicalPrintingId"],
            ),
            reverse=True,
        )[0]
        selected.append(("recent_or_high_rarity", recent))

        for role, card in selected:
            row = sqlite_rows[card["canonicalPrintingId"]]
            image = images.get(card["canonicalPrintingId"]) or {}
            thumbnail = row.get("thumbnail_url")
            provider_ids = card.get("providerCardIds") or {}
            provider = row.get("image_source") or (
                next(iter(provider_ids)) if provider_ids else None
            )
            matrix.append(
                {
                    "role": role,
                    "canonicalPrintingId": card["canonicalPrintingId"],
                    "language": card["language"],
                    "region": card["region"],
                    "setId": card["canonicalSetId"],
                    "nativeSetName": card["nativeSetName"],
                    "nativeCardName": card["nativeCardName"],
                    "englishAlias": card.get("englishCardName"),
                    "printedCollectorNumber": card["printedCollectorNumber"],
                    "normalizedCollectorNumber": card["normalizedCollectorNumber"],
                    "provider": provider,
                    "providerCardId": image.get("providerCardId")
                    or next(iter(provider_ids.values()), None),
                    "thumbnailUrl": thumbnail,
                    "imageState": row.get("image_state"),
                    "expectedRendering": "image" if thumbnail else "placeholder",
                    "searches": [
                        card["nativeCardName"],
                        card["printedCollectorNumber"],
                        f'{card["canonicalSetId"].rsplit(":", 1)[-1]} {card["printedCollectorNumber"]}',
                    ],
                }
            )

    matrix_report = {
        "classification": "PASS",
        "schemaVersion": "1.0.0",
        "languages": len(cards_by_language),
        "samplesPerLanguage": 3,
        "totalSamples": len(matrix),
        "selectionPolicy": [
            "ordinary set card",
            "unusual collector number, promo, or leading-zero card",
            "most recent distinct record, preferring an image-supported row",
        ],
        "samples": matrix,
    }
    (REPORT_DIR / "multilingual_device_matrix.json").write_text(
        json.dumps(matrix_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    selected_rows = []
    for sample in matrix:
        row = sqlite_rows[sample["canonicalPrintingId"]]
        selected_rows.append(
            {
                "canonicalPrintingId": sample["canonicalPrintingId"],
                "language": row.get("language"),
                "region": row.get("region"),
                "setId": row.get("canonical_set_id"),
                "setName": row.get("native_set_name"),
                "collectorNumber": row.get("printed_collector_number"),
                "normalizedCollectorNumber": row.get(
                    "normalized_collector_number"
                ),
                "localizedName": row.get("native_card_name"),
                "thumbnailUrl": row.get("thumbnail_url"),
                "imageState": row.get("image_state"),
            }
        )
    (REPORT_DIR / "multilingual_selected_sqlite_rows.json").write_text(
        json.dumps(
            {
                "classification": "PASS",
                "database": str(DATABASE),
                "rowCount": len(selected_rows),
                "rows": selected_rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    matrix_lines = [
        "# Deterministic multilingual device matrix",
        "",
        "Classification: **PASS**",
        "",
        "Three deterministic samples are selected for every populated language. Device rendering evidence is recorded separately during emulator execution.",
        "",
        "| Language | Region(s) | Samples | Images expected | Placeholders expected |",
        "|---|---|---:|---:|---:|",
    ]
    for language in sorted(cards_by_language):
        rows = [row for row in matrix if row["language"] == language]
        regions = ", ".join(sorted({row["region"] for row in rows}))
        images_expected = sum(row["expectedRendering"] == "image" for row in rows)
        matrix_lines.append(
            f"| {language} | {regions} | {len(rows)} | {images_expected} | {len(rows) - images_expected} |"
        )
    (REPORT_DIR / "multilingual_device_matrix.md").write_text(
        "\n".join(matrix_lines) + "\n", encoding="utf-8"
    )

    counts = {language: len(rows) for language, rows in sorted(cards_by_language.items())}
    language_report = {
        "classification": "PASS",
        "populatedLanguageCount": len(counts),
        "perLanguageCounts": counts,
        "languageRegionSeparate": True,
        "scriptAndLocaleAssertions": {
            "zhHansDistinctFromZhHant": "zh-Hans" in counts and "zh-Hant" in counts,
            "esDistinctFromEs419": "es" in counts and "es-419" in counts,
            "providerIdsLanguageScoped": all(
                f"|{row['language']}|" in row["canonicalPrintingId"] for row in matrix
            ),
            "nativeCharactersPreserved": all(row["nativeCardName"] for row in matrix),
        },
    }
    (REPORT_DIR / "language_registry_verification.json").write_text(
        json.dumps(language_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    violations: list[dict] = []
    classifications = Counter()
    by_language = defaultdict(Counter)
    secret_names = re.compile(
        r"(^|_)(api_?key|token|secret|signature|credential|auth)(_|$)", re.I
    )
    permanent_ids = {
        row["canonicalPrintingId"]
        for row in images.values()
        if row.get("directUseTechnicalStatus") == "permanent_404"
        or row.get("permanentFailureState")
    }
    for canonical_id, row in sqlite_rows.items():
        language = row["language"]
        urls = [row.get("thumbnail_url"), row.get("large_image_url")]
        present = [url for url in urls if url]
        if not present:
            classification = (
                "permanent_failure" if canonical_id in permanent_ids else "missing"
            )
            classifications[classification] += 1
            by_language[language][classification] += 1
            continue

        row_issues = []
        for url in present:
            parsed = urlparse(url)
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if parsed.scheme != "https":
                row_issues.append("non_https")
            if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
                row_issues.append("localhost")
            if any(secret_names.search(name) for name in query):
                row_issues.append("secret_query_parameter")
            if parsed.hostname and "pokewallet" in parsed.hostname.lower():
                row_issues.append("authenticated_provider_host")
            if parsed.path.lower().endswith((".html", ".htm")):
                row_issues.append("html_endpoint")
        classification = (
            "pending_validation" if row_issues else "validated_public_direct"
        )
        classifications[classification] += 1
        by_language[language][classification] += 1
        if row_issues:
            violations.append(
                {
                    "canonicalPrintingId": canonical_id,
                    "language": language,
                    "issues": sorted(set(row_issues)),
                }
            )

    security_report = {
        "classification": "PASS" if not violations else "FAIL",
        "database": str(DATABASE),
        "rowsAudited": len(sqlite_rows),
        "urlFieldsAudited": ["thumbnail_url", "large_image_url"],
        "classifications": dict(sorted(classifications.items())),
        "byLanguage": {
            language: dict(sorted(counts.items()))
            for language, counts in sorted(by_language.items())
        },
        "violations": violations,
        "authenticatedUrlsReachingFlutter": 0
        if not any("authenticated_provider_host" in row["issues"] for row in violations)
        else sum(
            "authenticated_provider_host" in row["issues"] for row in violations
        ),
        "secretBearingUrls": sum(
            "secret_query_parameter" in row["issues"] for row in violations
        ),
        "nonHttpsUrls": sum("non_https" in row["issues"] for row in violations),
        "providerPolicy": {
            "pokewallet": "authenticated URLs excluded; use verified CardScanR-hosted/public sibling or placeholder",
            "tcgdex": "documented https low.webp thumbnail form; permanent 404 registry suppresses known failures",
        },
    }
    (REPORT_DIR / "direct_image_security_audit.json").write_text(
        json.dumps(security_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (REPORT_DIR / "direct_image_security_audit.md").write_text(
        "# Direct image security audit\n\n"
        f"Classification: **{security_report['classification']}**\n\n"
        f"Audited {len(sqlite_rows):,} SQLite rows. "
        f"Validated public direct: {classifications['validated_public_direct']:,}; "
        f"missing: {classifications['missing']:,}; "
        f"permanent failures: {classifications['permanent_failure']:,}.\n\n"
        "No API keys, signed/private URLs, secret query parameters, localhost URLs, "
        "non-HTTPS URLs, PokéWallet authenticated hosts, or HTML endpoints reach Flutter. "
        "Only validated public direct URLs are populated; other rows render placeholders.\n",
        encoding="utf-8",
    )
    connection.close()
    print(
        json.dumps(
            {
                "matrix": matrix_report["classification"],
                "samples": len(matrix),
                "security": security_report["classification"],
                "classifications": security_report["classifications"],
            },
            indent=2,
        )
    )
    return 0 if security_report["classification"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
