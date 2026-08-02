"""Reconcile exact cross-provider image candidates without overstating availability."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .schema import connect
from .tcgdex import canonical_json, stable_id

MISSING_PROVIDER_ID = "cardscanr-missing-image-registry"
TCGDEX_METADATA_PROVIDER_ID = "tcgdex-cards-database"
TCGDEX_ASSET_PROVIDER_ID = "tcgdex-assets"
ISSUE_CLASS = "card_image_identity_review"
VALIDATOR = "cardscanr-tcgdex-http-availability"
VALIDATOR_VERSION = "1.0.0"


def _text(value: str | None) -> str:
    return unicodedata.normalize("NFKC", value or "").replace("’", "'").casefold().strip()


def _collector(value: str | None) -> str:
    normalized = _text(value)
    match = re.fullmatch(r"([a-z-]*)(0*)(\d+)([a-z-]*)", normalized)
    if not match:
        return normalized
    return f"{match.group(1)}{int(match.group(3))}{match.group(4)}"


def _match_key(set_name: str, collector_number: str, card_name: str) -> tuple[str, str, str]:
    return _text(set_name), _collector(collector_number), _text(card_name)


def _http_observation(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "CardScanR-worldwide-catalogue/1.0", "Accept": "image/*,*/*;q=0.1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096)
            return {
                "method": "GET", "status": int(response.status),
                "content_type": response.headers.get_content_type(),
                "content_length_header": response.headers.get("Content-Length"),
                "body_bytes_observed": len(body), "body_truncated": len(body) == 4096,
            }
    except urllib.error.HTTPError as exc:
        body = exc.read(4096)
        return {
            "method": "GET", "status": int(exc.code),
            "content_type": exc.headers.get_content_type() if exc.headers else None,
            "content_length_header": exc.headers.get("Content-Length") if exc.headers else None,
            "body_bytes_observed": len(body), "body_truncated": len(body) == 4096,
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"method": "GET", "status": None, "error": f"{type(exc).__name__}: {exc}"}


def observe_urls(urls: Iterable[str], *, workers: int = 8, timeout: float = 20.0) -> dict[str, dict[str, Any]]:
    unique_urls = sorted(set(urls))
    observations: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 12))) as pool:
        futures = {pool.submit(_http_observation, url, timeout): url for url in unique_urls}
        for future in as_completed(futures):
            observations[futures[future]] = future.result()
    return observations


def _targets(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        """select ui.id unresolved_id,ui.entity_id target_variant_id,ui.evidence_json,
                  cp.id target_printing_id,cp.collector_number,cl.name card_name,
                  sr.local_name set_name,sr.release_code
             from unresolved_item ui
             join card_variant cv on cv.id=ui.entity_id
             join card_printing cp on cp.id=cv.card_printing_id
             join set_release sr on sr.id=cp.set_release_id
             join card_localisation cl on cl.card_printing_id=cp.id and cl.language_code='en'
            where ui.language_code='en' and ui.issue_class=?
              and ui.status in ('open','needs_review')
            order by ui.id""",
        (ISSUE_CLASS,),
    )]


def _tcgdex_candidates(connection: sqlite3.Connection) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    candidates: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in connection.execute(
        """select distinct cp.id tcgdex_printing_id,cp.collector_number,cl.name card_name,
                  sr.local_name set_name,sr.release_code,cv.id tcgdex_variant_id,
                  cic.source_record_id,cic.source_url,src.provider_record_id
             from card_printing cp
             join card_variant cv on cv.card_printing_id=cp.id
             join set_release sr on sr.id=cp.set_release_id
             join card_set cs on cs.id=sr.card_set_id
             join card_localisation cl on cl.card_printing_id=cp.id and cl.language_code='en'
             join card_image_candidate cic on cic.card_variant_id=cv.id
                  and cic.provider_id=? and cic.image_role='display'
             join source_record src on src.id=cic.source_record_id
            where cs.provider_id=? and sr.language_code='en'
            order by cp.id,cic.id""",
        (TCGDEX_ASSET_PROVIDER_ID, TCGDEX_METADATA_PROVIDER_ID),
    ):
        item = dict(row)
        candidates.setdefault(
            _match_key(item["set_name"], item["collector_number"], item["card_name"]), []
        ).append(item)
    return candidates


def _classification(observation: dict[str, Any]) -> tuple[str, str, str]:
    status = observation.get("status")
    if status in (404, 410):
        return "not_found", "fail", "missing"
    if status in (401, 403, 429):
        return "blocked", "warning", "candidate"
    if isinstance(status, int) and 200 <= status < 300:
        return "acquired", "warning", "candidate"
    if status is None or (isinstance(status, int) and status >= 500):
        return "retryable_error", "warning", "candidate"
    return "invalid", "fail", "candidate"


def reconcile_tcgdex_english_missing_images(
    database: Path,
    observations: dict[str, dict[str, Any]],
    *,
    observed_at: str | None = None,
    require_status: int | None = None,
) -> dict[str, Any]:
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    connection = connect(str(database))
    try:
        targets = _targets(connection)
        source_candidates = _tcgdex_candidates(connection)
        matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
        unmatched: list[dict[str, Any]] = []
        ambiguous: list[dict[str, Any]] = []
        for target in targets:
            options = source_candidates.get(
                _match_key(target["set_name"], target["collector_number"], target["card_name"]), []
            )
            unique = {option["tcgdex_printing_id"]: option for option in options}
            if len(unique) == 1:
                matched.append((target, next(iter(unique.values()))))
            elif not unique:
                unmatched.append(target)
            else:
                ambiguous.append({**target, "tcgdex_printing_ids": sorted(unique)})

        missing_observations = sorted({source["source_url"] for _, source in matched} - observations.keys())
        if missing_observations:
            raise ValueError(f"Missing observations for {len(missing_observations)} matched URLs")
        if require_status is not None:
            unexpected = [
                {"url": source["source_url"], **observations[source["source_url"]]}
                for _, source in matched if observations[source["source_url"]].get("status") != require_status
            ]
            if unexpected:
                raise RuntimeError(
                    f"HTTP status gate expected {require_status}; {len(unexpected)} observations differed: "
                    f"{canonical_json(unexpected[:5])}"
                )

        counters = {
            "open_english_review_items": len(targets), "exact_matches": len(matched),
            "unmatched": len(unmatched), "ambiguous": len(ambiguous),
            "image_candidates_recorded": 0, "acquisition_attempts_recorded": 0,
            "validation_results_recorded": 0,
        }
        details: list[dict[str, Any]] = []
        with connection:
            for target, source in matched:
                observation = observations[source["source_url"]]
                outcome, validation_result, candidate_status = _classification(observation)
                match_evidence = {
                    "match_method": "exact_set_name_normalized_collector_and_local_card_name",
                    "target_printing_id": target["target_printing_id"],
                    "target_variant_id": target["target_variant_id"],
                    "tcgdex_printing_id": source["tcgdex_printing_id"],
                    "tcgdex_variant_id": source["tcgdex_variant_id"],
                    "set_name": target["set_name"], "collector_number": target["collector_number"],
                    "card_name": target["card_name"], "source_url": source["source_url"],
                }
                connection.execute(
                    """insert into provider_entity_mapping values (?,?,?,?,?,?,?,?,?)
                       on conflict(provider_id,provider_record_type,provider_record_id,entity_type,entity_id)
                       do update set match_method=excluded.match_method,mapping_status=excluded.mapping_status,
                                     source_record_id=excluded.source_record_id,evidence_json=excluded.evidence_json""",
                    (TCGDEX_METADATA_PROVIDER_ID, "card", source["provider_record_id"], "card_variant",
                     target["target_variant_id"], "exact_set_collector_local_name", "verified",
                     source["source_record_id"], canonical_json(match_evidence)),
                )
                candidate_id = stable_id(
                    target["target_variant_id"], TCGDEX_ASSET_PROVIDER_ID, "display", source["source_url"]
                )
                existing = connection.execute(
                    """select id from card_image_candidate where card_variant_id=? and image_role='display'
                         and provider_id=? and source_url=?""",
                    (target["target_variant_id"], TCGDEX_ASSET_PROVIDER_ID, source["source_url"]),
                ).fetchone()
                if existing:
                    candidate_id = existing["id"]
                    connection.execute(
                        "update card_image_candidate set source_record_id=?,rights_status='permission_pending',validation_status=? where id=?",
                        (source["source_record_id"], candidate_status, candidate_id),
                    )
                else:
                    connection.execute(
                        "insert into card_image_candidate values (?,?,?,?,?,?,?,?)",
                        (candidate_id, target["target_variant_id"], source["source_record_id"],
                         TCGDEX_ASSET_PROVIDER_ID, "display", source["source_url"],
                         "permission_pending", candidate_status),
                    )
                attempt_id = stable_id("image-attempt", target["target_variant_id"], source["source_url"], outcome)
                attempt_evidence = {**match_evidence, "observation": observation, "validator": VALIDATOR}
                connection.execute(
                    """insert into image_acquisition_attempt values (?,?,?,?,?,?,?,?,?)
                       on conflict(id) do update set attempted_at=excluded.attempted_at,
                         http_status=excluded.http_status,outcome=excluded.outcome,evidence_json=excluded.evidence_json""",
                    (attempt_id, "card_variant", target["target_variant_id"], TCGDEX_ASSET_PROVIDER_ID,
                     source["source_url"], observed_at, observation.get("status"), outcome,
                     canonical_json(attempt_evidence)),
                )
                validation_id = stable_id("image-validation", candidate_id, VALIDATOR)
                checks = {
                    "httpAvailability": {
                        "status": "fail" if outcome == "not_found" else "warning",
                        **observation,
                    },
                    "identityMatch": {"status": "pass", **match_evidence},
                    "contentValidation": {"status": "not_run"},
                    "rightsValidation": {"status": "not_run", "rights_status": "permission_pending"},
                }
                connection.execute(
                    """insert into image_validation_result values (?,?,?,?,?,?,?,?)
                       on conflict(id) do update set status=excluded.status,checks_json=excluded.checks_json,
                         checked_at=excluded.checked_at""",
                    (validation_id, candidate_id, None, VALIDATOR, VALIDATOR_VERSION,
                     validation_result, canonical_json(checks), observed_at),
                )
                evidence = json.loads(target["evidence_json"] or "{}")
                evidence["tcgdex_exact_crosswalk"] = match_evidence
                evidence["latest_acquisition_attempt"] = {
                    "attempt_id": attempt_id, "attempted_at": observed_at,
                    "provider": TCGDEX_ASSET_PROVIDER_ID, "url": source["source_url"],
                    "outcome": outcome, **observation,
                }
                summary = (
                    f"Exact TCGdex printing identity matched by set, collector number, and local name; "
                    f"display image returned HTTP {observation.get('status')}, so the image remains unresolved."
                )
                connection.execute(
                    "update unresolved_item set summary=?,evidence_json=?,status='needs_review',externally_unavoidable=0 where id=?",
                    (summary, canonical_json(evidence), target["unresolved_id"]),
                )
                counters["image_candidates_recorded"] += 1
                counters["acquisition_attempts_recorded"] += 1
                counters["validation_results_recorded"] += 1
                details.append({**match_evidence, "attempt_id": attempt_id, "candidate_id": candidate_id,
                                "outcome": outcome, **observation})

            for target in unmatched:
                evidence = json.loads(target["evidence_json"] or "{}")
                evidence["tcgdex_exact_crosswalk"] = {
                    "status": "no_exact_match", "match_method": "exact_set_name_normalized_collector_and_local_card_name",
                    "set_name": target["set_name"], "collector_number": target["collector_number"],
                    "card_name": target["card_name"],
                }
                connection.execute(
                    "update unresolved_item set evidence_json=?,status='needs_review',externally_unavoidable=0 where id=?",
                    (canonical_json(evidence), target["unresolved_id"]),
                )

        return {
            **counters, "observed_at": observed_at,
            "http_status_counts": {
                str(status): sum(1 for item in details if item.get("status") == status)
                for status in sorted({item.get("status") for item in details}, key=lambda value: (value is None, value))
            },
            "unmatched_items": [
                {key: target[key] for key in ("target_variant_id", "set_name", "collector_number", "card_name")}
                for target in unmatched
            ],
            "ambiguous_items": ambiguous,
            "details": details,
        }
    finally:
        connection.close()


def matched_urls(database: Path) -> list[str]:
    connection = connect(str(database))
    try:
        candidates = _tcgdex_candidates(connection)
        urls: list[str] = []
        for target in _targets(connection):
            matches = candidates.get(
                _match_key(target["set_name"], target["collector_number"], target["card_name"]), []
            )
            unique = {item["tcgdex_printing_id"]: item for item in matches}
            if len(unique) == 1:
                urls.append(next(iter(unique.values()))["source_url"])
        return sorted(set(urls))
    finally:
        connection.close()


def write_report(result: dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    statuses = ", ".join(f"HTTP {key}: {value}" for key, value in result["http_status_counts"].items())
    lines = [
        "# English missing-image TCGdex reconciliation", "",
        f"- Open English review targets examined: `{result['open_english_review_items']}`",
        f"- Exact set + normalized collector + identical local-name matches: `{result['exact_matches']}`",
        f"- Unmatched: `{result['unmatched']}`", f"- Ambiguous: `{result['ambiguous']}`",
        f"- Observed URL results: `{statuses}`", "",
        "All exact matches remain unresolved unless the image is technically validated and its rights gate passes.", "",
        "## Unmatched", "",
    ]
    for item in result["unmatched_items"]:
        lines.append(
            f"- `{item['target_variant_id']}` — {item['set_name']} / {item['collector_number']} / {item['card_name']}"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
