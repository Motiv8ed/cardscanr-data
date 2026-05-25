from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import random

from ..models import MarketPriceKey, ProviderResult, SoldComp


class MockMarketCompsProvider:
    provider_name = "mock"
    marketplace = "mock_ebay_sold"

    def fetch_comps(self, price_key: MarketPriceKey) -> ProviderResult:
        seed = int(hashlib.sha256(price_key.fingerprint.encode("utf-8")).hexdigest()[:16], 16)
        rng = random.Random(seed)
        base_price = round(15 + (seed % 5000) / 100, 2)
        currency = (price_key.currency or "usd").upper()
        sold_date_base = datetime(2026, 5, 20, tzinfo=timezone.utc)
        listing_prefix = f"https://mock.cardscanr.local/{price_key.fingerprint}"
        common_title = f"{price_key.card_name} {price_key.set_name} {price_key.collector_number}"
        raw_condition = "Raw" if price_key.variant == "raw" else price_key.variant.upper()
        comps: list[SoldComp] = []

        for index in range(9):
            sold_price = round(base_price + rng.uniform(-2.25, 2.25), 2)
            shipping_price = round(rng.uniform(0, 3.5), 2)
            comps.append(
                SoldComp(
                    source_listing_id=f"{price_key.fingerprint}-good-{index}",
                    title=f"{common_title} {raw_condition} sold comp {index + 1}",
                    sold_price=sold_price,
                    shipping_price=shipping_price,
                    total_price=round(sold_price + shipping_price, 2),
                    currency=currency,
                    sold_date=sold_date_base - timedelta(days=index),
                    listing_url=f"{listing_prefix}/good/{index}",
                    condition_text=raw_condition,
                    raw_metadata={"bucket": "good", "seed": seed, "index": index},
                )
            )

        outlier_price = round(base_price * 3.4, 2)
        comps.append(
            SoldComp(
                source_listing_id=f"{price_key.fingerprint}-outlier",
                title=f"{common_title} {raw_condition} premium sale",
                sold_price=outlier_price,
                shipping_price=0.0,
                total_price=outlier_price,
                currency=currency,
                sold_date=sold_date_base - timedelta(days=10),
                listing_url=f"{listing_prefix}/outlier",
                condition_text=raw_condition,
                raw_metadata={"bucket": "outlier", "seed": seed},
            )
        )

        rejected_titles = [
            ("lot_or_bundle", f"{common_title} lot of 4"),
            ("proxy_or_custom", f"{common_title} custom proxy"),
            ("graded", f"{common_title} PSA 10 graded"),
            ("sealed", f"{price_key.set_name} booster pack sealed"),
            ("digital", f"{common_title} online code digital"),
        ]
        for index, (bucket, title) in enumerate(rejected_titles):
            sold_price = round(base_price + 1 + index, 2)
            shipping_price = round(rng.uniform(0, 2.5), 2)
            comps.append(
                SoldComp(
                    source_listing_id=f"{price_key.fingerprint}-{bucket}",
                    title=title,
                    sold_price=sold_price,
                    shipping_price=shipping_price,
                    total_price=round(sold_price + shipping_price, 2),
                    currency=currency,
                    sold_date=sold_date_base - timedelta(days=11 + index),
                    listing_url=f"{listing_prefix}/{bucket}",
                    condition_text="Mixed",
                    raw_metadata={"bucket": bucket, "seed": seed},
                )
            )

        return ProviderResult(
            provider_name=self.provider_name,
            marketplace=self.marketplace,
            provider_fingerprint=f"mock:{seed:x}",
            query_used=f"{price_key.card_name} {price_key.set_name} {price_key.collector_number}",
            comps=comps,
            raw_metadata={"seed": seed, "basePrice": base_price, "currency": currency},
        )
