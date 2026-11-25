"""Child process fixtures for the Windows pricing worker integration test."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import runpy
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine import config, native_windows_pricing_browser as chrome
from cardscanr_market_engine import owned_daily_enablement as enablement
from cardscanr_market_engine import supabase_client
from cardscanr_market_engine.models import MarketPriceKey, MarketPriceRefreshJob

state = Path(os.environ["CSR_FIXTURE_STATE"])
origin = os.environ["CSR_FIXTURE_ORIGIN"]
config.REPORTS_DIR = state / "reports"
enablement.FLAG_PATH = state / "enabled.flag"
enablement.BUDGET_LEDGER_PATH = state / "ledger.json"
enablement.METRICS_PATH = state / "metrics.jsonl"
enablement.STATUS_PATH = state / "status.json"
enablement.FLAG_PATH.write_text("true", encoding="utf-8")

from cardscanr_market_engine import gaming_resource_pause, inter_card_cdp_recovery, local_browser_runtime
from cardscanr_market_engine.providers import desktop_win32_ebay_nav as nav
from cardscanr_market_engine.providers import ebay_browser_provider as browser_provider
from cardscanr_market_engine.providers import post_sold_capture as capture
from cardscanr_market_engine.providers import post_sold_capture_process as capture_process

for name in ("STATE_PATH", "STATUS_PATH", "EVENT_LOG_PATH", "INJECT_FLAG_PATH"):
    setattr(gaming_resource_pause, name, state / getattr(gaming_resource_pause, name).name)
chrome.profile_dir = lambda: state / "chrome"
nav._HOME = origin + "/"
nav._ORIGIN = origin
capture._ARTIFACT_ROOT = state / "artifacts"
capture._AUTHORITATIVE_CAPTURE_ROOT = state / "captures"


def legacy_runtime_called(*args, **kwargs):
    record("legacy_runtime", True)
    raise AssertionError("A Linux browser service was called from the Windows worker")


local_browser_runtime.ensure_xvfb = legacy_runtime_called
local_browser_runtime.probe_local_browser_runtime = legacy_runtime_called
inter_card_cdp_recovery.execute_inter_card_leftover_tab_self_heal = legacy_runtime_called

# The service boundary is replaced. Every queue/cache operation is recorded locally.
def record(kind, data):
    with (state / "service_calls.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": kind, "data": data}, default=str) + "\n")


class FixtureClient:
    def __init__(self, *, supabase_url, service_role_key):
        assert supabase_url == "http://127.0.0.1:1" and service_role_key == "offline-fixture"
        record("mode", os.getenv("EBAY_BROWSER_NAV_MODE"))

    def claim_jobs(self, *, worker_id, max_jobs, market_country):
        assert market_country == "AU" and max_jobs == 1
        job_id = os.environ["CSR_FIXTURE_JOB_ID"]
        record("claim", job_id)
        return [MarketPriceRefreshJob(job_id, "fixture-key", "force", 1, "running", 1)]

    def get_price_key(self, price_key_id):
        return MarketPriceKey(
            id="fixture-key", game="pokemon", card_name="Charizard ex", normalized_card_name="charizard ex",
            set_name="Obsidian Flames", set_code="sv3", collector_number="199/197",
            language="en", variant="raw", condition="raw", market_country="AU", currency="AUD",
            fingerprint="pokemon|en|sv3|199/197|charizard ex|raw|au|aud",
        )

    def insert_snapshot(self, payload):
        record("snapshot", payload)
        return {"id": "fixture-snapshot"}

    def insert_evidence(self, rows):
        record("evidence", rows)
        return rows

    def upsert_cache(self, payload):
        record("cache", payload)
        return payload

    def complete_job(self, **kwargs):
        record("complete", kwargs)
        return kwargs

    def fail_job(self, **kwargs):
        record("failure", kwargs)
        return kwargs

    def upsert_pipeline_heartbeat(self, **kwargs):
        record("heartbeat", kwargs)

    def list_owned_market_pricing_targets(self, *, include_zero_owners):
        record("scheduler_markets", os.getenv("OWNED_DAILY_ALLOWED_MARKETS"))
        return {"targets": []}


supabase_client.SupabaseMarketEngineClient = FixtureClient

# Route the real capture subprocess to the exact local fixture. The application
# still checks a marketplace URL and applies the real AU matcher and price rules.
real_capture = capture_process.run_capture_worker_process
real_market_assertion = browser_provider.assert_final_url_matches_requested_marketplace


def fixture_capture(**kwargs):
    assert kwargs["expected_url"].startswith(origin + "/sch/")
    kwargs["expected_origin"] = "127.0.0.1"
    result = real_capture(**kwargs)
    record("capture", {"pid": result.pid, "success": result.success})
    return result


def fixture_market_assertion(**kwargs):
    assert kwargs["final_url"].startswith(origin + "/sch/")
    kwargs["final_url"] = kwargs["final_url"].replace(origin, "https://www.ebay.com.au", 1)
    real_market_assertion(**kwargs)


capture_process.run_capture_worker_process = fixture_capture
browser_provider.assert_final_url_matches_requested_marketplace = fixture_market_assertion

script = ROOT / sys.argv[1]
sys.argv = [str(script), *sys.argv[2:]]
runpy.run_path(str(script), run_name="__main__")
