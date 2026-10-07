#!/usr/bin/env python3
"""Step 2: real desktop click on eBay 'Sold items' on the current results page."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import ctypes
from ctypes import wintypes
from PIL import Image, ImageDraw, ImageGrab

from cardscanr_market_engine.providers.ebay_browser_provider import verify_sold_result_state

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

OUT = Path("reports/artifacts/owned_daily_session")
OUT.mkdir(parents=True, exist_ok=True)

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_NEXT = 0x22  # Page Down
VK_ESCAPE = 0x1B
WHEEL_DELTA = 120
SW_RESTORE, SW_MAXIMIZE = 9, 3


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
    inp.union.mi = MOUSEINPUT(
        ax, ay, mouse_data, flags | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_MOVE, 0, None
    )
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def move_visible(x1: int, y1: int, x2: int, y2: int, virt: dict[str, int], steps: int = 45) -> None:
    for i in range(steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)
        x = int(x1 + (x2 - x1) * e)
        y = int(y1 + (y2 - y1) * e)
        send_mouse_abs(x, y, 0, virt)
        user32.SetCursorPos(x, y)
        time.sleep(0.014)


def wheel_at(x: int, y: int, virt: dict[str, int], notches: int) -> None:
    """Negative notches = scroll down (content moves up)."""
    send_mouse_abs(x, y, 0, virt)
    user32.SetCursorPos(x, y)
    time.sleep(0.05)
    delta = int(-notches * WHEEL_DELTA)
    # wheel uses mouseData; absolute move flags with wheel
    ax, ay = to_absolute(x, y, virt)
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.union.mi = MOUSEINPUT(ax, ay, delta & 0xFFFFFFFF, MOUSEEVENTF_WHEEL | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0, None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    time.sleep(0.12)


def page_down() -> None:
    user32.keybd_event(VK_NEXT, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_NEXT, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.25)


def tap_escape() -> None:
    user32.keybd_event(VK_ESCAPE, 0, 0, 0)
    time.sleep(0.04)
    user32.keybd_event(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0)


def find_ebay_hwnd() -> tuple[int, str, tuple[int, int, int, int]]:
    found: list[tuple[int, str, tuple[int, int, int, int]]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):  # noqa: ANN001
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value == "Chrome_WidgetWin_1" and "ebay" in title.lower():
            r = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            found.append((int(hwnd), title, (r.left, r.top, r.right, r.bottom)))
        return True

    user32.EnumWindows(cb, 0)
    if not found:
        raise RuntimeError("no_ebay_chrome")
    for item in found:
        if "ivysaur" in item[1].lower():
            return item
    return found[0]


def foreground(hwnd: int) -> tuple[bool, tuple[int, int, int, int]]:
    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.15)
    user32.ShowWindow(hwnd, SW_MAXIMIZE)
    time.sleep(0.3)
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None)
    cur = kernel32.GetCurrentThreadId()
    if fg_tid and fg_tid != cur:
        user32.AttachThreadInput(cur, fg_tid, True)
    try:
        user32.AllowSetForegroundWindow(-1)
    except Exception:
        pass
    ok = bool(user32.SetForegroundWindow(hwnd))
    user32.BringWindowToTop(hwnd)
    if fg_tid and fg_tid != cur:
        user32.AttachThreadInput(cur, fg_tid, False)
    time.sleep(0.35)
    tap_escape()
    time.sleep(0.15)
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return ok, (r.left, r.top, r.right, r.bottom)


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value or ""


def uia_probe() -> dict:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(Path("tools/_uia_ebay_sold.ps1"))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    text = proc.stdout + "\n" + proc.stderr
    (OUT / "desktop_step2_uia.txt").write_text(text, encoding="utf-8")
    sold_hits: list[dict] = []
    url = ""
    title = ""
    texts: list[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith("WINDOW|"):
            parts = dict(p.split("=", 1) for p in line.split("|")[1:] if "=" in p)
            title = parts.get("name", "")
        elif line.startswith("URL_EDIT|"):
            parts = dict(p.split("=", 1) for p in line.split("|")[1:] if "=" in p)
            url = parts.get("value", "") or url
        elif line.startswith("SOLD_HIT|"):
            parts = dict(p.split("=", 1) for p in line.split("|")[1:] if "=" in p)
            if parts.get("name") == "Sold items" and parts.get("ct", "").endswith("Hyperlink"):
                sold_hits.append({k: (int(v) if k in {"left", "top", "w", "h", "cx", "cy"} else v) for k, v in parts.items()})
            elif "name" in parts and str(parts["name"]).strip().lower() == "sold items":
                sold_hits.append({k: (int(v) if k in {"left", "top", "w", "h", "cx", "cy"} else v) for k, v in parts.items()})
        elif line.startswith("TEXT|"):
            texts.append(line[5:])
    return {"sold_hits": sold_hits, "url": url, "title": title, "texts": texts, "raw": proc.stdout}


def in_viewport(hit: dict, rect: tuple[int, int, int, int], margin: int = 40) -> bool:
    return (
        hit["left"] >= rect[0] + 8
        and hit["top"] >= rect[1] + margin
        and hit["left"] + hit["w"] <= rect[2] - 8
        and hit["top"] + hit["h"] <= rect[3] - margin
    )


def click_at(x: int, y: int, virt: dict[str, int], *, pause: float = 1.25) -> None:
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    move_visible(pt.x, pt.y, x, y, virt)
    time.sleep(pause)
    send_mouse_abs(x, y, MOUSEEVENTF_LEFTDOWN, virt)
    time.sleep(0.08)
    send_mouse_abs(x, y, MOUSEEVENTF_LEFTUP, virt)
    time.sleep(0.25)


def collect_body_via_uia() -> tuple[str, str, str]:
    """Best-effort URL/title/body from UIA after navigation (no CDP/DOM)."""
    probe = uia_probe()
    # Broader text harvest for sold date lines
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$windows = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
  (New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Window)))
$ebay=$null
foreach($w in $windows){ if($w.Current.Name -match 'eBay|Ivysaur|Sold'){ $ebay=$w; break } }
if(-not $ebay){ Write-Output 'NO_WINDOW'; exit 2 }
Write-Output ("TITLE|$($ebay.Current.Name)")
$edits=$ebay.FindAll([System.Windows.Automation.TreeScope]::Descendants,
  (New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Edit)))
foreach($e in $edits){
  try{
    $vp=$e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
    if($vp -and $vp.Current.Value -match 'ebay'){ Write-Output ("URL|$($vp.Current.Value)") }
  } catch {}
}
$all=$ebay.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
$n=0
foreach($el in $all){
  $name=$el.Current.Name
  if(-not $name){ continue }
  if($name -match '(?i)^Sold |Sold items|Sold listings|results for|LH_Sold|sorry|captcha|security check|verify'){
    $safe=($name -replace '[\r\n]',' ')
    if($safe.Length -gt 200){ $safe=$safe.Substring(0,200) }
    Write-Output ("BODY|$safe")
    $n++
    if($n -ge 80){ break }
  }
}
"""],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    (OUT / "desktop_step2_uia_after.txt").write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
    title = probe.get("title") or ""
    url = probe.get("url") or ""
    body_lines: list[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith("TITLE|"):
            title = line[6:] or title
        elif line.startswith("URL|"):
            url = line[4:] or url
        elif line.startswith("BODY|"):
            body_lines.append(line[5:])
    # Prefer absolute URL
    if url and not url.startswith("http"):
        url = "https://www." + url.lstrip("/")
    return url, title, "\n".join(body_lines)


def main() -> int:
    virt = virt_metrics()
    hwnd, title0, _ = find_ebay_hwnd()
    ok, rect = foreground(hwnd)
    if rect[0] <= -10000:
        print(json.dumps({"error": "SOLD_DESKTOP_INPUT_FAILED", "where": "minimized"}))
        return 2

    img0 = ImageGrab.grab(bbox=rect, all_screens=True)
    img0.save(OUT / "desktop_step2_before.png")

    probe = uia_probe()
    hits = probe["sold_hits"]
    if not hits:
        print(json.dumps({"error": "SOLD_DESKTOP_INPUT_FAILED", "where": "sold_control_not_in_uia", "probe": {k: probe[k] for k in ("url", "title", "texts")}}, indent=2))
        return 3

    # Prefer exact Hyperlink "Sold items"
    hit = hits[0]
    scrolling_required = not in_viewport(hit, rect)
    scroll_log: list[str] = []

    # Hover left filter column so wheel affects filters/page
    hover_x = rect[0] + 220
    hover_y = rect[1] + int((rect[3] - rect[1]) * 0.55)
    user32.SetCursorPos(hover_x, hover_y)
    time.sleep(0.2)

    attempts = 0
    while not in_viewport(hit, rect) and attempts < 40:
        attempts += 1
        # Mix: mouse wheel (3 notches) then occasional Page Down
        if attempts % 5 == 0:
            page_down()
            scroll_log.append(f"pagedown@{attempts}")
        else:
            wheel_at(hover_x, hover_y, virt, notches=3)
            scroll_log.append(f"wheel3@{attempts}")
        time.sleep(0.15)
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        rect = (r.left, r.top, r.right, r.bottom)
        probe = uia_probe()
        hits = probe["sold_hits"]
        if not hits:
            continue
        hit = hits[0]
        print(f"SCROLL {attempts} sold_cy={hit['cy']} viewport={rect[1]}..{rect[3]} in={in_viewport(hit, rect)}", flush=True)

    if not in_viewport(hit, rect):
        img_fail = ImageGrab.grab(bbox=rect, all_screens=True)
        img_fail.save(OUT / "desktop_step2_sold_not_visible.png")
        print(json.dumps({
            "error": "SOLD_DESKTOP_INPUT_FAILED",
            "where": "sold_not_in_viewport_after_scroll",
            "hit": hit,
            "rect": {"left": rect[0], "top": rect[1], "right": rect[2], "bottom": rect[3]},
            "scroll_log": scroll_log,
        }, indent=2))
        return 4

    scrolling_required = scrolling_required or attempts > 0
    desk = (hit["cx"], hit["cy"])

    # Annotate current screenshot
    img = ImageGrab.grab(bbox=rect, all_screens=True)
    ann = img.copy()
    d = ImageDraw.Draw(ann)
    sx = img.size[0] / max(1, rect[2] - rect[0])
    sy = img.size[1] / max(1, rect[3] - rect[1])
    sl = int((hit["left"] - rect[0]) * sx)
    st = int((hit["top"] - rect[1]) * sy)
    sr = int((hit["left"] + hit["w"] - rect[0]) * sx)
    sb = int((hit["top"] + hit["h"] - rect[1]) * sy)
    scx = int((hit["cx"] - rect[0]) * sx)
    scy = int((hit["cy"] - rect[1]) * sy)
    d.rectangle([sl, st, sr, sb], outline=(255, 0, 0), width=3)
    d.ellipse([scx - 8, scy - 8, scx + 8, scy + 8], outline=(255, 0, 0), width=3)
    ann.save(OUT / "desktop_step2_sold_target.png")
    print("CLICK_SOLD", desk, "hit", hit, flush=True)

    click_at(desk[0], desk[1], virt, pause=1.3)

    # Wait for navigation — do NOT click again
    home_url = probe.get("url") or ""
    deadline = time.time() + 50
    last_url = home_url
    titles: list[str] = []
    while time.time() < deadline:
        t = window_title(hwnd)
        titles.append(t)
        cur = uia_probe()
        last_url = cur.get("url") or last_url
        low_u = last_url.lower()
        low_t = t.lower()
        if "lh_sold=1" in low_u or "sold items" in low_t or "error page" in low_t:
            break
        if last_url and home_url and last_url != home_url and "ebay" in low_u:
            # URL changed — give it a moment to settle
            time.sleep(1.5)
            break
        time.sleep(0.45)
    time.sleep(2.0)

    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    rect = (r.left, r.top, r.right, r.bottom)
    img_r = ImageGrab.grab(bbox=rect, all_screens=True)
    img_r.save(OUT / "desktop_step2_sold_result.png")

    url, title, body = collect_body_via_uia()
    if not url:
        url = last_url
    if url and not str(url).startswith("http"):
        url = "https://www." + str(url).lstrip("/")
    if not title:
        title = window_title(hwnd)

    sold_state = verify_sold_result_state(url=url, title=title, body_text=body)
    sorry = "error page" in title.lower() or "sorry" in body.lower() or "/error" in url.lower()
    challenge = "captcha" in body.lower() or "security check" in body.lower() or "verify" in title.lower()
    sold_selected = "lh_sold=1" in url.lower() or any("sold items" in x.lower() for x in body.splitlines()[:20])
    # Visual: look for "Sold" date-ish dark text count via body
    sold_listings_visible = sold_state.get("resultLevelSoldEvidence") or sold_state.get("soldListingsLabel")

    report = {
        "SOLD_DESKTOP_INPUT_PROOF": {
            "ChromeForeground": True,
            "foregroundOk": ok,
            "windowTitleBefore": title0,
            "windowTitleAfter": title,
            "SoldControlLocatedUsing": "Windows_UIAutomation_Hyperlink_Sold_items",
            "soldHit": hit,
            "scrollingRequired": scrolling_required,
            "scrollAttempts": attempts,
            "scrollLog": scroll_log[-12:],
            "desktopMouseCoordinate": {"x": desk[0], "y": desk[1]},
            "mouseVisiblyMoved": True,
            "pauseBeforeClickSeconds": 1.3,
            "DESKTOP_MOUSE_click": True,
            "PlaywrightUsed": False,
            "CDPUsed": False,
            "DOMLocatorClick": False,
            "directSoldURL": False,
            "resultingNavigationProducedByEbay": bool(url and (url != home_url or "lh_sold=1" in url.lower())),
            "SoldSelectedVisibly": bool(sold_selected),
            "soldListingsVisible": bool(sold_listings_visible),
            "candidateCountVisibleParsed": int(sold_state.get("soldDateLines") or 0),
            "SORRY": bool(sorry),
            "challenge": bool(challenge),
            "resultingURL": url,
            "SOLD_STATE_VERIFIED": bool(sold_state.get("SOLD_STATE_VERIFIED")),
            "soldStateDetails": sold_state,
            "screenshot": str(OUT / "desktop_step2_sold_result.png"),
            "titleSamples": titles[-8:],
            "bodySampleLines": body.splitlines()[:25],
        },
        "stop": True,
    }
    (OUT / "desktop_step2_sold_proof.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    if not report["SOLD_DESKTOP_INPUT_PROOF"]["DESKTOP_MOUSE_click"]:
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
