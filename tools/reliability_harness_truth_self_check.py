#!/usr/bin/env python3
"""LOCAL harness self-check before next live reliability authorisation.

No eBay / no pricing / no navigation.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.live_navigation_attempt import (
    attempts_dir,
    attempts_dir_wsl,
    emit_search_submission_started,
    lookup_search_submission_event,
)
from cardscanr_market_engine.reliability_harness_evidence import classify_card_from_job_result
from cardscanr_market_engine.wsl_path import same_physical_path, windows_to_wsl_path, wsl_to_windows_path

OUT = ROOT / "reports" / "artifacts" / "reliability_harness_truth_closure"
FIXTURE = OUT / "fixtures" / "ceruledge_card1"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_self_check() -> dict:
    """Return self-check payload (also writes harness_self_check.json)."""
    OUT.mkdir(parents=True, exist_ok=True)
    results: dict = {"provedAtUtc": _utc(), "checks": {}}

    # 1) shared directory proof
    with tempfile.TemporaryDirectory() as tmp:
        win_dir = Path(tmp).resolve()
        wsl_dir = windows_to_wsl_path(win_dir)
        attempt = "selfcheck-shared-1"
        script = f"""
set -e
mkdir -p {wsl_dir}
python3 - <<'PY'
import json
from pathlib import Path
p = Path({wsl_dir!r}) / "{attempt}.SEARCH_SUBMISSION_STARTED.json"
p.write_text(json.dumps({{
  "event":"SEARCH_SUBMISSION_STARTED","attemptId":"{attempt}",
  "timestamp":"2026-10-02T00:00:00Z","queryFingerprint":"x","query":"q","pid":1,"priceKeyId":"pk"
}}, indent=2)+"\\n", encoding="utf-8")
print("OK")
PY
"""
        sh = Path(r"D:\DevCache\Temp\wsl_harness_selfcheck.sh")
        sh.write_bytes(script.replace("\r\n", "\n").encode("utf-8"))
        proc = subprocess.run(
            ["wsl", "-d", "Ubuntu", "--", "bash", "/mnt/d/DevCache/Temp/wsl_harness_selfcheck.sh"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        os.environ["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = str(win_dir)
        try:
            lookup = lookup_search_submission_event(attempt, expected_price_key_id="pk")
        finally:
            os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)
        results["checks"]["attemptEventDirectoryShared"] = bool(
            proc.returncode == 0 and lookup.get("valid") and same_physical_path(win_dir, wsl_to_windows_path(wsl_dir))
        )
        results["checks"]["attemptLookupById"] = bool(lookup.get("valid"))

    # 2) Ceruledge success fixture classification
    job = json.loads((FIXTURE / "job_result.json").read_text(encoding="utf-8"))
    ba = json.loads((FIXTURE / "before_after.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        src = FIXTURE / "65a39d2e-4b61-4406-8e03-561883973213.SEARCH_SUBMISSION_STARTED.json"
        Path(tmp, src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        os.environ["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = tmp
        try:
            c = classify_card_from_job_result(
                job,
                attempt_id="65a39d2e-4b61-4406-8e03-561883973213",
                price_key_id="686b5181-145f-4a87-a770-d4176e520ec1",
                job_id="ceae1679-2117-4cdb-88e8-5e706a298d37",
                fingerprint="pokemon|en|me2|20|ceruledge|raw|raw|au|aud",
                before=ba["before"],
                after=ba["after"],
            )
        finally:
            os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)
    results["checks"]["successfulFixtureClassification"] = c.get("cardVerdict") == "PASS_PRICE_UPDATED"
    results["checks"]["successfulFixtureCaptureEvidence"] = c.get("capture", {}).get("status") == "SUCCESS"
    results["checks"]["successfulFixtureParseEvidence"] = c.get("parse", {}).get("phase") == "PARSE_COMPLETE"
    results["checks"]["successfulFixtureWriteEvidence"] = (
        c.get("write", {}).get("outcome") == "UPDATED_FROM_EBAY"
        and float(c.get("write", {}).get("resultingPrice") or 0) == 2.99
    )
    results["ceruledgeReplay"] = c

    # 3) pre-submit + capture-failure classifications
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = tmp
        try:
            pre = classify_card_from_job_result(
                {"status": "failed", "error": "LOCAL_GUI"},
                attempt_id="pre-1",
                price_key_id="pk",
            )
            emit_search_submission_started(attempt_id="cap-1", query="q", price_key_id="pk")
            cap_fail = classify_card_from_job_result(
                {
                    "status": "failed",
                    "ownedDailyOutcome": "POST_SOLD_CAPTURE_FAILURE",
                    "x11SoldStateVerified": True,
                    "postSoldCapturePhase": "POST_SOLD_CAPTURE_FAILED",
                    "error": "CAPTURE_ENCODING_FAILURE",
                },
                attempt_id="cap-1",
                price_key_id="pk",
            )
        finally:
            os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)
    results["checks"]["preSubmitFixtureClassification"] = pre.get("cardVerdict") in {
        "STOP_PREFLIGHT",
        "FAIL_NAVIGATION",
    } and not pre.get("searchSubmitted")
    results["checks"]["captureFailureFixtureClassification"] = (
        cap_fail.get("searchSubmitted") is True and cap_fail.get("cardVerdict") == "FAIL_CAPTURE"
    )

    results["canonicalAttemptsDir"] = str(attempts_dir())
    results["canonicalAttemptsDirWsl"] = attempts_dir_wsl()
    results["ok"] = all(bool(v) for v in results["checks"].values())
    out_path = OUT / "harness_self_check.json"
    out_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    results["path"] = str(out_path)
    return results


def main() -> int:
    results = run_self_check()
    print(json.dumps({"ok": results["ok"], "checks": results["checks"], "path": results["path"]}, indent=2))
    return 0 if results["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
