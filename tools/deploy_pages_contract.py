#!/usr/bin/env python3
"""Inspect and deploy Cloudflare Pages contract files without printing secrets."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "cloudflare_env.local.json"
DEFAULT_PROJECT = "cardscanr-cache"
SECRET_KEYS = {"cloudflareApiToken", "cloudflarePagesApiToken", "r2AccessKeyId", "r2SecretAccessKey"}


def redacted(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: ("configured" if key in SECRET_KEYS and value else value)
        for key, value in payload.items()
    }


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid config: {path}")
    return payload


def pages_api_token(payload: dict[str, Any]) -> str | None:
    token = payload.get("cloudflarePagesApiToken") or payload.get("cloudflareApiToken")
    if token:
        return str(token).strip() or None
    return os.environ.get("CLOUDFLARE_PAGES_API_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN")


def api_get(*, account_id: str, token: str, project: str, suffix: str) -> dict[str, Any]:
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/pages/projects/{project}{suffix}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "CardScanRPagesDeploy/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("success"):
        raise RuntimeError(json.dumps(payload.get("errors") or payload, ensure_ascii=False))
    return payload["result"]


def inspect_project(*, account_id: str, token: str, project: str) -> dict[str, Any]:
    project_info = api_get(account_id=account_id, token=token, project=project, suffix="")
    deployments = api_get(
        account_id=account_id,
        token=token,
        project=project,
        suffix="/deployments?per_page=5",
    )
    source = project_info.get("source") or {}
    config = source.get("config") or {}
    build = project_info.get("build_config") or {}
    latest = project_info.get("latest_deployment") or {}
    return {
        "name": project_info.get("name"),
        "subdomain": project_info.get("subdomain"),
        "production_branch": project_info.get("production_branch"),
        "source_type": source.get("type"),
        "repo_owner": config.get("owner"),
        "repo_name": config.get("repo_name"),
        "root_directory": config.get("root_directory"),
        "production_branch_config": config.get("production_branch"),
        "build_command": build.get("build_command"),
        "destination_dir": build.get("destination_dir"),
        "latest_deployment_id": latest.get("id"),
        "latest_deployment_url": latest.get("url"),
        "latest_deployment_stage": (latest.get("latest_stage") or {}).get("name"),
        "latest_deployment_status": (latest.get("latest_stage") or {}).get("status"),
        "latest_deployment_trigger": latest.get("deployment_trigger"),
        "recent_deployments": [
            {
                "id": item.get("id"),
                "environment": item.get("environment"),
                "status": (item.get("latest_stage") or {}).get("status"),
                "created_on": item.get("created_on"),
                "trigger": item.get("deployment_trigger"),
            }
            for item in deployments
        ],
    }


def deploy_public(*, account_id: str, token: str, project: str, branch: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CLOUDFLARE_API_TOKEN"] = token
    env["CLOUDFLARE_ACCOUNT_ID"] = account_id
    return subprocess.run(
        [
            "npx",
            "wrangler",
            "pages",
            "deploy",
            "public",
            f"--project-name={project}",
            f"--branch={branch}",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect and deploy Cloudflare Pages contract files.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--project-name", default=DEFAULT_PROJECT)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--inspect-only", action="store_true")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    if not config_path.exists():
        print(json.dumps({"error": f"missing config: {config_path}"}, indent=2))
        return 1

    payload = load_config(config_path)
    account_id = str(payload.get("accountId") or "").strip()
    token = pages_api_token(payload)
    if not account_id:
        print(json.dumps({"error": "accountId missing from config"}, indent=2))
        return 1
    if not token:
        print(
            json.dumps(
                {
                    "error": "cloudflarePagesApiToken missing",
                    "hint": "Add Account → Cloudflare Pages → Edit token as cloudflarePagesApiToken in cloudflare_env.local.json",
                    "config": redacted(payload),
                },
                indent=2,
            )
        )
        return 1

    try:
        inspection = inspect_project(account_id=account_id, token=token, project=args.project_name)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        print(json.dumps({"error": "pages_api_auth_failed", "status": exc.code, "body": body}, indent=2))
        return 1
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 1

    print(json.dumps({"inspection": inspection}, indent=2))
    if args.inspect_only:
        return 0

    proc = deploy_public(
        account_id=account_id,
        token=token,
        project=args.project_name,
        branch=args.branch,
    )
    print(proc.stdout[-4000:] if proc.stdout else "")
    if proc.stderr:
        print(proc.stderr[-4000:], file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
