#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.providers.errors import sanitize_provider_diagnostics

OUTPUT_DIR = ROOT / "reports" / "chatgpt_uploads"
SUPPORTED_KINDS = {
    "ebay_browser_market_matrix",
    "ebay_browser_debug",
    "ebay_browser_live_write_smoke",
    "ebay_browser_live_worker_batch",
    "ebay_browser_live_scheduler",
    "market_price_engine_smoke",
}
MAX_JSONL_BYTES = 2_000_000
BLOCKED_PATH_MARKERS = (
    ".browser_profiles",
    "supabase_env.local.json",
    ".env",
    ".env.local",
    "cookie",
    "local storage",
    "login data",
    "token",
    "secret",
    "key",
)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a sanitized CardScanR market-engine upload bundle.")
    parser.add_argument("--kind", required=True, choices=sorted(SUPPORTED_KINDS))
    parser.add_argument("--include-html", action="store_true")
    parser.add_argument("--output")
    return parser.parse_args()


def _is_blocked_path(path: Path) -> bool:
    lowered = str(path).replace("\\", "/").lower()
    return any(marker in lowered for marker in BLOCKED_PATH_MARKERS)


def _safe_arcname(path: Path, *, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _sanitize_json_text(text: str) -> str:
    try:
        payload = json.loads(text)
    except Exception:
        return text
    return json.dumps(sanitize_provider_diagnostics(payload), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _sanitize_jsonl_text(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except Exception:
            continue
        lines.append(json.dumps(sanitize_provider_diagnostics(payload), ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")


def _add_file(zip_file: ZipFile, path: Path, *, root: Path, include_html: bool) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if _is_blocked_path(path):
        return False
    if path.suffix.lower() in {".html", ".htm"} and not include_html:
        return False
    if path.suffix.lower() == ".jsonl" and path.stat().st_size > MAX_JSONL_BYTES:
        return False
    arcname = _safe_arcname(path, root=root)
    suffix = path.suffix.lower()
    if suffix == ".json":
        zip_file.writestr(arcname, _sanitize_json_text(path.read_text(encoding="utf-8", errors="replace")))
    elif suffix == ".jsonl":
        zip_file.writestr(arcname, _sanitize_jsonl_text(path.read_text(encoding="utf-8", errors="replace")))
    else:
        zip_file.write(path, arcname)
    return True


def _candidate_files(kind: str, *, root: Path, include_html: bool) -> list[Path]:
    reports = root / "reports"
    files: list[Path] = []
    if kind == "ebay_browser_market_matrix":
        files.extend(
            [
                reports / "ebay_browser_market_matrix_latest.json",
                reports / "ebay_browser_market_matrix_runs.jsonl",
            ]
        )
        debug_root = reports / "ebay_browser_debug" / "market_matrix" / "latest"
        files.extend(debug_root.glob("**/debug_summary.json"))
        files.extend(debug_root.glob("**/screenshot.png"))
        if include_html:
            files.extend(debug_root.glob("**/page.html"))
    elif kind == "ebay_browser_debug":
        debug_root = reports / "ebay_browser_debug" / "latest"
        files.extend([debug_root / "debug_summary.json", debug_root / "screenshot.png"])
        if include_html:
            files.append(debug_root / "page.html")
    elif kind == "ebay_browser_live_write_smoke":
        files.extend(
            [
                reports / "ebay_browser_live_write_smoke_latest.json",
                reports / "ebay_browser_live_write_smoke_runs.jsonl",
            ]
        )
        for debug_root in [reports / "ebay_browser_debug" / "latest"]:
            files.extend([debug_root / "debug_summary.json", debug_root / "screenshot.png"])
            if include_html:
                files.append(debug_root / "page.html")
    elif kind == "ebay_browser_live_worker_batch":
        files.extend(
            [
                reports / "ebay_browser_live_worker_batch_latest.json",
                reports / "ebay_browser_live_worker_batch_runs.jsonl",
            ]
        )
        debug_root = reports / "ebay_browser_debug" / "live_worker_batch" / "latest"
        files.extend(debug_root.glob("**/debug_summary.json"))
        files.extend(debug_root.glob("**/screenshot.png"))
        if include_html:
            files.extend(debug_root.glob("**/page.html"))
    elif kind == "ebay_browser_live_scheduler":
        files.extend(
            [
                reports / "ebay_browser_live_scheduler_latest.json",
                reports / "ebay_browser_live_scheduler_runs.jsonl",
            ]
        )
    elif kind == "market_price_engine_smoke":
        files.extend(
            [
                reports / "market_price_engine_smoke_latest.json",
                reports / "market_price_engine_smoke_runs.jsonl",
            ]
        )
    return files


def default_output_path(kind: str, *, root: Path) -> Path:
    if kind == "ebay_browser_market_matrix":
        return root / "reports" / "chatgpt_uploads" / "ebay_browser_market_matrix_latest.zip"
    if kind == "ebay_browser_live_worker_batch":
        return root / "reports" / "chatgpt_uploads" / "ebay_browser_live_worker_batch_latest.zip"
    if kind == "ebay_browser_live_scheduler":
        return root / "reports" / "chatgpt_uploads" / "ebay_browser_live_scheduler_latest.zip"
    return root / "reports" / "chatgpt_uploads" / f"{kind}_{utc_stamp()}.zip"


def create_bundle(kind: str, *, include_html: bool = False, output: str | Path | None = None, root: Path = ROOT) -> Path:
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"Unsupported bundle kind: {kind}")
    output_path = Path(output) if output else default_output_path(kind, root=root)
    if not output_path.is_absolute():
        output_path = root / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    included: list[str] = []
    with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as zip_file:
        for path in _candidate_files(kind, root=root, include_html=include_html):
            if _add_file(zip_file, path, root=root, include_html=include_html):
                included.append(_safe_arcname(path, root=root))
        manifest = sanitize_provider_diagnostics(
            {
                "kind": kind,
                "createdAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "includeHtml": include_html,
                "includedFiles": included,
                "excludedByDefault": [".browser_profiles/", "supabase_env.local.json", ".env", ".env.local", "page.html"],
            }
        )
        zip_file.writestr("bundle_manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return output_path


def main() -> int:
    args = parse_args()
    bundle = create_bundle(kind=args.kind, include_html=args.include_html, output=args.output)
    print(str(bundle))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
