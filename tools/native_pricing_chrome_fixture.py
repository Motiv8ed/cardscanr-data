#!/usr/bin/env python3
"""Owner-run visible Chrome fixture. Never navigate to eBay or change pricing state.

Run from the repository root: python tools/native_pricing_chrome_fixture.py
Uses a disposable, uniquely named local QA profile and a local HTML file.
No live pricing profile is created, migrated, or reused.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cardscanr_market_engine.native_pricing_browser_identity import verify_chrome_identity

PORT = 9232
PS = r"""
$ErrorActionPreference = 'Stop'
$ls = @(Get-NetTCPConnection -State Listen -LocalPort 9232 -ErrorAction SilentlyContinue |
    ForEach-Object { @{address=$_.LocalAddress; port=[int]$_.LocalPort; pid=[int]$_.OwningProcess} })
$procs = @(foreach ($idValue in @($ls.pid | Sort-Object -Unique)) {
    Get-CimInstance Win32_Process -Filter "ProcessId=$idValue" |
        ForEach-Object { @{pid=[int]$_.ProcessId; session=[int]$_.SessionId;
            executable=$_.ExecutablePath; commandLine=$_.CommandLine;
            window=[long](Get-Process -Id $_.ProcessId).MainWindowHandle} }
})
@{listeners=$ls; processes=$procs} | ConvertTo-Json -Depth 5 -Compress
"""


def _inventory():
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", PS],
        capture_output=True, text=True, timeout=20, check=True)
    doc = json.loads(result.stdout)
    for row in doc["processes"]:
        row["argv"] = _argv(row.pop("commandLine"))
    return doc


def _argv(command_line):
    if not isinstance(command_line, str) or not command_line:
        raise RuntimeError("unknown process arguments")
    count = ctypes.c_int()
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = parse(command_line, ctypes.byref(count))
    if not argv:
        raise RuntimeError("cannot parse process arguments")
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def _chrome():
    roots = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    candidates = [Path(p) / "Google" / "Chrome" / "Application" / "chrome.exe" for p in roots if p]
    return next((p for p in candidates if p.is_file()), None)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _cdp(path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open("http://127.0.0.1:9232" + path, timeout=2) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise RuntimeError("CDP response too large")
    return json.loads(raw)


def main():
    if os.name != "nt":
        raise RuntimeError("Windows only")
    session = ctypes.c_ulong()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)) or session.value == 0:
        raise RuntimeError("interactive session required")
    if _inventory()["listeners"]:
        raise RuntimeError("port 9232 already occupied; nothing launched")
    chrome = _chrome()
    if chrome is None:
        raise RuntimeError("installed Chrome not found")
    # Never touch an existing pricing, Studio, social or personal profile.
    directory = Path(tempfile.mkdtemp(prefix="CardScanR-NativeChrome-QA-"))
    profile = directory / "profile"
    fixture = directory / "pricing-fixture.html"
    fixture.write_text("""<!doctype html><meta charset=utf-8>
<title>CardScanR local pricing browser test</title>
<style>body{background:#111827;color:#f5d78c;font:24px Arial;padding:60px}input,button{font:inherit}</style>
<h1>CardScanR Windows Chrome test</h1>
<p>Local test fixture only. No eBay access or pricing writes.</p>
<form onsubmit="event.preventDefault();document.getElementById('result').textContent='Local fixture received: '+document.getElementById('query').value">
<input id="query" placeholder="Test search" autocomplete="off"><button>Test</button></form>
<p id="result"></p>""", encoding="utf-8")
    proc = subprocess.Popen([
        str(chrome), "--remote-debugging-port=9232",
        "--remote-debugging-address=127.0.0.1", "--user-data-dir=" + str(profile),
        "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
        "--new-window", fixture.as_uri(),
    ])
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        time.sleep(1)
        inv = _inventory()
        if not inv["listeners"]:
            continue
        verified = verify_chrome_identity(
            listeners=inv["listeners"], processes=inv["processes"],
            profile=str(profile), chrome_executable=str(chrome),
            session_id=int(session.value), port=PORT)
        if not verified["verified"] or verified["pid"] != proc.pid:
            raise RuntimeError("unexpected listener owner; refusing attach")
        row = next(p for p in inv["processes"] if p["pid"] == proc.pid)
        if not row["window"] or not ctypes.windll.user32.IsWindowVisible(ctypes.c_void_p(row["window"])):
            continue
        try:
            version = _cdp("/json/version")
            pages = _cdp("/json/list")
        except (OSError, ValueError):
            continue
        ws = urlsplit(str(version.get("webSocketDebuggerUrl") or ""))
        if not (str(version.get("Browser", "")).startswith("Chrome/")
                and ws.scheme == "ws" and ws.hostname == "127.0.0.1"
                and ws.port == PORT and ws.path.startswith("/devtools/browser/")):
            raise RuntimeError("unexpected CDP identity")
        visible_pages = [p for p in pages if p.get("type") == "page"]
        if len(visible_pages) != 1 or visible_pages[0].get("url") != fixture.as_uri():
            raise RuntimeError("unexpected page; refusing further action")
        return {"status": "PASS_LOCAL_VISIBLE_CHROME_FIXTURE", "port": PORT,
                "exact_listener_owner": True, "visible_window": True,
                "local_fixture_verified": True, "marketplace_navigation_requested": False,
                "pricing_scheduler_modified": False, "challenge_cleared": False,
                "live_pricing_ready": False, "note": "Close the QA Chrome window manually."}
    raise RuntimeError("visible fixture not verified; no processes terminated")


if __name__ == "__main__":
    try:
        print(json.dumps(main(), indent=2))
    except Exception as exc:
        print(json.dumps({"status": "STOPPED_SAFELY", "reason": str(exc),
                          "live_pricing_ready": False}, indent=2))
        raise SystemExit(2)
