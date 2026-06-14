#!/usr/bin/env python3
"""Audit and optionally export imageUrl aliases for JP catalogue records."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = ROOT / "public" / "v1" / "catalog" / "pokemon"
PROVIDER_ROOT = ROOT / "public" / "v1" / "provider-catalog" / "pokewallet"
REPORTS_DIR = ROOT / "reports"
POKEWALLET_IMAGE_BASE_URL = "https://api.pokewallet.io"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_json_if_changed(path: Path, payload: Any) -> bool:
    encoded = json_bytes(payload)
    if path.exists() and path.read_bytes() == encoded:
        return False
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_bytes(encoded)
    os.replace(tmp, path)
    return True


def write_text_if_changed(path: Path, text: str) -> bool:
    encoded = text.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_bytes(encoded)
    os.replace(tmp, path)
    return True


def first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def provider_endpoint_url(endpoint: Any) -> str | None:
    raw = str(endpoint or "").strip()
    if not raw:
        return None
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("/"):
        return f"{POKEWALLET_IMAGE_BASE_URL}{raw}"
    return f"{POKEWALLET_IMAGE_BASE_URL}/{raw}"


def has_url(card: dict[str, Any]) -> bool:
    return bool(first_text(card.get("imageUrl"), card.get("cardImageUrl")))


def app_image_values(card: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    small = first_text(card.get("imageUrlSmall"), card.get("imageSmall"))
    large = first_text(card.get("imageUrlLarge"), card.get("imageLarge"))
    primary = first_text(card.get("imageUrl"), card.get("cardImageUrl"), small, large)
    source = first_text(card.get("providerImageSource"), card.get("imageSource"))
    return primary, small, large, source


def provider_image_values(card: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    small = first_text(card.get("imageUrlSmall"), provider_endpoint_url(card.get("imageEndpointLow")))
    large = first_text(card.get("imageUrlLarge"), provider_endpoint_url(card.get("imageEndpointHigh")))
    primary = first_text(card.get("imageUrl"), card.get("cardImageUrl"), small, large, provider_endpoint_url(card.get("imageEndpoint")))
    source = first_text(card.get("providerImageSource"), "pokewallet_api_image_endpoint")
    return primary, small, large, source


def export_aliases(card: dict[str, Any], values: tuple[str | None, str | None, str | None, str | None]) -> bool:
    primary, small, large, source = values
    changed = False
    for key, value in [
        ("imageUrl", primary),
        ("imageUrlSmall", small),
        ("imageUrlLarge", large),
        ("providerImageSource", source),
    ]:
        if value and card.get(key) != value:
            card[key] = value
            changed = True
    return changed


def iter_card_payloads(root: Path, language: str) -> list[tuple[Path, dict[str, Any]]]:
    cards_dir = root / language / "cards" if root == APP_ROOT else root / "cards" / language
    if not cards_dir.exists():
        return []
    payloads = []
    for path in sorted(cards_dir.glob("*.json"), key=lambda item: item.name.lower()):
        data = load_json(path)
        if isinstance(data, dict) and isinstance(data.get("cards"), list):
            payloads.append((path, data))
    return payloads


def summarize_cards(
    payloads: list[tuple[Path, dict[str, Any]]],
    *,
    source_name: str,
    write_aliases: bool,
) -> dict[str, Any]:
    total = 0
    with_image = 0
    source_counts: Counter[str] = Counter()
    set_counts: dict[str, Counter[str]] = defaultdict(Counter)
    samples_with: list[dict[str, Any]] = []
    samples_without: list[dict[str, Any]] = []
    changed_paths: list[str] = []

    for path, payload in payloads:
        changed = False
        set_id = str(payload.get("setId") or payload.get("providerSetCode") or payload.get("providerSetId") or path.stem)
        for card in payload.get("cards", []):
            if not isinstance(card, dict):
                continue
            values = app_image_values(card) if source_name == "tcgdex_app_catalogue" else provider_image_values(card)
            if write_aliases:
                changed = export_aliases(card, values) or changed
            primary, _small, _large, source = app_image_values(card) if source_name == "tcgdex_app_catalogue" else provider_image_values(card)
            total += 1
            image_present = bool(primary)
            with_image += int(image_present)
            source_counts[source or "unknown"] += 1
            set_counts[set_id]["total"] += 1
            set_counts[set_id]["withImageUrl"] += int(image_present)
            sample = {
                "path": path.relative_to(ROOT).as_posix(),
                "setId": set_id,
                "collectorNumber": card.get("collectorNumber") or card.get("cardNumber"),
                "name": card.get("name") or card.get("cleanName"),
                "imageUrl": primary,
                "source": source,
            }
            if image_present and len(samples_with) < 5:
                samples_with.append(sample)
            if not image_present and len(samples_without) < 5:
                samples_without.append(sample)
        if changed and write_aliases and write_json_if_changed(path, payload):
            changed_paths.append(path.relative_to(ROOT).as_posix())

    return {
        "totalCards": total,
        "withImageUrl": with_image,
        "missingImageUrl": total - with_image,
        "sourceCounts": dict(sorted(source_counts.items())),
        "setCounts": {
            key: {
                "total": counter["total"],
                "withImageUrl": counter["withImageUrl"],
                "missingImageUrl": counter["total"] - counter["withImageUrl"],
            }
            for key, counter in sorted(set_counts.items())
        },
        "samplesWithImageUrl": samples_with,
        "samplesMissingImageUrl": samples_without,
        "changedPaths": changed_paths,
    }


def update_provider_manifest() -> bool:
    manifest_path = PROVIDER_ROOT / "cards-manifest.json"
    if not manifest_path.exists():
        return False
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        return False
    changed = False
    languages = manifest.get("languages") if isinstance(manifest.get("languages"), dict) else {}
    for language_payload in languages.values():
        if not isinstance(language_payload, dict):
            continue
        set_files = language_payload.get("setFiles")
        if not isinstance(set_files, list):
            continue
        for item in set_files:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").lstrip("/")
            if not url:
                continue
            path = ROOT / "public" / "v1" / url
            if path.exists():
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
                if item.get("sha256") != sha:
                    item["sha256"] = sha
                    changed = True
    if changed:
        return write_json_if_changed(manifest_path, manifest)
    return False


def update_provider_cards_sample() -> bool:
    sample_path = PROVIDER_ROOT / "cards-sample.json"
    if not sample_path.exists():
        return False
    payload = load_json(sample_path)
    if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list):
        return False
    changed = False
    for card in payload["cards"]:
        if isinstance(card, dict):
            changed = export_aliases(card, provider_image_values(card)) or changed
    if changed:
        return write_json_if_changed(sample_path, payload)
    return False


def find_card(payloads: list[tuple[Path, dict[str, Any]]], set_id: str, collector: str) -> dict[str, Any] | None:
    target_set = set_id.lower()
    target_collector = collector.lower()
    for path, payload in payloads:
        payload_set = str(payload.get("setId") or payload.get("providerSetCode") or payload.get("providerSetId") or path.stem)
        if payload_set.lower() != target_set:
            continue
        for card in payload.get("cards", []):
            if not isinstance(card, dict):
                continue
            number = str(card.get("collectorNumber") or card.get("cardNumber") or "").lower()
            if number == target_collector:
                primary, small, large, source = app_image_values(card)
                return {
                    "path": path.relative_to(ROOT).as_posix(),
                    "setId": payload_set,
                    "collectorNumber": number,
                    "name": card.get("name") or card.get("cleanName"),
                    "imageUrl": primary,
                    "imageUrlSmall": small,
                    "imageUrlLarge": large,
                    "source": source,
                }
    return None


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# JP Image URL Audit",
        "",
        f"- Generated at: {report['generatedAtLocal']}",
        f"- Write aliases: {report['writeAliases']}",
        "",
        "## Summary",
        "",
    ]
    for label, data in report["summaries"].items():
        lines.append(
            f"- {label}: total={data['totalCards']:,}, with imageUrl={data['withImageUrl']:,}, "
            f"missing imageUrl={data['missingImageUrl']:,}"
        )
    lines.extend(["", "## Provider Counts", ""])
    for label, data in report["summaries"].items():
        lines.append(f"### {label}")
        for source, count in data["sourceCounts"].items():
            lines.append(f"- {source}: {count:,}")
        lines.append("")
    lines.extend(["## Regression Checks", ""])
    for name, value in report["regressionChecks"].items():
        if value:
            lines.append(f"- {name}: imageUrl={value.get('imageUrl')}")
        else:
            lines.append(f"- {name}: not found")
    lines.extend(["", "## Samples With Image URL", ""])
    for label, data in report["summaries"].items():
        lines.append(f"### {label}")
        for sample in data["samplesWithImageUrl"]:
            lines.append(
                f"- {sample.get('path')} {sample.get('collectorNumber')} {sample.get('name')}: {sample.get('imageUrl')}"
            )
        lines.append("")
    lines.extend(["## Samples Missing Image URL", ""])
    for label, data in report["summaries"].items():
        lines.append(f"### {label}")
        samples = data["samplesMissingImageUrl"]
        if not samples:
            lines.append("- None")
        for sample in samples:
            lines.append(f"- {sample.get('path')} {sample.get('collectorNumber')} {sample.get('name')}")
        lines.append("")
    lines.extend(["## Changed Files", ""])
    changed = report.get("changedPaths", [])
    if not changed:
        lines.append("- None")
    else:
        for path in changed[:200]:
            lines.append(f"- {path}")
        if len(changed) > 200:
            lines.append(f"- ... {len(changed) - 200:,} more")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit JP imageUrl availability and export aliases.")
    parser.add_argument("--write-aliases", action="store_true", help="Write imageUrl/imageUrlSmall/imageUrlLarge aliases.")
    args = parser.parse_args()

    jp_app_payloads = iter_card_payloads(APP_ROOT, "jp")
    en_app_payloads = iter_card_payloads(APP_ROOT, "en")
    jp_provider_payloads = iter_card_payloads(PROVIDER_ROOT, "jp")
    en_provider_payloads = iter_card_payloads(PROVIDER_ROOT, "en")

    summaries = {
        "jp_tcgdex_app_catalogue": summarize_cards(
            jp_app_payloads, source_name="tcgdex_app_catalogue", write_aliases=args.write_aliases
        ),
        "jp_pokewallet_provider_catalogue": summarize_cards(
            jp_provider_payloads, source_name="pokewallet_provider_catalogue", write_aliases=args.write_aliases
        ),
        "en_app_catalogue": summarize_cards(
            en_app_payloads, source_name="tcgdex_app_catalogue", write_aliases=args.write_aliases
        ),
        "en_pokewallet_provider_catalogue": summarize_cards(
            en_provider_payloads, source_name="pokewallet_provider_catalogue", write_aliases=args.write_aliases
        ),
    }
    sample_changed = update_provider_cards_sample() if args.write_aliases else False
    manifest_changed = update_provider_manifest() if args.write_aliases else False

    report = {
        "generatedAtLocal": datetime.now().isoformat(timespec="seconds"),
        "writeAliases": args.write_aliases,
        "providerManifestUpdated": manifest_changed,
        "providerCardsSampleUpdated": sample_changed,
        "summaries": summaries,
        "regressionChecks": {
            "EN sv10 Arrokuda 062/182": find_card(en_app_payloads, "sv10", "62"),
            "JP M3 Nihil Zero 032/080 Espurr": find_card(jp_app_payloads, "M3", "032"),
            "JP M3 013/080": find_card(jp_app_payloads, "M3", "013"),
            "JP M3 056/080": find_card(jp_app_payloads, "M3", "056"),
            "JP 043/132 candidate path": find_card(jp_app_payloads, "24310", "043") or find_card(jp_app_payloads, "SV9", "043"),
        },
    }
    changed_paths: list[str] = []
    for data in summaries.values():
        changed_paths.extend(data.get("changedPaths", []))
    if manifest_changed:
        changed_paths.append((PROVIDER_ROOT / "cards-manifest.json").relative_to(ROOT).as_posix())
    if sample_changed:
        changed_paths.append((PROVIDER_ROOT / "cards-sample.json").relative_to(ROOT).as_posix())
    report["changedPaths"] = sorted(set(changed_paths))

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"jp_image_url_audit_{now_stamp()}.md"
    write_text_if_changed(report_path, render_markdown(report))
    write_json_if_changed(REPORTS_DIR / "jp_image_url_audit_latest.json", report)
    write_text_if_changed(REPORTS_DIR / "jp_image_url_audit_latest.md", render_markdown(report))
    print(f"Wrote {report_path.relative_to(ROOT)}")
    print(
        "JP app catalogue: "
        f"{summaries['jp_tcgdex_app_catalogue']['withImageUrl']:,}/"
        f"{summaries['jp_tcgdex_app_catalogue']['totalCards']:,} with imageUrl"
    )
    print(
        "JP provider catalogue: "
        f"{summaries['jp_pokewallet_provider_catalogue']['withImageUrl']:,}/"
        f"{summaries['jp_pokewallet_provider_catalogue']['totalCards']:,} with imageUrl"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
