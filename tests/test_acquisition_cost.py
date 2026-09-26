"""Acquisition cost stays separate from sold market value; never invent shipping."""
from __future__ import annotations

import unittest

from cardscanr_market_engine.international.acquisition_cost import (
    combine_acquisition_cost,
    estimate_shipping_from_observations,
    unavailable_shipping,
)


class AcquisitionCostTests(unittest.TestCase):
    def test_unavailable_shipping_is_not_zero(self) -> None:
        shipping = unavailable_shipping(source_market="US", destination_market="AU")
        self.assertEqual(shipping.status, "unavailable")
        self.assertIsNone(shipping.typical_shipping)
        self.assertEqual(shipping.sample_count, 0)

        acquisition = combine_acquisition_cost(
            market_value=84.30,
            market_value_currency="AUD",
            shipping=shipping,
        )
        self.assertEqual(acquisition.market_value, 84.30)
        self.assertIsNone(acquisition.acquisition_typical)
        self.assertFalse(acquisition.taxes_included)

    def test_no_market_value_is_not_zero(self) -> None:
        shipping = estimate_shipping_from_observations(
            [18.0, 19.0, 20.0, 21.0],
            source_market="US",
            destination_market="AU",
            currency="AUD",
        )
        acquisition = combine_acquisition_cost(
            market_value=None,
            market_value_currency="AUD",
            shipping=shipping,
        )
        self.assertIsNone(acquisition.market_value)
        self.assertIsNone(acquisition.acquisition_typical)

    def test_shipping_median_and_range(self) -> None:
        shipping = estimate_shipping_from_observations(
            [16.0, 18.0, 20.0, 24.0],
            source_market="US",
            destination_market="AU",
            currency="AUD",
        )
        self.assertEqual(shipping.status, "available")
        self.assertEqual(shipping.typical_shipping, 19.0)
        self.assertEqual(shipping.shipping_low, 16.0)
        self.assertEqual(shipping.shipping_high, 24.0)
        self.assertEqual(shipping.sample_count, 4)

        acquisition = combine_acquisition_cost(
            market_value=84.30,
            market_value_currency="AUD",
            shipping=shipping,
        )
        self.assertEqual(acquisition.acquisition_typical, 103.30)
        self.assertEqual(acquisition.acquisition_low, 100.30)
        self.assertEqual(acquisition.acquisition_high, 108.30)
        self.assertFalse(acquisition.taxes_included)

    def test_limited_shipping_sample(self) -> None:
        shipping = estimate_shipping_from_observations(
            [18.4, 19.0],
            source_market="US",
            destination_market="AU",
            currency="AUD",
        )
        self.assertEqual(shipping.status, "limited")
        self.assertEqual(shipping.notes, "limited_shipping_data")

    def test_empty_observations_unavailable(self) -> None:
        shipping = estimate_shipping_from_observations(
            [],
            source_market="US",
            destination_market="AU",
            currency="AUD",
        )
        self.assertEqual(shipping.status, "unavailable")
        self.assertIsNone(shipping.typical_shipping)

    def test_sold_value_not_contaminated_by_shipping(self) -> None:
        shipping = estimate_shipping_from_observations(
            [18.0, 19.0, 20.0],
            source_market="US",
            destination_market="AU",
            currency="AUD",
        )
        acquisition = combine_acquisition_cost(
            market_value=55.40,
            market_value_currency="USD",
            shipping=shipping,
        )
        self.assertEqual(acquisition.market_value, 55.40)
        self.assertNotEqual(acquisition.market_value, acquisition.acquisition_typical)


if __name__ == "__main__":
    unittest.main()
