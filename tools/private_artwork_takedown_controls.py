"""Fail-closed CardScanR candidate artwork URL suppression + takedown receipts.

Designed for a private candidate overlay; it never downloads artwork, mutates
customer-owned cards, or configures public deployment. A rights complaint
blocks a source URL / printing, independently of the card metadata itself.

Publication also requires a separately opted-in application integration; this
script does not grant artwork permissions or trigger a deploy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ALLOWED_HOST = "assets.tcgdex.net"
CANONICAL_FIELD = "canonical_printing_id"
MAX_REGISTRY_ROWS = 200000
MAX_NOTICE_IDS = 20000


def _safe_image_base(url: str) -> bool:
    if not isinstance(url, str) or len(url) > 1000:
        return False
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != ALLOWED_HOST or
            parts.username or parts.password or parts.port not in (None, 443) or
            parts.query or parts.fragment):
        return False
    return bool(re.fullmatch(r"/[A-Za-z0-9-]+/[A-Za-z0-9-]+/[A-Za-z0-9-]+/[A-Za-z0-9-]+", parts.path))


def validate_candidate(row: dict) -> None:
    if not isinstance(row, dict):
        raise ValueError("candidate_not_object")
    cid = row.get(CANONICAL_FIELD)
    if not isinstance(cid, str) or not (1 <= len(cid) <= 256):
        raise ValueError("invalid_canonical_printing_id")
    if row.get("identity_match") != "exact_unique" and row.get("status") != "exact_unique":
        raise ValueError("unverified_printing_identity")
    base = row.get("source_image_base_url")
    if not _safe_image_base(base):
        raise ValueError("unsupported_image_source")
    low_url = row.get("provider_low_webp_url")
    if low_url is not None and low_url != base + "/low.webp":
        raise ValueError("image_variant_mismatch")


def validate_registry(rows: list[dict]) -> None:
    if not isinstance(rows, list) or len(rows) > MAX_REGISTRY_ROWS:
        raise ValueError("registry_size_invalid")
    seen = set()
    for row in rows:
        validate_candidate(row)
        cid = row[CANONICAL_FIELD]
        if cid in seen:
            raise ValueError("duplicate_canonical_printing_id")
        seen.add(cid)


def default_policy() -> dict:
    return {
        "schema_version": 1,
        "global_kill_switch": True,
        "blocked_canonical_printing_ids": [],
        "blocked_provider_image_urls": [],
        "blocked_providers": [],
        "notices": [],
    }


def validate_policy(policy: dict) -> None:
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise ValueError("unsupported_policy_schema")
    if not isinstance(policy.get("global_kill_switch"), bool):
        raise ValueError("kill_switch_required")
    for field in ("blocked_canonical_printing_ids", "blocked_provider_image_urls", "blocked_providers", "notices"):
        if not isinstance(policy.get(field), list):
            raise ValueError("invalid_policy_field:" + field)


def apply_notice(policy: dict, *, case_id: str, printing_ids: list[str] | None = None,
                 image_bases: list[str] | None = None, disable_everything: bool = False) -> dict:
    validate_policy(policy)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", str(case_id)):
        raise ValueError("invalid_case_id")
    printing_ids = printing_ids or []
    image_bases = image_bases or []
    if len(printing_ids) + len(image_bases) > MAX_NOTICE_IDS:
        raise ValueError("notice_too_large")
    if not (printing_ids or image_bases or disable_everything):
        raise ValueError("notice_must_block_something")
    if any(not isinstance(cid, str) or not (1 <= len(cid) <= 256) for cid in printing_ids):
        raise ValueError("invalid_blocked_id")
    if any(not _safe_image_base(base) for base in image_bases):
        raise ValueError("invalid_blocked_source")
    revised = json.loads(json.dumps(policy))
    revised["blocked_canonical_printing_ids"] = sorted(set(
        revised["blocked_canonical_printing_ids"] + printing_ids
    ))
    revised["blocked_provider_image_urls"] = sorted(set(
        revised["blocked_provider_image_urls"] + image_bases
    ))
    if disable_everything:
        revised["global_kill_switch"] = True
    revised["notices"].append({
        "case_id": case_id,
        "recorded_utc": datetime.now(timezone.utc).isoformat(),
        "blocked_printings": len(printing_ids),
        "blocked_source_urls": len(image_bases),
        "global_disable": disable_everything,
    })
    return revised


def resolve_candidate_images(rows: list[dict], policy: dict, *,
                             explicitly_enabled: bool = False) -> tuple[list[dict], dict]:
    validate_registry(rows)
    validate_policy(policy)
    ids = set(policy["blocked_canonical_printing_ids"])
    bases = set(policy["blocked_provider_image_urls"])
    provider_blocks = set(policy["blocked_providers"])
    enabled = explicitly_enabled and not policy["global_kill_switch"]
    served = []
    blocked = 0
    for row in rows:
        base = row["source_image_base_url"]
        if (not enabled or row[CANONICAL_FIELD] in ids or base in bases
                or row.get("provider", "tcgdex") in provider_blocks):
            blocked += 1
            continue
        served.append({
            CANONICAL_FIELD: row[CANONICAL_FIELD],
            "image_url": base + "/low.webp",
            "source_provider": "tcgdex",
        })
    return served, {
        "schema_version": 1,
        "candidate_count": len(rows),
        "served_image_urls": len(served),
        "blocked_candidate_count": blocked,
        "global_kill_switch": policy["global_kill_switch"],
        "explicitly_enabled": explicitly_enabled,
        "image_bodies_downloaded": 0,
        "production_writes": 0,
        "copyright_permission_granted": False,
        "cdn_purge_performed": False,
    }


def atomic_save_json(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise ValueError("symlink_destination_denied")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, temp = tempfile.mkstemp(prefix=".cardscanr-takedown-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load_rows(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
    validate_registry(rows)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--policy", required=True, type=Path)
    ap.add_argument("--case-id")
    ap.add_argument("--block-printing-id", action="append", default=[])
    ap.add_argument("--block-image-base", action="append", default=[])
    ap.add_argument("--disable-all", action="store_true")
    ap.add_argument("--candidate-jsonl", type=Path)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()
    policy = json.loads(args.policy.read_text("utf-8")) if args.policy.exists() else default_policy()
    if args.case_id:
        policy = apply_notice(
            policy, case_id=args.case_id,
            printing_ids=args.block_printing_id,
            image_bases=args.block_image_base, disable_everything=args.disable_all,
        )
        atomic_save_json(args.policy, policy)
    if args.candidate_jsonl:
        rows = load_rows(args.candidate_jsonl)
        # Default proof only. Does not enable art for the live customer app.
        _, receipt = resolve_candidate_images(rows, policy, explicitly_enabled=False)
        if args.report:
            atomic_save_json(args.report, receipt)
        print(json.dumps(receipt, sort_keys=True))
    elif not args.case_id:
        raise SystemExit("supply a notice case or --candidate-jsonl for a dry-run report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
