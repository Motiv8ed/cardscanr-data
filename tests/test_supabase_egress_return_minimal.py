from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from cardscanr_market_engine.bulk.reference_refresh import MARKET_PRICE_KEY_LIST_SELECT, BulkReferenceRefreshRunner
from cardscanr_market_engine.ebay_availability import EbayAvailabilitySnapshot, save_availability
from cardscanr_market_engine.international.fallback_runner import InternationalFallbackMixin
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.models import (
    EvaluatedComp,
    MarketPriceKey,
    MarketPriceRefreshJob,
    PricingStats,
    ProviderRequest,
    ProviderResult,
    SoldComp,
)
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from tests.test_bulk_reference_pricing import sample_engine_config
from tests.test_market_price_job_runner_cache_states import (
    _config,
    _job,
    _riolu_key,
    _sold_comp,
)


class _EmptyBodyResponse:
    def __init__(self, *, status_code: int = 204) -> None:
        self.status_code = status_code
        self.content = b""

    def json(self) -> list:
        raise AssertionError("json() must not be called on empty PostgREST body")

    def raise_for_status(self) -> None:
        return None


class _RecordingSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.posts: list[dict[str, object]] = []

    def post(self, url: str, *, json: object, headers: dict[str, str], timeout: int, params=None):
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout, "params": params})
        return _EmptyBodyResponse(status_code=204)


class _ReadStubSupabaseClient(SupabaseMarketEngineClient):
    """Real write helpers; stub expensive reads for offline job-runner integration."""

    def __init__(self, *, price_key: MarketPriceKey, session: _RecordingSession) -> None:
        super().__init__(supabase_url="https://example.supabase.co", service_role_key="service-role-secret")
        self.session = session  # type: ignore[assignment]
        self._price_key = price_key

    def get_price_key(self, price_key_id: str) -> MarketPriceKey:
        return self._price_key

    def get_cache_row(self, *, price_key_id: str) -> dict[str, Any] | None:
        return None

    def complete_job(self, **kwargs: object) -> dict[str, Any]:
        return {"status": "completed", **kwargs}

    def fail_job(self, **kwargs: object) -> dict[str, Any]:
        return {"status": "failed", **kwargs}

    def cancel_job(self, **kwargs: object) -> dict[str, Any]:
        return {"status": "cancelled", **kwargs}


class _StaticProvider:
    provider_name = "ebay_browser"

    def __init__(self, comps: list[SoldComp]) -> None:
        self._comps = comps

    def fetch_comps(self, _request: ProviderRequest) -> ProviderResult:
        return ProviderResult(
            provider_name="ebay_browser",
            marketplace="EBAY_AU",
            provider_fingerprint="fp",
            query_used="q",
            comps=self._comps,
        )


class _StaticInternationalProvider(_StaticProvider):
    marketplace_name = "ebay"


class SupabaseEgressReturnMinimalTests(unittest.TestCase):
    def _client(self) -> tuple[SupabaseMarketEngineClient, _RecordingSession]:
        session = _RecordingSession()
        client = SupabaseMarketEngineClient(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role-secret",
        )
        client.session = session  # type: ignore[assignment]
        return client, session

    def test_upsert_cache_prefers_minimal_merge_duplicates(self) -> None:
        client, session = self._client()
        payload = {
            "price_key_id": "pk-1",
            "refresh_status": "completed",
            "display_price_source": "fresh_ebay_au",
        }
        out = client.upsert_cache(payload)
        self.assertEqual(out["price_key_id"], "pk-1")
        self.assertEqual(out["display_price_source"], "fresh_ebay_au")
        prefer = session.posts[0]["headers"]["Prefer"]
        self.assertEqual(prefer, "resolution=merge-duplicates,return=minimal")
        self.assertIn("on_conflict", session.posts[0]["params"])

    def test_insert_snapshot_prefers_minimal_and_echoes_id(self) -> None:
        client, session = self._client()
        out = client.insert_snapshot({"price_key_id": "pk-1", "provider": "ebay", "marketplace": "EBAY_AU"})
        self.assertTrue(str(out["id"]))
        self.assertEqual(session.posts[0]["headers"]["Prefer"], "return=minimal")
        posted = session.posts[0]["json"]
        assert isinstance(posted, dict)
        self.assertEqual(posted["id"], out["id"])

    def test_insert_snapshot_respects_preset_id(self) -> None:
        client, session = self._client()
        preset = "11111111-1111-1111-1111-111111111111"
        out = client.insert_snapshot({"id": preset, "price_key_id": "pk-1"})
        self.assertEqual(out["id"], preset)

    def test_insert_evidence_prefers_minimal_and_returns_empty_list(self) -> None:
        client, session = self._client()
        rows = [{"price_key_id": "pk-1", "snapshot_id": "snap-1", "title": "t"}]
        out = client.insert_evidence(rows)
        self.assertEqual(out, [])
        self.assertEqual(session.posts[0]["headers"]["Prefer"], "return=minimal")

    def test_upsert_pipeline_heartbeat_prefers_minimal(self) -> None:
        client, session = self._client()
        result = client.upsert_pipeline_heartbeat(
            component="worker",
            worker_id="w1",
            state="idle",
            meta={"cycle": 1},
        )
        self.assertIsInstance(result, dict)
        self.assertEqual(result["component"], "worker")
        self.assertEqual(
            session.posts[0]["headers"]["Prefer"],
            "resolution=merge-duplicates,return=minimal",
        )

    def test_upsert_cache_echoes_full_payload_on_empty_postgrest_body(self) -> None:
        client, _session = self._client()
        out = client.upsert_cache({"price_key_id": "pk-2", "refresh_status": "failed"})
        self.assertEqual(out["price_key_id"], "pk-2")
        self.assertEqual(out["refresh_status"], "failed")

    def test_bulk_list_keys_pins_narrow_select(self) -> None:
        captured: list[str] = []

        class _PagingClient:
            def _table_get_paged(self, table: str, *, params: dict[str, Any], max_rows: int, page_size: int = 1000):
                captured.append(str(params.get("select")))
                return []

        runner = BulkReferenceRefreshRunner(
            client=_PagingClient(),  # type: ignore[arg-type]
            engine_config=sample_engine_config(),
            refresh_config=mock.Mock(max_keys=10),
            logger=lambda _m: None,
        )
        runner.list_keys()
        self.assertEqual(captured, [MARKET_PRICE_KEY_LIST_SELECT])
        self.assertNotIn("*", MARKET_PRICE_KEY_LIST_SELECT)


class JobRunnerMinimalPostgrestIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        avail = root / "ebay_availability_state.json"
        ops = root / "marketplace_ops_state.json"
        incidents = root / "control_plane_incidents.json"
        save_availability(
            EbayAvailabilitySnapshot(
                state="HEALTHY",
                market="AU",
                confirmed_healthy=True,
                updated_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
            ),
            path=avail,
            force=True,
        )
        ops.write_text('{"version":1,"markets":{}}\n', encoding="utf-8")
        incidents.write_text('{"version":1,"incidents":{}}\n', encoding="utf-8")
        self._prev_env = {
            "EBAY_AVAILABILITY_STATE_PATH": os.environ.get("EBAY_AVAILABILITY_STATE_PATH"),
            "MARKET_OPS_STATE_PATH": os.environ.get("MARKET_OPS_STATE_PATH"),
            "CONTROL_PLANE_INCIDENTS_PATH": os.environ.get("CONTROL_PLANE_INCIDENTS_PATH"),
            "MARKET_WORKER_ALLOWED_MARKETS": os.environ.get("MARKET_WORKER_ALLOWED_MARKETS"),
            "MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS": os.environ.get("MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS"),
        }
        os.environ["EBAY_AVAILABILITY_STATE_PATH"] = str(avail)
        os.environ["MARKET_OPS_STATE_PATH"] = str(ops)
        os.environ["CONTROL_PLANE_INCIDENTS_PATH"] = str(incidents)
        os.environ["MARKET_WORKER_ALLOWED_MARKETS"] = "AU,US,GB,CA,NZ"
        os.environ["MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS"] = "NONE"

    def tearDown(self) -> None:
        for key, value in self._prev_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmpdir.cleanup()

    def test_job_runner_completes_with_empty_write_responses(self) -> None:
        price_key = _riolu_key()
        session = _RecordingSession()
        client = _ReadStubSupabaseClient(price_key=price_key, session=session)
        runner = MarketPriceJobRunner(
            client=client,  # type: ignore[arg-type]
            provider=_StaticProvider([_sold_comp()]),  # type: ignore[arg-type]
            config=_config(),
            now_func=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc),
            logger=lambda _message: None,
        )
        result = runner.run_job(_job())

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["cacheRowId"], price_key.id)
        self.assertTrue(str(result.get("snapshotId")))
        self.assertIsNotNone(result.get("displayPriceSource"))
        write_headers = [post["headers"]["Prefer"] for post in session.posts]
        self.assertIn("return=minimal", write_headers[0])
        self.assertIn("resolution=merge-duplicates,return=minimal", write_headers[-1])


class InternationalFallbackMinimalPostgrestIntegrationTests(unittest.TestCase):
    class _Runner(InternationalFallbackMixin, MarketPriceJobRunner):
        pass

    def test_international_fallback_result_fields_with_empty_write_responses(self) -> None:
        price_key = _riolu_key(market_country="au", currency="aud")
        session = _RecordingSession()
        client = _ReadStubSupabaseClient(price_key=price_key, session=session)
        runner = self._Runner(
            client=client,  # type: ignore[arg-type]
            provider=_StaticInternationalProvider([_sold_comp()]),  # type: ignore[arg-type]
            config=_config(),
            now_func=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc),
            logger=lambda _message: None,
        )
        comp = _sold_comp()
        evaluated = [
            EvaluatedComp(
                comp=comp,
                included_in_estimate=True,
                rejection_reason=None,
                match_score=0.95,
            )
        ]
        stats = PricingStats(
            median_price=4.25,
            low_price=4.0,
            average_price=4.25,
            high_price=4.5,
            recommended_price=4.25,
            sample_size=1,
            confidence="medium",
            included_count=1,
            rejected_count=0,
            stale_after=datetime(2026, 6, 2, tzinfo=timezone.utc),
            price_spread_ratio=0.1,
            confidence_warnings=[],
            included_price_distribution=[],
            no_reliable_price_reason=None,
            price_reliability="reliable",
            clean_recent_comp_count=1,
            clean_stale_comp_count=0,
            oldest_clean_comp_date=None,
            newest_clean_comp_date=comp.sold_date,
            sold_listing_recency_threshold_days=90,
            price_basis="median",
        )
        provider_result = ProviderResult(
            provider_name="ebay_browser",
            marketplace="EBAY_US",
            provider_fingerprint="fp",
            query_used="q",
            comps=[comp],
        )
        provider_request = ProviderRequest(
            price_key=price_key,
            market_country="US",
            currency="USD",
            marketplace="ebay",
            provider_marketplace_id="EBAY_US",
            provider_domain="ebay.com",
            search_locale="en-US",
            display_name="United States",
            market_config=mock.Mock(),
        )
        eligibility = mock.Mock(eligible=True, reason="ok")
        conversion = mock.Mock(
            source_currency="USD",
            target_currency="AUD",
            metadata=lambda **kwargs: {},
        )

        with mock.patch(
            "cardscanr_market_engine.international.fallback_runner.evaluate_international_fallback_eligibility",
            return_value=eligibility,
        ):
            with mock.patch(
                "cardscanr_market_engine.international.fallback_runner.assert_fx_allows_international_conversion"
            ):
                with mock.patch(
                    "cardscanr_market_engine.international.fallback_runner.evaluate_fx_freshness",
                    return_value=mock.Mock(
                        allows_conversion=True,
                        stale=False,
                        age_hours=1,
                        rate_timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
                        provider_rate_date=None,
                        fetched_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                        source="ecb",
                        health="ok",
                        block_reason=None,
                    ),
                ):
                    with mock.patch.object(
                        runner,
                        "fetch_international_estimate_result",
                        return_value=(
                            provider_request,
                            provider_result,
                            evaluated,
                            stats,
                            stats,
                            conversion,
                            [{"fallbackLevel": 1}],
                            "US",
                        ),
                    ):
                        with mock.patch(
                            "cardscanr_market_engine.international.fallback_runner."
                            "evaluate_international_evidence_gate_from_stats",
                            return_value=mock.Mock(allows_user_facing_price=True, reason="ok"),
                        ):
                            job = MarketPriceRefreshJob(
                                id="job-intl-1",
                                price_key_id=price_key.id,
                                reason="international_fallback:target=us",
                                priority=5,
                                status="running",
                                attempt_count=1,
                            )
                            result = runner.run_international_fallback_job(job)

        self.assertEqual(result["cacheRowId"], price_key.id)
        self.assertTrue(str(result.get("snapshotId")))
        self.assertGreaterEqual(len(session.posts), 2)


if __name__ == "__main__":
    unittest.main()