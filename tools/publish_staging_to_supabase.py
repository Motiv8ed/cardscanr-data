#!/usr/bin/env python3
"""DEVELOPMENT-ONLY / archival utility: load normalized staging into Supabase.

This is NOT part of the standard production release path.
Production catalogue publication targets Cloudflare R2 via
``tools/publish_worldwide_catalogue.py``.

Writing worldwide catalogue rows to Supabase requires an explicit dangerous
opt-in. The production project is rejected by default.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_worldwide.supabase_publication import build_load_plan, execute_load

PRODUCTION_PROJECT_REF = "qstcdlczasmvexpgbpjk"
DANGEROUS_OPT_IN = "--i-understand-this-writes-catalogue-to-supabase"
ALLOW_PRODUCTION = "--allow-production-catalogue-load"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Optional archival/dev loader for worldwide catalogue rows into Supabase. "
            "Not part of the standard production release path."
        )
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--project-ref")
    parser.add_argument("--confirm-project-ref")
    parser.add_argument("--supabase-url")
    parser.add_argument("--service-role-key-env", default="SUPABASE_SERVICE_ROLE_KEY")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        DANGEROUS_OPT_IN,
        dest="dangerous_opt_in",
        action="store_true",
        help="Required to execute any worldwide catalogue write to Supabase.",
    )
    parser.add_argument(
        ALLOW_PRODUCTION,
        dest="allow_production_catalogue_load",
        action="store_true",
        help="Required in addition to the dangerous opt-in to target the production project.",
    )
    args = parser.parse_args()
    plan = build_load_plan(args.database)
    plan["mode"] = "dry_run"
    plan["standard_production_path"] = False
    plan["target_store"] = "supabase_optional_archival"
    if args.execute:
        if not args.dangerous_opt_in:
            parser.error(
                "execution blocked: worldwide catalogue writes to Supabase require "
                f"{DANGEROUS_OPT_IN}. Use Cloudflare R2 publication for production."
            )
        if not args.project_ref or args.confirm_project_ref != args.project_ref:
            parser.error("execution requires matching --project-ref and --confirm-project-ref")
        if args.project_ref == PRODUCTION_PROJECT_REF and not args.allow_production_catalogue_load:
            parser.error(
                f"execution blocked: project {PRODUCTION_PROJECT_REF} is production. "
                "Catalogue publication must use Cloudflare R2. "
                f"Override only with {ALLOW_PRODUCTION} plus {DANGEROUS_OPT_IN}."
            )
        expected_url = f"https://{args.project_ref}.supabase.co"
        if args.supabase_url and args.supabase_url.rstrip("/") != expected_url:
            parser.error("--supabase-url does not match --project-ref")
        key = os.environ.get(args.service_role_key_env)
        if not key:
            parser.error(f"missing service-role key environment variable {args.service_role_key_env}")
        plan["loaded_rows"] = execute_load(args.database, expected_url, key, batch_size=args.batch_size)
        plan["mode"] = "executed"
        plan["project_ref"] = args.project_ref
        plan["dangerous_opt_in"] = True
        plan["allow_production_catalogue_load"] = bool(args.allow_production_catalogue_load)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "mode": plan["mode"],
                "total_rows": plan["total_rows"],
                "plan_sha256": plan["plan_sha256"],
                "standard_production_path": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
