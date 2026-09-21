"""Unit tests for scraper.pricing - offline, no network, no browser."""

import unittest
from decimal import Decimal

from scraper.pricing import (
    compare_status,
    effective_unit_price,
    effective_units,
    is_undercutting,
    undercut_pct,
)


class TestEffectiveUnits(unittest.TestCase):
    def test_base_plus_bonus(self):
        self.assertEqual(effective_units(86, 8), 94)

    def test_no_bonus(self):
        self.assertEqual(effective_units(70, 0), 70)


class TestEffectiveUnitPrice(unittest.TestCase):
    def test_known_value(self):
        # 25000 / 94 = 265.957446... -> 265.9574 (4dp, half-up)
        result = effective_unit_price(25000, 86, 8)
        self.assertEqual(result, Decimal("265.9574"))

    def test_no_bonus(self):
        result = effective_unit_price(12000, 70, 0)
        self.assertEqual(result, Decimal("171.4286"))

    def test_accepts_decimal_string(self):
        result = effective_unit_price("25000", 86, 8)
        self.assertEqual(result, Decimal("265.9574"))

    def test_zero_units_returns_none(self):
        self.assertIsNone(effective_unit_price(25000, 0, 0))

    def test_none_price_returns_none(self):
        self.assertIsNone(effective_unit_price(None, 86, 8))

    def test_missing_data_never_returns_zero(self):
        self.assertIsNone(effective_unit_price(None, 0, 0))


class TestCompareStatus(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(compare_status(Decimal("100"), Decimal("100")), "price_match")

    def test_one_rupiah_difference_is_not_a_match(self):
        self.assertEqual(compare_status(Decimal("100"), Decimal("99.99")), "competitor_cheaper")
        self.assertEqual(compare_status(Decimal("100"), Decimal("100.01")), "dana_cheaper")

    def test_missing_data(self):
        self.assertEqual(compare_status(None, Decimal("100")), "unknown")
        self.assertEqual(compare_status(Decimal("100"), None), "unknown")


class TestUndercut(unittest.TestCase):
    def test_ten_percent_is_undercutting(self):
        # 100 -> 90 is exactly the 10% threshold: flagged.
        self.assertTrue(is_undercutting(Decimal("100"), Decimal("90")))

    def test_below_threshold_not_flagged(self):
        self.assertFalse(is_undercutting(Decimal("100"), Decimal("91")))

    def test_deep_discount_flagged(self):
        self.assertTrue(is_undercutting(Decimal("265.9574"), Decimal("230.85")))

    def test_competitor_more_expensive(self):
        self.assertFalse(is_undercutting(Decimal("100"), Decimal("120")))

    def test_pct_value(self):
        self.assertEqual(undercut_pct(Decimal("100"), Decimal("85")), Decimal("15.00"))

    def test_pct_none_when_more_expensive(self):
        self.assertIsNone(undercut_pct(Decimal("100"), Decimal("110")))

    def test_custom_threshold(self):
        self.assertTrue(is_undercutting(Decimal("100"), Decimal("96"), threshold_pct=Decimal("4")))


if __name__ == "__main__":
    unittest.main()
