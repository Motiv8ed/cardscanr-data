"""Ownership checks for the dedicated Windows pricing Chrome instance."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from .native_pricing_browser_identity import verify_chrome_identity

PORT = 9232
_INVENTORY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$listeners = @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -eq 9232 |
    ForEach-Object { @{address=$_.LocalAddress; port=[int]$_.LocalPort; pid=[int]$_.OwningProcess} })
$processes = @(foreach ($idValue in @($listeners | ForEach-Object { $_.pid } | Sort-Object -Unique)) {
    Get-CimInstance Win32_Process -Filter "ProcessId=$idValue" | ForEach-Object {
        @{pid=[int]$_.ProcessId; session=[int]$_.SessionId; executable=$_.ExecutablePath; commandLine=$_.CommandLine}
    }
})
@{listeners=$listeners; processes=$processes} | ConvertTo-Json -Depth 5 -Compress
"""


def profile_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "CardScanR" / "NativePricingChrome"


def _session() -> int:
    session = ctypes.c_ulong()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise RuntimeError("Windows session inspection failed")
    if not session.value or session.value != ctypes.windll.kernel32.WTSGetActiveConsoleSessionId():
        raise RuntimeError("Native pricing requires the active Windows console")
    return session.value


def _executable() -> Path:
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if os.getenv(key):
            candidate = Path(os.environ[key]) / "Google/Chrome/Application/chrome.exe"
            if candidate.is_file():
                return candidate
    raise RuntimeError("Installed Windows Chrome unavailable")


def _launch_args() -> list[str]:
    profile = profile_dir()
    if not profile.is_absolute() or profile.resolve() != profile:
        raise RuntimeError("Native pricing profile path must be local and canonical")
    return [
        str(_executable()), "--remote-debugging-port=9232",
        "--remote-debugging-address=127.0.0.1", f"--user-data-dir={profile}",
        "--profile-directory=Default", "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking",
        "--window-size=1050,760", "--new-window", "about:blank",
    ]


def _argv(command_line: str) -> list[str]:
    if not isinstance(command_line, str) or not command_line:
        raise RuntimeError("Chrome process arguments unavailable")
    count = ctypes.c_int()
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = parse(command_line, ctypes.byref(count))
    if not argv:
        raise RuntimeError("Chrome process arguments unparseable")
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def _inventory() -> dict:
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", _INVENTORY],
        check=True, capture_output=True, encoding="utf-8", timeout=12,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    inventory = json.loads(result.stdout)
    if (not isinstance(inventory, dict) or set(inventory) != {"listeners", "processes"}
            or not all(isinstance(rows, list) for rows in inventory.values())
            or not all(isinstance(row, dict) for rows in inventory.values() for row in rows)):
        raise RuntimeError("Malformed Windows listener inventory")
    for process in inventory["processes"]:
        process["argv"] = _argv(process.get("commandLine"))
    return inventory


def listener_owner(*, port: int = PORT) -> int | None:
    if port != PORT or os.name != "nt":
        raise RuntimeError("Native pricing requires Windows loopback port 9232")
    session = _session()
    launch = _launch_args()
    inventory = _inventory()
    listeners = inventory["listeners"]
    if not listeners and not inventory["processes"]:
        return None
    verified = verify_chrome_identity(
        **inventory, port=port, profile=str(profile_dir()),
        chrome_executable=launch[0], session_id=session,
    )
    if (not verified["verified"] or not any(row.get("address") == "127.0.0.1" for row in listeners)
            or len(inventory["processes"]) != 1 or inventory["processes"][0]["argv"] != launch):
        raise RuntimeError("Port 9232 has an unexpected or unverifiable owner")
    return verified["pid"]


@contextmanager
def browser_lock():
    """Serialize native launch/navigation across Windows workers, including restarts."""
    import msvcrt

    profile = profile_dir()
    _launch_args()  # validate the path before creating any state
    profile.mkdir(parents=True, exist_ok=True)
    with (profile / ".cardscanr-browser.lock").open("a+b") as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        yield


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def cdp_json(route: str):
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(f"http://127.0.0.1:{PORT}{route}", timeout=2) as response:
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise RuntimeError("Native CDP response too large")
    return json.loads(data)


def valid_socket(url: str, target_path: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "ws" and parsed.netloc == f"127.0.0.1:{PORT}"
        and parsed.path.startswith(target_path) and len(parsed.path) > len(target_path)
        and not parsed.query and not parsed.fragment
    )


def ensure_browser() -> dict:
    with browser_lock():
        owner = listener_owner()
        proc = None
        if owner is None:
            startup = subprocess.STARTUPINFO()
            startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 4  # SW_SHOWNOACTIVATE
            proc = subprocess.Popen(_launch_args(), startupinfo=startup)
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                pid = listener_owner()
                if proc is not None and (proc.poll() is not None or pid not in (None, proc.pid)):
                    raise RuntimeError("Native Chrome startup ownership changed")
                if pid is not None:
                    try:
                        meta = cdp_json("/json/version")
                    except (TimeoutError, URLError):
                        if proc is None:
                            raise
                        time.sleep(0.5)
                        continue
                    if not (isinstance(meta, dict) and str(meta.get("Browser", "")).startswith("Chrome/")
                            and valid_socket(str(meta.get("webSocketDebuggerUrl") or ""), "/devtools/browser/")):
                        raise RuntimeError("Unexpected native Chrome CDP metadata")
                    return {"alreadyRunning": owner is not None, "cdpPort": PORT, "pid": pid}
                time.sleep(0.5)
            raise RuntimeError("Native Chrome listener did not start")
        except BaseException:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            raise
