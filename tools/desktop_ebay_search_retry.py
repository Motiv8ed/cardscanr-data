#!/usr/bin/env python3
"""Step-1 retry: live DPI/window calibration + UIA Search button + screenshot field + SendInput."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import ctypes
from ctypes import wintypes
from PIL import Image, ImageDraw, ImageGrab

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
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
VK_CONTROL, VK_A, VK_BACK, VK_ESCAPE = 0x11, 0x41, 0x08, 0x1B
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
        "primary_w": user32.GetSystemMetrics(0),
        "primary_h": user32.GetSystemMetrics(1),
    }


def to_absolute(x: int, y: int, virt: dict[str, int]) -> tuple[int, int]:
    # Normalize to 0..65535 over virtual desktop
    ax = int((x - virt["x"]) * 65535 / max(1, virt["w"]))
    ay = int((y - virt["y"]) * 65535 / max(1, virt["h"]))
    return ax, ay


def send_mouse_abs(x: int, y: int, flags: int, virt: dict[str, int]) -> None:
    ax, ay = to_absolute(x, y, virt)
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.union.mi = MOUSEINPUT(ax, ay, 0, flags | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_MOVE, 0, None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def move_visible(x1: int, y1: int, x2: int, y2: int, virt: dict[str, int], steps: int = 50) -> None:
    for i in range(steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)
        x = int(x1 + (x2 - x1) * e)
        y = int(y1 + (y2 - y1) * e)
        send_mouse_abs(x, y, 0, virt)
        user32.SetCursorPos(x, y)
        time.sleep(0.014)


def click_at(x: int, y: int, virt: dict[str, int], *, from_pos: tuple[int, int] | None = None, pause: float = 1.2) -> None:
    if from_pos is None:
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        from_pos = (pt.x, pt.y)
    move_visible(from_pos[0], from_pos[1], x, y, virt)
    time.sleep(pause)  # Andrew can see landing
    send_mouse_abs(x, y, MOUSEEVENTF_LEFTDOWN, virt)
    time.sleep(0.08)
    send_mouse_abs(x, y, MOUSEEVENTF_LEFTUP, virt)
    time.sleep(0.2)


def type_slow(text: str, delay: float = 0.08) -> None:
    for ch in text:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.union.ki = KEYBDINPUT(0, ord(ch), flags, 0, None)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(delay)


def tap(vk: int) -> None:
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)


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
        if "ebay australia" in item[1].lower():
            return item
    return found[0]


def foreground(hwnd: int) -> tuple[bool, tuple[int, int, int, int]]:
    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.2)
    user32.ShowWindow(hwnd, SW_MAXIMIZE)
    time.sleep(0.35)
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
    time.sleep(0.45)
    tap(VK_ESCAPE)
    time.sleep(0.2)
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return ok, (r.left, r.top, r.right, r.bottom)


def get_dpi(hwnd: int) -> int:
    try:
        dpi = ctypes.c_uint()
        mon = user32.MonitorFromWindow(hwnd, 2)
        shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dpi), ctypes.byref(ctypes.c_uint()))
        return int(dpi.value)
    except Exception:
        try:
            return int(user32.GetDpiForWindow(hwnd))
        except Exception:
            return 96


def uia_locate() -> tuple[dict[str, int] | None, dict[str, int] | None]:
    """Return (search_field, search_button) from live UIA, either may be None."""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(Path("tools/_uia_ebay_search2.ps1"))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    (OUT / "desktop_retry3_uia.txt").write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
    field: dict[str, int] | None = None
    button: dict[str, int] | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("UNIQUE_SEARCH_BTN|"):
            parts = dict(p.split("=", 1) for p in line.split("|")[1:] if "=" in p)
            button = {k: int(v) for k, v in parts.items()}
            continue
        if line.startswith("HIT|") and "Search for anything" in line:
            parts = dict(p.split("=", 1) for p in line.split("|")[1:] if "=" in p)
            if "left" in parts and field is None:
                left = int(parts["left"])
                top = int(parts["top"])
                w = int(parts["w"])
                h = int(parts["h"])
                field = {
                    "left": left,
                    "top": top,
                    "w": w,
                    "h": h,
                    "right": left + w,
                    "cx": left + w // 2,
                    "cy": top + h // 2,
                }
    return field, button


def locate_field_from_button(img: Image.Image, btn_shot: dict[str, int]) -> dict[str, int] | None:
    """Longest white run on button row, left of Search button (current screenshot)."""
    px = img.load()
    w, h = img.size
    cy = max(0, min(h - 1, btn_shot["cy"]))
    limit = max(40, btn_shot["left"] - 5)
    runs: list[tuple[int, int, int]] = []
    run = 0
    start = None
    for x in range(40, limit):
        r, g, b = px[x, cy][:3]
        light = r > 230 and g > 230 and b > 230
        if light:
            if run == 0:
                start = x
            run += 1
        else:
            if run >= 180 and start is not None:
                runs.append((run, start, start + run - 1))
            run = 0
            start = None
    if run >= 180 and start is not None:
        runs.append((run, start, start + run - 1))
    if not runs:
        return None
    best = None
    for length, left, right in runs:
        gap = btn_shot["left"] - right
        if gap < 0:
            continue
        score = length - abs(gap - 100) * 0.4
        if best is None or score > best[0]:
            best = (score, length, left, right, gap)
    if best is None:
        length, left, right = runs[0]
        gap = btn_shot["left"] - right
    else:
        _, length, left, right, gap = best
    cx = left + int((right - left) * 0.45)
    return {"left": left, "right": right, "cx": cx, "cy": cy, "width": right - left, "gap": gap}


def query_dark_count(img: Image.Image, field: dict[str, int]) -> int:
    px = img.load()
    dark = 0
    for y in range(field["cy"] - 12, field["cy"] + 12):
        for x in range(field["left"] + 30, min(field["right"] - 40, field["left"] + 750), 2):
            if not (0 <= x < img.size[0] and 0 <= y < img.size[1]):
                continue
            r, g, b = px[x, y][:3]
            if r < 140 and g < 140 and b < 140:
                dark += 1
    return dark


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value or ""


def main() -> int:
    virt = virt_metrics()
    hwnd, title0, _ = find_ebay_hwnd()
    ok, rect = foreground(hwnd)
    if rect[0] <= -10000:
        print(json.dumps({"error": "REAL_DESKTOP_INPUT_FAILED", "where": "minimized"}))
        return 2
    dpi = get_dpi(hwnd)
    time.sleep(0.3)
    img = ImageGrab.grab(bbox=rect, all_screens=True)
    img.save(OUT / "desktop_retry3_live.png")
    sw, sh = img.size
    rw, rh = rect[2] - rect[0], rect[3] - rect[1]
    sx, sy = sw / float(rw), sh / float(rh)

    def shot_to_desk(ix: int, iy: int) -> tuple[int, int]:
        return int(round(rect[0] + ix / sx)), int(round(rect[1] + iy / sy))

    calib = {
        "desktopResolutionPrimary": {"w": virt["primary_w"], "h": virt["primary_h"]},
        "virtualScreen": {"x": virt["x"], "y": virt["y"], "w": virt["w"], "h": virt["h"]},
        "DPI": dpi,
        "DPIScaling": dpi / 96.0,
        "ChromeWindowRect": {"left": rect[0], "top": rect[1], "right": rect[2], "bottom": rect[3], "w": rw, "h": rh},
        "screenshotSize": {"w": sw, "h": sh},
        "screenshotCoordinateSpace": "pixels relative to captured Chrome window top-left",
        "desktopCoordinateSpace": "virtual-screen absolute pixels",
        "coordinateConversion": f"desktop=(window.left + shot_x/{sx:.4f}, window.top + shot_y/{sy:.4f})",
        "previousMissCause": "Fallback Y too high; focus verify failed on white-run click. Retry uses UIA Search button rect + live white-run field left of it + SendInput absolute mouse.",
        "foregroundOk": ok,
    }

    uia_field, uia_btn = uia_locate()
    if not uia_btn and not uia_field:
        print(json.dumps({"error": "REAL_DESKTOP_INPUT_FAILED", "where": "uia_locate", "calib": calib}, indent=2))
        return 3

    field_method = "UIA_Search_for_anything"
    if uia_field:
        field_desk = (uia_field["cx"], uia_field["cy"])
        field = {
            "left": int(round((uia_field["left"] - rect[0]) * sx)),
            "right": int(round((uia_field["right"] - rect[0]) * sx)),
            "cx": int(round((uia_field["cx"] - rect[0]) * sx)),
            "cy": int(round((uia_field["cy"] - rect[1]) * sy)),
            "width": uia_field["w"],
        }
    else:
        assert uia_btn is not None
        btn_shot = {
            "left": int(round((uia_btn["left"] - rect[0]) * sx)),
            "top": int(round((uia_btn["top"] - rect[1]) * sy)),
            "cx": int(round((uia_btn["cx"] - rect[0]) * sx)),
            "cy": int(round((uia_btn["cy"] - rect[1]) * sy)),
            "w": uia_btn.get("w", 0),
            "h": uia_btn.get("h", 0),
        }
        field = locate_field_from_button(img, btn_shot)
        if not field:
            print(json.dumps({"error": "REAL_DESKTOP_INPUT_FAILED", "where": "field_from_button", "calib": calib, "uia_btn": uia_btn}, indent=2))
            return 3
        field_desk = shot_to_desk(field["cx"], field["cy"])
        field_method = "UIA_Search_button_row + longest_white_run_left"

    if not uia_btn:
        print(json.dumps({"error": "REAL_DESKTOP_INPUT_FAILED", "where": "uia_search_button", "calib": calib, "uia_field": uia_field}, indent=2))
        return 3
    btn_desk = (uia_btn["cx"], uia_btn["cy"])
    btn_shot = {
        "left": int(round((uia_btn["left"] - rect[0]) * sx)),
        "top": int(round((uia_btn["top"] - rect[1]) * sy)),
        "cx": int(round((uia_btn["cx"] - rect[0]) * sx)),
        "cy": int(round((uia_btn["cy"] - rect[1]) * sy)),
        "w": uia_btn.get("w", 0),
        "h": uia_btn.get("h", 0),
    }

    ann = img.copy()
    d = ImageDraw.Draw(ann)
    d.rectangle([field["left"], field["cy"] - 20, field["right"], field["cy"] + 20], outline=(0, 255, 0), width=3)
    d.ellipse([field["cx"] - 8, field["cy"] - 8, field["cx"] + 8, field["cy"] + 8], outline=(0, 255, 0), width=3)
    d.rectangle(
        [btn_shot["left"], btn_shot["top"], btn_shot["left"] + max(10, btn_shot["w"]), btn_shot["top"] + max(10, btn_shot["h"])],
        outline=(0, 120, 255),
        width=3,
    )
    d.ellipse([btn_shot["cx"] - 8, btn_shot["cy"] - 8, btn_shot["cx"] + 8, btn_shot["cy"] + 8], outline=(0, 120, 255), width=3)
    ann.save(OUT / "desktop_retry3_targets.png")

    print("CALIB", json.dumps(calib), flush=True)
    print("FIELD_METHOD", field_method, "FIELD_DESK", field_desk, "BTN_DESK", btn_desk, "FIELD_SHOT", field, flush=True)
    print("UIA_FIELD", uia_field, "UIA_BTN", uia_btn, flush=True)

    start = (rect[0] + rw // 2, rect[1] + rh // 2)
    user32.SetCursorPos(*start)
    time.sleep(0.25)
    print("MOVE_CLICK_FIELD", field_desk, flush=True)
    click_at(field_desk[0], field_desk[1], virt, from_pos=start, pause=1.3)

    def map_field_shot(cur_rect: tuple[int, int, int, int], cur_img: Image.Image) -> dict[str, int]:
        csx = cur_img.size[0] / max(1, cur_rect[2] - cur_rect[0])
        csy = cur_img.size[1] / max(1, cur_rect[3] - cur_rect[1])
        if uia_field:
            return {
                "left": int(round((uia_field["left"] - cur_rect[0]) * csx)),
                "right": int(round((uia_field["right"] - cur_rect[0]) * csx)),
                "cx": int(round((uia_field["cx"] - cur_rect[0]) * csx)),
                "cy": int(round((uia_field["cy"] - cur_rect[1]) * csy)),
            }
        bshot = {
            "left": int(round((uia_btn["left"] - cur_rect[0]) * csx)),
            "cx": int(round((uia_btn["cx"] - cur_rect[0]) * csx)),
            "cy": int(round((uia_btn["cy"] - cur_rect[1]) * csy)),
        }
        return locate_field_from_button(cur_img, bshot) or field

    # Visual focus check before full query: probe char then require dark pixels in field
    time.sleep(0.4)
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    rect = (r.left, r.top, r.right, r.bottom)
    img_f = ImageGrab.grab(bbox=rect, all_screens=True)
    img_f.save(OUT / "desktop_retry3_after_field_click.png")
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    near = abs(pt.x - field_desk[0]) < 40 and abs(pt.y - field_desk[1]) < 40
    print("PROBE_TYPE I", flush=True)
    type_slow("I", delay=0.1)
    time.sleep(0.35)
    img_p = ImageGrab.grab(bbox=rect, all_screens=True)
    img_p.save(OUT / "desktop_retry3_probe.png")
    field_p = map_field_shot(rect, img_p)
    dark_probe = query_dark_count(img_p, field_p)
    focused = dark_probe >= 8
    print("FOCUS_PROBE", focused, dark_probe, "cursorNear", near, flush=True)
    if not focused:
        # Re-resolve live UIA and click field center again
        uia_field2, uia_btn2 = uia_locate()
        if uia_field2:
            uia_field = uia_field2
            field_desk = (uia_field["cx"], uia_field["cy"])
        if uia_btn2:
            uia_btn = uia_btn2
            btn_desk = (uia_btn["cx"], uia_btn["cy"])
        click_at(field_desk[0], field_desk[1], virt, pause=1.2)
        time.sleep(0.25)
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        tap(VK_A)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        tap(VK_BACK)
        time.sleep(0.1)
        type_slow("I", delay=0.1)
        time.sleep(0.35)
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        rect = (r.left, r.top, r.right, r.bottom)
        img_p = ImageGrab.grab(bbox=rect, all_screens=True)
        field_p = map_field_shot(rect, img_p)
        dark_probe = query_dark_count(img_p, field_p)
        focused = dark_probe >= 8
        img_p.save(OUT / "desktop_retry3_probe2.png")
        print("FOCUS_PROBE2", focused, dark_probe, flush=True)

    if not focused:
        report = {
            "error": "REAL_DESKTOP_INPUT_FAILED",
            "where": "field_focus_not_verified_after_probe",
            "DESKTOP_COORDINATE_CALIBRATION": calib,
            "SEARCH_FIELD": {
                "locatedFromCurrentScreen": True,
                "desktopCoordinate": {"x": field_desk[0], "y": field_desk[1]},
                "mouseVisiblyMoved": True,
                "click": True,
                "fieldFocusVerified": False,
                "probeDarkPixels": dark_probe,
                "uiaSearchButton": uia_btn,
            },
        }
        (OUT / "desktop_retry3_step1_proof.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 4

    # Clear probe and type full query
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    tap(VK_A)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.08)
    tap(VK_BACK)
    time.sleep(0.12)
    query = "Ivysaur Base Set 30/102 Pokemon"
    print("TYPE", query, flush=True)
    type_slow(query, delay=0.08)
    time.sleep(0.55)

    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    rect = (r.left, r.top, r.right, r.bottom)
    img_t = ImageGrab.grab(bbox=rect, all_screens=True)
    img_t.save(OUT / "desktop_retry3_typed.png")
    field_t = map_field_shot(rect, img_t)
    dark = query_dark_count(img_t, field_t)
    present = dark >= 30
    print("QUERY_PRESENT", present, dark, flush=True)
    if not present:
        report = {
            "error": "REAL_DESKTOP_INPUT_FAILED",
            "where": "query_not_visible",
            "DESKTOP_COORDINATE_CALIBRATION": calib,
            "SEARCH_FIELD": {
                "desktopCoordinate": {"x": field_desk[0], "y": field_desk[1]},
                "fieldFocusVerified": True,
                "queryVisiblyPresent": False,
                "queryDarkPixels": dark,
            },
        }
        (OUT / "desktop_retry3_step1_proof.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 5

    # Refresh UIA button from current (same coords usually) and click
    print("CLICK_SEARCH_BTN", btn_desk, flush=True)
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    click_at(btn_desk[0], btn_desk[1], virt, from_pos=(pt.x, pt.y), pause=1.2)

    home = title0
    deadline = time.time() + 45
    titles: list[str] = []
    while time.time() < deadline:
        t = window_title(hwnd)
        titles.append(t)
        low = t.lower()
        if "ivysaur" in low or "error page" in low:
            break
        if t != home and "ebay" in low and "ebay australia | electronics" not in low:
            break
        time.sleep(0.4)
    time.sleep(2.5)
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    rect = (r.left, r.top, r.right, r.bottom)
    img_r = ImageGrab.grab(bbox=rect, all_screens=True)
    img_r.save(OUT / "desktop_retry3_results.png")
    final = window_title(hwnd)
    sorry = "error page" in final.lower() or "sorry" in final.lower()
    results = "ivysaur" in final.lower() and not sorry
    challenge = "security" in final.lower() or "verify" in final.lower()

    report = {
        "DESKTOP_COORDINATE_CALIBRATION": calib,
        "SEARCH_FIELD": {
            "locatedFromCurrentScreen": True,
            "method": field_method,
            "screenshotCoords": field_t,
            "uiaField": uia_field,
            "desktopCoordinate": {"x": field_desk[0], "y": field_desk[1]},
            "mouseVisiblyMoved": True,
            "click": True,
            "fieldFocusVerified": True,
            "keyboardInput": True,
            "queryVisiblyPresent": True,
            "queryDarkPixels": dark,
        },
        "SEARCH_BUTTON": {
            "method": "Windows_UIAutomation_BoundingRectangle",
            "desktopCoordinate": {"x": btn_desk[0], "y": btn_desk[1]},
            "uia": uia_btn,
            "mouseVisiblyMoved": True,
            "click": True,
            "navigationOccurred": final != home or results or sorry,
        },
        "RESULT": {
            "searchResultsVisible": results,
            "foregroundWindowTitle": final,
            "resultingURL": "confirm_in_address_bar",
            "SORRY": sorry,
            "challenge": challenge,
            "screenshot": str(OUT / "desktop_retry3_results.png"),
            "titleSamples": titles[-8:],
        },
        "Playwright": False,
        "CDP": False,
        "directSearchURL": False,
        "stop": True,
    }
    (OUT / "desktop_retry3_step1_proof.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
