"""Crosswalk Japanese missing-image targets to exact official card records."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .schema import connect
from .tcgdex import canonical_json, digest, stable_id

PROVIDER_ID = "pokemon-japan-cards-official"


def normalized_text(value: str | None) -> str:
    return re.sub(r"[^\w]+", "", unicodedata.normalize("NFKC", value or "").casefold())


def normalized_number(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").strip().upper()
    numeric = re.fullmatch(r"0*(\d+)", text)
    return str(int(numeric.group(1))) if numeric else re.sub(r"\s+", "", text)


def reconcile(database: Path, report_json: Path | None = None,
              report_md: Path | None = None) -> dict[str, object]:
    connection = connect(str(database))
    connection.row_factory = sqlite3.Row
    counters: Counter[str] = Counter()
    details: list[dict[str, object]] = []
    try:
        official: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        rows = connection.execute(
            """select pem.provider_record_id,pem.source_record_id,pem.entity_id official_variant_id,
                      sr.local_name set_name,cp.collector_number,cl.name card_name,cic.source_url
                 from provider_entity_mapping pem
                 join card_variant cv on cv.id=pem.entity_id
                 join card_printing cp on cp.id=cv.card_printing_id
                 join set_release sr on sr.id=cp.set_release_id
                 left join card_localisation cl on cl.card_printing_id=cp.id and cl.language_code='ja'
                 join card_image_candidate cic on cic.card_variant_id=cv.id and cic.provider_id=?
                where pem.provider_id=? and pem.provider_record_type='card' and pem.entity_type='card_variant'
                order by pem.provider_record_id,cic.source_url""", (PROVIDER_ID, PROVIDER_ID),
        ).fetchall()
        for row in rows:
            official[(normalized_text(row["set_name"]), normalized_number(row["collector_number"]))].append(row)

        targets = connection.execute(
            """select u.*,sr.local_name set_name,cp.collector_number,cl.name card_name
                 from unresolved_item u
                 join card_variant cv on cv.id=u.entity_id
                 join card_printing cp on cp.id=cv.card_printing_id
                 join set_release sr on sr.id=cp.set_release_id
                 left join card_localisation cl on cl.card_printing_id=cp.id and cl.language_code='ja'
                where u.issue_class='missing_card_image' and u.language_code='ja'
                  and u.status in ('open','needs_review') order by u.id"""
        ).fetchall()
        with connection:
            for target in targets:
                key = (normalized_text(target["set_name"]), normalized_number(target["collector_number"]))
                matches_by_record: dict[str, sqlite3.Row] = {}
                for match in official.get(key, []):
                    matches_by_record.setdefault(match["provider_record_id"], match)
                matches = list(matches_by_record.values())
                if len(matches) != 1:
                    outcome = "no_exact_match" if not matches else "ambiguous_exact_identity"
                    counters[outcome] += 1
                    details.append({
                        "unresolved_id": target["id"], "target_variant_id": target["entity_id"],
                        "set_name": target["set_name"], "collector_number": target["collector_number"],
                        "outcome": outcome, "official_record_ids": sorted(matches_by_record),
                    })
                    continue
                match = matches[0]
                evidence = {
                    "match_method": "exact_language_region_normalized_set_name_and_collector_number",
                    "language": "ja", "region": "JP", "set_name": target["set_name"],
                    "collector_number": target["collector_number"],
                    "official_provider_record_id": match["provider_record_id"],
                    "official_variant_id": match["official_variant_id"],
                    "target_name": target["card_name"], "official_name": match["card_name"],
                    "name_equal": normalized_text(target["card_name"]) == normalized_text(match["card_name"]),
                    "uniqueness": "one official provider record for exact set and collector identity",
                }
                candidate_id = stable_id(target["entity_id"], PROVIDER_ID, "display", digest(match["source_url"])[:16])
                connection.execute(
                    """insert into card_image_candidate values (?, ?, ?, ?, 'display', ?, 'link_only', 'candidate')
                       on conflict(id) do update set source_record_id=excluded.source_record_id,
                        source_url=excluded.source_url,rights_status='link_only'""",
                    (candidate_id, target["entity_id"], match["source_record_id"], PROVIDER_ID, match["source_url"]),
                )
                connection.execute(
                    """insert into provider_entity_mapping values (?, 'card', ?, 'card_variant', ?, ?, 'verified', ?, ?)
                       on conflict(provider_id,provider_record_type,provider_record_id,entity_type,entity_id)
                       do update set match_method=excluded.match_method,mapping_status='verified',
                        source_record_id=excluded.source_record_id,evidence_json=excluded.evidence_json""",
                    (PROVIDER_ID, match["provider_record_id"], target["entity_id"],
                     "exact_set_collector_missing_image_crosswalk", match["source_record_id"], canonical_json(evidence)),
                )
                previous = json.loads(target["evidence_json"] or "{}")
                previous["official_japanese_crosswalk"] = evidence
                connection.execute(
                    """update unresolved_item set summary=?,evidence_json=?,status='open',externally_unavoidable=0
                       where id=?""",
                    ("Exact official Japanese image candidate found; technical acquisition and validation remain pending",
                     canonical_json(previous), target["id"]),
                )
                counters["exact_candidates"] += 1
                details.append({**evidence, "unresolved_id": target["id"],
                                "target_variant_id": target["entity_id"], "candidate_id": candidate_id,
                                "source_url": match["source_url"], "outcome": "exact_candidate"})
        report = {
            "schema_version": 1, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "provider_id": PROVIDER_ID, "targets": len(targets), "official_candidate_rows": len(rows),
            **dict(counters), "items": details,
        }
        if report_json:
            report_json.parent.mkdir(parents=True, exist_ok=True)
            report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        if report_md:
            report_md.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                "# Japanese missing-image official reconciliation", "",
                f"- Missing-image targets: `{len(targets)}`",
                f"- Exact official candidates: `{counters['exact_candidates']}`",
                f"- No exact match: `{counters['no_exact_match']}`",
                f"- Ambiguous exact identities: `{counters['ambiguous_exact_identity']}`", "",
                "Matches require Japanese/JP identity, an exact normalized local set name, an exact collector number,",
                "and exactly one official provider record. Card name is recorded as corroboration but is never the primary key.",
            ]
            report_md.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        return report
    finally:
        connection.close()

