"""Deterministic SQLite-to-Supabase publication planning and loading."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx


NAMESPACE = uuid.UUID("703b15e1-63c0-5c15-a97e-baa7bc96209a")

LANGUAGES = {
    "de": ("German", "Deutsch", "Latn"), "en": ("English", "English", "Latn"),
    "es": ("Spanish", "Español", "Latn"), "es-mx": ("Mexican Spanish", "Español", "Latn"),
    "fr": ("French", "Français", "Latn"), "id": ("Indonesian", "Bahasa Indonesia", "Latn"),
    "it": ("Italian", "Italiano", "Latn"), "ja": ("Japanese", "日本語", "Jpan"),
    "ko": ("Korean", "한국어", "Kore"), "nl": ("Dutch", "Nederlands", "Latn"),
    "pl": ("Polish", "Polski", "Latn"), "pt": ("Portuguese", "Português", "Latn"),
    "pt-br": ("Brazilian Portuguese", "Português do Brasil", "Latn"),
    "ru": ("Russian", "Русский", "Cyrl"), "th": ("Thai", "ไทย", "Thai"),
    "zh-cn": ("Simplified Chinese", "简体中文", "Hans"),
    "zh-tw": ("Traditional Chinese", "繁體中文", "Hant"),
}

REGIONS = {
    "BR": "Brazil", "CN": "Mainland China", "HK": "Hong Kong", "ID": "Indonesia",
    "INTL": "International", "JP": "Japan", "KR": "South Korea", "MX": "Mexico",
    "MY": "Malaysia", "PH": "Philippines", "SG": "Singapore", "TH": "Thailand",
    "TW": "Taiwan", "US": "United States",
}

PROVIDER_TYPES = {
    "official": "official", "official_archive": "archive", "open_dataset": "open_dataset",
    "community": "community", "community_dataset": "community",
    "community_corroboration": "community", "first_party_audit": "internal",
    "internal": "internal", "internal_derivation": "internal", "self_controlled_storage": "internal",
}

RIGHTS = {
    "approved_for_mirror": "approved_for_mirror", "link_only": "link_only",
    "metadata_only": "metadata_only", "permission_pending": "permission_pending",
    "noncommercial_no_redistribution": "restricted", "internal_audit": "metadata_only",
    "self_controlled": "approved_for_mirror",
}

VERIFICATION = {
    "verified": "verified", "verified_existing_catalogue": "verified", "source": "verified",
    "corroborated": "corroborated", "provisional": "provisional", "disputed": "disputed",
    "quarantined": "quarantined",
}


def stable_uuid(kind: str, value: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"cardscanr-worldwide:{kind}:{value}"))


def parse_json(value: str | None, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def iso_date(value: str | None) -> str | None:
    return value[:10] if value and len(value) >= 10 else None


def mapped(mapping: dict[str, str], value: str, field: str) -> str:
    try:
        return mapping[value]
    except KeyError as error:
        raise ValueError(f"Unsupported {field}: {value!r}") from error


def _rows(connection: sqlite3.Connection, query: str) -> Iterator[sqlite3.Row]:
    yield from connection.execute(query)


def _language_codes(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute(
        """select language_code from set_release
           union select language_code from card_localisation
           union select language_code from sealed_product_variant where language_code is not null
           union select language_code from unresolved_item where language_code is not null
           order by 1"""
    )]


def _region_codes(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute(
        """select region_code from set_release
           union select region_code from sealed_product_variant
           union select region_code from unresolved_item where region_code is not null
           order by 1"""
    )]


def _description(raw: str) -> str | None:
    value = parse_json(raw, {})
    parsed = value.get("parsed") if isinstance(value, dict) else None
    if isinstance(parsed, dict) and parsed.get("description"):
        return str(parsed["description"])
    return str(value.get("description")) if isinstance(value, dict) and value.get("description") else None


def _attempted_providers(evidence: dict[str, Any]) -> list[str]:
    for key in ("attempted_providers", "attemptedProviders", "providers"):
        value = evidence.get(key)
        if isinstance(value, list):
            return sorted({str(item) for item in value if item})
    return []


@dataclass(frozen=True)
class TableSpec:
    name: str
    conflict: str
    rows: Callable[[sqlite3.Connection], Iterable[dict[str, Any]]]


def table_specs() -> list[TableSpec]:
    def franchises(_: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        yield {"id": "pokemon", "name": "Pokémon Trading Card Game", "owner_name": "The Pokémon Company"}

    def languages(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for code in _language_codes(c):
            english, native, script = LANGUAGES.get(code, (code, code, None))
            yield {"code": code, "english_name": english, "native_name": native,
                   "script_code": script, "officially_printed": True, "aliases": []}

    def regions(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for code in _region_codes(c):
            yield {"code": code, "name": REGIONS.get(code, code),
                   "territory_codes": [] if code == "INTL" else [code]}

    def providers(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from source_provider order by id"):
            yield {"id": row["id"], "name": row["name"],
                   "provider_type": mapped(PROVIDER_TYPES, row["provider_type"], "provider_type"),
                   "base_url": row["base_url"], "rights_status": mapped(RIGHTS, row["rights_status"], "rights_status"),
                   "attribution_text": row["attribution_text"], "terms_url": row["terms_url"], "enabled": True}

    def import_runs(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from import_run order by started_at,id"):
            yield {"id": stable_uuid("import-run", row["id"]), "provider_id": row["provider_id"],
                   "collector_name": row["provider_id"], "collector_version": None,
                   "status": row["status"] if row["status"] in {"running","completed","failed","cancelled","partial"} else "partial",
                   "checkpoint": parse_json(row["checkpoint_json"], {}), "counters": parse_json(row["counters_json"], {}),
                   "started_at": row["started_at"], "completed_at": row["completed_at"],
                   "error_summary": row["error_summary"]}

    def snapshots(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from source_snapshot order by fetched_at,id"):
            yield {"id": stable_uuid("source-snapshot", row["id"]), "provider_id": row["provider_id"],
                   "import_run_id": stable_uuid("import-run", row["import_run_id"]), "source_url": row["source_uri"],
                   "fetched_at": row["fetched_at"], "byte_size": row["byte_size"], "sha256": row["source_sha256"],
                   "storage_uri": row["storage_uri"], "response_headers": {}, "source_version": row["source_version"]}

    def source_records(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        query = """select sr.*,ss.fetched_at from source_record sr
                     join source_snapshot ss on ss.id=sr.snapshot_id order by sr.id"""
        for row in _rows(c, query):
            raw = parse_json(row["raw_payload_json"], None)
            if raw is None:
                raw = {"source_path": row["source_path"], "collector_error": row["error"]}
            yield {"id": stable_uuid("source-record", row["id"]), "provider_id": row["provider_id"],
                   "snapshot_id": stable_uuid("source-snapshot", row["snapshot_id"]),
                   "import_run_id": stable_uuid("import-run", row["import_run_id"]),
                   "provider_record_type": row["record_type"], "provider_record_id": row["provider_record_id"],
                   "provider_parent_id": row["provider_parent_id"], "raw_payload": raw,
                   "raw_sha256": row["source_sha256"], "observed_at": row["fetched_at"]}

    def series(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from series order by id"):
            yield {"id": row["id"], "franchise_id": "pokemon",
                   "source_record_id": stable_uuid("source-record", row["source_record_id"]),
                   "name": row["canonical_name"], "local_names": parse_json(row["local_names_json"], {}),
                   "series_kind": "expansion"}

    def sets(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        query = """select cs.*,case when exists(
                     select 1 from set_release sr where sr.card_set_id=cs.id and sr.verification_status in ('verified','source')
                   ) then 'verified' else 'provisional' end resolved_status from card_set cs order by cs.id"""
        for row in _rows(c, query):
            yield {"id": row["id"], "franchise_id": "pokemon",
                   "source_record_id": stable_uuid("source-record", row["source_record_id"]),
                   "series_id": row["series_id"], "canonical_name": row["canonical_name"],
                   "set_kind": row["set_kind"], "verification_status": row["resolved_status"]}

    def releases(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        query = """select sr.*,(select count(*) from card_printing cp where cp.set_release_id=sr.id) expected
                     from set_release sr order by sr.id"""
        for row in _rows(c, query):
            yield {"id": row["id"], "set_id": row["card_set_id"],
                   "source_record_id": stable_uuid("source-record", row["source_record_id"]),
                   "language_code": row["language_code"], "region_code": row["region_code"],
                   "local_name": row["local_name"], "release_code": row["release_code"],
                   "release_date": iso_date(row["release_date"]), "official_total": row["official_count"],
                   "expected_printing_count": row["expected"],
                   "verification_status": mapped(VERIFICATION, row["verification_status"], "set verification")}

    def designs(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from card_design order by id"):
            kind = row["design_kind"]
            if kind == "pokémon": kind = "pokemon"
            if kind == "existing_catalogue_card": kind = "other"
            yield {"id": row["id"], "franchise_id": "pokemon", "design_kind": kind,
                   "national_pokedex_numbers": parse_json(row["national_pokedex_numbers_json"], []),
                   "canonical_name": row["canonical_name"], "rules_identity_key": row["source_identity_key"]}

    def printings(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        query = """select cp.*,sr.official_count from card_printing cp
                     join set_release sr on sr.id=cp.set_release_id order by cp.id"""
        for row in _rows(c, query):
            yield {"id": row["id"], "card_design_id": row["card_design_id"],
                   "set_release_id": row["set_release_id"],
                   "source_record_id": stable_uuid("source-record", row["source_record_id"]),
                   "collector_number": row["collector_number"], "printed_collector_number": row["collector_number"],
                   "printed_total": row["official_count"], "local_printing_key": row["local_printing_key"],
                   "regulation_mark": row["regulation_mark"], "rarity": row["rarity"],
                   "illustrator": row["illustrator"], "supertype": row["supertype"], "stage": row["stage"],
                   "hp": row["hp"], "types": parse_json(row["types_json"], []),
                   "weaknesses": parse_json(row["weaknesses_json"], []),
                   "resistances": parse_json(row["resistances_json"], []),
                   "retreat_cost": parse_json(row["retreat_json"], []),
                   "verification_status": mapped(VERIFICATION, row["verification_status"], "printing verification")}

    def variants(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from card_variant order by id"):
            attributes = parse_json(row["attributes_json"], {})
            if row["subtype"]: attributes = {**attributes, "stagingSubtype": row["subtype"]}
            yield {"id": row["id"], "card_printing_id": row["card_printing_id"],
                   "variant_key": row["variant_key"], "finish": row["finish"],
                   "foil_pattern": row["foil_pattern"], "stamp": row["stamp"],
                   "oversized": bool(row["oversized"]), "attributes": attributes,
                   "recognition_status": row["recognition_status"]}

    def localisations(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from card_localisation order by card_printing_id,language_code"):
            key = f"{row['card_printing_id']}:{row['language_code']}"
            yield {"id": stable_uuid("card-localisation", key), "card_printing_id": row["card_printing_id"],
                   "language_code": row["language_code"], "name": row["name"],
                   "flavor_text": row["flavor_text"], "rules": parse_json(row["rules_json"], []),
                   "translation_status": row["translation_status"] if row["translation_status"] in {"official","source","community","machine","unknown"} else "unknown"}

    def attacks(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from attack order by card_printing_id,ordinal,language_code"):
            key = f"{row['card_printing_id']}:{row['ordinal']}:{row['language_code']}"
            cost = parse_json(row["cost_json"], [])
            yield {"id": stable_uuid("attack", key), "card_printing_id": row["card_printing_id"],
                   "ordinal": row["ordinal"], "name": row["name"], "cost": cost,
                   "converted_energy_cost": len(cost), "damage": row["damage"], "text": row["effect"]}

    def abilities(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from ability order by card_printing_id,ordinal,language_code"):
            key = f"{row['card_printing_id']}:{row['ordinal']}:{row['language_code']}"
            yield {"id": stable_uuid("ability", key), "card_printing_id": row["card_printing_id"],
                   "ordinal": row["ordinal"], "ability_type": row["ability_type"],
                   "name": row["name"], "text": row["effect"]}

    def card_images(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from card_image_candidate order by id"):
            yield {"id": stable_uuid("card-image", row["id"]), "card_variant_id": row["card_variant_id"],
                   "source_record_id": stable_uuid("source-record", row["source_record_id"]),
                   "source_provider_id": row["provider_id"], "image_role": row["image_role"],
                   "source_url": row["source_url"], "source_rights_status": row["rights_status"],
                   "validation_status": row["validation_status"]}

    def products(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from sealed_product order by id"):
            yield {"id": row["id"], "franchise_id": "pokemon",
                   "source_record_id": stable_uuid("source-record", row["source_record_id"]),
                   "canonical_name": row["canonical_name"], "product_type": row["product_type"],
                   "description": _description(row["raw_product_json"]),
                   "verification_status": mapped(VERIFICATION, row["verification_status"], "product verification")}

    def accessories(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        allowed = {"binder","album","sleeves","deck_box","playmat","coin","dice","storage","marker","other"}
        for row in _rows(c, "select * from accessory order by id"):
            yield {"id": row["id"], "franchise_id": "pokemon",
                   "source_record_id": stable_uuid("source-record", row["source_record_id"]),
                   "canonical_name": row["canonical_name"],
                   "accessory_type": row["accessory_type"] if row["accessory_type"] in allowed else "other",
                   "description": row["description"], "verification_status": row["verification_status"]}

    def product_variants(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from sealed_product_variant order by id"):
            yield {"id": row["id"], "sealed_product_id": row["sealed_product_id"],
                   "language_code": row["language_code"], "region_code": row["region_code"],
                   "local_name": row["local_name"], "variant_key": row["variant_key"],
                   "release_date": iso_date(row["release_date"]), "attributes": parse_json(row["attributes_json"], {})}

    def product_contents(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from product_content order by sealed_product_variant_id,ordinal"):
            key = f"{row['sealed_product_variant_id']}:{row['ordinal']}"
            item = {"id": stable_uuid("product-content", key),
                    "sealed_product_variant_id": row["sealed_product_variant_id"], "ordinal": row["ordinal"],
                    "content_kind": row["content_kind"], "description": row["description"],
                    "quantity": row["quantity"], "attributes": parse_json(row["attributes_json"], {})}
            if row["entity_id"] and row["content_kind"] == "card_printing": item["card_printing_id"] = row["entity_id"]
            if row["entity_id"] and row["content_kind"] == "card_variant": item["card_variant_id"] = row["entity_id"]
            if row["entity_id"] and row["content_kind"] == "set_release": item["set_release_id"] = row["entity_id"]
            if row["entity_id"] and row["content_kind"] == "accessory": item["accessory_id"] = row["entity_id"]
            if row["entity_id"] and row["content_kind"] == "sealed_product_variant": item["nested_product_variant_id"] = row["entity_id"]
            yield item

    def product_images(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from product_image_candidate order by id"):
            yield {"id": stable_uuid("product-image", row["id"]),
                   "sealed_product_variant_id": row["sealed_product_variant_id"],
                   "source_record_id": stable_uuid("source-record", row["source_record_id"]),
                   "source_provider_id": row["provider_id"], "image_role": row["image_role"],
                   "source_url": row["source_url"], "source_rights_status": row["rights_status"],
                   "validation_status": row["validation_status"]}

    def marketplace(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from marketplace_mapping order by entity_type,entity_id,marketplace,marketplace_id"):
            key = ":".join(str(row[k]) for k in ("entity_type","entity_id","marketplace","marketplace_id"))
            yield {"id": stable_uuid("marketplace-mapping", key), "entity_type": row["entity_type"],
                   "entity_id": row["entity_id"], "marketplace": row["marketplace"],
                   "marketplace_id": row["marketplace_id"], "mapping_status": row["mapping_status"],
                   "source_record_id": stable_uuid("source-record", row["source_record_id"])}

    def provider_mappings(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from provider_entity_mapping order by provider_id,provider_record_type,provider_record_id,entity_type,entity_id"):
            key = ":".join(str(row[k]) for k in ("provider_id","provider_record_type","provider_record_id","entity_type","entity_id"))
            entity_type = "set" if row["entity_type"] == "card_set" else row["entity_type"]
            yield {"id": stable_uuid("provider-entity-mapping", key), "provider_id": row["provider_id"],
                   "provider_record_type": row["provider_record_type"], "provider_record_id": row["provider_record_id"],
                   "entity_type": entity_type, "entity_id": row["entity_id"], "match_method": row["match_method"],
                   "mapping_status": row["mapping_status"],
                   "source_record_id": stable_uuid("source-record", row["source_record_id"]),
                   "evidence": parse_json(row["evidence_json"], {})}

    def validation_results(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from image_validation_result order by id"):
            item = {"id": stable_uuid("image-validation", row["id"]), "validator": row["validator"],
                    "validator_version": row["validator_version"], "status": row["status"],
                    "checks": parse_json(row["checks_json"], {}), "checked_at": row["checked_at"]}
            if row["card_image_candidate_id"]:
                item["card_image_id"] = stable_uuid("card-image", row["card_image_candidate_id"])
            else:
                item["product_image_id"] = stable_uuid("product-image", row["product_image_candidate_id"])
            yield item

    def acquisition_attempts(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from image_acquisition_attempt order by id"):
            yield {"id": stable_uuid("image-acquisition", row["id"]), "entity_type": row["entity_type"],
                   "entity_id": row["entity_id"], "provider_id": row["provider_id"],
                   "source_url": row["source_url"], "attempted_at": row["attempted_at"],
                   "http_status": row["http_status"], "outcome": row["outcome"],
                   "evidence": parse_json(row["evidence_json"], {})}

    def publication_runs(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from publication_run order by started_at,id"):
            yield {"id": stable_uuid("publication-run", row["id"]), "version": row["version"],
                   "status": row["status"], "catalogue_sha256": row["catalogue_sha256"],
                   "manifest_sha256": row["manifest_sha256"], "object_prefix": row["object_prefix"],
                   "previous_publication_id": stable_uuid("publication-run", row["previous_publication_id"]) if row["previous_publication_id"] else None,
                   "counters": parse_json(row["counters_json"], {}), "gates": parse_json(row["gates_json"], {}),
                   "started_at": row["started_at"], "activated_at": row["activated_at"],
                   "completed_at": row["completed_at"], "rollback_retained": bool(row["rollback_retained"])}

    def artifacts(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from publication_artifact order by publication_run_id,object_key"):
            yield {"id": stable_uuid("publication-artifact", row["id"]),
                   "publication_run_id": stable_uuid("publication-run", row["publication_run_id"]),
                   "artifact_type": row["artifact_type"], "object_key": row["object_key"],
                   "public_url": row["public_url"], "byte_size": row["byte_size"], "sha256": row["sha256"],
                   "verified_at": row["verified_at"]}

    def unresolved(c: sqlite3.Connection) -> Iterable[dict[str, Any]]:
        for row in _rows(c, "select * from unresolved_item order by id"):
            evidence = parse_json(row["evidence_json"], {})
            yield {"id": stable_uuid("unresolved", row["id"]), "entity_type": row["entity_type"],
                   "entity_id": row["entity_id"], "language_code": row["language_code"],
                   "region_code": row["region_code"], "issue_class": row["issue_class"],
                   "summary": row["summary"], "evidence": evidence,
                   "attempted_providers": _attempted_providers(evidence), "status": row["status"],
                   "externally_unavoidable": bool(row["externally_unavoidable"])}

    return [
        TableSpec("franchises", "id", franchises), TableSpec("languages", "code", languages),
        TableSpec("regions", "code", regions), TableSpec("source_providers", "id", providers),
        TableSpec("import_runs", "id", import_runs), TableSpec("source_snapshots", "id", snapshots),
        TableSpec("source_records", "id", source_records), TableSpec("series", "id", series),
        TableSpec("sets", "id", sets), TableSpec("set_releases", "id", releases),
        TableSpec("card_designs", "id", designs), TableSpec("card_printings", "id", printings),
        TableSpec("card_variants", "id", variants), TableSpec("card_text_localisations", "id", localisations),
        TableSpec("abilities", "id", abilities), TableSpec("attacks", "id", attacks),
        TableSpec("card_images", "id", card_images), TableSpec("sealed_products", "id", products),
        TableSpec("accessories", "id", accessories), TableSpec("sealed_product_variants", "id", product_variants),
        TableSpec("product_contents", "id", product_contents), TableSpec("product_images", "id", product_images),
        TableSpec("marketplace_mappings", "id", marketplace),
        TableSpec("provider_entity_mappings", "id", provider_mappings),
        TableSpec("image_validation_results", "id", validation_results),
        TableSpec("image_acquisition_attempts", "id", acquisition_attempts),
        TableSpec("publication_runs", "id", publication_runs),
        TableSpec("publication_artifacts", "id", artifacts), TableSpec("unresolved_items", "id", unresolved),
    ]


def _fingerprint(row: dict[str, Any], hasher: Any) -> None:
    hasher.update(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    hasher.update(b"\n")


def build_load_plan(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    result: dict[str, Any] = {"schema_version": 1, "database": str(database.resolve()), "tables": {}}
    total = 0
    try:
        integrity = connection.execute("pragma integrity_check").fetchone()[0]
        foreign_keys = connection.execute("pragma foreign_key_check").fetchall()
        if integrity != "ok" or foreign_keys:
            raise RuntimeError(f"staging integrity failed: integrity={integrity}, foreign_keys={len(foreign_keys)}")
        for spec in table_specs():
            count = 0
            hasher = hashlib.sha256()
            for row in spec.rows(connection):
                _fingerprint(row, hasher)
                count += 1
            result["tables"][spec.name] = {"rows": count, "sha256": hasher.hexdigest(), "conflict": spec.conflict}
            total += count
    finally:
        connection.close()
    result["total_rows"] = total
    result["integrity"] = {"sqlite": "ok", "foreign_key_failures": 0}
    result["plan_sha256"] = hashlib.sha256(json.dumps(result["tables"], sort_keys=True).encode()).hexdigest()
    return result


def _batches(rows: Iterable[dict[str, Any]], batch_size: int, max_bytes: int = 4_000_000) -> Iterator[list[dict[str, Any]]]:
    def normalized(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        keys = set().union(*(item.keys() for item in items))
        return [{key: item.get(key) for key in keys} for item in items]

    batch: list[dict[str, Any]] = []
    size = 2
    for row in rows:
        row_size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
        if batch and (len(batch) >= batch_size or size + row_size > max_bytes):
            yield normalized(batch)
            batch, size = [], 2
        batch.append(row)
        size += row_size
    if batch:
        yield normalized(batch)


def execute_load(database: Path, supabase_url: str, service_role_key: str, *, batch_size: int = 500) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    headers = {"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}",
               "Content-Type": "application/json", "Content-Profile": "public",
               "Prefer": "resolution=merge-duplicates,return=minimal,missing=default"}
    counts: dict[str, int] = {}
    try:
        with httpx.Client(base_url=supabase_url.rstrip("/"), headers=headers, timeout=120) as client:
            for spec in table_specs():
                count = 0
                for batch in _batches(spec.rows(connection), batch_size):
                    response = client.post(f"/rest/v1/{spec.name}?on_conflict={spec.conflict}", json=batch)
                    if response.status_code not in (200, 201, 204):
                        raise RuntimeError(f"Supabase upsert failed for {spec.name}: HTTP {response.status_code}: {response.text[:1000]}")
                    count += len(batch)
                counts[spec.name] = count
    finally:
        connection.close()
    return counts
