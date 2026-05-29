from __future__ import annotations

import unittest

from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient, SupabaseRpcError


class FakeResponse:
    def __init__(self, *, status_code: int, payload: object | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.headers: dict[str, str] = {}
        self.posts: list[dict[str, object]] = []

    def post(self, url: str, *, json: dict[str, object], headers: dict[str, str], timeout: int) -> FakeResponse:
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return self.response


class SupabaseClientRpcTests(unittest.TestCase):
    def test_request_market_price_refresh_uses_sql_p_argument_names(self) -> None:
        session = FakeSession(FakeResponse(status_code=200, payload={"action": "cache_fresh"}))
        client = SupabaseMarketEngineClient(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role-secret",
        )
        client.session = session  # type: ignore[assignment]

        result = client.request_market_price_refresh(
            game="pokemon",
            card_name="Charizard ex",
            normalized_card_name="charizard ex",
            set_name="Obsidian Flames",
            set_code="sv03",
            collector_number="125/197",
            language="en",
            variant="raw",
            condition="raw",
            market_country="au",
            currency="aud",
            fingerprint="fingerprint",
            reason="live_ebay_write_smoke",
            force_refresh=False,
        )

        self.assertEqual(result["action"], "cache_fresh")
        payload = session.posts[0]["json"]
        assert isinstance(payload, dict)
        self.assertEqual(
            sorted(payload.keys()),
            [
                "p_card_name",
                "p_collector_number",
                "p_condition",
                "p_currency",
                "p_fingerprint",
                "p_force_refresh",
                "p_game",
                "p_language",
                "p_market_country",
                "p_normalized_card_name",
                "p_reason",
                "p_set_code",
                "p_set_name",
                "p_variant",
            ],
        )
        self.assertNotIn("card_name", payload)
        self.assertNotIn("market_country", payload)
        self.assertTrue(str(session.posts[0]["url"]).endswith("/rest/v1/rpc/request_market_price_refresh"))

    def test_rpc_http_error_includes_status_body_rpc_name_and_payload_keys(self) -> None:
        body = '{"code":"PGRST202","message":"Could not find function public.request_market_price_refresh"}'
        session = FakeSession(FakeResponse(status_code=404, text=body))
        client = SupabaseMarketEngineClient(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role-secret",
        )
        client.session = session  # type: ignore[assignment]

        with self.assertRaises(SupabaseRpcError) as ctx:
            client.request_market_price_refresh(
                game="pokemon",
                card_name="Charizard ex",
                normalized_card_name="charizard ex",
                set_name="Obsidian Flames",
                set_code="sv03",
                collector_number="125/197",
                language="en",
                variant="raw",
                condition="raw",
                market_country="au",
                currency="aud",
                fingerprint="fingerprint",
            )

        message = str(ctx.exception)
        self.assertIn("request_market_price_refresh", message)
        self.assertIn("status_code=404", message)
        self.assertIn("PGRST202", message)
        self.assertIn("p_market_country", message)
        self.assertNotIn("service-role-secret", message)


if __name__ == "__main__":
    unittest.main()
