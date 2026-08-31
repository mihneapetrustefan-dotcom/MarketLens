"""
tests/portfolio/test_exposure.py
-------------------------------------
Tests for exposure aggregation and instrument classification.

The load-bearing behaviour here is that exposure which cannot be
attributed is COUNTED rather than dropped. A sector breakdown that
silently omits a third of the book looks complete and is not, and a
sector cap evaluated against it is not a cap at all.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.portfolio_models import ExposureDimension
from src.portfolio.exposure import ExposureEngine, InstrumentClassifier
from tests.portfolio.helpers import (
    AS_OF, add_instrument, make_connection, make_snapshot, make_valuation,
)


class TestInstrumentClassifier(unittest.TestCase):
    def setUp(self):
        self.conn = make_connection()
        add_instrument(self.conn, "i-a", "AAA", "technology")
        add_instrument(self.conn, "i-b", "BBB", "energy", asset_class="stock")
        add_instrument(self.conn, "i-c", "CCC", None, asset_class="crypto")
        self.classifier = InstrumentClassifier(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_resolves_sector_through_the_canonical_chain(self):
        result = self.classifier.classify(["i-a"])["i-a"]
        self.assertEqual(result.sector_id, "technology")
        self.assertEqual(result.ticker, "AAA")
        self.assertEqual(result.asset_class, "stock")

    def test_instrument_without_a_sector_still_resolves_asset_class(self):
        result = self.classifier.classify(["i-c"])["i-c"]
        self.assertIsNone(result.sector_id)
        self.assertEqual(result.asset_class, "crypto")

    def test_unknown_instrument_returns_an_empty_classification_not_an_error(self):
        result = self.classifier.classify(["i-nowhere"])["i-nowhere"]
        self.assertFalse(result.is_known)

    def test_results_are_cached_across_calls(self):
        self.classifier.classify(["i-a"])
        self.conn.execute("DELETE FROM instruments")
        self.conn.commit()
        # Served from cache, so a second call does not lose what it knew.
        self.assertEqual(self.classifier.classify(["i-a"])["i-a"].sector_id, "technology")

    def test_missing_canonical_tables_degrade_to_unknown(self):
        bare = make_connection()
        bare.execute("DROP TABLE instruments")
        result = InstrumentClassifier(bare).classify(["i-a"])["i-a"]
        self.assertFalse(result.is_known)
        bare.close()


class TestExposureBreakdown(unittest.TestCase):
    def setUp(self):
        self.conn = make_connection()
        add_instrument(self.conn, "i-a", "AAA", "technology")
        add_instrument(self.conn, "i-b", "BBB", "technology")
        add_instrument(self.conn, "i-c", "CCC", "energy")
        self.engine = ExposureEngine(InstrumentClassifier(self.conn))

    def tearDown(self):
        self.conn.close()

    def test_sector_buckets_sum_positions_in_the_same_sector(self):
        snapshot = make_snapshot([
            make_valuation("i-a", 10.0, 10.0),   # 100
            make_valuation("i-b", 10.0, 10.0),   # 100
            make_valuation("i-c", 10.0, 10.0),   # 100
        ], cash=700.0)
        breakdown = self.engine.breakdown(snapshot, ExposureDimension.SECTOR)
        technology = breakdown.bucket_for("technology")
        self.assertEqual(technology.exposure, 200.0)
        self.assertEqual(technology.position_count, 2)
        self.assertAlmostEqual(technology.weight, 0.20)

    def test_short_adds_to_gross_bucket_exposure_and_to_short_side(self):
        snapshot = make_snapshot([
            make_valuation("i-a", 10.0, 10.0),
            make_valuation("i-b", -10.0, 10.0),
        ], cash=1000.0)
        technology = self.engine.breakdown(
            snapshot, ExposureDimension.SECTOR).bucket_for("technology")
        self.assertEqual(technology.exposure, 200.0)
        self.assertEqual(technology.long_exposure, 100.0)
        self.assertEqual(technology.short_exposure, 100.0)
        self.assertEqual(technology.net_exposure, 0.0)

    def test_unclassifiable_exposure_is_counted_separately(self):
        snapshot = make_snapshot([
            make_valuation("i-a", 10.0, 10.0),
            make_valuation("i-unknown", 10.0, 10.0),
        ], cash=800.0)
        breakdown = self.engine.breakdown(snapshot, ExposureDimension.SECTOR)
        self.assertEqual(breakdown.unclassified_exposure, 100.0)
        self.assertEqual(breakdown.unclassified_count, 1)
        self.assertFalse(breakdown.is_complete)

    def test_fully_classified_breakdown_is_complete(self):
        snapshot = make_snapshot([make_valuation("i-a", 10.0, 10.0)], cash=900.0)
        self.assertTrue(
            self.engine.breakdown(snapshot, ExposureDimension.SECTOR).is_complete)

    def test_total_exposure_includes_the_unclassified_remainder(self):
        snapshot = make_snapshot([
            make_valuation("i-a", 10.0, 10.0),
            make_valuation("i-unknown", 5.0, 10.0),
        ], cash=850.0)
        breakdown = self.engine.breakdown(snapshot, ExposureDimension.SECTOR)
        self.assertEqual(breakdown.total_exposure, 150.0)

    def test_weights_are_none_when_equity_is_not_positive(self):
        snapshot = make_snapshot([
            make_valuation("i-a", 10.0, 10.0),
            make_valuation("i-c", -10.0, 10.0),
        ], cash=0.0)
        breakdown = self.engine.breakdown(snapshot, ExposureDimension.SECTOR)
        for bucket in breakdown.buckets:
            self.assertIsNone(bucket.weight)

    def test_currency_dimension_uses_the_position_currency(self):
        snapshot = make_snapshot([
            make_valuation("i-a", 10.0, 10.0, currency="USD"),
            make_valuation("i-c", 10.0, 10.0, currency="EUR"),
        ], cash=800.0)
        breakdown = self.engine.breakdown(snapshot, ExposureDimension.CURRENCY)
        self.assertEqual({b.key for b in breakdown.buckets}, {"USD", "EUR"})

    def test_buckets_are_ordered_by_exposure_descending(self):
        snapshot = make_snapshot([
            make_valuation("i-a", 1.0, 10.0),
            make_valuation("i-c", 50.0, 10.0),
        ], cash=1000.0)
        breakdown = self.engine.breakdown(snapshot, ExposureDimension.SECTOR)
        self.assertEqual(breakdown.buckets[0].key, "energy")

    def test_empty_portfolio_yields_an_empty_breakdown(self):
        breakdown = self.engine.breakdown(make_snapshot([], cash=100.0),
                                          ExposureDimension.SECTOR)
        self.assertEqual(breakdown.buckets, [])
        self.assertTrue(breakdown.is_complete)

    def test_all_breakdowns_covers_every_dimension(self):
        snapshot = make_snapshot([make_valuation("i-a", 10.0, 10.0)], cash=900.0)
        result = self.engine.all_breakdowns(snapshot)
        self.assertEqual(set(result), set(ExposureDimension))


if __name__ == "__main__":
    unittest.main()
