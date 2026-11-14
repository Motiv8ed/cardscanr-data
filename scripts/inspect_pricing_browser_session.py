#!/usr/bin/env python3
"""Read-only diagnostic for a proposed dedicated pricing Chrome CDP endpoint.

This script NEVER launches Chrome, navigates to eBay, edits a profile, clears a
HARD_STOP, or touches the pricing queue. It only reads local filesystem metadata
and requests /json/version from the explicitly supplied loopback port.

For backend maintainers: this is a preflight for an optional attach-based browser
service; it is not a claim that the current production provider uses this port.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

BRIDGE_AND_RECORDER_PORTS = frozenset({9230, 9231})
MAX_BODY_BYTES = 65536


def _debug_socket_is_local(url: object, port: int) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "ws"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.port == port
            and parsed.path.startswith("/devtools/browser/")
            and bool(parsed.path.removeprefix("/devtools/browser/"))
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def inspect_browser_cdp(
    *, port: int, profile: Path | None = None, timeout_seconds: float = 1.0
) -> dict[str, Any]:
    """Return safe machine-readable status; network access is loopback-only."""
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    if not 0.1 <= timeout_seconds <= 5.0:
        raise ValueError("timeout_seconds must be between 0.1 and 5")

    result: dict[str, Any] = {
        "schemaVersion": 1,
        "mode": "READ_ONLY_PREFLIGHT",
        "cdpPort": port,
        "cdpReady": False,
        "status": "UNKNOWN",
        "profilePresent": profile.is_dir() if profile is not None else None,
        "profileLockArtifactPresent": (
            any((profile / name).exists() for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"))
            if profile is not None and profile.is_dir()
            else None
        ),
    }
    if port in BRIDGE_AND_RECORDER_PORTS:
        result["status"] = "RESERVED_BRIDGE_RECORDER_PORT"
        return result

    url = f"http://127.0.0.1:{port}/json/version"
    # Never use a proxy, even if the owner's environment has HTTP_PROXY set.
    opener = build_opener(ProxyHandler({}))
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            raw = response.read(MAX_BODY_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        result["status"] = "CDP_UNREACHABLE"
        result["errorClass"] = exc.__class__.__name__
        return result

    if len(raw) > MAX_BODY_BYTES:
        result["status"] = "CDP_OVERSIZED_RESPONSE"
        return result
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        result["status"] = "CDP_INVALID_JSON"
        return result
    if not isinstance(payload, dict):
        result["status"] = "CDP_INVALID_SHAPE"
        return result

    browser = payload.get("Browser")
    websocket_url = payload.get("webSocketDebuggerUrl")
    if not isinstance(browser, str) or not browser.startswith(("Chrome/", "Chromium/", "HeadlessChrome/")):
        result["status"] = "CDP_NOT_CHROME"
        return result
    if not _debug_socket_is_local(websocket_url, port):
        result["status"] = "CDP_UNSAFE_DEBUGGER_TARGET"
        return result
    result["status"] = "CDP_READY"
    result["cdpReady"] = True
    result["browserFamily"] = browser.split("/", 1)[0]
    # Never print debugger URLs, browser page URLs, cookies, profile path or PIDs.
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9232, help="Dedicated pricing CDP loopback port; default 9232")
    parser.add_argument("--profile", type=Path, help="Optional dedicated profile directory; never printed")
    parser.add_argument("--timeout", type=float, default=1.0, help="Loopback probe timeout in seconds")
    args = parser.parse_args(argv)
    try:
        result = inspect_browser_cdp(port=args.port, profile=args.profile, timeout_seconds=args.timeout)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["cdpReady"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
