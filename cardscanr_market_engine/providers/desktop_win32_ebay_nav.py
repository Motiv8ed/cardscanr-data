"""Real Windows desktop mouse/keyboard navigation for eBay AU sold search.

Navigation only — no Playwright clicks, no CDP input, no page.goto search URLs.
Playwright/CDP may attach afterward solely to READ the already-loaded page.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shcore = ctypes.windll.shcore
try:
    shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_CONTROL, VK_A, VK_BACK, VK_RETURN = 0x11, 0x41, 0x08, 0x0D
VK_ESCAPE, VK_NEXT, VK_L = 0x1B, 0x22, 0x4C
WHEEL_DELTA = 120
SW_RESTORE, SW_MAXIMIZE = 9, 3

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
DEFAULT_PROFILE = ROOT / ".browser_profiles" / "cardscanr"
DEFAULT_CDP_PORT = 9333


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


@dataclass
class DesktopNavResult:
    ok: bool
    query: str
    search_success: bool
    sold_click_success: bool
    sold_state_verified: bool
    url: str
    title: str
    sorry: bool
    challenge: bool
    sold_date_lines: int
    error: str | None = None
    diagnostics: dict[str, Any] | None = None


def virt_metrics() -> dict[str, int]:
    return {
        "x": user32.GetSystemMetrics(76),
        "y": user32.GetSystemMetrics(77),
        "w": user32.GetSystemMetrics(78),
        "h": user32.GetSystemMetrics(79),
    }


def to_absolute(x: int, y: int, virt: dict[str, int]) -> tuple[int, int]:
    ax = int((x - virt["x"]) * 65535 / max(1, virt["w"]))
    ay = int((y - virt["y"]) * 65535 / max(1, virt["h"]))
    return ax, ay


def send_mouse_abs(x: int, y: int, flags: int, virt: dict[str, int], mouse_data: int = 0) -> None:
    ax, ay = to_absolute(x, y, virt)
    inp = INPUT()
    inp.type = INPUT_MOUSE
    md = mouse_data & 0xFFFFFFFF
    inp.union.mi = MOUSEINPUT(
        ax, ay, md, flags | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_MOVE, 0, None
    )
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def move_visible(x1: int, y1: int, x2: int, y2: int, virt: dict[str, int], steps: int = 40) -> None:
    for i in range(steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)
        x = int(x1 + (x2 - x1) * e)
        y = int(y1 + (y2 - y1) * e)
        send_mouse_abs(x, y, 0, virt)
        user32.SetCursorPos(x, y)
        time.sleep(0.012)


def click_at(x: int, y: int, virt: dict[str, int], *, pause: float = 1.0) -> None:
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    move_visible(pt.x, pt.y, x, y, virt)
    time.sleep(pause)
    send_mouse_abs(x, y, MOUSEEVENTF_LEFTDOWN, virt)
    time.sleep(0.07)
    send_mouse_abs(x, y, MOUSEEVENTF_LEFTUP, virt)
    time.sleep(0.2)


def wheel_at(x: int, y: int, virt: dict[str, int], notches: int) -> None:
    send_mouse_abs(x, y, 0, virt)
    user32.SetCursorPos(x, y)
    time.sleep(0.04)
    delta = int(-notches * WHEEL_DELTA)
    send_mouse_abs(x, y, MOUSEEVENTF_WHEEL, virt, mouse_data=delta)
    time.sleep(0.1)


def tap(vk: int) -> None:
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.04)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.04)


def type_slow(text: str, delay: float = 0.07) -> None:
    for ch in text:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.union.ki = KEYBDINPUT(0, ord(ch), flags, 0, None)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(delay)


def find_ebay_hwnd() -> tuple[int, str, tuple[int, int, int, int]] | None:
    found: list[tuple[int, str, tuple[int, int, int, int]]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):  # noqa: ANN001
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value == "Chrome_WidgetWin_1" and "ebay" in title.lower():
            r = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            if r.left > -10000:
                found.append((int(hwnd), title, (r.left, r.top, r.right, r.bottom)))
        return True

    user32.EnumWindows(cb, 0)
    if not found:
        return None
    for item in found:
        if "ebay australia" in item[1].lower() or "for sale" in item[1].lower() or "sold" in item[1].lower():
            return item
    return found[0]


def foreground_ebay() -> tuple[int, str, tuple[int, int, int, int]]:
    item = find_ebay_hwnd()
    if not item:
        raise RuntimeError("no_ebay_chrome_window")
    hwnd, title, _ = item
    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.12)
    user32.ShowWindow(hwnd, SW_MAXIMIZE)
    time.sleep(0.25)
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None)
    cur = kernel32.GetCurrentThreadId()
    if fg_tid and fg_tid != cur:
        user32.AttachThreadInput(cur, fg_tid, True)
    try:
        user32.AllowSetForegroundWindow(-1)
    except Exception:
        pass
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    if fg_tid and fg_tid != cur:
        user32.AttachThreadInput(cur, fg_tid, False)
    time.sleep(0.3)
    tap(VK_ESCAPE)
    time.sleep(0.12)
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return hwnd, title, (r.left, r.top, r.right, r.bottom)


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value or ""


def _run_ps(script_path: Path) -> str:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout


def uia_search_controls() -> tuple[dict[str, int] | None, dict[str, int] | None, str]:
    text = _run_ps(TOOLS / "_uia_ebay_search2.ps1")
    field = None
    button = None
    url = ""
    for line in text.splitlines():
        if line.startswith("UNIQUE_SEARCH_BTN|"):
            parts = dict(p.split("=", 1) for p in line.split("|")[1:] if "=" in p)
            button = {k: int(v) for k, v in parts.items()}
        elif line.startswith("HIT|") and "Search for anything" in line and field is None:
            parts = dict(p.split("=", 1) for p in line.split("|")[1:] if "=" in p)
            left, top, w, h = int(parts["left"]), int(parts["top"]), int(parts["w"]), int(parts["h"])
            field = {"left": left, "top": top, "w": w, "h": h, "right": left + w, "cx": left + w // 2, "cy": top + h // 2}
    return field, button, text


def uia_sold_and_url() -> tuple[dict[str, int] | None, str, str, list[str]]:
    text = _run_ps(TOOLS / "_uia_ebay_sold.ps1")
    sold = None
    url = ""
    title = ""
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("WINDOW|"):
            parts = dict(p.split("=", 1) for p in line.split("|")[1:] if "=" in p)
            title = parts.get("name", "")
        elif line.startswith("URL_EDIT|"):
            parts = dict(p.split("=", 1) for p in line.split("|")[1:] if "=" in p)
            url = parts.get("value", "") or url
        elif line.startswith("SOLD_HIT|") and "|name=Sold items|" in f"|{line}|":
            parts = dict(p.split("=", 1) for p in line.split("|")[1:] if "=" in p)
            if str(parts.get("name")) == "Sold items":
                sold = {k: (int(v) if k in {"left", "top", "w", "h", "cx", "cy"} else v) for k, v in parts.items()}
        elif line.startswith("TEXT|"):
            body.append(line[5:])
    return sold, url, title, body


def in_viewport(hit: dict[str, int], rect: tuple[int, int, int, int], margin: int = 40) -> bool:
    return (
        hit["left"] >= rect[0] + 8
        and hit["top"] >= rect[1] + margin
        and hit["left"] + hit["w"] <= rect[2] - 8
        and hit["top"] + hit["h"] <= rect[3] - margin
    )


def ensure_chrome_with_cdp(
    *,
    profile_dir: Path | None = None,
    cdp_port: int = DEFAULT_CDP_PORT,
    start_url: str = "https://www.ebay.com.au/",
) -> dict[str, Any]:
    """Relaunch CardScanR Chrome profile with remote debugging if needed."""
    import urllib.request

    profile = Path(profile_dir or DEFAULT_PROFILE)
    endpoint = f"http://127.0.0.1:{cdp_port}/json/version"
    try:
        with urllib.request.urlopen(endpoint, timeout=2) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
            return {"alreadyRunning": True, "cdpPort": cdp_port, "browser": meta.get("Browser"), "profile": str(profile)}
    except Exception:
        pass

    # Stop only processes using this user-data-dir (not other Chrome profiles).
    marker = str(profile).replace("/", "\\").lower()
    ps = f"""
$marker = '{marker}'.ToLower()
Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | ForEach-Object {{
  if ($_.CommandLine -and $_.CommandLine.ToLower().Contains($marker)) {{
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }}
}}
Start-Sleep -Seconds 1.5
$chrome = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
$args = @(
  '--remote-debugging-port={cdp_port}',
  '--remote-debugging-address=127.0.0.1',
  '--remote-allow-origins=*',
  '--user-data-dir={str(profile)}',
  '--profile-directory=Default',
  '--no-first-run',
  '--no-default-browser-check',
  '--new-window',
  '{start_url}'
)
Start-Process -FilePath $chrome -ArgumentList $args
"""
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], check=False)
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(endpoint, timeout=2) as resp:
                meta = json.loads(resp.read().decode("utf-8"))
                time.sleep(1.5)
                return {
                    "alreadyRunning": False,
                    "cdpPort": cdp_port,
                    "browser": meta.get("Browser"),
                    "profile": str(profile),
                    "startUrl": start_url,
                }
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"chrome_cdp_not_ready port={cdp_port}")


def wait_for_search_controls(*, timeout_s: float = 30.0) -> tuple[dict[str, int] | None, dict[str, int] | None]:
    deadline = time.time() + timeout_s
    last_field = None
    last_button = None
    while time.time() < deadline:
        try:
            foreground_ebay()
        except Exception:
            time.sleep(0.5)
            continue
        last_field, last_button, _ = uia_search_controls()
        if last_field and last_button:
            return last_field, last_button
        time.sleep(0.6)
    return last_field, last_button


def desktop_goto_homepage(virt: dict[str, int]) -> None:
    """Address-bar navigation via real keyboard (not Playwright/CDP)."""
    hwnd, title, rect = foreground_ebay()
    # Already on AU homepage with search UI — skip address-bar churn.
    field, button = wait_for_search_controls(timeout_s=3.0)
    if field and button and "ebay australia" in title.lower() and "for sale" not in title.lower():
        _ = virt
        return
    user32.SetCursorPos(rect[0] + (rect[2] - rect[0]) // 2, rect[1] + 120)
    time.sleep(0.15)
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    tap(VK_L)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.15)
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    tap(VK_A)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.08)
    type_slow("https://www.ebay.com.au/", delay=0.04)
    time.sleep(0.1)
    tap(VK_RETURN)
    time.sleep(2.0)
    wait_for_search_controls(timeout_s=25.0)
    _ = hwnd


def desktop_search_and_sold(query: str, *, pause_before_click: float = 1.0) -> DesktopNavResult:
    """Homepage assumed loaded. Search via desktop input, then Sold items click."""
    from cardscanr_market_engine.providers.ebay_browser_provider import verify_sold_result_state

    virt = virt_metrics()
    diag: dict[str, Any] = {"query": query, "steps": []}
    try:
        hwnd, title0, rect = foreground_ebay()
        diag["steps"].append({"step": "foreground", "title": title0, "rect": list(rect)})

        field, button = wait_for_search_controls(timeout_s=30.0)
        if not field or not button:
            return DesktopNavResult(
                False, query, False, False, False, "", title0, False, False, 0,
                error="search_controls_not_found", diagnostics=diag,
            )

        click_at(field["cx"], field["cy"], virt, pause=pause_before_click)
        time.sleep(0.2)
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        tap(VK_A)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        tap(VK_BACK)
        time.sleep(0.1)
        type_slow(query, delay=0.07)
        time.sleep(0.35)
        diag["steps"].append({"step": "typed_query", "field": field})

        click_at(button["cx"], button["cy"], virt, pause=pause_before_click)
        deadline = time.time() + 45
        search_ok = False
        last_title = ""
        last_url = ""
        while time.time() < deadline:
            last_title = window_title(hwnd)
            if "error page" in last_title.lower():
                return DesktopNavResult(
                    False, query, False, False, False, "", last_title, True, False, 0,
                    error="SORRY_after_search", diagnostics=diag,
                )
            _, last_url, _, _ = uia_sold_and_url()
            url_l = (last_url or "").lower()
            title_l = last_title.lower()
            token = query.split()[0].lower()
            if (
                token in title_l
                or "for sale" in title_l
                or "results for" in title_l
                or f"_nkw={token}" in url_l
                or token in url_l.replace("+", " ").replace("%20", " ")
                or "/sch/" in url_l
            ):
                search_ok = True
                break
            time.sleep(0.4)
        time.sleep(2.0)
        if not search_ok:
            return DesktopNavResult(
                False, query, False, False, False, last_url, last_title, False, False, 0,
                error="search_results_not_confirmed",
                diagnostics={**diag, "lastTitle": last_title, "lastUrl": last_url},
            )
        diag["steps"].append({"step": "search_results", "title": window_title(hwnd)})

        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        rect = (r.left, r.top, r.right, r.bottom)
        sold, url, title, _ = uia_sold_and_url()
        if not sold:
            return DesktopNavResult(
                False, query, True, False, False, url, title, False, False, 0,
                error="sold_control_not_found", diagnostics=diag,
            )

        hover_x = rect[0] + 220
        hover_y = rect[1] + int((rect[3] - rect[1]) * 0.55)
        user32.SetCursorPos(hover_x, hover_y)
        attempts = 0
        while not in_viewport(sold, rect) and attempts < 40:
            attempts += 1
            if attempts % 5 == 0:
                tap(VK_NEXT)
            else:
                wheel_at(hover_x, hover_y, virt, notches=3)
            time.sleep(0.12)
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            rect = (r.left, r.top, r.right, r.bottom)
            sold, url, title, _ = uia_sold_and_url()
            if not sold:
                continue
        if not sold or not in_viewport(sold, rect):
            return DesktopNavResult(
                False, query, True, False, False, url, title, False, False, 0,
                error="sold_not_in_viewport", diagnostics={**diag, "scrollAttempts": attempts},
            )

        click_at(int(sold["cx"]), int(sold["cy"]), virt, pause=pause_before_click)
        diag["steps"].append({"step": "sold_click", "sold": sold, "scrollAttempts": attempts})

        home_url = url
        deadline = time.time() + 50
        while time.time() < deadline:
            sold2, url, title, body = uia_sold_and_url()
            _ = sold2
            if url and not url.startswith("http"):
                url = "https://www." + url.lstrip("/")
            low = (url or "").lower()
            if "lh_sold=1" in low or "error page" in title.lower():
                break
            if url and home_url and url != home_url:
                time.sleep(1.2)
                break
            time.sleep(0.4)
        time.sleep(1.8)
        _, url, title, body = uia_sold_and_url()
        if url and not url.startswith("http"):
            url = "https://www." + url.lstrip("/")
        body_text = "\n".join(body)
        sold_state = verify_sold_result_state(url=url, title=title, body_text=body_text)
        sorry = "error page" in title.lower() or "/error" in (url or "").lower()
        challenge = "captcha" in body_text.lower() or "security measure" in body_text.lower()
        url_sold = "lh_sold=1" in (url or "").lower()
        # Nav gate: eBay-generated sold URL after physical click. Strict SOLD_STATE_VERIFIED
        # is re-checked from the full page body after CDP attach for pricing parse.
        verified_nav = bool(url_sold and not sorry and not challenge)
        return DesktopNavResult(
            ok=verified_nav,
            query=query,
            search_success=True,
            sold_click_success=True,
            sold_state_verified=verified_nav,
            url=url,
            title=title,
            sorry=sorry,
            challenge=challenge,
            sold_date_lines=int(sold_state.get("soldDateLines") or 0),
            error=None if verified_nav else "sold_url_not_confirmed",
            diagnostics={
                **diag,
                "soldStateUiaSparse": sold_state,
                "url": url,
                "uiaStrictVerified": sold_state.get("SOLD_STATE_VERIFIED"),
            },
        )
    except Exception as exc:
        return DesktopNavResult(
            False, query, False, False, False, "", "", False, False, 0,
            error=f"{type(exc).__name__}:{exc}", diagnostics=diag,
        )


def navigate_query_to_sold(query: str, *, reset_homepage: bool = True) -> DesktopNavResult:
    virt = virt_metrics()
    if reset_homepage:
        desktop_goto_homepage(virt)
    return desktop_search_and_sold(query)
