from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from cardscanr_global_catalogue import images
from cardscanr_global_catalogue.artifacts import CANONICAL_PRINTING_SCHEMA
from cardscanr_global_catalogue.contracts import (
    AmbiguousLanguageError,
    build_printing_record,
    canonical_base_id,
    canonical_printing_id,
    canonical_set_id,
    canonicalize_language,
    normalize_collector_number,
    region_for_language,
)
from cardscanr_global_catalogue.images import build_global_image_path
from cardscanr_global_catalogue.metadata import (
    PermanentProviderError,
    ProviderRateLimiter,
    TcgdexClient,
    parse_retry_after,
    tcgdex_set_url,
)
from cardscanr_global_catalogue.providers import credential_status


def _printing(
    *,
    source_language: str = "en",
    provider_set_id: str = "base1",
    provider_card_id: str = "base1-1",
    name: str = "Alakazam",
    collector_number: str = "1",
    variant: str = "unspecified",
) -> dict:
    return build_printing_record(
        source_language=source_language,
        provider="tcgdex",
        provider_set_id=provider_set_id,
        provider_card_id=provider_card_id,
        native_set_name="Base Set",
        native_card_name=name,
        collector_number=collector_number,
        official_set_total=102,
        release_date="1999-01-09",
        image_url="https://assets.tcgdex.net/en/base/base1/1",
        serie_id="base",
        serie_name="Base",
        variant=variant,
    )


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        ("jp", "ja"),
        ("zh-cn", "zh-Hans"),
        ("zh-tw", "zh-Hant"),
        ("kr", "ko"),
        ("es-mx", "es-419"),
        ("pt-br", "pt-BR"),
    ],
)
def test_language_aliases_are_canonical_and_reversible(
    legacy: str,
    canonical: str,
) -> None:
    assert canonicalize_language(legacy) == canonical


@pytest.mark.parametrize("ambiguous", ["zh", "chn", "pt"])
def test_ambiguous_legacy_languages_are_not_guessed(ambiguous: str) -> None:
    with pytest.raises(AmbiguousLanguageError):
        canonicalize_language(ambiguous)


def test_language_and_region_are_separate() -> None:
    assert canonicalize_language("zh-tw") == "zh-Hant"
    assert region_for_language("zh-Hant") == "MULTI"
    assert canonicalize_language("es-mx", provider="tcgdex") == "es-419"
    assert region_for_language("es-419") == "LATAM"


def test_collector_number_normalization_preserves_designators() -> None:
    assert normalize_collector_number(" 001 / 102 ") == "1/102"
    assert normalize_collector_number("TG01 / TG30") == "TG1/TG30"
    assert normalize_collector_number("SV001") == "SV1"


def test_exact_identity_does_not_match_by_name() -> None:
    first = _printing(provider_set_id="base1", provider_card_id="base1-1")
    second = _printing(provider_set_id="base2", provider_card_id="base2-1")
    assert first["nativeCardName"] == second["nativeCardName"]
    assert first["canonicalPrintingId"] != second["canonicalPrintingId"]


def test_language_and_region_change_identity() -> None:
    english_set = canonical_set_id(
        language="en",
        region="GLOBAL",
        provider="tcgdex",
        provider_set_id="base1",
    )
    japanese_set = canonical_set_id(
        language="ja",
        region="JP",
        provider="tcgdex",
        provider_set_id="base1",
    )
    english = canonical_base_id(
        language="en",
        region="GLOBAL",
        canonical_set=english_set,
        collector_number="1",
    )
    japanese = canonical_base_id(
        language="ja",
        region="JP",
        canonical_set=japanese_set,
        collector_number="1",
    )
    assert english != japanese


def test_physical_variant_changes_printing_identity() -> None:
    record = _printing()
    reverse = canonical_printing_id(
        canonical_base=record["canonicalBaseId"],
        variant="reverse_holo",
    )
    assert reverse != record["canonicalPrintingId"]


def test_canonical_printing_record_validates_against_schema() -> None:
    jsonschema.Draft202012Validator(
        CANONICAL_PRINTING_SCHEMA,
        format_checker=jsonschema.FormatChecker(),
    ).validate(_printing())


def test_credential_status_never_contains_secret_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "do-not-leak-this-credential"
    monkeypatch.setenv("POKEWALLET_API_KEY", secret)
    payload = credential_status(validate=False, provider_filter="pokewallet")
    encoded = json.dumps(payload, sort_keys=True)
    assert secret not in encoded
    assert payload["providers"][0]["keyPresent"] == "yes"


def test_retry_after_delta_and_http_date() -> None:
    assert parse_retry_after("12") == 12
    now = datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc).timestamp()
    assert parse_retry_after("Fri, 10 Jul 2026 00:00:30 GMT", now=now) == 30


def test_tcgdex_set_url_percent_encodes_reserved_set_id() -> None:
    assert tcgdex_set_url("ja", "SM1+").endswith("/ja/sets/SM1%2B")


def test_global_rate_limiter_records_provider_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cardscanr_global_catalogue.metadata.time.monotonic",
        lambda: 100.0,
    )
    limiter = ProviderRateLimiter(0.2)
    limiter.globally_pause(15)
    assert limiter._blocked_until_monotonic == 115.0


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        payload: object | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.content = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self._payload


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses
        self.calls = 0
        self.headers: dict[str, str] = {}

    def get(self, _url: str, *, timeout: int) -> _FakeResponse:
        del timeout
        response = self.responses[self.calls]
        self.calls += 1
        return response


def test_permanent_404_is_not_retried() -> None:
    client = TcgdexClient(request_interval_seconds=0, max_retries=4)
    fake = _FakeSession([_FakeResponse(404)])
    client.session = fake  # type: ignore[assignment]
    with pytest.raises(PermanentProviderError):
        client.get_json("https://example.invalid/card")
    assert fake.calls == 1
    assert client.stats.permanent_404s == 1


def test_retry_after_429_retries_once_without_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TcgdexClient(request_interval_seconds=0, max_retries=2)
    fake = _FakeSession(
        [
            _FakeResponse(429, headers={"Retry-After": "0"}),
            _FakeResponse(200, payload={"ok": True}),
        ]
    )
    client.session = fake  # type: ignore[assignment]
    monkeypatch.setattr(client.rate_limiter, "wait", lambda: None)
    assert client.get_json("https://example.invalid/card") == {"ok": True}
    assert fake.calls == 2
    assert client.stats.retries == 1


def test_global_r2_paths_are_immutable_and_region_scoped() -> None:
    path = build_global_image_path(
        language="zh-Hant",
        region="TW",
        canonical_set_id="pokemon|zh-Hant|TW|tcgdex:sv1",
        canonical_printing_id="pokemon|zh-Hant|TW|set:abc|1|unspecified",
        content_hash_sha256="a" * 64,
        variant="thumb",
    )
    assert path.startswith("pokemon/zh-Hant/tw/")
    assert path.endswith("/v/aaaaaaaaaaaaaaaa/thumb.webp")
    assert build_global_image_path(
        language="zh-Hant",
        region="HK",
        canonical_set_id="pokemon|zh-Hant|HK|tcgdex:sv1",
        canonical_printing_id="pokemon|zh-Hant|HK|set:def|1|unspecified",
        content_hash_sha256="a" * 64,
        variant="thumb",
    ) != path


def _write_sample_cards(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            **_printing(
                provider_card_id=f"base1-{number}",
                collector_number=str(number),
                name=f"Card {number}",
            )
        }
        for number in range(1, 8)
    ]
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_canary_plan_is_deterministic_and_redacts_provider_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalogue = tmp_path / "catalogue"
    reports = tmp_path / "reports"
    _write_sample_cards(catalogue / "cards.jsonl")
    monkeypatch.setattr(images, "CATALOGUE_DIR", catalogue)
    monkeypatch.setattr(images, "REPORT_DIR", reports)
    first = images.create_multilingual_canary_plan(sample_size=3, seed="fixed")
    second = images.create_multilingual_canary_plan(sample_size=3, seed="fixed")
    assert first["batches"][0]["cards"] == second["batches"][0]["cards"]
    assert all(
        "sourceUrl" not in card
        for batch in first["batches"]
        for card in batch["cards"]
    )
    assert first["executionPerformed"] is False
    assert first["classification"] == "BLOCKED"

