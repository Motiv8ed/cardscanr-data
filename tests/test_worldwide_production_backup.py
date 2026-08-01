from __future__ import annotations

import gzip
import json
from pathlib import Path

from tools import backup_worldwide_production_state as backup


class FakeResponse:
    def __init__(self, payload, *, headers=None, status_code=200):
        self._payload = payload
        self.headers = headers or {}
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected fake HTTP status {self.status_code}")


def test_secret_key_is_never_sent_as_bearer_token() -> None:
    headers = backup.supabase_headers("sb_secret_test")
    assert headers["apikey"] == "sb_secret_test"
    assert "Authorization" not in headers


def test_openapi_discovery_excludes_rpc_and_parameterized_paths(monkeypatch) -> None:
    monkeypatch.setattr(
        backup.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            {
                "paths": {
                    "/cards": {},
                    "/_internal": {},
                    "/rpc/do_work": {},
                    "/cards/{id}": {},
                    "/bad-name": {},
                }
            }
        ),
    )
    assert backup.discover_supabase_resources("https://example.supabase.co", "secret") == [
        "_internal",
        "cards",
    ]


def test_paginated_export_is_jsonl_gzip_with_verified_count(monkeypatch, tmp_path: Path) -> None:
    responses = iter(
        [
            FakeResponse([{"id": 1}, {"id": 2}], headers={"Content-Range": "0-1/3"}),
            FakeResponse([{"id": 3}], headers={"Content-Range": "2-2/3"}),
        ]
    )
    monkeypatch.setattr(backup.requests, "get", lambda *args, **kwargs: next(responses))
    output = tmp_path / "cards.jsonl.gz"
    result = backup.export_supabase_resource(
        base_url="https://example.supabase.co",
        secret="secret",
        resource="cards",
        destination=output,
        page_size=2,
    )
    with gzip.open(output, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    assert rows == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert result["rows"] == 3
    assert result["expectedRows"] == 3
    assert result["pages"] == 2
    assert len(result["sha256"]) == 64


def test_publication_manifest_selection_is_narrow() -> None:
    assert backup.is_publication_manifest("v2/catalog/manifest.json")
    assert backup.is_publication_manifest("v2/catalog/index.json")
    assert not backup.is_publication_manifest("v2/catalog/cards.json")
    assert not backup.is_publication_manifest("v2/catalog/catalogue.sqlite")
