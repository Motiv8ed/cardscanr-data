"""Build collector-scan / manual-acquisition queue from unresolved ledger."""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "image_independence"
CATALOGUE = ROOT / "public" / "v1" / "catalog" / "pokemon"
OUT_CSV = REPORT / "en_jp_manual_acquisition_queue.csv"
OUT_MD = REPORT / "en_jp_manual_acquisition_queue.md"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_catalogue_meta() -> dict[str, dict]:
    meta: dict[str, dict] = {}
    for folder, lang in (("en", "en"), ("jp", "ja")):
        for path in (CATALOGUE / folder / "cards").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            cards = data.get("cards") if isinstance(data, dict) else data
            for raw in cards or []:
                if not isinstance(raw, dict):
                    continue
                cid = raw.get("canonicalBaseId") or raw.get("id")
                if not cid:
                    continue
                urls = []
                for key in ("imageUrl", "image", "images"):
                    val = raw.get(key)
                    if isinstance(val, str):
                        urls.append(val)
                    elif isinstance(val, list):
                        urls.extend(str(x) for x in val if x)
                    elif isinstance(val, dict):
                        urls.extend(str(x) for x in val.values() if x)
                meta[str(cid)] = {
                    "name": raw.get("name"),
                    "setName": raw.get("setName"),
                    "setId": raw.get("setId"),
                    "collectorNumber": raw.get("collectorNumber") or raw.get("number"),
                    "language": lang,
                    "imageSource": raw.get("imageSource"),
                    "urls": urls,
                    "external": raw.get("externalIds") or raw.get("external") or {},
                }
    return meta


def permission_status(reason: str, hosts: str) -> str:
    if "pokewallet" in (hosts or "").lower() or reason == "auth_only_source_no_alternate":
        return "pokewallet_auth_gated_no_bypass"
    if "ambiguous" in reason:
        return "identity_unconfirmed_fail_closed"
    if "placeholder" in reason or "corrupt" in reason or "dead" in reason:
        return "no_permitted_live_source_found"
    return "manual_review_required"


def next_action(reason: str) -> str:
    if reason == "auth_only_source_no_alternate":
        return "obtain_pokewallet_written_rehost_or_collector_scan"
    if "ambiguous" in reason:
        return "resolve_identity_with_printed_number_variant_evidence"
    if "corrupt" in reason or "dead" in reason or "placeholder" in reason:
        return "locate_alternate_permitted_source_or_collector_scan"
    return "manual_or_new_permitted_source"


def main() -> int:
    unresolved_path = REPORT / "en_jp_unresolved_images.csv"
    if not unresolved_path.exists():
        raise SystemExit(f"missing {unresolved_path}")
    catalogue = load_catalogue_meta()
    rows_out = []
    with unresolved_path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            cid = row["canonical_card_id"]
            cat = catalogue.get(cid, {})
            hosts = row.get("attempted_sources") or ""
            reason = row.get("failure_reason") or row.get("category") or ""
            rows_out.append(
                {
                    "canonical_card_id": cid,
                    "language": row.get("language") or cat.get("language") or "",
                    "set_id": row.get("set_id") or cat.get("setId") or "",
                    "set_name": cat.get("setName") or "",
                    "collector_number": row.get("collector_number") or cat.get("collectorNumber") or "",
                    "card_name": cat.get("name") or "",
                    "failure_reason": reason,
                    "reference_evidence": hosts[:500],
                    "catalogue_image_hosts": ",".join(
                        sorted({u.split("/")[2] for u in (cat.get("urls") or []) if "://" in u})
                    ),
                    "external_ids": json.dumps(cat.get("external") or {}, ensure_ascii=False),
                    "provenance_status": "catalogue_identity_retained",
                    "usage_permission_status": permission_status(reason, hosts),
                    "recommended_next_action": next_action(reason),
                    "queued_at_utc": utc_now(),
                }
            )

    fields = list(rows_out[0].keys()) if rows_out else []
    REPORT.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows_out)

    by_reason = Counter(r["failure_reason"] for r in rows_out)
    by_perm = Counter(r["usage_permission_status"] for r in rows_out)
    by_lang = Counter(r["language"] for r in rows_out)
    by_set = Counter((r["language"], r["set_id"], r["set_name"]) for r in rows_out)

    lines = [
        "# EN/JA Manual Acquisition Queue",
        "",
        f"Generated: `{utc_now()}`",
        "",
        f"Total unresolved queued: **{len(rows_out)}**",
        "",
        "## By language",
        "",
    ]
    for lang, n in by_lang.most_common():
        lines.append(f"- `{lang}`: {n}")
    lines += ["", "## By failure reason", ""]
    for reason, n in by_reason.most_common():
        lines.append(f"- `{reason}`: {n}")
    lines += ["", "## By permission status", ""]
    for perm, n in by_perm.most_common():
        lines.append(f"- `{perm}`: {n}")
    lines += ["", "## Largest remaining sets", ""]
    for (lang, sid, sn), n in by_set.most_common(40):
        lines.append(f"- {n}: `{lang}` `{sid}` — {sn}")
    lines += [
        "",
        "## Unlock paths",
        "",
        "1. PokéWallet written rehost authorization (same class as Scrydex 2026-10-07).",
        "2. Collector physical scans for JA e-Card/PCG/ADV and EN exclusives with no public CDN.",
        "3. New permitted image APIs/CDNs with clear rehost terms.",
        "4. Identity repairs only with printed-number + set evidence (fail-closed).",
        "",
        f"CSV: `{OUT_CSV.as_posix()}`",
        "",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"queued": len(rows_out), "by_reason": dict(by_reason), "csv": str(OUT_CSV)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
