from __future__ import annotations

import unittest

from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient


class _EmptyBodyResponse:
    status_code = 204
    content = b""

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
        return _EmptyBodyResponse()


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
        payload = {"price_key_id": "pk-1", "refresh_status": "completed"}
        out = client.upsert_cache(payload)
        self.assertEqual(out["price_key_id"], "pk-1")
        self.assertIsNone(out.get("id"))
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

    def test_upsert_cache_callers_tolerate_empty_postgrest_body(self) -> None:
        """Call-site contract: echo price_key_id; cache row id may be absent."""
        client, _session = self._client()
        out = client.upsert_cache({"price_key_id": "pk-2", "refresh_status": "failed"})
        self.assertEqual(out["price_key_id"], "pk-2")
        self.assertIsNone(out.get("id"))


if __name__ == "__main__":
    unittest.main()