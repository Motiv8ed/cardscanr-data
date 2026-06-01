#!/usr/bin/env python3
from __future__ import annotations

from playwright.sync_api import sync_playwright


def main() -> int:
    print("[live-ebay-config] Playwright import: ok")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        browser.close()
    print("[live-ebay-config] Installed Chrome channel launch: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
