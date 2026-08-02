"""Deterministically export a versioned, app-consumable worldwide catalogue bundle."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value is not None else fallback
    except json.JSONDecodeError:
        return fallback


def _normalized_collector(value: str) -> str:
    return "".join(re.findall(r"[0-9a-z]+", value.casefold())) or value.casefold()


class BundleWriter:
    def __init__(self, directory: Path):
        if directory.exists() and any(directory.iterdir()):
            raise FileExistsError(f"Immutable publication directory is not empty: {directory}")
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.outputs: dict[str, dict[str, Any]] = {}

    def jsonl(self, name: str, rows: Iterable[dict[str, Any]]) -> None:
        path = self.directory / name
        count = 0
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(canonical_json(row) + "\n")
                count += 1
        self.outputs[name] = {"rows": count, "bytes": path.stat().st_size, "sha256": file_sha256(path)}


def export_bundle(database: Path, output_root: Path, version: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", version):
        raise ValueError("version must be a safe immutable path component")
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    writer = BundleWriter(output_root / version)
    counts: Counter[str] = Counter()
    try:
        integrity = connection.execute("pragma integrity_check").fetchone()[0]
        foreign_keys = connection.execute("pragma foreign_key_check").fetchall()
        if integrity != "ok" or foreign_keys:
            raise RuntimeError(f"staging integrity gate failed: integrity={integrity}, foreign_keys={len(foreign_keys)}")

        def sets() -> Iterable[dict[str, Any]]:
            for row in connection.execute(
                """select sr.*,cs.canonical_name,cs.set_kind,cs.local_names_json,cs.provider_id,
                          cs.provider_record_id,src.id source_record_id,src.source_sha256
                     from set_release sr join card_set cs on cs.id=sr.card_set_id
                     join source_record src on src.id=sr.source_record_id order by sr.id"""
            ):
                yield {
                    "canonicalSetId": row["card_set_id"], "setReleaseId": row["id"],
                    "language": row["language_code"], "region": row["region_code"],
                    "nativeSetName": row["local_name"], "canonicalSetName": row["canonical_name"],
                    "setKind": row["set_kind"], "releaseCode": row["release_code"],
                    "releaseDate": row["release_date"], "officialSetTotal": row["official_count"],
                    "verificationStatus": row["verification_status"],
                    "providerSetIds": {row["provider_id"]: row["provider_record_id"]},
                    "sourceRecordId": row["source_record_id"], "sourceSha256": row["source_sha256"],
                }

        def cards() -> Iterable[dict[str, Any]]:
            query = """with english_names as (
                         select cp.card_design_id,min(cl.name) name
                           from card_printing cp join set_release sr on sr.id=cp.set_release_id
                           join card_localisation cl on cl.card_printing_id=cp.id and cl.language_code='en'
                          where sr.language_code='en' group by cp.card_design_id
                       )
                       select cp.*,sr.card_set_id,sr.language_code,sr.region_code,sr.local_name set_name,
                              sr.release_code,sr.release_date,sr.official_count,cs.canonical_name canonical_set_name,
                              cs.set_kind,cd.canonical_name canonical_card_name,
                              cl.name native_name,cl.translation_status native_name_status,
                              en.name english_name,src.provider_id,src.provider_record_id,src.id source_record_id,
                              src.source_sha256
                         from card_printing cp join set_release sr on sr.id=cp.set_release_id
                         join card_set cs on cs.id=sr.card_set_id join card_design cd on cd.id=cp.card_design_id
                         join source_record src on src.id=cp.source_record_id
                         left join card_localisation cl on cl.card_printing_id=cp.id
                              and cl.language_code=sr.language_code
                         left join english_names en on en.card_design_id=cp.card_design_id
                        order by cp.id"""
            for row in connection.execute(query):
                native_name = row["native_name"]
                aliases = sorted({str(value) for value in (
                    native_name, row["english_name"], row["canonical_card_name"], row["set_name"],
                    row["canonical_set_name"], row["collector_number"], row["release_code"],
                ) if value})
                provider_card_ids = {row["provider_id"]: row["provider_record_id"]}
                yield {
                    "canonicalPrintingId": row["id"], "canonicalBaseId": row["id"],
                    "cardDesignId": row["card_design_id"], "canonicalSetId": row["card_set_id"],
                    "setReleaseId": row["set_release_id"], "language": row["language_code"],
                    "region": row["region_code"], "nativeCardName": native_name,
                    "nativeNameStatus": row["native_name_status"] or "missing",
                    "englishCardName": row["english_name"], "canonicalCardName": row["canonical_card_name"],
                    "nativeSetName": row["set_name"], "englishSetName": row["canonical_set_name"],
                    "printedCollectorNumber": row["collector_number"],
                    "normalizedCollectorNumber": _normalized_collector(row["collector_number"]),
                    "officialSetTotal": row["official_count"], "rarity": row["rarity"],
                    "illustrator": row["illustrator"], "supertype": row["supertype"],
                    "stage": row["stage"], "hp": row["hp"], "types": _json(row["types_json"], []),
                    "regulationMark": row["regulation_mark"], "weaknesses": _json(row["weaknesses_json"], []),
                    "resistances": _json(row["resistances_json"], []),
                    "retreatCost": _json(row["retreat_json"], []), "releaseDate": row["release_date"],
                    "setKind": row["set_kind"], "verificationStatus": row["verification_status"],
                    "providerCardIds": provider_card_ids,
                    "providerSetIds": {row["provider_id"]: row["release_code"]} if row["release_code"] else {},
                    "searchAliases": aliases, "metadataProvenance": [{
                        "provider": row["provider_id"], "sourceRecordId": row["source_record_id"],
                        "sourceSha256": row["source_sha256"],
                    }],
                }

        def variants() -> Iterable[dict[str, Any]]:
            for row in connection.execute(
                """select cv.*,sr.language_code,sr.region_code from card_variant cv
                     join card_printing cp on cp.id=cv.card_printing_id
                     join set_release sr on sr.id=cp.set_release_id order by cv.id"""
            ):
                yield {
                    "canonicalVariantId": row["id"], "canonicalPrintingId": row["card_printing_id"],
                    "language": row["language_code"], "region": row["region_code"],
                    "variantKey": row["variant_key"], "finish": row["finish"],
                    "foilPattern": row["foil_pattern"], "subtype": row["subtype"],
                    "stamp": row["stamp"], "oversized": bool(row["oversized"]),
                    "attributes": _json(row["attributes_json"], {}),
                    "recognitionStatus": row["recognition_status"],
                }

        def direct_images() -> Iterable[dict[str, Any]]:
            rows = connection.execute(
                """select cp.id printing_id,cv.id variant_id,cic.* from card_image_candidate cic
                     join card_variant cv on cv.id=cic.card_variant_id
                     join card_printing cp on cp.id=cv.card_printing_id
                    where cic.validation_status in ('verified','acquired','published')
                      and cic.rights_status in ('approved_for_mirror','link_only')
                    order by cp.id,case cic.image_role when 'thumbnail' then 0 when 'display' then 1 else 2 end,cic.id"""
            ).fetchall()
            chosen: dict[str, dict[str, Any]] = {}
            for row in rows:
                item = chosen.setdefault(row["printing_id"], {
                    "canonicalPrintingId": row["printing_id"], "canonicalVariantId": row["variant_id"],
                    "provider": row["provider_id"], "authenticationRequirement": "not_required",
                    "directUseTechnicalStatus": "verified", "mirrorPermissionStatus": row["rights_status"],
                })
                key = "normalizedThumbnailUrl" if row["image_role"] == "thumbnail" else "normalizedDisplayUrl"
                item.setdefault(key, row["source_url"])
            yield from (chosen[key] for key in sorted(chosen))

        def products() -> Iterable[dict[str, Any]]:
            for row in connection.execute(
                """select sp.*,spv.id variant_id,spv.language_code,spv.region_code,spv.local_name,
                          spv.variant_key,spv.release_date,spv.attributes_json,src.id source_record_id,
                          src.source_sha256 from sealed_product sp
                     join sealed_product_variant spv on spv.sealed_product_id=sp.id
                     join source_record src on src.id=sp.source_record_id order by spv.id"""
            ):
                yield {
                    "canonicalProductId": row["id"], "productVariantId": row["variant_id"],
                    "language": row["language_code"], "region": row["region_code"],
                    "localName": row["local_name"], "canonicalName": row["canonical_name"],
                    "productType": row["product_type"], "variantKey": row["variant_key"],
                    "releaseDate": row["release_date"], "attributes": _json(row["attributes_json"], {}),
                    "verificationStatus": row["verification_status"],
                    "providerProductIds": {row["provider_id"]: row["provider_record_id"]},
                    "sourceRecordId": row["source_record_id"], "sourceSha256": row["source_sha256"],
                }

        def product_contents() -> Iterable[dict[str, Any]]:
            for row in connection.execute("select * from product_content order by sealed_product_variant_id,ordinal"):
                yield {
                    "productVariantId": row["sealed_product_variant_id"], "ordinal": row["ordinal"],
                    "contentKind": row["content_kind"], "entityId": row["entity_id"],
                    "description": row["description"], "quantity": row["quantity"],
                    "attributes": _json(row["attributes_json"], {}),
                }

        def product_images() -> Iterable[dict[str, Any]]:
            for row in connection.execute("select * from product_image_candidate order by sealed_product_variant_id,id"):
                yield {
                    "imageCandidateId": row["id"], "productVariantId": row["sealed_product_variant_id"],
                    "provider": row["provider_id"], "imageRole": row["image_role"],
                    "sourceUrl": row["source_url"], "rightsStatus": row["rights_status"],
                    "validationStatus": row["validation_status"], "attributes": _json(row["attributes_json"], {}),
                    "sourceRecordId": row["source_record_id"],
                }

        def unresolved() -> Iterable[dict[str, Any]]:
            for row in connection.execute("select * from unresolved_item order by id"):
                yield {**dict(row), "evidence_json": _json(row["evidence_json"], {})}

        def acquisition_attempts() -> Iterable[dict[str, Any]]:
            for row in connection.execute("select * from image_acquisition_attempt order by id"):
                yield {**dict(row), "evidence_json": _json(row["evidence_json"], {})}

        def validation_results() -> Iterable[dict[str, Any]]:
            for row in connection.execute("select * from image_validation_result order by id"):
                yield {**dict(row), "checks_json": _json(row["checks_json"], {})}

        def publication_runs() -> Iterable[dict[str, Any]]:
            for row in connection.execute("select * from publication_run order by started_at,id"):
                yield {
                    **dict(row), "counters_json": _json(row["counters_json"], {}),
                    "gates_json": _json(row["gates_json"], {}),
                    "rollback_retained": bool(row["rollback_retained"]),
                }

        def publication_artifacts() -> Iterable[dict[str, Any]]:
            yield from (dict(row) for row in connection.execute(
                "select * from publication_artifact order by publication_run_id,object_key"
            ))

        for name, factory in (
            ("sets.jsonl", sets), ("cards.jsonl", cards), ("card_variants.jsonl", variants),
            ("direct_images.jsonl", direct_images), ("products.jsonl", products),
            ("product_contents.jsonl", product_contents), ("product_images.jsonl", product_images),
            ("image_acquisition_attempts.jsonl", acquisition_attempts),
            ("image_validation_results.jsonl", validation_results),
            ("publication_runs.jsonl", publication_runs),
            ("publication_artifacts.jsonl", publication_artifacts),
            ("unresolved.jsonl", unresolved),
        ):
            writer.jsonl(name, factory())
            counts[name] = writer.outputs[name]["rows"]
        manifest = {
            "schemaVersion": "2.1.0", "catalogueVersion": version,
            "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
            "classification": "STAGED_NOT_PUBLISHED",
            "sourceDatabaseSha256": file_sha256(database), "sourceDatabaseBytes": database.stat().st_size,
            "integrity": {"sqliteIntegrityCheck": integrity, "foreignKeyFailures": len(foreign_keys)},
            "outputs": writer.outputs, "productionPublished": False,
        }
        manifest_path = writer.directory / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        manifest["manifestSha256"] = file_sha256(manifest_path)
        return manifest
    finally:
        connection.close()
