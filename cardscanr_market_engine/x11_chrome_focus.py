"""Chrome X11 window discovery and focus for bare Xvfb (no EWMH WM).

CardScanR runs Chrome on DISPLAY=:99 under Xvfb without a window manager.
``xdotool windowactivate`` requires ``_NET_ACTIVE_WINDOW`` and aborts when no
EWMH WM is present. Production must use ``windowfocus`` / Xlib input focus and
verify focus before keyboard injection.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Any

DISPLAY_NAME = os.environ.get("DISPLAY", ":99")

REASON_NOT_FOUND = "X11_CHROME_WINDOW_NOT_FOUND"
REASON_STALE = "X11_CHROME_WINDOW_STALE"
REASON_NOT_MAPPED = "X11_CHROME_WINDOW_NOT_MAPPED"
REASON_EWMH_UNAVAILABLE = "X11_EWMH_UNAVAILABLE"
REASON_FOCUS_FAILED = "X11_FOCUS_FAILED"
REASON_FOCUS_VERIFY_FAILED = "X11_FOCUS_VERIFICATION_FAILED"
REASON_READY = "X11_CHROME_FOCUS_READY"


@dataclass
class ChromeFocusResult:
    window_id: int | None = None
    window_id_hex: str | None = None
    window_pid: int | None = None
    window_class: str | None = None
    window_name: str | None = None
    window_mapped: bool = False
    window_manager_present: bool = False
    ewmh_active_window_supported: bool = False
    focus_method: str | None = None
    focus_verified: bool = False
    focused_window_id: int | None = None
    reason_code: str = REASON_FOCUS_FAILED
    geometry: dict[str, int] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.reason_code == REASON_READY and self.focus_verified

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ready"] = self.ready
        payload["windowId"] = self.window_id
        payload["windowPid"] = self.window_pid
        payload["windowClass"] = self.window_class
        payload["windowMapped"] = self.window_mapped
        payload["windowManagerPresent"] = self.window_manager_present
        payload["focusMethod"] = self.focus_method
        payload["focusVerified"] = self.focus_verified
        payload["focusedWindowId"] = self.focused_window_id
        payload["reasonCode"] = self.reason_code
        return payload


def _run(cmd: list[str] | str, *, shell: bool = False, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        shell=shell,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "DISPLAY": DISPLAY_NAME},
    )


def probe_ewmh_window_manager() -> dict[str, Any]:
    """Detect whether an EWMH-capable WM owns the root window."""
    supporting = _run(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"])
    active = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    clients = _run(["xprop", "-root", "_NET_CLIENT_LIST"])
    supporting_txt = (supporting.stdout or "") + (supporting.stderr or "")
    active_txt = (active.stdout or "") + (active.stderr or "")
    clients_txt = (clients.stdout or "") + (clients.stderr or "")
    supporting_ok = bool(
        re.search(r"_NET_SUPPORTING_WM_CHECK\(WINDOW\):\s*window id #", supporting_txt)
    )
    active_atom = "not found" not in active_txt.lower() and "no such atom" not in active_txt.lower()
    present = bool(supporting_ok)
    return {
        "windowManagerPresent": present,
        "ewmhActiveWindowSupported": bool(present and active_atom),
        "supportingWmCheck": supporting_txt.strip()[:500],
        "activeWindowProp": active_txt.strip()[:500],
        "clientListProp": clients_txt.strip()[:500],
        "classification": "EWMH_WM_PRESENT" if present else "NO_EWMH_WINDOW_MANAGER",
    }


def _parse_xwininfo(text: str) -> dict[str, Any]:
    def grab(label: str) -> int | None:
        for line in text.splitlines():
            if label in line:
                try:
                    return int(line.split()[-1])
                except ValueError:
                    return None
        return None

    mapped = "IsViewable" in text or "Map State: IsViewable" in text
    if "IsUnMapped" in text or "Map State: IsUnMapped" in text:
        mapped = False
    return {
        "x": grab("Absolute upper-left X") or 0,
        "y": grab("Absolute upper-left Y") or 0,
        "w": grab("Width:") or 0,
        "h": grab("Height:") or 0,
        "mapped": mapped,
        "raw": text,
    }


def _xprop_id(wid_hex: str) -> dict[str, Any]:
    proc = _run(
        [
            "xprop",
            "-id",
            wid_hex,
            "WM_CLASS",
            "WM_NAME",
            "WM_STATE",
            "_NET_WM_PID",
            "WM_TRANSIENT_FOR",
        ]
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    wm_class = None
    m = re.search(r"WM_CLASS\(STRING\)\s*=\s*(.+)", text)
    if m:
        wm_class = m.group(1).strip()
    wm_name = None
    m = re.search(r'WM_NAME\([^)]+\)\s*=\s*"([^"]*)"', text)
    if m:
        wm_name = m.group(1)
    pid = None
    m = re.search(r"_NET_WM_PID\(CARDINAL\)\s*=\s*(\d+)", text)
    if m:
        pid = int(m.group(1))
    transient = False
    if re.search(r"WM_TRANSIENT_FOR\(WINDOW\):\s*window id #", text):
        transient = True
    return {
        "wmClass": wm_class,
        "wmName": wm_name,
        "pid": pid,
        "transient": transient,
        "raw": text,
    }


def _candidate_windows() -> list[dict[str, Any]]:
    """Enumerate likely Chrome top-level windows from the root tree."""
    tree = _run("xwininfo -root -tree", shell=True)
    lines = (tree.stdout or "").splitlines()
    candidates: list[dict[str, Any]] = []
    for line in lines:
        if "Google Chrome" not in line and "google-chrome" not in line.lower():
            continue
        m = re.match(r"\s*(0x[0-9a-fA-F]+)", line)
        if not m:
            continue
        wid_hex = m.group(1)
        geom_m = re.search(r"(\d+)x(\d+)\+\-?\d+\+\-?\d+", line)
        tree_w = int(geom_m.group(1)) if geom_m else 0
        tree_h = int(geom_m.group(2)) if geom_m else 0
        if tree_w and tree_h and (tree_w < 200 or tree_h < 200):
            continue
        info = _run(["xwininfo", "-id", wid_hex])
        geom = _parse_xwininfo(info.stdout or "")
        props = _xprop_id(wid_hex)
        if props.get("transient"):
            continue
        wm_class = str(props.get("wmClass") or "")
        if wm_class and "chrome" not in wm_class.lower():
            continue
        if geom["w"] < 400 or geom["h"] < 300:
            continue
        candidates.append(
            {
                "wid_hex": wid_hex,
                "wid": int(wid_hex, 16),
                "x": geom["x"],
                "y": geom["y"],
                "w": geom["w"],
                "h": geom["h"],
                "mapped": bool(geom["mapped"]),
                "wmClass": props.get("wmClass"),
                "wmName": props.get("wmName"),
                "pid": props.get("pid"),
                "area": int(geom["w"]) * int(geom["h"]),
            }
        )

    def sort_key(c: dict[str, Any]) -> tuple:
        cls = str(c.get("wmClass") or "").lower()
        profile_bonus = 1 if "cardscanr-chrome" in cls else 0
        return (1 if c.get("mapped") else 0, profile_bonus, c.get("area") or 0)

    candidates.sort(key=sort_key, reverse=True)
    return candidates


def discover_chrome_toplevel(*, preferred_wid: int | None = None) -> dict[str, Any]:
    ewmh = probe_ewmh_window_manager()
    candidates = _candidate_windows()
    if preferred_wid is not None:
        for c in candidates:
            if int(c["wid"]) == int(preferred_wid):
                return {**c, **ewmh, "discoveryMethod": "preferred_wid_validated"}
        return {
            "wid": None,
            "error": REASON_STALE,
            "preferredWid": preferred_wid,
            "candidates": [{"wid": c["wid"], "name": c.get("wmName")} for c in candidates[:5]],
            **ewmh,
            "discoveryMethod": "preferred_wid_rejected_stale",
        }
    if not candidates:
        out = _run(
            "xwininfo -root -tree | awk '/Google Chrome/{print $1; exit}'",
            shell=True,
        )
        wid_hex = (out.stdout or "").strip()
        if not wid_hex:
            return {"wid": None, "error": REASON_NOT_FOUND, **ewmh, "discoveryMethod": "none"}
        info = _run(["xwininfo", "-id", wid_hex])
        geom = _parse_xwininfo(info.stdout or "")
        props = _xprop_id(wid_hex)
        return {
            "wid_hex": wid_hex,
            "wid": int(wid_hex, 16),
            "x": geom["x"],
            "y": geom["y"],
            "w": geom["w"],
            "h": geom["h"],
            "mapped": bool(geom["mapped"]),
            "wmClass": props.get("wmClass"),
            "wmName": props.get("wmName"),
            "pid": props.get("pid"),
            **ewmh,
            "discoveryMethod": "legacy_awk_first_google_chrome",
        }
    chosen = candidates[0]
    return {**chosen, **ewmh, "discoveryMethod": "largest_mapped_google_chrome_toplevel"}


def _window_exists(wid: int) -> bool:
    proc = _run(["xwininfo", "-id", hex(wid)])
    return proc.returncode == 0 and "xwininfo: Window id:" in (proc.stdout or "")


def _get_focused_wid() -> int | None:
    proc = _run(["xdotool", "getwindowfocus"])
    if proc.returncode != 0:
        return None
    try:
        return int((proc.stdout or "").strip())
    except ValueError:
        return None


def _xlib_set_input_focus(wid: int) -> bool:
    try:
        from Xlib import X, display as xdisplay
    except Exception:
        return False
    try:
        d = xdisplay.Display(DISPLAY_NAME)
        win = d.create_resource_object("window", wid)
        win.map()
        d.sync()
        d.set_input_focus(win, X.RevertToParent, X.CurrentTime)
        d.sync()
        d.close()
        return True
    except Exception:
        return False


def focus_chrome_window(
    *,
    preferred_wid: int | None = None,
    allow_activate_if_ewmh: bool = True,
    verify: bool = True,
) -> ChromeFocusResult:
    """Locate current Chrome top-level window and establish keyboard focus."""
    result = ChromeFocusResult()
    discovered = discover_chrome_toplevel(preferred_wid=preferred_wid)
    result.diagnostics["discovery"] = {k: v for k, v in discovered.items() if k != "raw"}
    result.window_manager_present = bool(discovered.get("windowManagerPresent"))
    result.ewmh_active_window_supported = bool(discovered.get("ewmhActiveWindowSupported"))

    if discovered.get("error") == REASON_STALE:
        result.reason_code = REASON_STALE
        return result
    wid = discovered.get("wid")
    if wid is None:
        result.reason_code = REASON_NOT_FOUND
        return result

    wid = int(wid)
    result.window_id = wid
    result.window_id_hex = discovered.get("wid_hex") or hex(wid)
    result.window_pid = discovered.get("pid")
    result.window_class = discovered.get("wmClass")
    result.window_name = discovered.get("wmName")
    result.window_mapped = bool(discovered.get("mapped"))
    result.geometry = {
        "x": int(discovered.get("x") or 0),
        "y": int(discovered.get("y") or 0),
        "w": int(discovered.get("w") or 0),
        "h": int(discovered.get("h") or 0),
    }

    if not _window_exists(wid):
        result.reason_code = REASON_STALE
        return result
    if not result.window_mapped:
        _xlib_set_input_focus(wid)
        rediscovered = discover_chrome_toplevel(preferred_wid=wid)
        result.window_mapped = bool(rediscovered.get("mapped"))
        if not result.window_mapped:
            result.reason_code = REASON_NOT_MAPPED
            return result

    methods_tried: list[dict[str, Any]] = []
    focus_ok = False
    method_used: str | None = None

    if allow_activate_if_ewmh and result.ewmh_active_window_supported:
        proc = _run(["xdotool", "windowactivate", "--sync", str(wid)])
        methods_tried.append(
            {
                "method": "xdotool_windowactivate",
                "exit": proc.returncode,
                "stderr": ((proc.stderr or "") + (proc.stdout or ""))[:400],
            }
        )
        if proc.returncode == 0:
            focus_ok = True
            method_used = "xdotool_windowactivate"

    if not focus_ok:
        proc = _run(["xdotool", "windowfocus", "--sync", str(wid)])
        methods_tried.append(
            {
                "method": "xdotool_windowfocus",
                "exit": proc.returncode,
                "stderr": ((proc.stderr or "") + (proc.stdout or ""))[:400],
            }
        )
        if proc.returncode == 0:
            focus_ok = True
            method_used = "xdotool_windowfocus"

    if not focus_ok:
        ok = _xlib_set_input_focus(wid)
        methods_tried.append({"method": "xlib_set_input_focus", "ok": ok})
        if ok:
            focus_ok = True
            method_used = "xlib_set_input_focus"

    _run(["xdotool", "windowraise", str(wid)])
    time.sleep(0.15)

    result.diagnostics["methodsTried"] = methods_tried
    result.focus_method = method_used

    if not focus_ok:
        if not result.window_manager_present:
            result.diagnostics["note"] = REASON_EWMH_UNAVAILABLE
        result.reason_code = REASON_FOCUS_FAILED
        return result

    focused = _get_focused_wid()
    result.focused_window_id = focused
    if verify and focused != wid:
        name_proc = _run(["xdotool", "getwindowname", str(focused or 0)])
        name = (name_proc.stdout or "").strip()
        result.diagnostics["focusedWindowName"] = name
        if "Chrome" not in name and "chrome" not in name.lower():
            result.focus_verified = False
            result.reason_code = REASON_FOCUS_VERIFY_FAILED
            return result
    result.focus_verified = True
    result.reason_code = REASON_READY
    return result


def is_local_runtime_failure_message(message: str | None) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    markers = (
        "x11_focus",
        "x11_chrome_window",
        "x11_chrome_focus",
        "windowactivate",
        "windowfocus",
        "chrome_window_not_found",
        "x11_ewmh_unavailable",
        "x11_focus_failed",
        "x11_focus_verification_failed",
        "x11_chrome_window_stale",
        "x11_chrome_window_not_mapped",
        "command 'xdotool",
        "desktop sold navigation failed: command 'xdotool",
        "pre_submit_gui",
        "keyboard_injection",
        "linux_chrome_cdp_failed",
        "display :99 not ready",
        "cdp_fail",
        "xvfb",
        "x11_nav_python_missing",
    )
    return any(m in text for m in markers)


__all__ = [
    "ChromeFocusResult",
    "REASON_READY",
    "discover_chrome_toplevel",
    "focus_chrome_window",
    "is_local_runtime_failure_message",
    "probe_ewmh_window_manager",
]
