#!/usr/bin/env python3
"""Normalize cloudflare_env.local.json without printing secrets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET_KEYS = {"cloudflareApiToken", "r2AccessKeyId", "r2SecretAccessKey"}


def is_s3_endpoint(value: str | None) -> bool:
    return bool(value and "r2.cloudflarestorage.com" in value)


def normalize_payload(payload: dict, *, public_dev_url: str | None = None) -> dict:
    account_id = str(payload.get("accountId") or "").strip()
    if account_id and not payload.get("r2S3Endpoint"):
        payload["r2S3Endpoint"] = f"https://{account_id}.r2.cloudflarestorage.com"

    public_candidate = public_dev_url or payload.get("r2PublicDevUrl") or payload.get("r2PublicBaseUrl")
    if is_s3_endpoint(str(public_candidate or "")):
        public_candidate = payload.get("r2PublicDevUrl")

    if public_candidate and not is_s3_endpoint(str(public_candidate)):
        payload["r2PublicDevUrl"] = str(public_candidate).rstrip("/")
        payload["r2PublicBaseUrl"] = payload["r2PublicDevUrl"]
    elif is_s3_endpoint(str(payload.get("r2PublicBaseUrl") or "")):
        payload.pop("r2PublicBaseUrl", None)

    return payload


def redacted(payload: dict) -> dict:
    return {
        key: ("configured" if key in SECRET_KEYS and value else value)
        for key, value in payload.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize Cloudflare env config.")
    parser.add_argument("--config", default=str(ROOT / "cloudflare_env.local.json"))
    parser.add_argument("--public-dev-url", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    path = Path(args.config)
    payload = json.loads(path.read_text(encoding="utf-8"))
    normalized = normalize_payload(payload, public_dev_url=args.public_dev_url)
    print(json.dumps(redacted(normalized), indent=2, sort_keys=True))
    if not args.dry_run:
        path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
