"""Import preserved, self-controlled R2 image manifests into worldwide staging."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .schema import connect
from .tcgdex import canonical_json, digest, stable_id

PROVIDER_ID = "cardscanr-existing-r2-images"
VALIDATOR = "cardscanr-preserved-r2-manifest"
VALIDATOR_VERSION = "1.0.0"


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _name(value: str | None) -> str:
    return re.sub(r"[\s_\-・]+", "", unicodedata.normalize("NFKC", value or "").casefold())


def _collector(value: str | None) -> str:
    normalized = (value or "").casefold().lstrip("0")
    return normalized or "0"


def _load_manifest_rows(paths: Iterable[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    snapshots: list[dict[str, Any]] = []
    for rank, path in enumerate(paths):
        path = path.resolve()
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError(f"R2 manifest must be a list: {path}")
        checksum = file_sha256(path)
        snapshots.append({"path": path, "sha256": checksum, "bytes": path.stat().st_size, "rows": len(rows)})
        for row in rows:
            if not isinstance(row, dict) or not row.get("language") or not row.get("card_id"):
                raise ValueError(f"Malformed R2 manifest row in {path}")
            key = (str(row["language"]), str(row["card_id"]))
            prior = chosen.get(key)
            candidate = {**row, "_manifest_path": str(path), "_manifest_sha256": checksum, "_rank": rank}
            if prior:
                same_asset = (
                    prior.get("content_sha256"), prior.get("public_display_url"), prior.get("public_thumbnail_url")
                ) == (
                    candidate.get("content_sha256"), candidate.get("public_display_url"),
                    candidate.get("public_thumbnail_url"),
                )
                if not same_asset:
                    raise RuntimeError(f"Conflicting preserved R2 rows for {key}")
                prior_time = str(prior.get("uploaded_at") or "")
                candidate_time = str(candidate.get("uploaded_at") or "")
                if (candidate_time, rank) < (prior_time, int(prior.get("_rank", 0))):
                    continue
            chosen[key] = candidate
    return [chosen[key] for key in sorted(chosen)], snapshots


def _app_catalogue_index(root: Path | None) -> dict[str, dict[str, Any]]:
    if root is None:
        return {}
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(root.resolve().rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for card in payload.get("cards", []) if isinstance(payload, dict) else []:
            canonical_id = card.get("canonicalBaseId")
            if not canonical_id:
                continue
            record = {"card": card, "set": {key: payload.get(key) for key in (
                "setId", "setName", "language", "cardCount", "releaseDate", "series"
            )}, "source_path": str(path)}
            prior = records.get(canonical_id)
            if prior and canonical_json(prior["card"]) != canonical_json(card):
                raise RuntimeError(f"Conflicting existing catalogue records for {canonical_id}")
            records[canonical_id] = record
    return records


def _target_indexes(connection: sqlite3.Connection) -> tuple[dict[str, list[sqlite3.Row]], dict[tuple[str, str], list[sqlite3.Row]]]:
    english: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
        """select src.provider_record_id,cp.id printing_id,cv.id variant_id,cv.variant_key
             from card_printing cp join source_record src on src.id=cp.source_record_id
             join card_variant cv on cv.card_printing_id=cp.id
            where src.provider_id='pokemontcg-data'"""
    ):
        english[row["provider_record_id"]].append(row)
    japanese: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
        """select sr.release_code,cp.collector_number,cp.id printing_id,cv.id variant_id,
                  cv.variant_key,cl.name
             from card_printing cp join set_release sr on sr.id=cp.set_release_id
             join card_variant cv on cv.card_printing_id=cp.id
             left join card_localisation cl on cl.card_printing_id=cp.id and cl.language_code='ja'
            where sr.language_code='ja'"""
    ):
        japanese[((row["release_code"] or "").casefold(), _collector(row["collector_number"]))].append(row)
    return english, japanese


def _preferred(options: Iterable[sqlite3.Row]) -> list[sqlite3.Row]:
    return [row for row in options if row["variant_key"] in ("unspecified", "depiction-unspecified", "standard")]


def _create_existing_printing(
    connection: sqlite3.Connection,
    row: dict[str, Any],
    app_record: dict[str, Any],
    source_id: str,
    counters: Counter[str],
) -> tuple[str, str] | None:
    card = app_record["card"]
    canonical_id = card["canonicalBaseId"]
    language = "en" if row["language"] == "en" else "ja"
    set_id = str(card.get("setId") or "")
    release = connection.execute(
        """select sr.id from set_release sr join card_set cs on cs.id=sr.card_set_id
             where cs.provider_record_id=? and sr.language_code=?
             order by case cs.provider_id when 'pokemontcg-data' then 0 when 'tcgdex-cards-database' then 1 else 2 end
             limit 1""",
        (set_id, language),
    ).fetchone()
    if not release:
        return None
    design_id = stable_id(PROVIDER_ID, "design", canonical_id)
    connection.execute(
        """insert into card_design values (?,?,?,?,?) on conflict(id) do nothing""",
        (design_id, "existing_catalogue_card", card.get("name") or card.get("displayName"),
         canonical_json(card.get("nationalPokedexNumbers") or []), canonical_id),
    )
    collector = str(card.get("collectorNumber") or "")
    hp_value = card.get("hp")
    try:
        hp = int(hp_value) if hp_value not in (None, "") else None
    except (TypeError, ValueError):
        hp = None
    connection.execute(
        """insert into card_printing values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           on conflict(id) do update set source_record_id=excluded.source_record_id,
             verification_status=excluded.verification_status,raw_card_json=excluded.raw_card_json""",
        (canonical_id, design_id, release["id"], source_id, collector, canonical_id,
         card.get("rarity"), card.get("illustrator"), card.get("supertype"), None, hp,
         canonical_json(card.get("types") or []), card.get("regulationMark"), "[]", "[]", "[]",
         "verified_existing_catalogue", canonical_json(card)),
    )
    variant_id = stable_id(canonical_id, "standard")
    connection.execute(
        """insert into card_variant values (?,?,'standard',null,null,null,null,0,'{}','recognized')
           on conflict(id) do nothing""",
        (variant_id, canonical_id),
    )
    connection.execute(
        """insert into card_localisation values (?,? ,?,null,'{}','verified_existing_catalogue')
           on conflict(card_printing_id,language_code) do update set name=excluded.name,
             translation_status=excluded.translation_status""",
        (canonical_id, language, card.get("name") or card.get("displayName") or canonical_id),
    )
    counters["existing_catalogue_printings_created"] += 1
    return canonical_id, variant_id


def import_existing_r2_manifests(
    database: Path,
    manifest_paths: Iterable[Path],
    *,
    app_catalogue_root: Path | None = None,
) -> dict[str, Any]:
    rows, snapshots = _load_manifest_rows(manifest_paths)
    app_index = _app_catalogue_index(app_catalogue_root)
    input_sha = digest(canonical_json([{key: str(value) for key, value in item.items()} for item in snapshots]))
    now = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    counters: Counter[str] = Counter()
    connection = connect(str(database))
    try:
        english, japanese = _target_indexes(connection)
        snapshot_ids: dict[str, str] = {}
        with connection:
            connection.execute(
                """insert into source_provider values (?, 'CardScanR preserved R2 card images', 'self_controlled_storage',
                'https://cardscanr-images.andygore149.workers.dev', 'approved_for_mirror',
                'CardScanR', null, null) on conflict(id) do update set rights_status=excluded.rights_status""",
                (PROVIDER_ID,),
            )
            connection.execute(
                "insert into import_run values (?,?,'running',?,?, '{}','{}',?,null,null)",
                (run_id, PROVIDER_ID, ";".join(str(item["path"]) for item in snapshots), input_sha, now),
            )
            for item in snapshots:
                snapshot_id = stable_id(PROVIDER_ID, "snapshot", item["sha256"][:24])
                snapshot_ids[str(item["path"])] = snapshot_id
                connection.execute(
                    """insert into source_snapshot values (?,?,?,?,?,null,?,?,?)
                       on conflict(id) do update set import_run_id=excluded.import_run_id,fetched_at=excluded.fetched_at""",
                    (snapshot_id, PROVIDER_ID, run_id, str(item["path"]), item["sha256"],
                     item["bytes"], now, str(item["path"])),
                )

            for row in rows:
                counters["manifest_rows"] += 1
                language = str(row["language"])
                card_id = str(row["card_id"])
                identity = row.get("source_card_identifier")
                source_payload = canonical_json({key: value for key, value in row.items() if not key.startswith("_")})
                source_id = stable_id(PROVIDER_ID, language, card_id, str(row.get("content_sha256"))[:16])
                provider_record_id = f"{language}:{card_id}:{str(row.get('content_sha256'))[:16]}"
                connection.execute(
                    """insert into source_record values (?,?,?,?, 'card_image',?,null,?,?,?,null)
                       on conflict(id) do update set import_run_id=excluded.import_run_id,snapshot_id=excluded.snapshot_id""",
                    (source_id, PROVIDER_ID, run_id, snapshot_ids[row["_manifest_path"]], provider_record_id,
                     row["_manifest_path"], digest(source_payload), source_payload),
                )

                matches: list[sqlite3.Row] = []
                match_method = ""
                if language == "en":
                    matches = _preferred(english.get(card_id, []))
                    match_method = "exact_pokemontcg_provider_card_id"
                elif language == "ja" and identity:
                    parts = str(identity).split("|", 5)
                    if len(parts) >= 5:
                        options = _preferred(japanese.get((parts[2].casefold(), _collector(parts[3])), []))
                        exact_names = [option for option in options if _name(option["name"]) == _name(parts[4])]
                        matches = exact_names if exact_names else options
                        match_method = (
                            "exact_set_collector_normalized_local_name" if exact_names
                            else "unique_exact_set_collector_with_documented_name_punctuation_difference"
                        )
                if len(matches) == 1:
                    target = (matches[0]["printing_id"], matches[0]["variant_id"])
                else:
                    target = None
                    if not matches and identity and identity in app_index:
                        target = _create_existing_printing(
                            connection, row, app_index[identity], source_id, counters,
                        )
                        if target:
                            match_method = "exact_existing_cardscanr_canonical_identity"

                if not target:
                    issue_id = stable_id(PROVIDER_ID, "identity-review", language, card_id)
                    connection.execute(
                        """insert into unresolved_item values (?, 'preserved_r2_image', ?, ?, ?,
                           'existing_r2_image_identity_review', ?, ?, 'needs_review', 0)
                           on conflict(id) do update set summary=excluded.summary,evidence_json=excluded.evidence_json,
                             status='needs_review',externally_unavoidable=0""",
                        (issue_id, f"{language}:{card_id}", "en" if language == "en" else "ja",
                         "INTL" if language == "en" else "JP",
                         "A preserved verified R2 image lacks an exact staged card-variant identity",
                         canonical_json({
                             "card_id": card_id, "source_card_identifier": identity,
                             "content_sha256": row.get("content_sha256"),
                             "public_display_url": row.get("public_display_url"),
                             "candidate_match_count": len(matches), "manifest_path": row["_manifest_path"],
                         })),
                    )
                    counters["identity_review"] += 1
                    continue

                printing_id, variant_id = target
                counters[f"mapped_{language}"] += 1
                verified = row.get("verification_status") == "verified" and row.get("publicVerifySkipped") is False
                candidate_status = "verified" if verified else "candidate"
                validation_status = "pass" if verified else "warning"
                mapping_evidence = {
                    "match_method": match_method, "manifest_card_id": card_id,
                    "source_card_identifier": identity, "target_printing_id": printing_id,
                    "target_variant_id": variant_id, "content_sha256": row.get("content_sha256"),
                }
                connection.execute(
                    """insert into provider_entity_mapping values (?,?,?,?,?,?,?,?,?)
                       on conflict(provider_id,provider_record_type,provider_record_id,entity_type,entity_id)
                       do update set mapping_status=excluded.mapping_status,source_record_id=excluded.source_record_id,
                         match_method=excluded.match_method,evidence_json=excluded.evidence_json""",
                    (PROVIDER_ID, "card_image", provider_record_id, "card_variant", variant_id,
                     match_method, "verified", source_id, canonical_json(mapping_evidence)),
                )
                for role, url, object_key in (
                    ("display", row.get("public_display_url"), row.get("r2_display_key")),
                    ("thumbnail", row.get("public_thumbnail_url"), row.get("r2_thumbnail_key")),
                ):
                    if not url:
                        continue
                    candidate_id = stable_id(variant_id, PROVIDER_ID, role, row.get("content_sha256"))
                    existing_candidate = connection.execute(
                        """select id from card_image_candidate where card_variant_id=? and image_role=?
                             and provider_id=? and source_url=?""",
                        (variant_id, role, PROVIDER_ID, url),
                    ).fetchone()
                    if existing_candidate:
                        candidate_id = existing_candidate["id"]
                        connection.execute(
                            "update card_image_candidate set validation_status=?,rights_status='approved_for_mirror',source_record_id=? where id=?",
                            (candidate_status, source_id, candidate_id),
                        )
                    else:
                        connection.execute(
                            "insert into card_image_candidate values (?,?,?,?,?,?,?,?)",
                            (candidate_id, variant_id, source_id, PROVIDER_ID, role, url,
                             "approved_for_mirror", candidate_status),
                        )
                    checks = {
                        "preservedManifest": {"status": "pass", "path": row["_manifest_path"]},
                        "identity": {"status": "pass", **mapping_evidence},
                        "technical": {
                            "status": "pass" if verified else "warning",
                            "verification_status": row.get("verification_status"),
                            "public_verify_skipped": row.get("publicVerifySkipped"),
                            "content_sha256": row.get("content_sha256"), "width": row.get("width"),
                            "height": row.get("height"), "byte_size": row.get("byte_size"),
                            "mime_type": row.get("mime_type"), "object_key": object_key,
                        },
                        "rights": {"status": "pass", "rights_status": row.get("rights_status")},
                    }
                    validation_id = stable_id("image-validation", candidate_id, VALIDATOR)
                    connection.execute(
                        """insert into image_validation_result values (?,?,?,?,?,?,?,?)
                           on conflict(id) do update set status=excluded.status,checks_json=excluded.checks_json,
                             checked_at=excluded.checked_at""",
                        (validation_id, candidate_id, None, VALIDATOR, VALIDATOR_VERSION,
                         validation_status, canonical_json(checks), row.get("uploaded_at") or now),
                    )
                    counters[f"{role}_candidates"] += 1
                attempt_id = stable_id("image-attempt", variant_id, PROVIDER_ID, row.get("content_sha256"))
                connection.execute(
                    """insert into image_acquisition_attempt values (?,?,?,?,?,?,?,?,?)
                       on conflict(id) do update set evidence_json=excluded.evidence_json""",
                    (attempt_id, "card_variant", variant_id, PROVIDER_ID, row.get("source_url"),
                     row.get("uploaded_at") or now, 200 if verified else None,
                     "acquired", canonical_json({
                         **mapping_evidence, "r2_bucket": row.get("r2_bucket"),
                         "r2_display_key": row.get("r2_display_key"),
                         "r2_thumbnail_key": row.get("r2_thumbnail_key"),
                     })),
                )
                connection.execute(
                    """update unresolved_item set status='resolved'
                         where entity_type='preserved_r2_image' and entity_id=?
                           and issue_class='existing_r2_image_identity_review'""",
                    (f"{language}:{card_id}",),
                )
                counters["acquisition_attempts"] += 1
                counters["verified_assets" if verified else "pending_assets"] += 1

            counters["snapshot_files"] = len(snapshots)
            connection.execute(
                """update import_run set status='completed',checkpoint_json=?,counters_json=?,completed_at=? where id=?""",
                (canonical_json({"complete": True, "identity_review": counters["identity_review"]}),
                 canonical_json(dict(counters)), datetime.now(timezone.utc).isoformat(), run_id),
            )
        return {**dict(counters), "input_sha256": input_sha, "run_id": run_id}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
