from __future__ import annotations

import os
from pathlib import Path

from ..config import REPORTS_DIR

AUDIT_SUBDIR = Path("runtime") / "ebay_scale_audit"


def ebay_scale_audit_dir() -> Path:
    override = os.getenv("EBAY_SCALE_AUDIT_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (REPORTS_DIR / AUDIT_SUBDIR).resolve()


def audit_jsonl_path() -> Path:
    return ebay_scale_audit_dir() / "set_sweep_audit.jsonl"


def audit_csv_path() -> Path:
    return ebay_scale_audit_dir() / "set_sweep_audit.csv"


def audit_reports_dir() -> Path:
    return (REPORTS_DIR / "ebay_scale").resolve()