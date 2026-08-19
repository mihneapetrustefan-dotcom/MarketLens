"""
test_check_registry_collisions.py
--------------------------------------
Unit tests for check_registry_collisions.py.

TESTING STRATEGY: two kinds of tests here —
1. Tests against the REAL registry, confirming it's currently clean
   (these are the ones that would actually fail CI if a future edit
   introduces a real problem).
2. Tests against small SYNTHETIC data, confirming the detection
   functions themselves work correctly, independent of whatever state
   the real registry happens to be in.
"""

import unittest

import check_registry_collisions as checker


class TestRealRegistryIsClean(unittest.TestCase):
    """These are the tests that matter in CI — they fail the moment a real collision is introduced."""

    def test_no_duplicate_canonical_names(self):
        self.assertEqual(checker.find_duplicate_canonical_names(), [])

    def test_no_exact_alias_collisions(self):
        self.assertEqual(checker.find_alias_collisions(), {})

    def test_no_case_insensitive_alias_collisions(self):
        self.assertEqual(checker.find_case_insensitive_alias_collisions(), {})

    def test_every_company_has_a_sector(self):
        self.assertEqual(checker.find_companies_missing_sector(), [])

    def test_run_all_checks_passes(self):
        self.assertTrue(checker.run_all_checks(verbose=False))

    def test_known_ticker_collision_is_reported_but_not_blocking(self):
        # EL: Electrica (BVB) and Estee Lauder (NYSE) — a real,
        # documented, non-blocking coincidence (see company_registry.py).
        collisions = checker.find_ticker_collisions()
        self.assertIn("EL", collisions)
        self.assertTrue(checker.run_all_checks(verbose=False))  # still passes overall


class TestDetectionLogicWithSyntheticData(unittest.TestCase):
    """Confirm the detection functions work correctly in isolation, using controlled fixtures."""

    def setUp(self):
        self._original_registry = checker.COMPANY_REGISTRY
        self._original_sector_map = checker.COMPANY_SECTOR_MAP

    def tearDown(self):
        checker.COMPANY_REGISTRY = self._original_registry
        checker.COMPANY_SECTOR_MAP = self._original_sector_map

    def test_detects_duplicate_canonical_name(self):
        checker.COMPANY_REGISTRY = [
            {"canonical_name": "Test Co", "aliases": ["Test Co"], "ticker": "TC1", "category": "stocks"},
            {"canonical_name": "Test Co", "aliases": ["Test Co Alt"], "ticker": "TC2", "category": "stocks"},
        ]
        self.assertEqual(checker.find_duplicate_canonical_names(), ["Test Co"])

    def test_detects_exact_alias_collision(self):
        checker.COMPANY_REGISTRY = [
            {"canonical_name": "Alpha Corp", "aliases": ["Shared Name"], "ticker": "A", "category": "stocks"},
            {"canonical_name": "Beta Corp", "aliases": ["Shared Name"], "ticker": "B", "category": "stocks"},
        ]
        collisions = checker.find_alias_collisions()
        self.assertIn("Shared Name", collisions)
        self.assertEqual(collisions["Shared Name"], {"Alpha Corp", "Beta Corp"})

    def test_detects_case_insensitive_collision_for_long_aliases(self):
        checker.COMPANY_REGISTRY = [
            {"canonical_name": "Alpha Corp", "aliases": ["Oracle"], "ticker": "A", "category": "stocks"},
            {"canonical_name": "Beta Corp", "aliases": ["ORACLE"], "ticker": "B", "category": "stocks"},
        ]
        collisions = checker.find_case_insensitive_alias_collisions()
        self.assertIn("oracle", collisions)

    def test_short_aliases_not_flagged_by_case_insensitive_check(self):
        checker.COMPANY_REGISTRY = [
            {"canonical_name": "Alpha Corp", "aliases": ["ABC"], "ticker": "A", "category": "stocks"},
            {"canonical_name": "Beta Corp", "aliases": ["abc"], "ticker": "B", "category": "stocks"},
        ]
        # Below the case-insensitive threshold (5 chars) — not flagged
        # by this check (though it WOULD be an exact-match collision
        # if identically-cased, which is a separate check).
        collisions = checker.find_case_insensitive_alias_collisions()
        self.assertEqual(collisions, {})

    def test_no_collision_for_distinct_aliases(self):
        checker.COMPANY_REGISTRY = [
            {"canonical_name": "Alpha Corp", "aliases": ["Alpha"], "ticker": "A", "category": "stocks"},
            {"canonical_name": "Beta Corp", "aliases": ["Beta"], "ticker": "B", "category": "stocks"},
        ]
        self.assertEqual(checker.find_alias_collisions(), {})
        self.assertEqual(checker.find_case_insensitive_alias_collisions(), {})

    def test_detects_ticker_collision(self):
        checker.COMPANY_REGISTRY = [
            {"canonical_name": "Alpha Corp", "aliases": ["Alpha"], "ticker": "XYZ", "category": "stocks"},
            {"canonical_name": "Beta Corp", "aliases": ["Beta"], "ticker": "XYZ", "category": "bvb"},
        ]
        collisions = checker.find_ticker_collisions()
        self.assertIn("XYZ", collisions)

    def test_detects_missing_sector(self):
        checker.COMPANY_REGISTRY = [
            {"canonical_name": "Unmapped Co", "aliases": ["Unmapped Co"], "ticker": "U", "category": "stocks"},
        ]
        checker.COMPANY_SECTOR_MAP = {}
        self.assertEqual(checker.find_companies_missing_sector(), ["Unmapped Co"])

    def test_run_all_checks_fails_on_duplicate_name(self):
        checker.COMPANY_REGISTRY = [
            {"canonical_name": "Dup Co", "aliases": ["Dup Co"], "ticker": "D1", "category": "stocks"},
            {"canonical_name": "Dup Co", "aliases": ["Dup Co 2"], "ticker": "D2", "category": "stocks"},
        ]
        checker.COMPANY_SECTOR_MAP = {"Dup Co": "Technology"}
        self.assertFalse(checker.run_all_checks(verbose=False))

    def test_run_all_checks_ignores_ticker_only_collision(self):
        checker.COMPANY_REGISTRY = [
            {"canonical_name": "Alpha Corp", "aliases": ["Alpha"], "ticker": "XYZ", "category": "stocks"},
            {"canonical_name": "Beta Corp", "aliases": ["Beta"], "ticker": "XYZ", "category": "bvb"},
        ]
        checker.COMPANY_SECTOR_MAP = {"Alpha Corp": "Technology", "Beta Corp": "Financial Services"}
        self.assertTrue(checker.run_all_checks(verbose=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
