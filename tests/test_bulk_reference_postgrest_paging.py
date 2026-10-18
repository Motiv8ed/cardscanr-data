"""Chunked cache reads, paged key reads, and fatal-error exit behaviour."""
from __future__ import annotations

import importlib.util
import io
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from cardscanr_market_engine.bulk.reference_refresh import BulkReferenceRefreshRunner, BulkRefreshConfig
from cardscanr_market_engine.supabase_client import (
    DEFAULT_POSTGREST_BUDGET_ORIGIN,
    MAX_GET_URL_LENGTH,
    MAX_IN_FILTER_CHUNK,
    SupabaseMarketEngineClient,
    prepared_get_url,
)
from tests.test_bulk_reference_pricing import NOW, FakeBulkClient, _sample_key, sample_engine_config

ROOT = Path(__file__).resolve().parents[1]


def _load_worker():
    path = ROOT / "workers" / "bulk_reference_price_sync.py"
    spec = importlib.util.spec_from_file_location("bulk_reference_price_sync_ct001", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WORKER = _load_worker()


def _runner(client, **overrides) -> BulkReferenceRefreshRunner:
    config = {
        "dry_run": True,
        "max_keys": 5000,
        "enable_live_tcgdex": False,
        "verification_budget_per_run": 0,
        "high_value_threshold": 50.0,
        "reference_fresh_hours": 24,
    }
    config.update(overrides)
    return BulkReferenceRefreshRunner(
        client=client,  # type: ignore[arg-type]
        engine_config=sample_engine_config(),
        refresh_config=BulkRefreshConfig(**config),
        now_func=lambda: NOW,
    )


def _ids_from_in_filter(value: str) -> list[str]:
    prefix = "in.("
    if not (value.startswith(prefix) and value.endswith(")")):
        raise AssertionError(f"expected in.(...) filter, got {value!r}")
    body = value[len(prefix) : -1]
    if not body:
        return []
    return body.split(",")


class RecordingCacheClient:
    supabase_url = DEFAULT_POSTGREST_BUDGET_ORIGIN
    _budget_base_url = SupabaseMarketEngineClient._budget_base_url
    _table_get_in_chunks = SupabaseMarketEngineClient._table_get_in_chunks

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _table_get(self, table: str, *, params: dict):
        if table != "market_price_cache":
            raise AssertionError(table)
        self.calls.append(dict(params))
        ids = _ids_from_in_filter(str(params["price_key_id"]))
        return [{"price_key_id": key_id, "current_market_price": 1} for key_id in ids]


class PagingKeysClient:
    supabase_url = DEFAULT_POSTGREST_BUDGET_ORIGIN
    _budget_base_url = SupabaseMarketEngineClient._budget_base_url
    _table_get_paged = SupabaseMarketEngineClient._table_get_paged
    _table_get_in_chunks = SupabaseMarketEngineClient._table_get_in_chunks

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[dict] = []

    def _table_get(self, table: str, *, params: dict):
        if table != "market_price_keys":
            raise AssertionError(table)
        self.calls.append(dict(params))
        offset = int(params.get("offset", 0))
        # Reproduce the hosted db-max-rows cap even if the caller asks for more.
        limit = min(int(params["limit"]), 1000)
        return self.rows[offset : offset + limit]


class CacheMapChunkTests(unittest.TestCase):
    def test_load_cache_map_chunks_1518_ids_under_8kb(self) -> None:
        ids = [str(uuid.uuid4()) for _ in range(1518)]
        client = RecordingCacheClient()
        merged = _runner(client).load_cache_map(ids)

        self.assertEqual(set(merged), set(ids))
        self.assertGreaterEqual(len(client.calls), 11)
        seen: list[str] = []
        for params in client.calls:
            url = prepared_get_url(client.supabase_url, "market_price_cache", params)
            self.assertLess(len(url), MAX_GET_URL_LENGTH)
            chunk = _ids_from_in_filter(str(params["price_key_id"]))
            self.assertGreater(len(chunk), 0)
            self.assertLessEqual(len(chunk), MAX_IN_FILTER_CHUNK)
            self.assertEqual(params["limit"], str(len(chunk)))
            seen.extend(chunk)
        self.assertEqual(seen, ids)

    def test_load_cache_map_empty_skips_get(self) -> None:
        client = RecordingCacheClient()
        self.assertEqual(_runner(client).load_cache_map([]), {})
        self.assertEqual(client.calls, [])


class ListKeysPagingTests(unittest.TestCase):
    def _rows(self, count: int) -> list[dict]:
        rows = []
        for index in range(count):
            key = _sample_key(
                id=f"00000000-0000-4000-8000-{index:012d}",
                collector_number=str((index % 200) + 1),
            )
            rows.append(key.raw)
        return rows

    def test_list_keys_paginates_past_1000_row_cap(self) -> None:
        rows = self._rows(1518)
        client = PagingKeysClient(rows)
        keys = _runner(client, max_keys=5000).list_keys()

        self.assertEqual([key.id for key in keys], [row["id"] for row in rows])
        self.assertGreaterEqual(len(client.calls), 2)
        self.assertTrue(all(int(call["limit"]) <= 1000 for call in client.calls))
        self.assertTrue(any(int(call["offset"]) >= 1000 for call in client.calls))
        self.assertEqual(int(client.calls[0]["limit"]), 1000)
        self.assertEqual(int(client.calls[0]["offset"]), 0)
        self.assertIn("id.asc", client.calls[0]["order"])

    def test_list_keys_stops_at_max_keys(self) -> None:
        client = PagingKeysClient(self._rows(1518))
        keys = _runner(client, max_keys=1200).list_keys()
        self.assertEqual(len(keys), 1200)
        self.assertEqual(int(client.calls[-1]["offset"]), 1000)
        self.assertEqual(int(client.calls[-1]["limit"]), 200)


class FatalSyncExitTests(unittest.TestCase):
    def test_list_keys_and_cache_map_failures_propagate(self) -> None:
        client = FakeBulkClient()
        client.keys = [_sample_key(id="abc")]

        def boom(table: str, params: dict | None = None):
            if table == "market_price_keys":
                raise RuntimeError("list_keys failed")
            return FakeBulkClient._table_get(client, table, params)

        client._table_get = boom  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "list_keys failed"):
            _runner(client).run()

        def boom_cache(table: str, params: dict | None = None):
            if table == "market_price_cache":
                raise RuntimeError("load_cache_map failed")
            return FakeBulkClient._table_get(client, table, params)

        client._table_get = boom_cache  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "load_cache_map failed"):
            _runner(client).run()

    def test_preload_set_failure_propagates(self) -> None:
        client = FakeBulkClient()
        client.keys = [_sample_key(id="abc")]
        runner = _runner(client, enable_live_tcgdex=True)
        with patch(
            "cardscanr_market_engine.bulk.tcgdex_client.TcgdexRunCache.preload_set",
            side_effect=RuntimeError("preload_set failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "preload_set failed"):
                runner.run()

    def test_per_key_error_accounting_unchanged(self) -> None:
        client = FakeBulkClient()
        client.keys = [
            _sample_key(id="bad"),
            _sample_key(id="good", collector_number="2"),
        ]
        runner = _runner(client)
        original = BulkReferenceRefreshRunner.process_key

        def wrapped(self, *, key, prior_cache, counters, verification_budget):
            if key.id == "bad":
                raise RuntimeError("bad key")
            return original(
                self,
                key=key,
                prior_cache=prior_cache,
                counters=counters,
                verification_budget=verification_budget,
            )

        with patch.object(BulkReferenceRefreshRunner, "process_key", wrapped):
            report = runner.run()
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["errorDetails"][0]["priceKeyId"], "bad")
        self.assertEqual(report["errorDetails"][0]["stage"], "process_key")
        self.assertEqual(report["errorDetails"][0]["error"], "bad key")
        self.assertEqual(report["keysScanned"], 1)
        self.assertEqual(report["status"], "success")


class WorkerFatalExitTests(unittest.TestCase):
    def _client(self, rows: list[dict] | None = None, *, fail: str | None = None) -> PagingKeysClient:
        client = PagingKeysClient(rows or [])
        original = client._table_get

        def _table_get(table: str, *, params: dict):
            if fail == "list_keys" and table == "market_price_keys":
                raise RuntimeError("list_keys failed")
            if fail == "load_cache_map" and table == "market_price_cache":
                raise RuntimeError("load_cache_map failed")
            if table == "market_price_cache":
                return []
            return original(table, params=params)

        client._table_get = _table_get  # type: ignore[method-assign]
        return client

    def _run_main(self, client, *, tcgdex: str = "false", preload_error: bool = False) -> tuple[int, str, str | None]:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            env = {
                "SUPABASE_URL": DEFAULT_POSTGREST_BUDGET_ORIGIN,
                "SUPABASE_SECRET_KEY": "unit-test-not-a-real-secret",
                "BULK_REFERENCE_DRY_RUN": "true",
                "BULK_REFERENCE_ENABLE_LIVE_TCGDEX": tcgdex,
                "BULK_REFERENCE_MAX_KEYS_PER_RUN": "5000",
            }
            with patch.dict("os.environ", env, clear=False):
                with patch.object(WORKER, "load_supabase_env", lambda *args, **kwargs: None):
                    with patch.object(WORKER, "SupabaseMarketEngineClient", return_value=client):
                        with patch.object(WORKER, "REPORTS_DIR", reports):
                            with patch(
                                "cardscanr_market_engine.international.fx_cache.maybe_refresh_ecb_fx_cache",
                                return_value={"source": "test", "providerRateDate": "2026-10-16", "fetchedAt": "t"},
                            ):
                                preload = patch(
                                    "cardscanr_market_engine.bulk.tcgdex_client.TcgdexRunCache.preload_set",
                                    side_effect=RuntimeError("preload_set failed"),
                                )
                                with redirect_stderr(stderr):
                                    if preload_error:
                                        with preload:
                                            code = WORKER.main(["--dry-run"])
                                    else:
                                        code = WORKER.main(["--dry-run"])
            report_path = reports / "bulk_reference_sync_latest.json"
            report_text = report_path.read_text(encoding="utf-8") if report_path.is_file() else None
            return code, stderr.getvalue(), report_text

    def test_setup_failures_exit_nonzero_with_traceback(self) -> None:
        cases = (
            ("list_keys", self._client(fail="list_keys"), "false", False, "list_keys failed"),
            (
                "load_cache_map",
                self._client([_sample_key(id="abc").raw], fail="load_cache_map"),
                "false",
                False,
                "load_cache_map failed",
            ),
            (
                "preload_set",
                self._client([_sample_key(id="abc").raw]),
                "true",
                True,
                "preload_set failed",
            ),
        )
        for name, client, tcgdex, preload_error, message in cases:
            with self.subTest(name=name):
                code, stderr, report_text = self._run_main(client, tcgdex=tcgdex, preload_error=preload_error)
                self.assertEqual(code, 1)
                self.assertIn("Traceback (most recent call last)", stderr)
                self.assertIn(message, stderr)
                self.assertIsNone(report_text)

    def test_per_key_errors_stay_in_the_report(self) -> None:
        client = self._client(
            [
                _sample_key(id="bad").raw,
                _sample_key(id="good", collector_number="2").raw,
            ]
        )
        original = BulkReferenceRefreshRunner.process_key

        def wrapped(self, *, key, prior_cache, counters, verification_budget):
            if key.id == "bad":
                raise RuntimeError("bad key")
            return original(
                self,
                key=key,
                prior_cache=prior_cache,
                counters=counters,
                verification_budget=verification_budget,
            )

        with patch.object(BulkReferenceRefreshRunner, "process_key", wrapped):
            code, stderr, report_text = self._run_main(client)
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback (most recent call last)", stderr)
        self.assertIsNotNone(report_text)
        assert report_text is not None
        self.assertIn('"errors": 1', report_text)
        self.assertIn('"stage": "process_key"', report_text)


class WrapperScriptTests(unittest.TestCase):
    def test_wrappers_capture_stderr_and_keep_exit_code(self) -> None:
        for name in (
            "scripts/ensure_live_bulk_reference_pricing_runtime.ps1",
            "scripts/ensure_ecb_fx_refresh_runtime.ps1",
        ):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("function Invoke-LoggedPython", text)
            self.assertIn("RedirectStandardError = $true", text)
            self.assertIn("RedirectStandardOutput = $true", text)
            self.assertIn("$proc.ExitCode", text)
            self.assertIn('"simulate-stderr"', text)
            self.assertIn("Invoke-LoggedPython -PythonPath $pythonPath", text)
            self.assertNotIn("& $pythonPath @argList 1>> $stdout 2>> $stderr", text)
            self.assertIn("Traceback: simulated", text)
            self.assertIn("raise SystemExit(3)", text)


if __name__ == "__main__":
    unittest.main()
