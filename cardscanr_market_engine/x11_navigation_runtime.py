"""Durable X11 navigation runtime probe/ensure (no eBay contact).

Production GUI search/Sold tooling requires a persistent CardScanR-owned
Python interpreter under ``$HOME/.local/cardscanr-gui/venv`` (not ``/tmp``).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .browser_lifecycle_policy import (
    RUNTIME_COLD_START,
    PriorCardContext,
    evaluate_runtime_targets,
)
from .local_browser_runtime import (
    DEFAULT_CDP_PORT,
    DEFAULT_DISPLAY,
    WSL_DISTRO,
    ensure_xvfb,
    probe_local_browser_runtime,
)

ROOT = Path(__file__).resolve().parents[1]
SEARCH_SCRIPT = ROOT / "tools" / "linux_x11_ebay_search.py"
SOLD_SCRIPT = ROOT / "tools" / "linux_x11_ebay_sold.py"
LEGACY_TMP_VENV = "/tmp/cardscanr-xlib-venv"
DEFAULT_DURABLE_VENV_REL = ".local/cardscanr-gui/venv"
REQUIRED_IMPORTS = ("PIL", "Xlib")
REQUIRED_BINARIES = ("xdotool", "xwininfo", "xdpyinfo")


@dataclass
class X11NavigationRuntimeStatus:
    ready: bool = False
    reason_codes: list[str] = field(default_factory=list)
    interpreter: str | None = None
    interpreter_version: str | None = None
    imports_ok: dict[str, bool] = field(default_factory=dict)
    binaries_ok: dict[str, bool] = field(default_factory=dict)
    display: str | None = None
    display_ready: bool = False
    cdp_ready: bool | None = None
    ebay_targets: list[str] = field(default_factory=list)
    search_script_ok: bool = False
    sold_script_ok: bool = False
    search_self_check_ok: bool = False
    sold_self_check_ok: bool = False
    chrome_window_ready: bool = False
    window_focus_ready: bool = False
    keyboard_injection_ready: bool | None = None
    pre_submit_gui_ready: bool | None = None
    legacy_tmp_venv_referenced: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "reasonCodes": list(self.reason_codes),
            "interpreter": self.interpreter,
            "interpreterVersion": self.interpreter_version,
            "importsOk": dict(self.imports_ok),
            "binariesOk": dict(self.binaries_ok),
            "display": self.display,
            "displayReady": self.display_ready,
            "cdpReady": self.cdp_ready,
            "ebayTargets": list(self.ebay_targets),
            "searchScriptOk": self.search_script_ok,
            "soldScriptOk": self.sold_script_ok,
            "searchSelfCheckOk": self.search_self_check_ok,
            "soldSelfCheckOk": self.sold_self_check_ok,
            "chromeWindowReady": self.chrome_window_ready,
            "windowFocusReady": self.window_focus_ready,
            "keyboardInjectionReady": self.keyboard_injection_ready,
            "preSubmitGuiReady": self.pre_submit_gui_ready,
            "legacyTmpVenvReferenced": self.legacy_tmp_venv_referenced,
            "notes": list(self.notes),
            "x11NavigationRuntimeReady": self.ready,
        }


def durable_x11_python_path() -> str:
    override = (os.getenv("CARDSCANR_X11_PYTHON") or "").strip()
    if override:
        return override
    # Path is resolved inside WSL; Windows callers pass it through bash.
    return f"$HOME/{DEFAULT_DURABLE_VENV_REL}/bin/python"


def _wsl_bash(script: str, *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    tmp = Path(r"D:\DevCache\Temp") / f"wsl_x11nav_{os.getpid()}_{int(time.time() * 1000)}.sh"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(script.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))
    wsl_path = "/mnt/d/DevCache/Temp/" + tmp.name
    try:
        return subprocess.run(
            ["wsl", "-d", WSL_DISTRO, "--", "bash", wsl_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _prefix_env_block() -> str:
    return """
PREFIX="${CARDSCANR_GUI_PREFIX:-$HOME/.local/cardscanr-gui}"
PY="${CARDSCANR_X11_PYTHON:-$PREFIX/venv/bin/python}"
export PATH="$PREFIX/root/usr/bin:$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="$PREFIX/root/usr/lib/x86_64-linux-gnu:$PREFIX/root/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export XKB_CONFIG_ROOT="$PREFIX/root/usr/share/X11/xkb"
export DISPLAY="${CARDSCANR_XVFB_DISPLAY:-:99}"
unset WAYLAND_DISPLAY || true
"""


def probe_x11_navigation_runtime(
    *,
    cdp_port: int = DEFAULT_CDP_PORT,
    display: str = DEFAULT_DISPLAY,
    require_cdp: bool = True,
    run_self_check: bool = True,
    runtime_mode: str = RUNTIME_COLD_START,
    expected_prior: PriorCardContext | dict[str, Any] | None = None,
    raw_targets: list[dict[str, Any]] | None = None,
) -> X11NavigationRuntimeStatus:
    """Validate the actual production X11 navigation stack. Never opens eBay."""
    status = X11NavigationRuntimeStatus(display=display)
    reasons: list[str] = []
    mode = (runtime_mode or RUNTIME_COLD_START).upper()
    prior = (
        expected_prior
        if isinstance(expected_prior, PriorCardContext)
        else PriorCardContext.from_dict(expected_prior if isinstance(expected_prior, dict) else None)
    )

    # Host-side script presence (Windows workspace).
    status.search_script_ok = SEARCH_SCRIPT.is_file()
    status.sold_script_ok = SOLD_SCRIPT.is_file()
    if not status.search_script_ok:
        reasons.append("X11_NAV_SCRIPT_MISSING")
    if not status.sold_script_ok:
        reasons.append("X11_NAV_SCRIPT_MISSING")

    # Refuse production code still pointing at ephemeral /tmp venv.
    try:
        nav_src = (ROOT / "cardscanr_market_engine" / "providers" / "linux_x11_ebay_nav.py").read_text(
            encoding="utf-8"
        )
        status.legacy_tmp_venv_referenced = LEGACY_TMP_VENV in nav_src
        if status.legacy_tmp_venv_referenced:
            reasons.append("X11_NAV_LEGACY_TMP_VENV")
            status.notes.append("production_nav_still_references_/tmp/cardscanr-xlib-venv")
    except OSError as exc:
        status.notes.append(f"nav_source_read_error:{exc}")

    browser = probe_local_browser_runtime(cdp_port=cdp_port, display=display)
    status.display_ready = bool(browser.xvfb_ready)
    status.ebay_targets = list(browser.ebay_targets)
    targets_for_policy = list(raw_targets) if raw_targets is not None else list(browser.raw_targets)
    target_policy = evaluate_runtime_targets(targets_for_policy, mode=mode, prior=prior)
    status.notes.append(f"runtimeMode={mode}")
    if require_cdp:
        status.cdp_ready = bool(browser.cdp_ready)
        if not status.cdp_ready:
            reasons.append("X11_NAV_CDP_NOT_READY")
    else:
        status.cdp_ready = bool(browser.cdp_ready) if browser.cdp_ready else None
    if not status.display_ready:
        reasons.append("X11_NAV_DISPLAY_NOT_READY")
    if not target_policy.ok:
        # Preserve legacy code for cold-start unexpected pages; use policy codes otherwise.
        if "COLD_START_UNEXPECTED_EBAY_TARGET" in target_policy.reason_codes:
            reasons.append("X11_NAV_UNEXPECTED_EBAY_TARGET")
        reasons.extend(target_policy.reason_codes)

    self_check_flag = "1" if run_self_check else "0"
    prior_path = Path(r"D:\DevCache\Temp") / f"cardscanr_expected_prior_{os.getpid()}.json"
    prior_path.parent.mkdir(parents=True, exist_ok=True)
    prior_path.write_text(json.dumps(prior.to_dict() if prior else {}), encoding="utf-8")
    prior_wsl = "/mnt/d/DevCache/Temp/" + prior_path.name
    script = f"""
set +e
{_prefix_env_block()}
export CARDSCANR_RUNTIME_MODE='{mode}'
export CARDSCANR_EXPECTED_PRIOR_JSON_PATH='{prior_wsl}'
echo PY_PATH=$PY
if [ ! -x "$PY" ]; then
  echo PYTHON_MISSING
else
  echo PYTHON_OK
  "$PY" - <<'PY'
import json, sys
out = {{"version": sys.version, "executable": sys.executable, "imports": {{}}}}
for name in ("PIL", "Xlib"):
    try:
        __import__(name)
        out["imports"][name] = True
    except Exception as e:
        out["imports"][name] = False
print("IMPORT_JSON=" + json.dumps(out))
PY
fi
for b in xdotool xwininfo xdpyinfo; do
  if command -v "$b" >/dev/null 2>&1; then echo BIN_OK:$b; else echo BIN_MISSING:$b; fi
done
if xdpyinfo -display {display} >/dev/null 2>&1; then echo XDPY_OK; else echo XDPY_DOWN; fi
if [ "{self_check_flag}" = "1" ] && [ -x "$PY" ]; then
  "$PY" /mnt/d/cardscanr-data/tools/linux_x11_ebay_search.py --self-check --runtime-mode {mode}
  echo SEARCH_SELF_RC=$?
  "$PY" /mnt/d/cardscanr-data/tools/linux_x11_ebay_sold.py --self-check --runtime-mode {mode}
  echo SOLD_SELF_RC=$?
fi
"""
    try:
        proc = _wsl_bash(script, timeout=120)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except Exception as exc:
        status.notes.append(f"wsl_probe_error:{type(exc).__name__}:{exc}")
        reasons.append("X11_NAV_PYTHON_MISSING")
        status.reason_codes = sorted(set(reasons))
        status.ready = False
        return status

    if "PYTHON_MISSING" in out or "PYTHON_OK" not in out:
        reasons.append("X11_NAV_PYTHON_MISSING")
    else:
        for line in out.splitlines():
            if line.startswith("PY_PATH="):
                status.interpreter = line.split("=", 1)[1].strip() or None
            if line.startswith("IMPORT_JSON="):
                try:
                    payload = json.loads(line.split("=", 1)[1])
                    status.interpreter_version = str(payload.get("version") or "") or None
                    if payload.get("executable"):
                        status.interpreter = str(payload.get("executable"))
                    imports = payload.get("imports") or {}
                    if isinstance(imports, dict):
                        status.imports_ok = {str(k): bool(v) for k, v in imports.items()}
                except json.JSONDecodeError:
                    status.notes.append("import_json_parse_failed")

    for name in REQUIRED_IMPORTS:
        if not status.imports_ok.get(name):
            reasons.append("X11_NAV_DEPENDENCY_MISSING")
            break

    for name in REQUIRED_BINARIES:
        ok = f"BIN_OK:{name}" in out
        status.binaries_ok[name] = ok
        if not ok:
            reasons.append("X11_NAV_DEPENDENCY_MISSING")

    if "XDPY_OK" not in out:
        if "X11_NAV_DISPLAY_NOT_READY" not in reasons:
            reasons.append("X11_NAV_DISPLAY_NOT_READY")
        status.display_ready = False
    else:
        status.display_ready = True

    if run_self_check:
        status.search_self_check_ok = "SEARCH_SELF_RC=0" in out or (
            '"ok": true' in out.lower() and "searchSelfCheck" in out
        )
        status.sold_self_check_ok = "SOLD_SELF_RC=0" in out or (
            '"ok": true' in out.lower() and "soldSelfCheck" in out
        )
        # Prefer explicit RC markers.
        for line in out.splitlines():
            if line.startswith("SEARCH_SELF_RC="):
                status.search_self_check_ok = line.split("=", 1)[1].strip() == "0"
            if line.startswith("SOLD_SELF_RC="):
                status.sold_self_check_ok = line.split("=", 1)[1].strip() == "0"
        if not status.search_self_check_ok or not status.sold_self_check_ok:
            reasons.append("X11_NAV_SELF_CHECK_FAILED")
            status.notes.append((out[-1500:] if len(out) > 1500 else out))

    # Parse focus readiness from search self-check output.
    if '"windowFocusReady": true' in out or '"windowFocusReady":true' in out:
        status.window_focus_ready = True
    if '"chromeWindowReady": true' in out or '"chromeWindowReady":true' in out:
        status.chrome_window_ready = True
    try:
        marker = '"selfCheck": "linux_x11_ebay_search"'
        idx = out.find(marker)
        if idx >= 0:
            start = out.rfind("{", 0, idx)
            # Find matching end by scanning forward for SEARCH_SELF_RC
            end_marker = out.find("SEARCH_SELF_RC=", idx)
            chunk = out[start:end_marker] if end_marker > start else out[start:]
            end = chunk.rfind("}")
            if start >= 0 and end > 0:
                blob = json.loads(chunk[: end + 1])
                details = blob.get("details") if isinstance(blob, dict) else None
                if isinstance(details, dict):
                    status.chrome_window_ready = bool(details.get("chromeWindowReady"))
                    status.window_focus_ready = bool(details.get("windowFocusReady"))
    except Exception as exc:
        status.notes.append(f"self_check_focus_parse:{exc}")

    if run_self_check and status.search_self_check_ok:
        if not status.chrome_window_ready:
            reasons.append("X11_CHROME_WINDOW_NOT_READY")
        if not status.window_focus_ready:
            reasons.append("X11_WINDOW_FOCUS_NOT_READY")

    # Optional deep GUI proof artifact (keyboard + pre-submit) from prior offline proof.
    proof_path = ROOT / "reports" / "artifacts" / "x11_chrome_focus_closure" / "keyboard_injection_proof.json"
    if proof_path.is_file():
        try:
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            status.keyboard_injection_ready = bool(proof.get("keyboardInjectionReady") or proof.get("typedMarkerVerified"))
            status.pre_submit_gui_ready = bool(
                proof.get("preSubmitGuiReady") or proof.get("resultCode") == "PRE_SUBMIT_GUI_READY"
            )
            if proof.get("ok") and status.keyboard_injection_ready and status.pre_submit_gui_ready:
                pass
            else:
                reasons.append("X11_GUI_PROOF_NOT_READY")
        except Exception as exc:
            status.notes.append(f"gui_proof_read:{exc}")
            reasons.append("X11_GUI_PROOF_NOT_READY")
    elif run_self_check:
        # Deep proof required for declaring full X11 navigation runtime ready.
        reasons.append("X11_GUI_PROOF_NOT_READY")

    status.reason_codes = sorted(set(reasons))
    status.ready = len(status.reason_codes) == 0
    if status.ready:
        status.reason_codes = ["X11_NAV_READY"]
    return status


def ensure_x11_navigation_runtime(
    *,
    cdp_port: int = DEFAULT_CDP_PORT,
    display: str = DEFAULT_DISPLAY,
    require_cdp: bool = False,
) -> dict[str, Any]:
    """Ensure Xvfb + durable venv deps where possible. Never opens eBay."""
    xv = ensure_xvfb(display=display)
    probe = probe_x11_navigation_runtime(
        cdp_port=cdp_port,
        display=display,
        require_cdp=require_cdp,
        run_self_check=True,
    )
    return {
        "ok": bool(probe.ready),
        "xvfb": xv,
        "probe": probe.to_dict(),
        "reasonCodes": list(probe.reason_codes),
    }


def require_x11_navigation_runtime_ready(**kwargs: Any) -> X11NavigationRuntimeStatus:
    status = probe_x11_navigation_runtime(**kwargs)
    if not status.ready:
        codes = ",".join(status.reason_codes) or "X11_NAV_NOT_READY"
        raise RuntimeError(f"X11_NAVIGATION_RUNTIME_NOT_READY:{codes}")
    return status


__all__ = [
    "LEGACY_TMP_VENV",
    "X11NavigationRuntimeStatus",
    "durable_x11_python_path",
    "ensure_x11_navigation_runtime",
    "probe_x11_navigation_runtime",
    "require_x11_navigation_runtime_ready",
]
