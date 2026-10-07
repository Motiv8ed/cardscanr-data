#!/usr/bin/env python3
"""Real Windows desktop mouse/keyboard control for visible CardScanR Chrome.

Uses Win32 SetCursorPos / mouse_event / keybd_event / SendInput only.
Does NOT use Playwright, CDP, locator.click, DOM, or page.goto(search URL).
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import ctypes
from ctypes import wintypes

try:
    from PIL import ImageGrab
except Exception:  # pragma: no cover
    ImageGrab = None  # type: ignore

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.SetProcessDPIAware()

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_A = 0x41
VK_BACK = 0x08
SW_RESTORE = 9
SW_SHOW = 5


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


@dataclass
class ChromeWindow:
    hwnd: int
    title: str
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def list_ebay_chrome_windows() -> list[ChromeWindow]:
    found: list[ChromeWindow] = []

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
            rect = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            found.append(
                ChromeWindow(
                    hwnd=int(hwnd),
                    title=title,
                    left=rect.left,
                    top=rect.top,
                    right=rect.right,
                    bottom=rect.bottom,
                )
            )
        return True

    user32.EnumWindows(cb, 0)
    return found


def prefer_homepage(windows: list[ChromeWindow]) -> ChromeWindow | None:
    if not windows:
        return None
    for win in windows:
        t = win.title.lower()
        if "ebay australia" in t and "|" in t and "ivysaur" not in t and "/sch/" not in t:
            return win
    return windows[0]


def bring_to_front(hwnd: int) -> dict[str, Any]:
    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.15)
    user32.ShowWindow(hwnd, SW_SHOW)
    # AttachThreadInput dance improves SetForegroundWindow success rate.
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None)
    cur_tid = kernel32.GetCurrentThreadId()
    attached = False
    if fg_tid and fg_tid != cur_tid:
        attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
    ok = bool(user32.SetForegroundWindow(hwnd))
    user32.BringWindowToTop(hwnd)
    if attached:
        user32.AttachThreadInput(cur_tid, fg_tid, False)
    time.sleep(0.35)
    return {"setForeground": ok, "foregroundNow": int(user32.GetForegroundWindow())}


def desktop_click(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.08)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.15)


def key_down(vk: int) -> None:
    user32.keybd_event(vk, 0, 0, 0)


def key_up(vk: int) -> None:
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def tap_vk(vk: int, *, pause: float = 0.05) -> None:
    key_down(vk)
    time.sleep(pause)
    key_up(vk)
    time.sleep(pause)


def type_unicode(text: str, *, delay: float = 0.04) -> None:
    """Type via Unicode key events (desktop keyboard path, not DOM fill)."""

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]

    INPUT_KEYBOARD = 1
    for ch in text:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.union.ki = KEYBDINPUT(0, ord(ch), flags, 0, None)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(delay)


def search_box_point(win: ChromeWindow) -> tuple[int, int]:
    # eBay AU header search field: mid-upper chrome content area.
    x = win.left + int(win.width * 0.42)
    y = win.top + 95
    return x, y


def search_button_point(win: ChromeWindow) -> tuple[int, int]:
    # Search button sits to the right of the query field on AU header.
    x = win.left + int(win.width * 0.72)
    y = win.top + 95
    return x, y


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value or ""


def refresh_window(hwnd: int) -> ChromeWindow | None:
    for win in list_ebay_chrome_windows():
        if win.hwnd == hwnd:
            return win
    wins = list_ebay_chrome_windows()
    return wins[0] if wins else None


def screenshot_window(win: ChromeWindow, path: Path) -> str | None:
    if ImageGrab is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    bbox = (win.left, win.top, win.right, win.bottom)
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    img.save(path)
    return str(path)


def run_search(*, query: str, out_dir: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "phase": "C_DESKTOP_SEARCH",
        "mechanism": "Win32 SetCursorPos/mouse_event + SendInput unicode keyboard",
        "playwrightUsed": False,
        "pageGotoSearchUrl": False,
        "locatorClick": False,
        "domClick": False,
        "desktopMouse": True,
        "desktopKeyboard": True,
        "query": query,
        "steps": [],
    }
    wins = list_ebay_chrome_windows()
    report["ebayWindowsFound"] = [asdict(w) for w in wins]
    win = prefer_homepage(wins)
    if win is None:
        report["error"] = "no_visible_ebay_chrome_window"
        return report
    report["targetWindowBefore"] = asdict(win)
    report["foreground"] = bring_to_front(win.hwnd)

    sx, sy = search_box_point(win)
    report["steps"].append({"action": "desktop_click_search_box", "x": sx, "y": sy, "class": "DESKTOP_MOUSE"})
    desktop_click(sx, sy)
    time.sleep(0.25)

    # Select-all + backspace via desktop keys, then unicode type.
    key_down(VK_CONTROL)
    tap_vk(VK_A, pause=0.04)
    key_up(VK_CONTROL)
    time.sleep(0.1)
    tap_vk(VK_BACK, pause=0.04)
    time.sleep(0.1)
    report["steps"].append({"action": "desktop_type_query", "query": query, "class": "DESKTOP_KEYBOARD"})
    type_unicode(query, delay=0.035)
    time.sleep(0.35)

    # Activate Search via desktop Enter only (no Playwright / no URL goto).
    report["steps"].append({"action": "desktop_press_enter", "class": "DESKTOP_KEYBOARD"})
    tap_vk(VK_RETURN, pause=0.06)
    time.sleep(1.2)

    # If still on homepage title, click visible Search button via desktop mouse.
    title_now = window_title(win.hwnd).lower()
    win2 = refresh_window(win.hwnd) or win
    if "ebay australia" in title_now and "ivysaur" not in title_now:
        bx, by = search_button_point(win2)
        report["steps"].append({"action": "desktop_click_search_button", "x": bx, "y": by, "class": "DESKTOP_MOUSE"})
        desktop_click(bx, by)

    # Wait for title change indicating results (no URL navigation API).
    deadline = time.time() + 45
    titles: list[str] = []
    while time.time() < deadline:
        title = window_title(win.hwnd)
        titles.append(title)
        lower = title.lower()
        if "ivysaur" in lower:
            break
        if "ebay australia |" not in lower and "ebay" in lower and len(title) > 24:
            break
        time.sleep(0.5)
    time.sleep(2.0)
    win3 = refresh_window(win.hwnd) or win2
    final_title = window_title(win3.hwnd)
    shot = out_dir / "desktop_search_results_agent_capture.png"
    shot_path = screenshot_window(win3, shot)
    report["targetWindowAfter"] = asdict(win3)
    report["finalTitle"] = final_title
    report["titleSamples"] = titles[-8:]
    report["agentScreenshot"] = shot_path
    report["searchAppearsSuccessful"] = "ivysaur" in final_title.lower()
    report["andrewConfirmedScreenshot"] = False
    report["stop"] = True
    report["SORRY_hint"] = "sorry" in final_title.lower()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="Ivysaur Base Set 30/102 Pokemon")
    parser.add_argument(
        "--out-dir",
        default=str(Path("reports/artifacts/owned_daily_session")),
    )
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = run_search(query=args.query, out_dir=out_dir)
    path = out_dir / "desktop_search_phase_c.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"WROTE {path}")
    return 0 if not report.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
