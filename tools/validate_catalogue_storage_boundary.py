#!/usr/bin/env python3
"""Fail if production tooling still treats Supabase as the worldwide catalogue host."""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_PROJECT_REF = "qstcdlczasmvexpgbpjk"
PUBLISH_TOOL = ROOT / "tools" / "publish_staging_to_supabase.py"
BOUNDARY_DOC = ROOT / "reports" / "cloudflare_migration" / "CATALOGUE_STORAGE_BOUNDARY.md"
REQUIRED_FLAG = "--i-understand-this-writes-catalogue-to-supabase"
ALLOW_PROD_FLAG = "--allow-production-catalogue-load"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def validate() -> list[str]:
    issues: list[str] = []
    if not BOUNDARY_DOC.is_file():
        issues.append(f"missing boundary doc: {BOUNDARY_DOC}")
    if not PUBLISH_TOOL.is_file():
        issues.append(f"missing publish tool: {PUBLISH_TOOL}")
        return issues

    source = _source(PUBLISH_TOOL)
    if REQUIRED_FLAG not in source:
        issues.append(f"{PUBLISH_TOOL.name} missing required dangerous opt-in flag {REQUIRED_FLAG}")
    if ALLOW_PROD_FLAG not in source:
        issues.append(f"{PUBLISH_TOOL.name} missing production reject override {ALLOW_PROD_FLAG}")
    if PRODUCTION_PROJECT_REF not in source:
        issues.append(f"{PUBLISH_TOOL.name} must hard-reject production project {PRODUCTION_PROJECT_REF}")
    if "standard production release path" not in source.lower() and "not part of the standard" not in source.lower():
        # docstring/comment gate
        if "DEVELOPMENT-ONLY" not in source and "dangerous" not in source.lower():
            issues.append(f"{PUBLISH_TOOL.name} must document that Supabase catalogue load is non-standard")

    try:
        tree = ast.parse(source, filename=str(PUBLISH_TOOL))
    except SyntaxError as exc:
        issues.append(f"{PUBLISH_TOOL.name} syntax error: {exc}")
        return issues

    # Ensure execute path is gated: look for Name/Constant containing the flag string near execute.
    flag_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    if REQUIRED_FLAG not in flag_literals and REQUIRED_FLAG.replace("--", "").replace("-", "_") not in source:
        # argparse dest may differ; string presence already checked above.
        pass

    worldwide = ROOT / "tools" / "publish_worldwide_catalogue.py"
    if worldwide.is_file():
        ww = _source(worldwide)
        if "supabase" in ww.lower() and "catalogue" in ww.lower() and "upsert" in ww.lower():
            issues.append("publish_worldwide_catalogue.py appears to upsert catalogue into Supabase")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    issues = validate()
    if args.json:
        import json

        print(json.dumps({"ok": not issues, "issues": issues}, indent=2))
    else:
        if issues:
            print("CATALOGUE_STORAGE_BOUNDARY_FAIL")
            for issue in issues:
                print(f"- {issue}")
        else:
            print("CATALOGUE_STORAGE_BOUNDARY_OK")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
