"""Opt-in Windows console test; submits only to an ephemeral loopback fixture."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.getenv("CARDSCANR_RUN_WINDOWS_FIXTURE") != "1",
    reason="requires an idle, active Windows console and installed Chrome",
)


def test_native_windows_provider_and_matcher(tmp_path, monkeypatch):
    import ctypes

    from cardscanr_market_engine import local_browser_runtime
    from cardscanr_market_engine import native_windows_pricing_browser as chrome
    from cardscanr_market_engine.control_plane_incidents import register_incident
    from cardscanr_market_engine.ebay_scale.set_sweep_matcher import (
        SetSweepCatalogueCard, match_sold_row_to_catalogue,
    )
    from cardscanr_market_engine.ebay_scale.sold_row_html import parse_sold_results_html
    from cardscanr_market_engine.live_navigation_attempt import lookup_search_submission_event
    from cardscanr_market_engine.providers import desktop_win32_ebay_nav as nav
    from cardscanr_market_engine.providers import ebay_browser_provider as provider
    from cardscanr_market_engine.providers import post_sold_capture as capture

    saved_html = (Path(__file__).parent / "fixtures/ebay_scale/sv3_set_sweep_fixture.html").read_bytes()
    query = "Obsidian Flames Pokemon"
    submissions = []
    active_attempt = "fixture-attempt"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parts = urlsplit(self.path)
            params = parse_qs(parts.query)
            if parts.path == "/":
                html = b'<form action="/sch/"><input name="_nkw" aria-label="Search for anything"><button>Search</button></form>'
            elif parts.path == "/sch/" and "LH_Sold" not in params:
                submissions.append(lookup_search_submission_event(active_attempt))
                sold = urlencode({"_nkw": params["_nkw"][0], "LH_Sold": 1, "LH_Complete": 1})
                html = f'<a href="/sch/?{sold}">Sold items</a>'.encode()
            elif parts.path == "/sch/":
                html = saved_html.replace(b"<body>", b"<body><h1>Sold listings</h1>")
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(b'<meta charset="utf-8"><title>Local pricing fixture</title>' + html)

        def log_message(self, *args):
            pass

    for key, value in {
        "EBAY_BROWSER_NAV_MODE": "desktop_win32",
        "EBAY_AVAILABILITY_STATE_PATH": tmp_path / "availability.json",
        "MARKET_OPS_STATE_PATH": tmp_path / "ops.json",
        "CONTROL_PLANE_INCIDENTS_PATH": tmp_path / "incidents.json",
        "CARDSCANR_LIVE_NAV_ATTEMPTS_DIR": tmp_path / "attempts",
        "CARDSCANR_PRICE_KEY_ID": "fixture-price-key",
        "CARDSCANR_MARKET": "AU",
        "CARDSCANR_PRE_SUBMIT_ONLY": "",
    }.items():
        monkeypatch.setenv(key, str(value))
    profile = tmp_path / "profile"
    monkeypatch.setattr(chrome, "profile_dir", lambda: profile)
    monkeypatch.setattr(capture, "resolve_authoritative_capture_html_path", lambda **_: tmp_path / "capture.html")
    monkeypatch.setattr("cardscanr_market_engine.live_navigation_attempt.new_attempt_id", lambda: active_attempt)

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", chrome.PORT))
        occupied.listen()
        with pytest.raises(RuntimeError, match="unexpected or unverifiable owner|arguments unavailable"):
            chrome.ensure_browser()

    with monkeypatch.context() as failure:
        def inventory_failure():
            raise PermissionError("fixture inspection failure")

        failure.setattr(chrome, "_inventory", inventory_failure)
        with pytest.raises(PermissionError, match="fixture inspection failure"):
            chrome.ensure_browser()

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setattr(nav, "_ORIGIN", origin)
        monkeypatch.setattr(nav, "_HOME", origin + "/")
        pid = None
        try:
            pid = chrome.ensure_browser()["pid"]
            window = ctypes.c_void_p(nav._window(pid))
            assert not ctypes.windll.user32.IsZoomed(window)
            recovery = subprocess.check_output(
                [sys.executable, "-c",
                 "import sys; from pathlib import Path; "
                 "from cardscanr_market_engine import native_windows_pricing_browser as b; "
                 "b.profile_dir = lambda: Path(sys.argv[1]); print(b.ensure_browser()['pid'])",
                 str(profile)], text=True, timeout=25, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert int(recovery.strip()) == pid
            assert local_browser_runtime.probe_pre_live_runtime()["ready"]
            ready = nav.navigate_query_to_sold(query, attempt_id="dry-run", pre_submit_only=True)
            assert ready.error == "PRE_SUBMIT_QUERY_READY", ready
            assert not submissions and not lookup_search_submission_event("dry-run")["found"]

            try:
                data = provider.EbayBrowserSoldCompsProvider().fetch_set_sweep_results_html(query_text=query)
            except provider.ProviderTemporaryError as exc:
                pytest.fail(f"{exc}: {exc.diagnostics}")
            assert len(submissions) == 1 and submissions[0]["valid"]
            assert submissions[0]["event"]["priceKeyId"] == "fixture-price-key"
            rows = parse_sold_results_html(data)
            card = SetSweepCatalogueCard("charizard", "Charizard ex", "charizard ex",
                                        "Obsidian Flames", "sv3", "199/197")
            assert match_sold_row_to_catalogue(rows[0], catalogue=[card]).catalogue_card_id == "charizard"
            assert match_sold_row_to_catalogue(rows[2], catalogue=[card]) is None

            duplicate = nav.navigate_query_to_sold(query, attempt_id=active_attempt)
            assert duplicate.error == "SEARCH_ATTEMPT_MISSING_OR_ALREADY_CONSUMED", duplicate
            activate = nav._activate_control

            def interrupt_submit(target, state, control, **kwargs):
                if control == "submit" and lookup_search_submission_event("interrupted")["valid"]:
                    raise RuntimeError("FIXTURE_INTERRUPTED_AFTER_EVENT")
                activate(target, state, control, **kwargs)

            with monkeypatch.context() as failure:
                failure.setattr(nav, "_activate_control", interrupt_submit)
                interrupted = nav.navigate_query_to_sold(query, attempt_id="interrupted")
            assert interrupted.error == "FIXTURE_INTERRUPTED_AFTER_EVENT", interrupted
            assert len(submissions) == 1 and lookup_search_submission_event("interrupted")["valid"]
            assert nav.navigate_query_to_sold(query, attempt_id="interrupted").error == (
                "SEARCH_ATTEMPT_MISSING_OR_ALREADY_CONSUMED"
            )
            register_incident(market="AU", incident_type="CHALLENGE", classification="CHALLENGE_REQUIRED")
            denied = nav.navigate_query_to_sold(query, attempt_id="blocked")
            assert denied.error == "MARKETPLACE_WORK_GATE_BLOCKED"
            with pytest.raises(provider.ProviderBlockedError):
                provider.EbayBrowserSoldCompsProvider().fetch_set_sweep_results_html(query_text=query)
            assert len(submissions) == 1 and not lookup_search_submission_event("blocked")["found"]
        finally:
            try:
                if pid is not None:
                    chrome.close_browser()
                    assert chrome.listener_owner() is None
            finally:
                server.shutdown()
