"""Unit tests for scraper.matcher - offline."""

import unittest

from scraper.matcher import CandidatePackage, SkuDefinition, match_package, normalize_name

SKUS = [
    SkuDefinition(id=1, sku_code="mlbb-94", display_name="86 Diamonds + 8 Bonus", base_units=86, bonus_units=8),
    SkuDefinition(id=2, sku_code="mlbb-70", display_name="70 Diamonds", base_units=70, bonus_units=0),
    SkuDefinition(id=3, sku_code="mlbb-275", display_name="219 + 56 Bonus", base_units=219, bonus_units=56),
    SkuDefinition(id=4, sku_code="mlbb-94b", display_name="94 Diamonds (alt split)", base_units=94, bonus_units=0),
]

# Unique-effective-units view of the same catalog (no 86+8 vs 94+0 overlap).
UNIQUE_SKUS = [s for s in SKUS if s.id != 4]


class TestMatchPackage(unittest.TestCase):
    def test_exact_effective_units_match(self):
        package = CandidatePackage("86 Dm + 8 Bonus", base_units=86, bonus_units=8)
        result = match_package(package, UNIQUE_SKUS)
        self.assertIsNotNone(result.sku)
        self.assertEqual(result.sku.id, 1)

    def test_bonusless_competitor_package(self):
        package = CandidatePackage("70 Diamonds", base_units=70, bonus_units=0)
        result = match_package(package, UNIQUE_SKUS)
        self.assertEqual(result.sku.id, 2)

    def test_ambiguous_match_returns_none(self):
        # Effective units 94 exist as both 86+8 and 94+0 -> ambiguous.
        package = CandidatePackage("94 Diamonds", base_units=94, bonus_units=0)
        result = match_package(package, SKUS)
        self.assertIsNone(result.sku)
        self.assertTrue(result.ambiguous)
        self.assertEqual(len(result.candidates), 2)

    def test_no_match(self):
        package = CandidatePackage("1000 Diamonds", base_units=1000, bonus_units=0)
        result = match_package(package, SKUS)
        self.assertIsNone(result.sku)
        self.assertFalse(result.ambiguous)

    def test_manual_override_wins(self):
        package = CandidatePackage("70 Diamonds", base_units=70, bonus_units=0)
        result = match_package(package, SKUS, manual_sku_id=3)
        self.assertEqual(result.sku.id, 3)

    def test_manual_override_to_unknown_sku(self):
        package = CandidatePackage("70 Diamonds", base_units=70, bonus_units=0)
        result = match_package(package, SKUS, manual_sku_id=999)
        self.assertIsNone(result.sku)


class TestPassMatching(unittest.TestCase):
    def test_pass_matches_by_name(self):
        skus = [
            SkuDefinition(id=10, sku_code="mlbb-weekly", display_name="Weekly Diamond Pass",
                          base_units=0, bonus_units=0),
        ]
        package = CandidatePackage("Weekly Diamond Pass", base_units=0, bonus_units=0)
        result = match_package(package, skus)
        self.assertIsNotNone(result.sku)
        self.assertEqual(result.sku.id, 10)

    def test_pass_name_normalization(self):
        self.assertEqual(normalize_name("Weekly  Diamond Pass!"), "weeklydiamondpass")
        self.assertEqual(normalize_name("weekly diamond pass"), "weeklydiamondpass")

    def test_pass_does_not_match_unit_skus(self):
        package = CandidatePackage("Weekly Diamond Pass", base_units=0, bonus_units=0)
        result = match_package(package, UNIQUE_SKUS)
        self.assertIsNone(result.sku)

    def test_pass_no_match_leaves_unmatched(self):
        skus = [
            SkuDefinition(id=10, sku_code="mlbb-weekly", display_name="Weekly Diamond Pass",
                          base_units=0, bonus_units=0),
        ]
        package = CandidatePackage("Monthly Diamond Pass", base_units=0, bonus_units=0)
        result = match_package(package, skus)
        self.assertIsNone(result.sku)


class TestCandidatePackage(unittest.TestCase):
    def test_effective_units(self):
        package = CandidatePackage("219 + 56 Bonus", base_units=219, bonus_units=56)
        self.assertEqual(package.effective_units, 275)


class TestSkuDefinition(unittest.TestCase):
    def test_effective_units(self):
        sku = SkuDefinition(id=None, sku_code="x", display_name="x", base_units=86, bonus_units=8)
        self.assertEqual(sku.effective_units, 94)


if __name__ == "__main__":
    unittest.main()
