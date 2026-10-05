#!/usr/bin/env python3
"""Local Xvfb/CDP readiness — independent of marketplace challenge eligibility.

Contract:
  - Production MAY safely bootstrap Xvfb + Chrome/CDP before navigation.
  - Bootstrap MUST open about:blank (or another non-eBay local target).
  - Bootstrap does NOT increment liveNavigationStarted / reliability attempts.
  - Challenge/control-plane gates remain separate and fail-closed.

WSL/WSLg notes:
  - System DISPLAY often :0 (WSLg). CardScanR uses :99 via bundled Xvfb.
  - ``/tmp/.X11-unix`` may be a WSLg mount that rejects chmod / new sockets.
  - Bundled Xvfb still execs absolute ``/usr/bin/xkbcomp`` (often missing on host).
  - ``ensure_xvfb`` starts Xvfb via ``tools/cardscanr_ensure_xvfb.sh`` inside a
    user+mount namespace with an overlay providing ``/usr/bin/xkbcomp``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .browser_lifecycle_policy import (
    RUNTIME_COLD_START,
    RUNTIME_INTER_CARD,
    PriorCardContext,
    evaluate_runtime_targets,
    extra_inter_card_ebay_target_ids,
    is_ebay_marketplace_host,
    hostname_of,
)


DEFAULT_CDP_PORT = int(os.getenv("EBAY_BROWSER_CDP_PORT", "9444"))
DEFAULT_DISPLAY = os.getenv("CARDSCANR_XVFB_DISPLAY", ":99")
WSL_DISTRO = os.getenv("CARDSCANR_WSL_DISTRO", "Ubuntu")
ROOT = Path(__file__).resolve().parents[1]
ENSURE_XVFB_SH = ROOT / "tools" / "cardscanr_ensure_xvfb.sh"
XVFB_PIDFILE = "/tmp/cardscanr_xvfb.pid"
XVFB_LOG = "/tmp/cardscanr_xvfb.log"


@dataclass
class LocalBrowserRuntimeStatus:
    xvfb_ready: bool = False
    display: str | None = None
    xvfb_pid: int | None = None
    cdp_ready: bool = False
    cdp_port: int = DEFAULT_CDP_PORT
    cdp_browser: str | None = None
    ebay_targets: list[str] = field(default_factory=list)
    raw_targets: list[dict[str, Any]] = field(default_factory=list)
    wsl_available: bool = False
    xkbcomp_resolved: bool | None = None
    x11_unix_mode: str | None = None
    notes: list[str] = field(default_factory=list)
    ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "xvfbReady": self.xvfb_ready,
            "display": self.display,
            "xvfbPid": self.xvfb_pid,
            "cdpReady": self.cdp_ready,
            "cdpPort": self.cdp_port,
            "cdpBrowser": self.cdp_browser,
            "ebayTargets": list(self.ebay_targets),
            "rawTargets": list(self.raw_targets),
            "wslAvailable": self.wsl_available,
            "xkbcompResolved": self.xkbcomp_resolved,
            "x11UnixMode": self.x11_unix_mode,
            "notes": list(self.notes),
            "ready": self.ready,
        }


def _wsl_bash(script: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    tmp = Path(r"D:\DevCache\Temp") / f"wsl_runtime_{os.getpid()}_{int(time.time() * 1000)}.sh"
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
export PATH="$PREFIX/root/usr/bin:$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="$PREFIX/root/usr/lib/x86_64-linux-gnu:$PREFIX/root/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export XKB_CONFIG_ROOT="$PREFIX/root/usr/share/X11/xkb"
unset WAYLAND_DISPLAY || true
"""


def probe_local_browser_runtime(
    *,
    cdp_port: int = DEFAULT_CDP_PORT,
    display: str = DEFAULT_DISPLAY,
) -> LocalBrowserRuntimeStatus:
    """Read-only readiness probe. Never starts Chrome or contacts eBay."""
    status = LocalBrowserRuntimeStatus(cdp_port=int(cdp_port), display=display)
    disp_num = display.lstrip(":")
    script = f"""
set +e
{_prefix_env_block()}
echo WSL_OK
if command -v xkbcomp >/dev/null 2>&1; then echo XKBCOMP_PREFIX_OK; else echo XKBCOMP_PREFIX_MISSING; fi
if [ -x /usr/bin/xkbcomp ]; then echo XKBCOMP_SYSTEM_OK; else echo XKBCOMP_SYSTEM_MISSING; fi
if [ -d /tmp/.X11-unix ]; then
  echo X11_UNIX_MODE=$(stat -c '%a' /tmp/.X11-unix 2>/dev/null || echo unknown)
else
  echo X11_UNIX_MISSING
fi
if [ -e /tmp/.X{disp_num}-lock ]; then echo LOCK_PRESENT; else echo LOCK_ABSENT; fi
PIDS=$(pgrep -a Xvfb 2>/dev/null | grep -E "Xvfb[ ]+{display}([ ]|$)" || true)
if [ -n "$PIDS" ]; then
  echo XVFB_PROC_PRESENT
  echo "$PIDS" | sed 's/^/XVFB_PROC /'
else
  echo XVFB_PROC_ABSENT
fi
if [ -f {XVFB_PIDFILE} ]; then echo PIDFILE=$(cat {XVFB_PIDFILE}); fi
if xdpyinfo -display {display} >/dev/null 2>&1; then
  echo XDPY_OK
  xdpyinfo -display {display} 2>/dev/null | sed -n '1,30p' | sed 's/^/XDPY /'
else
  echo XDPY_DOWN
fi
"""
    try:
        proc = _wsl_bash(script, timeout=30)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        status.wsl_available = "WSL_OK" in out
        if not status.wsl_available:
            status.notes.append(f"wsl_unavailable:rc={proc.returncode}")
            if proc.stderr:
                status.notes.append((proc.stderr or "")[:400])
        else:
            status.xkbcomp_resolved = ("XKBCOMP_PREFIX_OK" in out) or ("XKBCOMP_SYSTEM_OK" in out)
            if "XKBCOMP_SYSTEM_MISSING" in out and "XKBCOMP_PREFIX_OK" in out:
                status.notes.append("xkbcomp_prefix_only_system_path_missing")
            m = re.search(r"X11_UNIX_MODE=(\S+)", out)
            if m:
                status.x11_unix_mode = m.group(1)
            m = re.search(r"PIDFILE=(\d+)", out)
            if m:
                status.xvfb_pid = int(m.group(1))
            m = re.search(r"XVFB_PROC\s+(\d+)\s+", out)
            if m and status.xvfb_pid is None:
                status.xvfb_pid = int(m.group(1))
            status.xvfb_ready = ("XDPY_OK" in out) and (
                "XVFB_PROC_PRESENT" in out or "XDPY_OK" in out
            )
            # Prefer requiring both; if xdpy answers, treat as ready even if pgrep text differs.
            if "XDPY_OK" in out:
                status.xvfb_ready = True
            else:
                status.xvfb_ready = False
                status.notes.append("xvfb_or_display_not_ready")
            if "XDPY_OK" in out and "XVFB_PROC_ABSENT" in out:
                status.notes.append("xdpy_ok_but_pgrep_miss")
    except Exception as exc:
        status.notes.append(f"wsl_probe_error:{type(exc).__name__}:{exc}")

    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{int(cdp_port)}/json/version",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            meta = json.loads(resp.read().decode("utf-8", errors="replace"))
        status.cdp_ready = True
        status.cdp_browser = str(meta.get("Browser") or "") or None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        status.cdp_ready = False
        status.notes.append(f"cdp_down:{type(exc).__name__}")

    if status.cdp_ready:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{int(cdp_port)}/json/list", method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                targets = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(targets, list):
                for t in targets:
                    if not isinstance(t, dict):
                        continue
                    status.raw_targets.append(
                        {
                            "id": t.get("id"),
                            "type": t.get("type") or "page",
                            "url": t.get("url") or "",
                            "title": t.get("title") or "",
                        }
                    )
                    url = str(t.get("url") or "")
                    # Hostname-only marketplace detection — do not match ebay. inside ad query strings.
                    if is_ebay_marketplace_host(hostname_of(url)):
                        status.ebay_targets.append(url)
        except Exception as exc:
            status.notes.append(f"cdp_list_error:{type(exc).__name__}")

    # Default ready uses COLD_START semantics (no unexpected top-level marketplace pages).
    cold = evaluate_runtime_targets(status.raw_targets, mode=RUNTIME_COLD_START, prior=None)
    status.ready = bool(status.xvfb_ready and status.cdp_ready and cold.ok)
    if not cold.ok:
        status.notes.extend(cold.reason_codes)
    return status


def ensure_xvfb(*, display: str = DEFAULT_DISPLAY) -> dict[str, Any]:
    """Start Xvfb on the CardScanR display if missing. No network / eBay activity."""
    before = probe_local_browser_runtime(display=display)
    if before.xvfb_ready:
        return {
            "ok": True,
            "alreadyRunning": True,
            "reason": "already_running",
            "display": display,
            "pid": before.xvfb_pid,
            "status": before.to_dict(),
        }
    if not before.wsl_available:
        return {
            "ok": False,
            "alreadyRunning": False,
            "reason": "wsl_unavailable",
            "display": display,
            "status": before.to_dict(),
            "notes": before.notes,
        }

    # Ensure LF script on the Windows-hosted path, then execute via WSL.
    script_host = ENSURE_XVFB_SH
    if not script_host.exists():
        return {
            "ok": False,
            "alreadyRunning": False,
            "reason": "ensure_script_missing",
            "path": str(script_host),
            "status": before.to_dict(),
        }
    text = script_host.read_text(encoding="utf-8")
    script_host.write_bytes(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))
    wsl_script = "/mnt/d/cardscanr-data/tools/cardscanr_ensure_xvfb.sh"
    try:
        proc = subprocess.run(
            ["wsl", "-d", WSL_DISTRO, "--", "bash", wsl_script, display],
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        return {
            "ok": False,
            "alreadyRunning": False,
            "reason": f"xvfb_start_error:{type(exc).__name__}:{exc}",
            "display": display,
            "status": probe_local_browser_runtime(display=display).to_dict(),
        }

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    payload: dict[str, Any] = {}
    try:
        start = out.rfind("{")
        end = out.rfind("}")
        if start >= 0 and end > start:
            payload = json.loads(out[start : end + 1])
    except json.JSONDecodeError:
        payload = {}

    after = probe_local_browser_runtime(display=display)
    ok = bool(payload.get("ok")) and after.xvfb_ready
    reason = str(payload.get("reason") or ("ok" if ok else "xvfb_start_failed"))
    return {
        "ok": ok,
        "alreadyRunning": reason == "already_running",
        "reason": reason,
        "display": display,
        "pid": payload.get("pid") or after.xvfb_pid,
        "unsharePid": payload.get("unsharePid"),
        "statusFile": payload.get("statusFile"),
        "logTail": payload.get("logTail") or "",
        "stderrTail": err[-800:],
        "status": after.to_dict(),
    }


def assert_safe_chrome_start_url(start_url: str) -> str:
    """Return normalised local start URL or raise if eBay would be contacted."""
    safe = (start_url or "about:blank").strip() or "about:blank"
    if "ebay." in safe.lower():
        raise RuntimeError(
            "chrome_start_url_rejects_ebay: bootstrap must use about:blank "
            "(or another non-eBay local target); eBay navigation is a separate authorised step"
        )
    return safe


def reconcile_inter_card_cdp_tabs(
    *,
    cdp_port: int = DEFAULT_CDP_PORT,
    prior: PriorCardContext | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Close leftover top-level eBay tabs that are not the expected prior Sold page."""
    prior_ctx = (
        prior
        if isinstance(prior, PriorCardContext)
        else PriorCardContext.from_dict(prior if isinstance(prior, dict) else None)
    )
    browser = probe_local_browser_runtime(cdp_port=cdp_port)
    policy = evaluate_runtime_targets(browser.raw_targets, mode=RUNTIME_INTER_CARD, prior=prior_ctx)
    close_ids = extra_inter_card_ebay_target_ids(policy)
    closed: list[str] = []
    errors: list[str] = []
    for target_id in close_ids:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{cdp_port}/json/close/{target_id}",
                timeout=3,
            ) as resp:
                resp.read()
            closed.append(target_id)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            errors.append(f"{target_id}:{exc}")
    if closed:
        time.sleep(0.4)
        browser = probe_local_browser_runtime(cdp_port=cdp_port)
        policy = evaluate_runtime_targets(browser.raw_targets, mode=RUNTIME_INTER_CARD, prior=prior_ctx)
    return {
        "closedTargetIds": closed,
        "closeErrors": errors,
        "ok": bool(policy.ok),
        "targetPolicy": policy.to_dict(),
    }


def probe_pre_live_runtime(
    *,
    cdp_port: int = DEFAULT_CDP_PORT,
    display: str = DEFAULT_DISPLAY,
    runtime_mode: str = RUNTIME_COLD_START,
    expected_prior: PriorCardContext | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full pre-live gate: Xvfb + CDP + mode-aware target policy + X11 navigation runtime."""
    from .x11_navigation_runtime import probe_x11_navigation_runtime

    prior = (
        expected_prior
        if isinstance(expected_prior, PriorCardContext)
        else PriorCardContext.from_dict(expected_prior if isinstance(expected_prior, dict) else None)
    )
    mode = (runtime_mode or RUNTIME_COLD_START).upper()
    if mode == RUNTIME_INTER_CARD and prior is not None:
        reconcile_inter_card_cdp_tabs(cdp_port=cdp_port, prior=prior)
    browser = probe_local_browser_runtime(cdp_port=cdp_port, display=display)
    target_policy = evaluate_runtime_targets(browser.raw_targets, mode=mode, prior=prior)
    x11 = probe_x11_navigation_runtime(
        cdp_port=cdp_port,
        display=display,
        require_cdp=True,
        run_self_check=True,
        runtime_mode=mode,
        expected_prior=prior,
        raw_targets=browser.raw_targets,
    )
    browser_ok = bool(browser.xvfb_ready and browser.cdp_ready and target_policy.ok)
    ready = bool(browser_ok and x11.ready)
    return {
        "ready": ready,
        "runtimeMode": mode,
        "xvfbReady": browser.xvfb_ready,
        "cdpReady": browser.cdp_ready,
        "ebayTargets": list(browser.ebay_targets),
        "targetPolicy": target_policy.to_dict(),
        "interCardTargetAccepted": bool(target_policy.expected_prior_accepted),
        "x11NavigationRuntimeReady": x11.ready,
        "chromeWindowReady": x11.chrome_window_ready,
        "windowFocusReady": x11.window_focus_ready,
        "keyboardInjectionReady": bool(x11.keyboard_injection_ready),
        "preSubmitGuiReady": bool(x11.pre_submit_gui_ready),
        "searchToolSelfCheck": x11.search_self_check_ok,
        "soldToolSelfCheck": x11.sold_self_check_ok,
        "browser": browser.to_dict(),
        "x11Navigation": x11.to_dict(),
        "reasonCodes": [] if ready else sorted(
            set(
                (["BROWSER_RUNTIME_NOT_READY"] if not browser_ok else [])
                + list(target_policy.reason_codes)
                + list(x11.reason_codes)
            )
        ),
    }


__all__ = [
    "DEFAULT_CDP_PORT",
    "DEFAULT_DISPLAY",
    "LocalBrowserRuntimeStatus",
    "assert_safe_chrome_start_url",
    "ensure_xvfb",
    "probe_local_browser_runtime",
    "probe_pre_live_runtime",
    "reconcile_inter_card_cdp_tabs",
]
