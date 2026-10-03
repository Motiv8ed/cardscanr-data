"""Deterministic Windows ↔ WSL path conversion for CardScanR local boundary."""
from __future__ import annotations

import re
from pathlib import Path


_WIN_DRIVE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def windows_to_wsl_path(path: str | Path) -> str:
    """Convert a Windows path to a WSL /mnt/<drive>/... path.

    Examples:
      D:\\CardScanR_Data\\cardscanr-data → /mnt/d/CardScanR_Data/cardscanr-data
      D:/foo/bar → /mnt/d/foo/bar
    Already-WSL paths are returned normalized with forward slashes.
    """
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        raise ValueError("empty_path")
    if text.startswith("/mnt/"):
        return text
    match = _WIN_DRIVE.match(text)
    if not match:
        # Relative or POSIX non-/mnt path — return as forward-slash POSIX-ish.
        return text if text.startswith("/") else text
    drive = match.group(1).lower()
    rest = match.group(2).lstrip("/")
    return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"


def wsl_to_windows_path(path: str | Path) -> str:
    """Convert /mnt/<drive>/... to <DRIVE>:\\... Windows path."""
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        raise ValueError("empty_path")
    if re.match(r"^[A-Za-z]:/", text):
        return str(Path(text))
    match = re.match(r"^/mnt/([A-Za-z])/(.*)$", text)
    if not match:
        return text
    drive = match.group(1).upper()
    rest = match.group(2).replace("/", "\\")
    return f"{drive}:\\{rest}"


def same_physical_path(a: str | Path, b: str | Path) -> bool:
    """True when both paths resolve to the same filesystem location (best-effort)."""
    try:
        pa = Path(wsl_to_windows_path(a) if str(a).replace("\\", "/").startswith("/mnt/") else a).resolve()
        pb = Path(wsl_to_windows_path(b) if str(b).replace("\\", "/").startswith("/mnt/") else b).resolve()
        return pa == pb
    except OSError:
        return windows_to_wsl_path(a) == windows_to_wsl_path(b)


__all__ = [
    "same_physical_path",
    "windows_to_wsl_path",
    "wsl_to_windows_path",
]
