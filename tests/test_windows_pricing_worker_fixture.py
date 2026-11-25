"""Opt-in process-level test of the native Windows worker and scheduler."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.getenv("CARDSCANR_RUN_WINDOWS_FIXTURE") != "1",
    reason="requires the active Windows console and installed Chrome",
)


def test_native_windows_worker_launch_recovery_and_hold(tmp_path, monkeypatch):
    from cardscanr_market_engine import native_windows_pricing_browser as chrome
    from cardscanr_market_engine.control_plane_incidents import register_incident
    from cardscanr_market_engine.live_navigation_attempt import lookup_search_submission_event
    from cardscanr_market_engine.providers.desktop_win32_ebay_nav import _window

    attempts_dir = tmp_path / "attempts"
    monkeypatch.setenv("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", str(attempts_dir))
    monkeypatch.setattr(chrome, "profile_dir", lambda: tmp_path / "chrome")
    received = []
    sold_date = datetime.now(timezone.utc).strftime("%d %b %Y")
    rows = "".join(
        f'<li class="s-item"><a class="s-item__link" href="https://www.ebay.com.au/itm/{123456789010+i}">'
        '<h3 class="s-item__title">Charizard ex 199/197 Obsidian Flames Pokemon</h3></a>'
        f'<span class="s-item__price">AU ${price}.00</span><p class="s-item__caption">Sold {sold_date}</p>'
        '<span class="s-item__shipping">Free postage</span></li>'
        for i, price in enumerate((80, 85, 90))
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parts = urlsplit(self.path)
            params = parse_qs(parts.query)
            if parts.path == "/":
                body = '<form action="/sch/"><input name="_nkw"><button>Search</button></form>'
            elif parts.path == "/sch/" and "LH_Sold" not in params:
                events = sorted(path.name.split(".")[0] for path in attempts_dir.glob("*.json"))
                received.append([lookup_search_submission_event(event) for event in events])
                sold = urlencode({"_nkw": params["_nkw"][0], "LH_Sold": 1, "LH_Complete": 1})
                body = f'<a href="/sch/?{sold}">Sold items</a>'
            elif parts.path == "/sch/":
                body = '<h1>Sold listings</h1><ul class="srp-results">' + rows + "</ul>"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(("<!doctype html><title>Local Windows pricing worker</title>" + body).encode())

        def log_message(self, *args):
            pass

    root = Path(__file__).resolve().parents[1]
    support = root / "tests/fixtures/windows_pricing_worker_process.py"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Supply only system essentials and fixture configuration to the child.
    env = {key: value for key, value in os.environ.items() if not key.startswith(
        ("SUPABASE_", "CARDSCANR_", "MARKET_", "EBAY_", "OWNED_DAILY_", "CONTROL_PLANE_")
    )}
    env.update(
        PYTHONPATH=str(root),
        CSR_FIXTURE_STATE=str(tmp_path),
        CSR_FIXTURE_ORIGIN=f"http://127.0.0.1:{server.server_port}",
        EBAY_BROWSER_NAV_MODE="desktop_win32",
        EBAY_BROWSER_ENABLED="true",
        MARKET_LOOKUP_PROVIDER="ebay_browser",
        CONFIRM_LIVE_EBAY_WORKER="true",
        SUPABASE_URL="http://127.0.0.1:1",
        SUPABASE_SECRET_KEY="offline-fixture",
        EBAY_AVAILABILITY_STATE_PATH=str(tmp_path / "availability.json"),
        MARKET_OPS_STATE_PATH=str(tmp_path / "ops.json"),
        CONTROL_PLANE_INCIDENTS_PATH=str(tmp_path / "incidents.json"),
        CARDSCANR_LIVE_NAV_ATTEMPTS_DIR=str(attempts_dir),
        CARDSCANR_NAV_CONTEXT_JSON_PATH=str(tmp_path / "nav.json"),
    )

    def run(script, job, *args, expected=0):
        result = subprocess.run(
            [sys.executable, str(support), script, *args], env={**env, "CSR_FIXTURE_JOB_ID": job},
            cwd=tmp_path, text=True, capture_output=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        return result

    def calls(kind):
        log = tmp_path / "service_calls.jsonl"
        return [row["data"] for line in log.read_text(encoding="utf-8").splitlines()
                if (row := json.loads(line))["kind"] == kind]

    try:
        run("workers/owned_daily_price_scheduler.py", "scheduler", "--once", "--dry-run")
        assert calls("scheduler_markets") == ["AU"]
        run("tools/ebay_au_controlled_continuous_owned_daily_rollout.py", "job-1")
        pid = chrome.listener_owner()
        assert pid
        hwnd = _window(pid)
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        assert not user32.IsZoomed(ctypes.c_void_p(hwnd))
        assert user32.GetForegroundWindow() != hwnd
        assert calls("complete"), calls("failure")
        assert float(calls("cache")[0]["current_market_price"]) == pytest.approx(85)
        assert len(received) == 1 and received[0][0]["valid"]
        assert received[0][0]["event"]["priceKeyId"] == "fixture-key"
        assert calls("capture")[0]["success"] and calls("capture")[0]["pid"]

        run("workers/market_price_worker.py", "job-1", "--once")
        assert len(received) == 1 and "WINDOWS_SEARCH_ATTEMPT_ALREADY_CONSUMED" in str(calls("failure"))
        run("workers/market_price_worker.py", "job-2", "--max-cycles", "1")
        assert chrome.listener_owner() == pid
        assert len(received) == 2 and len(calls("complete")) == 2
        assert all(mode == "desktop_win32" for mode in calls("mode"))
        assert not calls("legacy_runtime")

        register_incident(market="AU", incident_type="CHALLENGE", classification="CHALLENGE_REQUIRED",
                          path=tmp_path / "incidents.json")
        stopped = run("workers/market_price_worker.py", "blocked", "--max-cycles", "1", expected=2)
        assert "ACTIVE_CHALLENGE" in stopped.stdout
        assert calls("claim") == ["job-1", "job-1", "job-2"]
        assert len(received) == 2 and len(calls("cache")) == 2
    finally:
        try:
            chrome.close_browser()
            assert chrome.listener_owner() is None
        finally:
            server.shutdown()
            server.server_close()
