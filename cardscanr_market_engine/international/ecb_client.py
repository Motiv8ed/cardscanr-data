"""Official ECB euro foreign-exchange reference rates (eurofxref-daily.xml)."""
from __future__ import annotations

import ssl
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

ECB_DAILY_XML_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ECB_SOURCE_NAME = "ECB"
ECB_SOURCE_LABEL = "European Central Bank"
REQUIRED_CURRENCIES = ("AUD", "USD", "GBP", "CAD", "JPY", "NZD")


@dataclass(frozen=True)
class EcbFxSnapshot:
    source: str
    source_url: str
    provider_rate_date: date
    fetched_at: datetime
    eur_rates: dict[str, Decimal]  # currency -> units per 1 EUR (EUR itself = 1)

    def pair_rate(self, source_currency: str, target_currency: str) -> Decimal:
        source = source_currency.strip().upper()
        target = target_currency.strip().upper()
        if source == target:
            return Decimal("1")
        source_per_eur = self._per_eur(source)
        target_per_eur = self._per_eur(target)
        if source_per_eur <= 0 or target_per_eur <= 0:
            raise ValueError(f"Invalid ECB rate for {source}->{target}")
        # 1 SOURCE = (1 / source_per_eur) EUR = (target_per_eur / source_per_eur) TARGET
        return target_per_eur / source_per_eur

    def _per_eur(self, currency: str) -> Decimal:
        code = currency.strip().upper()
        if code == "EUR":
            return Decimal("1")
        if code not in self.eur_rates:
            raise ValueError(f"Unsupported currency for ECB snapshot: {code}")
        return self.eur_rates[code]

    def pair_rates_float(self) -> dict[str, float]:
        """Build SOURCE:TARGET float pairs for MarketEngine currency_conversion."""
        currencies = sorted({"EUR", *self.eur_rates.keys()})
        out: dict[str, float] = {}
        for source in currencies:
            for target in currencies:
                if source == target:
                    continue
                out[f"{source}:{target}"] = float(self.pair_rate(source, target))
        return out

    def to_cache_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "sourceLabel": ECB_SOURCE_LABEL,
            "sourceUrl": self.source_url,
            "providerRateDate": self.provider_rate_date.isoformat(),
            "fetchedAt": self.fetched_at.isoformat().replace("+00:00", "Z"),
            "baseCurrency": "EUR",
            "eurRates": {code: str(rate) for code, rate in sorted(self.eur_rates.items())},
            "pairRates": {key: value for key, value in sorted(self.pair_rates_float().items())},
            "currencies": sorted({"EUR", *self.eur_rates.keys()}),
        }


def parse_ecb_daily_xml(xml_text: str, *, fetched_at: datetime | None = None) -> EcbFxSnapshot:
    fetched = fetched_at or datetime.now(timezone.utc)
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    root = ET.fromstring(xml_text)
    ns = {
        "gesmes": "http://www.gesmes.org/xml/2002-08-01",
        "efx": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref",
    }
    day_cube = root.find(".//{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}Cube[@time]")
    if day_cube is None:
        # Namespace-agnostic fallback
        for elem in root.iter():
            if elem.tag.endswith("Cube") and elem.attrib.get("time"):
                day_cube = elem
                break
    if day_cube is None:
        raise ValueError("ECB XML missing timed Cube element")
    raw_date = day_cube.attrib.get("time")
    if not raw_date:
        raise ValueError("ECB XML Cube missing time attribute")
    provider_date = date.fromisoformat(raw_date)
    rates: dict[str, Decimal] = {}
    for child in list(day_cube):
        if not child.tag.endswith("Cube"):
            continue
        currency = str(child.attrib.get("currency") or "").strip().upper()
        rate_raw = str(child.attrib.get("rate") or "").strip()
        if not currency or not rate_raw:
            continue
        try:
            rate = Decimal(rate_raw)
        except InvalidOperation as exc:
            raise ValueError(f"Invalid ECB rate for {currency}: {rate_raw}") from exc
        if rate <= 0:
            raise ValueError(f"Non-positive ECB rate for {currency}: {rate}")
        rates[currency] = rate
    missing = [code for code in REQUIRED_CURRENCIES if code not in rates]
    if missing:
        raise ValueError(f"ECB XML missing required currencies: {', '.join(missing)}")
    return EcbFxSnapshot(
        source=ECB_SOURCE_NAME,
        source_url=ECB_DAILY_XML_URL,
        provider_rate_date=provider_date,
        fetched_at=fetched.astimezone(timezone.utc),
        eur_rates=rates,
    )


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def fetch_ecb_daily_xml(
    *,
    url: str = ECB_DAILY_XML_URL,
    timeout_seconds: float = 30.0,
) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "CardScanR-FX/1.0 (+https://cardscanr.app; ECB reference rates)",
            "Accept": "application/xml,text/xml,*/*",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds, context=_ssl_context()) as response:
            status = getattr(response, "status", None) or response.getcode()
            if status != 200:
                raise ValueError(f"ECB HTTP status {status}")
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise ValueError(f"ECB HTTP error {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"ECB network failure: {exc.reason}") from exc
    except OSError as exc:
        raise ValueError(f"ECB network failure: {exc}") from exc
    if not body:
        raise ValueError("ECB response body empty")
    return body.decode("utf-8")


def fetch_ecb_snapshot(*, url: str = ECB_DAILY_XML_URL, now: datetime | None = None) -> EcbFxSnapshot:
    xml_text = fetch_ecb_daily_xml(url=url)
    return parse_ecb_daily_xml(xml_text, fetched_at=now or datetime.now(timezone.utc))
