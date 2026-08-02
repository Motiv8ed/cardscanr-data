#!/usr/bin/env python3
"""Dry-run or execute the deterministic normalized Supabase load."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_worldwide.supabase_publication import build_load_plan, execute_load


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--project-ref")
    parser.add_argument("--confirm-project-ref")
    parser.add_argument("--supabase-url")
    parser.add_argument("--service-role-key-env", default="SUPABASE_SERVICE_ROLE_KEY")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    plan = build_load_plan(args.database)
    plan["mode"] = "dry_run"
    if args.execute:
        if not args.project_ref or args.confirm_project_ref != args.project_ref:
            parser.error("execution requires matching --project-ref and --confirm-project-ref")
        expected_url = f"https://{args.project_ref}.supabase.co"
        if args.supabase_url and args.supabase_url.rstrip("/") != expected_url:
            parser.error("--supabase-url does not match --project-ref")
        key = os.environ.get(args.service_role_key_env)
        if not key:
            parser.error(f"missing service-role key environment variable {args.service_role_key_env}")
        plan["loaded_rows"] = execute_load(args.database, expected_url, key, batch_size=args.batch_size)
        plan["mode"] = "executed"
        plan["project_ref"] = args.project_ref
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"mode": plan["mode"], "total_rows": plan["total_rows"],
                      "plan_sha256": plan["plan_sha256"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
