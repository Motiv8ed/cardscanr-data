"""OS-enforceable post-Sold capture process boundary.

The pricing worker must never block on Playwright/CDP sync calls. Capture runs
in a disposable child process that the parent can terminate/kill on a hard
wall-clock deadline.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .post_sold_capture import (
    CAPTURE_ARTIFACT_WRITE_FAILURE,
    CAPTURE_ENCODING_FAILURE,
    CAPTURE_INTEGRITY_FAILURE,
    CAPTURE_PROTOCOL_FAILURE,
    CDP_CAPTURE_PROCESS_CRASH,
    CDP_CAPTURE_PROCESS_TIMEOUT,
    CDP_CONNECT_FAILURE,
    POST_SOLD_CAPTURE_FAILED,
    POST_SOLD_CAPTURE_READY,
    SoldPageCaptureResult,
)

ROOT = Path(__file__).resolve().parent.parent.parent
WORKER_MODULE = "cardscanr_market_engine.providers.post_sold_capture_worker"

DEFAULT_CAPTURE_DEADLINE_SECONDS = 15.0
MAX_CAPTURE_DEADLINE_SECONDS = 20.0
DEFAULT_GRACE_SECONDS = 1.0
DEFAULT_POST_SOLD_FINALIZE_SECONDS = 30.0


def capture_deadline_seconds(default: float = DEFAULT_CAPTURE_DEADLINE_SECONDS) -> float:
    raw = os.getenv("EBAY_BROWSER_CAPTURE_PROCESS_TIMEOUT_SECONDS", "").strip()
    try:
        value = float(raw) if raw else float(default)
    except ValueError:
        value = float(default)
    return max(1.0, min(float(MAX_CAPTURE_DEADLINE_SECONDS), value))


def post_sold_finalize_deadline_seconds(default: float = DEFAULT_POST_SOLD_FINALIZE_SECONDS) -> float:
    raw = os.getenv("EBAY_BROWSER_POST_SOLD_FINALIZE_TIMEOUT_SECONDS", "").strip()
    try:
        value = float(raw) if raw else float(default)
    except ValueError:
        value = float(default)
    return max(10.0, min(60.0, value))


@dataclass
class CaptureProcessResult:
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0
    pid: int | None = None
    exit_code: int | None = None
    orphan_count_after: int = 0
    killed: bool = False
    stdout_raw: str = ""
    stderr_raw: str = ""

    @property
    def success(self) -> bool:
        return self.status == "SUCCESS"


def _creation_flags() -> int:
    if sys.platform == "win32":
        # New process group so we can taskkill /T the tree without touching Chrome.
        return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    return 0


def _kill_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def _terminate_process(proc: subprocess.Popen[Any], *, grace_seconds: float) -> bool:
    """Terminate then kill. Returns True if kill was required."""
    if proc.poll() is not None:
        return False
    killed = False
    pid = int(proc.pid or 0)
    try:
        if sys.platform == "win32":
            # Soft request first; CTRL_BREAK can fail without a console.
            subprocess.run(
                ["taskkill", "/PID", str(pid)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=max(0.1, grace_seconds))
        return False
    except Exception:
        pass
    killed = True
    _kill_process_tree(pid)
    try:
        proc.wait(timeout=max(0.5, grace_seconds))
    except Exception:
        pass
    return killed


def count_worker_descendants(pid: int | None) -> int:
    """Best-effort count of still-living capture-worker related processes for pid."""
    if not pid:
        return 0
    if sys.platform == "win32":
        try:
            # tasklist filter by PID is exact; tree check via wmic parent.
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\";"
                        f"if($p){{1}}else{{0}}"
                    ),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return 1 if (completed.stdout or "").strip() == "1" else 0
        except Exception:
            return 0
    try:
        os.kill(pid, 0)
        return 1
    except Exception:
        return 0


def run_capture_worker_process(
    *,
    cdp_endpoint: str,
    expected_url: str | None,
    expected_query: str | None,
    expected_origin: str = "ebay.com.au",
    deadline_seconds: float | None = None,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    max_results: int = 60,
    socket_timeout: float = 4.0,
    hang_at: str | None = None,
    artifact_path: str | None = None,
    python_executable: str | None = None,
) -> CaptureProcessResult:
    """Spawn capture worker; enforce hard OS deadline; never hang the parent."""
    budget = float(deadline_seconds if deadline_seconds is not None else capture_deadline_seconds())
    budget = max(1.0, min(MAX_CAPTURE_DEADLINE_SECONDS, budget))
    started = time.monotonic()
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    # Explicit UTF-8 contract for the capture subprocess (not a substitute for
    # binary stdout protocol / UTF-8 artifact writes, but required on Windows).
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if hang_at:
        env["CARDSCANR_CAPTURE_WORKER_HANG_AT"] = str(hang_at)
    else:
        env.pop("CARDSCANR_CAPTURE_WORKER_HANG_AT", None)

    py = python_executable or sys.executable
    cmd = [
        py,
        "-u",
        "-X",
        "utf8",
        "-m",
        WORKER_MODULE,
        "--cdp-endpoint",
        str(cdp_endpoint),
        "--expected-url",
        str(expected_url or ""),
        "--expected-query",
        str(expected_query or ""),
        "--expected-origin",
        str(expected_origin or "ebay.com.au"),
        "--max-results",
        str(int(max_results)),
        "--socket-timeout",
        str(float(socket_timeout)),
        "--deadline-seconds",
        str(budget),
    ]
    if artifact_path:
        cmd.extend(["--artifact-path", str(artifact_path)])

    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
        "cwd": str(ROOT),
        # Bytes mode: never decode via Windows charmap.
        "text": False,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = _creation_flags()
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603 — controlled worker argv
    pid = int(proc.pid or 0)
    killed = False
    try:
        stdout_b, stderr_b = proc.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        killed = _terminate_process(proc, grace_seconds=grace_seconds)
        try:
            stdout_b, stderr_b = proc.communicate(timeout=max(1.0, grace_seconds + 1.0))
        except Exception:
            stdout_b, stderr_b = b"", b""
        # Drain may still leave zombies briefly; force tree kill again.
        if proc.poll() is None:
            _kill_process_tree(pid)
            killed = True
        elapsed_ms = int((time.monotonic() - started) * 1000)
        time.sleep(0.05)
        orphans = count_worker_descendants(pid)
        return CaptureProcessResult(
            status=CDP_CAPTURE_PROCESS_TIMEOUT,
            payload={
                "status": CDP_CAPTURE_PROCESS_TIMEOUT,
                "elapsed_ms": elapsed_ms,
                "deadline_seconds": budget,
                "killed": killed,
            },
            elapsed_ms=elapsed_ms,
            pid=pid,
            exit_code=proc.poll(),
            orphan_count_after=orphans,
            killed=killed,
            stdout_raw=(stdout_b or b"").decode("utf-8", errors="replace"),
            stderr_raw=(stderr_b or b"").decode("utf-8", errors="replace"),
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    time.sleep(0.02)
    orphans = count_worker_descendants(pid)
    try:
        stdout = (stdout_b or b"").decode("utf-8")
    except UnicodeDecodeError as exc:
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        return CaptureProcessResult(
            status=CAPTURE_PROTOCOL_FAILURE,
            payload={
                "status": CAPTURE_PROTOCOL_FAILURE,
                "error": f"UnicodeDecodeError:{exc}",
                "elapsed_ms": elapsed_ms,
            },
            elapsed_ms=elapsed_ms,
            pid=pid,
            exit_code=proc.poll(),
            orphan_count_after=orphans,
            killed=killed,
            stdout_raw=stdout,
            stderr_raw=(stderr_b or b"").decode("utf-8", errors="replace"),
        )
    stderr = (stderr_b or b"").decode("utf-8", errors="replace")
    exit_code = proc.poll()

    payload: dict[str, Any] = {}
    status = CDP_CAPTURE_PROCESS_CRASH
    # Prefer last JSON object line on stdout.
    for line in reversed([ln.strip() for ln in stdout.splitlines() if ln.strip()]):
        try:
            parsed = json.loads(line)
        except Exception:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            status = str(parsed.get("status") or CDP_CAPTURE_PROCESS_CRASH)
            break
    else:
        if stdout.strip() and not stdout.strip().startswith("{"):
            status = CDP_CAPTURE_PROCESS_CRASH
            payload = {
                "status": status,
                "error": "malformed_json_output",
                "stdoutPreview": stdout[:500],
            }
        elif exit_code not in (0, None):
            status = CDP_CAPTURE_PROCESS_CRASH
            payload = {
                "status": status,
                "exitCode": exit_code,
                "stderrPreview": stderr[:500],
            }
        else:
            status = CAPTURE_PROTOCOL_FAILURE
            payload = {"status": status, "error": "empty_worker_output"}

    payload = _hydrate_capture_payload(payload)

    return CaptureProcessResult(
        status=status,
        payload=payload,
        elapsed_ms=elapsed_ms,
        pid=pid,
        exit_code=exit_code,
        orphan_count_after=orphans,
        killed=killed,
        stdout_raw=stdout,
        stderr_raw=stderr,
    )


def _is_shared_diagnostic_capture_path(path: str | Path | None) -> bool:
    if not path:
        return False
    norm = str(path).replace("\\", "/").lower()
    return "post_sold_capture_last" in norm


def _hydrate_capture_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Load UTF-8 HTML/body from durable artifact paths into the payload.

    Only hydrates on SUCCESS from the current-job paths returned by the worker.
    Never backfills from a previous run's leftover last_capture.* files — that
    would contaminate failure diagnostics. Shared post_sold_capture_last/*
    diagnostic mirrors are refused.
    """
    out = dict(payload)
    if str(out.get("status") or "") != "SUCCESS":
        return out
    html_path = out.get("html_path") or out.get("htmlPath")
    body_path = out.get("body_path") or out.get("bodyPath")
    if _is_shared_diagnostic_capture_path(html_path) or _is_shared_diagnostic_capture_path(body_path):
        out.setdefault("hydrateErrors", [])
        if isinstance(out["hydrateErrors"], list):
            out["hydrateErrors"].append("refused_shared_diagnostic_last_capture_path")
        out["status"] = CAPTURE_ARTIFACT_WRITE_FAILURE
        out["error"] = "hydrate_refused_shared_diagnostic_path"
        return out
    if html_path and not out.get("html"):
        try:
            out["html"] = Path(str(html_path)).read_text(encoding="utf-8")
        except Exception as exc:
            out.setdefault("hydrateErrors", [])
            if isinstance(out["hydrateErrors"], list):
                out["hydrateErrors"].append(f"html:{type(exc).__name__}:{exc}")
    if body_path and not out.get("body_text"):
        try:
            out["body_text"] = Path(str(body_path)).read_text(encoding="utf-8")
        except Exception as exc:
            out.setdefault("hydrateErrors", [])
            if isinstance(out["hydrateErrors"], list):
                out["hydrateErrors"].append(f"body:{type(exc).__name__}:{exc}")
    candidates_path = out.get("candidates_path") or out.get("candidatesPath")
    if candidates_path and not out.get("candidates"):
        if _is_shared_diagnostic_capture_path(candidates_path):
            out.setdefault("hydrateErrors", [])
            if isinstance(out["hydrateErrors"], list):
                out["hydrateErrors"].append("refused_shared_diagnostic_candidates_path")
        else:
            try:
                loaded = json.loads(Path(str(candidates_path)).read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    out["candidates"] = loaded
            except Exception as exc:
                out.setdefault("hydrateErrors", [])
                if isinstance(out["hydrateErrors"], list):
                    out["hydrateErrors"].append(f"candidates:{type(exc).__name__}:{exc}")
    return out


def capture_process_result_to_sold_page(
    result: CaptureProcessResult,
    *,
    x11_sold_state_verified: bool,
) -> SoldPageCaptureResult:
    """Map process result into SoldPageCaptureResult for provider integration."""
    payload = result.payload if isinstance(result.payload, dict) else {}
    success = result.success and bool(payload.get("body_text") or payload.get("html"))
    out = SoldPageCaptureResult(
        success=False,
        x11_sold_state_verified=bool(x11_sold_state_verified),
        capture_phase=POST_SOLD_CAPTURE_FAILED,
        target_id=payload.get("target_id"),
        target_url=payload.get("target_url"),
        target_title=payload.get("target_title"),
        capture_method=payload.get("capture_method"),
        html_or_text=str(payload.get("html") or payload.get("body_text") or "") or None,
        capture_elapsed_ms=int(payload.get("elapsed_ms") or result.elapsed_ms or 0),
        failure_class=None if success else str(result.status),
        failure_detail=str(payload.get("error") or payload.get("failure_detail") or "") or None,
        sold_state=payload.get("sold_state") if isinstance(payload.get("sold_state"), dict) else None,
        diagnostics={
            "captureProcess": {
                "status": result.status,
                "pid": result.pid,
                "exitCode": result.exit_code,
                "killed": result.killed,
                "orphanCountAfter": result.orphan_count_after,
                "elapsedMs": result.elapsed_ms,
            },
            "readiness": payload.get("readiness"),
            "methods": payload.get("methods"),
            "targets": (payload.get("diagnostics") or {}).get("targets")
            if isinstance(payload.get("diagnostics"), dict)
            else None,
            "canonicalItmHrefCount": payload.get("canonical_itm_href_count"),
            "uniqueItemIds": payload.get("unique_item_ids"),
            "candidateCount": (
                len(payload.get("candidates") or [])
                if isinstance(payload.get("candidates"), list)
                else int(payload.get("candidate_count") or 0)
            ),
            "workerDiagnostics": payload.get("diagnostics"),
        },
    )
    # Preserve artifact paths / hashes for diagnostics even on failure.
    for key in (
        "html_path",
        "body_path",
        "candidates_path",
        "meta_path",
        "html_sha256",
        "body_sha256",
        "html_length_bytes",
        "html_length",
        "job_id",
        "attempt_id",
        "price_key_id",
        "fingerprint",
        "capture_origin",
        "authoritativeArtifact",
        "sharedLastCaptureCritical",
        "diagnosticMirrorUpdated",
        "diagnosticMirrorWarning",
    ):
        if payload.get(key) is not None:
            out.diagnostics[key] = payload.get(key)

    if success:
        # Production provenance gate: require canonical /itm/ evidence OR candidate rows.
        itm_count = int(payload.get("canonical_itm_href_count") or 0)
        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        candidate_count = len(candidates) if candidates else int(payload.get("candidate_count") or 0)
        if itm_count <= 0 and candidate_count <= 0:
            out.success = False
            out.failure_class = CAPTURE_INTEGRITY_FAILURE
            out.failure_detail = "no_canonical_itm_provenance"
            out.capture_phase = POST_SOLD_CAPTURE_FAILED
            out.diagnostics["provenanceRejected"] = True
            return out
        out.success = True
        out.capture_phase = POST_SOLD_CAPTURE_READY
        out.failure_class = None
        out.diagnostics["candidates"] = candidates
    elif result.status == CDP_CAPTURE_PROCESS_TIMEOUT:
        out.failure_class = CDP_CAPTURE_PROCESS_TIMEOUT
        out.failure_detail = f"capture_worker_exceeded_{payload.get('deadline_seconds')}s"
    else:
        # Keep worker/process status as the authoritative failure class.
        # Never collapse encoding/write/protocol into CDP_CONNECT_FAILURE.
        out.failure_class = str(result.status or POST_SOLD_CAPTURE_FAILED)
        if payload.get("error"):
            out.failure_detail = str(payload.get("error"))
    return out


__all__ = [
    "CaptureProcessResult",
    "DEFAULT_CAPTURE_DEADLINE_SECONDS",
    "DEFAULT_POST_SOLD_FINALIZE_SECONDS",
    "MAX_CAPTURE_DEADLINE_SECONDS",
    "capture_deadline_seconds",
    "capture_process_result_to_sold_page",
    "count_worker_descendants",
    "post_sold_finalize_deadline_seconds",
    "run_capture_worker_process",
]
