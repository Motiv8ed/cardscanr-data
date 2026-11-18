"""Block accidental writes to repo reports/runtime from automated tests."""
from __future__ import annotations

import os
from pathlib import Path

_REPO_RUNTIME = Path(__file__).resolve().parents[1] / "reports" / "runtime"


def assert_safe_runtime_json_write(path: Path) -> None:
    if os.environ.get("CARDSCANR_ALLOW_REAL_RUNTIME_REPORTS") == "1":
        return
    target = path.resolve()
    runtime_root = _REPO_RUNTIME.resolve()
    try:
        target.relative_to(runtime_root)
    except ValueError:
        return
    allowed_root = os.environ.get("CARDSCANR_TEST_RUNTIME_ROOT", "").strip()
    if allowed_root:
        try:
            target.relative_to(Path(allowed_root).resolve())
            return
        except ValueError:
            pass
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("CARDSCANR_PYTEST") == "1":
        raise RuntimeError(
            f"Refusing to write production runtime file during tests: {path}. "
            "Use tmp_path and patch BUDGET_LEDGER_PATH / REPORTS_DIR, "
            "or set CARDSCANR_TEST_RUNTIME_ROOT."
        )