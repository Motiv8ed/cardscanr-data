from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import random

from ..models import ProviderRequest, ProviderResult, SoldComp


class MockMarketCompsProvider:
    provider_name = "mock"
    marketplace_name = "ebay"

    def fetch_comps(self, request: ProviderRequest) -> ProviderResult:
        price_key = request.price_key
        seed_source = (
            f"{price_key.fingerprint}|{request.provider_marketplace_id}|"
            f"{request.market_country}|{request.currency}|{request.marketplace}"
        )
        seed = int(hashlib.sha256(seed_source.encode("utf-8")).hexdigest()[:16], 16)
        rng = random.Random(seed)
        base_price = round(15 + (seed % 5000) / 100, 2)
        currency = (request.currency or price_key.currency or "usd").upper()
        sold_date_base = datetime(2026, 5, 20, tzinfo=timezone.utc)
        listing_prefix = f"https://www.{request.provider_domain}/itm/mock-{price_key.fingerprint}"
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
                    listing_url=f"{listing_prefix}-good-{index}",
                    condition_text=raw_condition,
                    raw_metadata={
                        "bucket": "good",
                        "seed": seed,
                        "index": index,
                        "providerDomain": request.provider_domain,
                        "searchLocale": request.search_locale,
                        "marketDisplayName": request.display_name,
                    },
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
                listing_url=f"{listing_prefix}-outlier",
                condition_text=raw_condition,
                raw_metadata={
                    "bucket": "outlier",
                    "seed": seed,
                    "providerDomain": request.provider_domain,
                    "searchLocale": request.search_locale,
                    "marketDisplayName": request.display_name,
                },
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
                    listing_url=f"{listing_prefix}-{bucket}",
                    condition_text="Mixed",
                    raw_metadata={
                        "bucket": bucket,
                        "seed": seed,
                        "providerDomain": request.provider_domain,
                        "searchLocale": request.search_locale,
                        "marketDisplayName": request.display_name,
                    },
                )
            )

        return ProviderResult(
            provider_name=self.provider_name,
            marketplace=request.provider_marketplace_id,
            provider_fingerprint=f"mock:{request.provider_marketplace_id}:{seed:x}",
            query_used=f"{price_key.card_name} {price_key.set_name} {price_key.collector_number}",
            comps=comps,
            raw_metadata={
                "seed": seed,
                "basePrice": base_price,
                "currency": currency,
                "marketCountry": request.market_country,
                "marketplace": request.marketplace,
                "providerMarketplaceId": request.provider_marketplace_id,
                "providerDomain": request.provider_domain,
                "searchLocale": request.search_locale,
                "displayName": request.display_name,
            },
        )
