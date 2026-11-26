"""Opt-in Windows owner-recovery tests. The only browser origin is loopback."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from cardscanr_market_engine import native_au_pricing_recovery as recovery
from cardscanr_market_engine.ebay_availability import EbayAvailabilitySnapshot, load_availability, save_availability
from cardscanr_market_engine.ebay_browser_work_gate import evaluate_ebay_browser_work_gate


@pytest.fixture
def reviewed_state(tmp_path, monkeypatch):
    for key, name in (
        ("EBAY_AVAILABILITY_STATE_PATH", "availability.json"),
        ("CONTROL_PLANE_INCIDENTS_PATH", "incidents.json"),
        ("MARKET_OPS_STATE_PATH", "ops.json"),
        ("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", "attempts"),
    ):
        monkeypatch.setenv(key, str(tmp_path / name))
    save_availability(EbayAvailabilitySnapshot(
        state="PROBE_REQUIRED", market="AU", last_outcome="owner_reviewed_challenge_resolved_probe_required",
    ), force=True)
    (tmp_path / "incidents.json").write_text(json.dumps({"version": 1, "incidents": {
        "fixture-review": {"incidentId": "fixture-review", "market": "AU", "incidentType": "CHALLENGE",
                           "status": "resolved", "classification": "CHALLENGE_REQUIRED"},
    }}), encoding="utf-8")
    (tmp_path / "ops.json").write_text('{"version":1,"markets":{}}', encoding="utf-8")
    monkeypatch.setattr(recovery, "_assert_windows_runtime_idle", lambda: None)
    monkeypatch.setattr(recovery, "_release", lambda: "fixture-release")
    path = tmp_path / "review.json"
    document = {
        "schema": "cardscanr-owner-native-au-probe-v1", "release": "fixture-release",
        "incidentId": "fixture-review", "priceKeyId": "fixture-key",
        "expiresAtUtc": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "stateSha256": recovery._reviewed_state(),
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path, document


def test_recovery_refuses_missing_or_stale_owner_approval(reviewed_state, monkeypatch):
    path, review = reviewed_state

    def unexpected(*_args):
        pytest.fail("A refused probe must stop before the browser check")

    monkeypatch.setattr(recovery, "_assert_reviewed_browser", unexpected)
    with pytest.raises(RuntimeError, match="OWNER_APPROVAL_REQUIRED"):
        recovery.run_native_recovery(path, "")
    review["stateSha256"] = {}
    path.write_text(json.dumps(review), encoding="utf-8")
    with pytest.raises(RuntimeError, match="OWNER_REVIEW_STATE_CHANGED"):
        recovery.run_native_recovery(path, recovery.APPROVAL)
    assert not load_availability(market="AU").probe_in_flight


def test_recovery_accepts_review_without_marketplace_authorization_reference(reviewed_state, monkeypatch):
    path, review = reviewed_state
    review["browserIdentity"] = {"pid": 1, "sessionId": 1, "targetId": "page", "browserId": "browser"}
    review["stateSha256"] = recovery._reviewed_state()
    path.write_text(json.dumps(review), encoding="utf-8")
    assert "marketplaceAuthorizationReference" not in review

    reached = []

    def reached_browser_check(*_args, **_kwargs):
        reached.append(True)
        raise RuntimeError("REACHED_BROWSER_CHECK")

    monkeypatch.setattr(recovery, "_assert_reviewed_browser", reached_browser_check)
    with pytest.raises(RuntimeError, match="REACHED_BROWSER_CHECK"):
        recovery.run_native_recovery(path, recovery.APPROVAL)
    assert reached


@pytest.mark.parametrize(
    ("diagnostics", "expected_status", "expected_availability"),
    (
        ({"providerOutcome": "challenge_required"}, "PROBE_CHALLENGE", "CHALLENGE_REQUIRED"),
        ({"ownedDailyOutcome": "EBAY_ACCESS_DENIED_403"}, "PROBE_COOLDOWN", "COOLDOWN"),
        ({"failureClass": "EBAY_RATE_LIMITED"}, "PROBE_COOLDOWN", "COOLDOWN"),
    ),
)
def test_recovery_probe_stops_on_challenge_access_denied_and_rate_limit(
    reviewed_state, monkeypatch, diagnostics, expected_status, expected_availability,
):
    from cardscanr_market_engine.providers import desktop_win32_ebay_nav as nav
    from cardscanr_market_engine.providers.errors import ProviderError

    path, review = reviewed_state
    review["browserIdentity"] = {"pid": 1, "sessionId": 1, "targetId": "page", "browserId": "browser"}
    review["stateSha256"] = recovery._reviewed_state()
    path.write_text(json.dumps(review), encoding="utf-8")

    def raise_provider_error(*_args, **_kwargs):
        raise ProviderError("fixture", diagnostics=diagnostics)

    monkeypatch.setattr(recovery, "_assert_reviewed_browser", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(recovery, "_fetch", raise_provider_error)
    monkeypatch.setattr(nav, "recovery_browser_scope", lambda *_args, **_kwargs: nullcontext())

    outcome = recovery.run_native_recovery(path, recovery.APPROVAL)
    assert outcome["status"] == expected_status
    assert load_availability(market="AU").state == expected_availability
    assert not load_availability(market="AU").probe_in_flight


def test_recovery_refuses_new_incident_even_with_reviewed_hashes(reviewed_state, monkeypatch):
    from cardscanr_market_engine.control_plane_incidents import register_incident

    path, review = reviewed_state
    register_incident(market="AU", incident_type="CHALLENGE", classification="CHALLENGE_REQUIRED")
    review["stateSha256"] = recovery._reviewed_state()
    path.write_text(json.dumps(review), encoding="utf-8")
    with pytest.raises(RuntimeError, match="RECOVERY_GATE_NOT_OPEN"):
        recovery.run_native_recovery(path, recovery.APPROVAL)
    assert not load_availability(market="AU").probe_in_flight


@pytest.mark.parametrize("operation", ["navigate", "submit"])
@pytest.mark.parametrize("change", ["expiry", "pid", "sessionId", "targetId", "browserId"])
def test_recovery_revalidates_at_browser_action(monkeypatch, operation, change):
    from cardscanr_market_engine.providers import desktop_win32_ebay_nav as nav

    original = {"pid": 123, "sessionId": 1, "targetId": "page", "browserId": "browser"}
    current = original.copy()
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    expiry = now[0] + timedelta(seconds=10)
    monkeypatch.setattr(recovery, "_now", lambda: now[0])
    monkeypatch.setattr(recovery, "reviewed_browser_identity", lambda: current)
    monkeypatch.setattr(nav, "_page_socket", lambda target: (target, "fixture-socket"))
    state = {"url": "about:blank", "title": "", "body": "", "challengeUi": {}}
    actions = []

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            # Simulate a change after preliminary verification and socket selection.
            if change == "expiry":
                now[0] = expiry
            else:
                replacement = {"pid": 456, "sessionId": 2, "targetId": "replacement", "browserId": "new-browser"}
                current[change] = replacement[change]

        def evaluate(self, *_args, **_kwargs):
            return state

        def call(self, *_args, **_kwargs):
            actions.append("navigation")

        def evaluate_function(self, *_args, **_kwargs):
            actions.append("submission")

        def close(self):
            pass

    monkeypatch.setattr(nav, "DirectCdpSession", FakeSession)
    guard = lambda target: recovery._verify_browser(original, expiry, target)
    guard("page")
    expected = "OWNER_REVIEW_EXPIRED" if change == "expiry" else "REVIEWED_BROWSER_IDENTITY_CHANGED"
    with nav.recovery_browser_scope(guard), pytest.raises(RuntimeError, match=expected):
        if operation == "navigate":
            nav._open_home("page")
        else:
            nav._activate_control("page", state, "submit")
    assert not actions
    assert nav.verify_recovery_browser() is False


@pytest.mark.skipif(sys.platform != "win32" or os.getenv("CARDSCANR_RUN_WINDOWS_FIXTURE") != "1",
                    reason="requires an active Windows console and Chrome")
@pytest.mark.parametrize("case", ["success", "cooldown", "embedded"])
def test_reviewed_native_recovery_uses_real_browser_and_capture(reviewed_state, tmp_path, monkeypatch, case):
    from cardscanr_market_engine import continuous_safety, native_windows_pricing_browser as chrome, supabase_client
    from cardscanr_market_engine.live_navigation_attempt import lookup_search_submission_event
    from cardscanr_market_engine.models import MarketPriceKey
    from cardscanr_market_engine.providers import desktop_win32_ebay_nav as nav
    from cardscanr_market_engine.providers import ebay_browser_provider as browser
    from cardscanr_market_engine.providers import post_sold_capture as capture, post_sold_capture_process as process

    path, review = reviewed_state
    events = []
    date = datetime.now(timezone.utc).strftime("%d %b %Y")
    sold = (f'<h1>Sold listings</h1><ul class="srp-results"><li class="s-item">'
            '<a class="s-item__link" href="https://www.ebay.com.au/itm/123456789012">'
            '<h3 class="s-item__title">Charizard ex 199/197 Obsidian Flames Pokemon</h3></a>'
            f'<span class="s-item__price">AU $85.00</span><p class="s-item__caption">Sold {date}</p>'
            '<span class="s-item__shipping">Free postage</span></li></ul>')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parts = urlsplit(self.path)
            params = parse_qs(parts.query)
            title = "Local AU fixture"
            if parts.path == "/sch/" and not params:
                body = "Owner-reviewed local page"
                if case == "embedded":
                    body += '<iframe title="Verification" srcdoc="<p>Verify you are human</p>"></iframe>'
                elif case == "success":
                    body += '<iframe id="recaptcha-bootstrap" hidden srcdoc="<p>Passive resource</p>"></iframe>'
            elif parts.path == "/":
                body = '<form action="/sch/"><input name="_nkw"><button>Search</button></form>'
            elif parts.path == "/sch/" and "LH_Sold" not in params and "_nkw" in params:
                event_path, = (tmp_path / "attempts").glob("*.json")
                events.append(lookup_search_submission_event(event_path.name.split(".")[0]))
                if case == "cooldown":
                    title = "Error Page"
                    body = "Sorry, there was a temporary error."
                else:
                    query = urlencode({"_nkw": params["_nkw"][0], "LH_Sold": 1, "LH_Complete": 1})
                    body = f'<a href="/sch/?{query}">Sold items</a>'
            elif parts.path == "/sch/" and "LH_Sold" in params:
                body = sold
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(f"<!doctype html><title>{title}</title>{body}".encode())

        def log_message(self, *_args):
            pass

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["supabase_url"] == "http://127.0.0.1:1"

        def get_price_key(self, key):
            return MarketPriceKey(
                id=key, game="pokemon", card_name="Charizard ex", normalized_card_name="charizard ex",
                set_name="Obsidian Flames", set_code="sv3", collector_number="199/197", language="en",
                variant="raw", condition="raw", market_country="AU", currency="AUD", fingerprint="fixture-au",
            )

    monkeypatch.setattr(supabase_client, "SupabaseMarketEngineClient", FakeClient)
    monkeypatch.setattr(continuous_safety, "BUDGET_LEDGER_PATH", tmp_path / "budget.json")
    monkeypatch.setattr(chrome, "profile_dir", lambda: tmp_path / "chrome")
    monkeypatch.setattr(capture, "_ARTIFACT_ROOT", tmp_path / "artifacts")
    monkeypatch.setattr(capture, "_AUTHORITATIVE_CAPTURE_ROOT", tmp_path / "captures")
    monkeypatch.setenv("SUPABASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "offline-fixture")
    monkeypatch.delenv("CARDSCANR_PRE_SUBMIT_ONLY", raising=False)
    monkeypatch.delenv("PRE_SUBMIT_ONLY", raising=False)
    # Snapshot environment mutations during the test of the production one-shot CLI.
    for key in ("EBAY_BROWSER_NAV_MODE", "EBAY_BROWSER_ENABLED", "EBAY_BROWSER_MAX_QUERY_ATTEMPTS",
                "MARKET_LOOKUP_PROVIDER", "CARDSCANR_LIVE_ATTEMPT_ID", "CARDSCANR_PRICE_KEY_ID",
                "CARDSCANR_FINGERPRINT", "CARDSCANR_MARKET", "CARDSCANR_CURRENCY",
                "CARDSCANR_RUNTIME_MODE", "CARDSCANR_NAV_RUNTIME_MODE", "CARDSCANR_NAV_CONTEXT_JSON"):
        monkeypatch.setenv(key, os.environ.get(key, ""))

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setattr(nav, "_ORIGIN", origin)
        monkeypatch.setattr(nav, "_HOME", origin + "/sch/")
        real_capture = process.run_capture_worker_process
        real_market_check = browser.assert_final_url_matches_requested_marketplace

        def local_capture(**kwargs):
            assert kwargs["expected_url"].startswith(origin + "/sch/")
            kwargs["expected_origin"] = "127.0.0.1"
            return real_capture(**kwargs)

        def local_market_check(**kwargs):
            assert kwargs["final_url"].startswith(origin + "/sch/")
            kwargs["final_url"] = kwargs["final_url"].replace(origin, "https://www.ebay.com.au", 1)
            return real_market_check(**kwargs)

        monkeypatch.setattr(process, "run_capture_worker_process", local_capture)
        monkeypatch.setattr(browser, "assert_final_url_matches_requested_marketplace", local_market_check)
        try:
            chrome.ensure_browser()
            nav._open_home(nav._page_socket()[0])
            if case == "embedded":
                from time import monotonic, sleep

                deadline = monotonic() + 8
                while monotonic() < deadline:
                    _, state = nav._read()
                    if state["challengeUi"]["visibleCaptchaFrameCount"]:
                        break
                    sleep(0.15)
                assert state["url"] == origin + "/sch/" and state["title"] == "Local AU fixture"
                assert state["challengeUi"]["visibleCaptchaFrameCount"] == 1
            else:
                nav._wait(nav._page_socket()[0], lambda value: value["url"] == origin + "/sch/")
            review["browserIdentity"] = recovery.reviewed_browser_identity()
            path.write_text(json.dumps(review), encoding="utf-8")
            monkeypatch.setattr(nav, "_HOME", origin + "/")
            if case == "embedded":
                with pytest.raises(RuntimeError, match="CHALLENGE_REQUIRED"):
                    recovery.run_native_recovery(path, recovery.APPROVAL)
                assert not events and not (tmp_path / "attempts").exists()
                assert not load_availability(market="AU").probe_in_flight
                save_availability(EbayAvailabilitySnapshot(state="HEALTHY", market="AU"), force=True)
                monkeypatch.setenv("CARDSCANR_MARKET", "AU")
                navigation = nav.navigate_query_to_sold("Local fixture", attempt_id="embedded-refusal")
                assert navigation.error == "CHALLENGE_REQUIRED"
                assert not events and not list((tmp_path / "attempts").glob("*.json"))
                return
            result = recovery.run_native_recovery(path, recovery.APPROVAL)
            assert len(events) == 1 and events[0]["valid"]
            assert events[0]["attemptId"] == result["attemptId"]
            assert events[0]["event"]["priceKeyId"] == "fixture-key"
            snap = load_availability(market="AU")
            assert not snap.probe_in_flight and not snap.confirmed_healthy
            assert result["status"] == ("PROBE_COOLDOWN" if case == "cooldown" else "PROBE_HEALTHY_STREAK_PENDING")
            assert snap.state == ("COOLDOWN" if case == "cooldown" else "HEALTHY")
            with pytest.raises(RuntimeError, match="OWNER_REVIEW_STATE_CHANGED"):
                recovery.run_native_recovery(path, recovery.APPROVAL)
            assert len(events) == 1
        finally:
            chrome.close_browser()
            server.shutdown()
