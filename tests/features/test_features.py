"""
tests/features/test_features.py
------------------------------------
Tests for the Phase 8 feature layer.

Includes all SEVEN adversarial leakage cases from spec §59, plus
numerical validation against known mathematical definitions (§60).
"""

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.feature_models import (
    FeatureDefinition, FeatureSet, FeatureStatus, MissingPolicy,
    ComputationCost, TimestampSemantics, FeatureQuality,
)
from src.domain.research_models import FeatureNamespace
from src.features.engine import (
    FeatureRegistry, FeatureEngine, FeatureContext, CircularDependencyError,
)
from src.features.library import (
    build_default_registry, build_default_feature_sets,
    zscore_point_in_time, percentile_rank_point_in_time, winsorize, cross_sectional_rank,
)

CUTOFF = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)


class Candle:
    def __init__(self, timestamp, price, volume=1_000_000):
        self.timestamp = timestamp
        self.price = price
        self.volume = volume


class Event:
    def __init__(self, publication_time, event_type="earnings"):
        self.publication_time = publication_time
        self.event_type = event_type


class Article:
    def __init__(self, published_at, source_name="Reuters", sentiment_score=0.5):
        self.published_at = published_at
        self.source_name = source_name
        self.sentiment_score = sentiment_score


def candles(count=60, start_price=100.0, step=1.0, volume=1_000_000, end=CUTOFF):
    """A regular daily series ENDING at `end`."""
    return [Candle(end - timedelta(days=count - 1 - i), start_price + step * i, volume)
            for i in range(count)]


def context(**overrides):
    base = dict(cutoff=CUTOFF, instrument_id="nvda", candles=candles(), events=[], articles=[])
    base.update(overrides)
    return FeatureContext(**base)


class TestAdversarialLeakage(unittest.TestCase):
    """Spec §59 — all seven cases. Each passes only if future data is REJECTED."""

    def setUp(self):
        self.registry = build_default_registry()
        self.engine = FeatureEngine(self.registry)

    def test_case_1_data_after_the_event_is_not_visible(self):
        past = candles(60, 100.0, 0.0)
        future = [Candle(CUTOFF + timedelta(days=i), 999.0) for i in range(1, 6)]
        ctx = context(candles=past + future)

        self.assertTrue(all(c.timestamp <= CUTOFF for c in ctx.known_candles()))
        self.assertNotIn(999.0, ctx.prices())

    def test_case_2_rolling_feature_excludes_future_observations(self):
        """A 20-day return must not reach past the cutoff for its endpoint."""
        past = candles(60, 100.0, 0.0)              # perfectly flat -> return 0
        future = [Candle(CUTOFF + timedelta(days=1), 500.0)]
        ctx = context(candles=past + future)

        value = self.engine.compute_one("market.return_20d", ctx)
        self.assertAlmostEqual(value.value, 0.0, places=6)   # NOT influenced by the 500 spike

    def test_case_3_cross_sectional_rank_uses_only_the_supplied_universe(self):
        """
        A historical universe must not be widened with instruments that
        did not exist then — the ranking function only ever sees what
        the caller passes.
        """
        historical_universe = {"a": 0.01, "b": 0.02, "c": 0.03}
        rank_then = cross_sectional_rank(0.02, historical_universe, "b")

        widened = dict(historical_universe, newly_listed=0.99)
        rank_with_future_member = cross_sectional_rank(0.02, widened, "b")
        self.assertNotEqual(rank_then, rank_with_future_member)

    def test_case_4_normalization_parameters_come_only_from_history(self):
        """Fitting mean/std on the full dataset would leak; the z-score takes explicit history."""
        history = [0.01, 0.02, 0.03]
        without_future = zscore_point_in_time(0.02, history)
        with_future = zscore_point_in_time(0.02, history + [10.0])
        self.assertNotEqual(without_future, with_future)

        import inspect
        signature = inspect.signature(zscore_point_in_time)
        self.assertIn("history", signature.parameters)   # history is REQUIRED, not optional/global

    def test_case_5_future_events_never_enter_count_or_recency_features(self):
        past_event = Event(CUTOFF - timedelta(days=3))
        future_event = Event(CUTOFF + timedelta(days=1))
        ctx = context(events=[past_event, future_event], metadata={"event_type": "earnings"})

        count = self.engine.compute_one("event.count_30d", ctx)
        recency = self.engine.compute_one("event.days_since_last", ctx)
        self.assertEqual(count.value, 1)                          # the future event is invisible
        self.assertAlmostEqual(recency.value, 3.0, places=2)      # measured from the PAST event

    def test_case_6_no_feature_can_read_the_current_events_outcome(self):
        """
        Structural: FeatureContext exposes no accessor for post-cutoff
        data at all, so the current event's own future reaction cannot
        become one of its features.
        """
        forbidden = {"outcome", "outcomes", "future", "label", "labels",
                      "future_candles", "outcome_after", "post_event"}
        self.assertEqual(forbidden & set(dir(FeatureContext)), set())

    def test_case_7_restated_values_do_not_silently_replace_historical_ones(self):
        """
        A restated fundamental carries a later as_of, so a
        cutoff-bounded lookup returns the ORIGINAL value, not the
        restatement.
        """
        from src.research.temporal import latest_known_at
        revisions = [
            {"value": 100, "as_of": CUTOFF - timedelta(days=60)},   # originally reported
            {"value": 118, "as_of": CUTOFF + timedelta(days=30)},   # restated later
        ]
        result = latest_known_at(revisions, CUTOFF, lambda r: r["as_of"])
        self.assertEqual(result["value"], 100)

    def test_computed_feature_is_stamped_at_the_cutoff_never_later(self):
        ctx = context()
        value = self.engine.compute_one("market.return_5d", ctx)
        self.assertEqual(value.as_of, CUTOFF)


class TestNumericalValidation(unittest.TestCase):
    """Spec §60 — validate against known mathematical definitions."""

    def setUp(self):
        self.registry = build_default_registry()
        self.engine = FeatureEngine(self.registry)

    def test_5d_return_matches_the_documented_formula(self):
        """close[t]/close[t-5] - 1, verified by hand."""
        series = candles(10, 100.0, 0.0)
        for i, price in enumerate([100, 101, 102, 103, 104, 110]):
            series[-6 + i].price = float(price)
        ctx = context(candles=series)
        value = self.engine.compute_one("market.return_5d", ctx)
        self.assertAlmostEqual(value.value, 110 / 100 - 1, places=6)

    def test_insufficient_lookback_returns_none_not_a_guess(self):
        ctx = context(candles=candles(3, 100.0, 1.0))
        self.assertIsNone(self.engine.compute_one("market.return_20d", ctx).value)

    def test_rsi_of_a_monotonic_rise_is_100(self):
        """No losing sessions -> avg_loss = 0 -> RSI at its maximum."""
        ctx = context(candles=candles(30, 100.0, 1.0))
        self.assertAlmostEqual(self.engine.compute_one("technical.rsi_14", ctx).value, 100.0, places=2)

    def test_rsi_of_a_flat_series_is_neutral(self):
        ctx = context(candles=candles(30, 100.0, 0.0))
        self.assertAlmostEqual(self.engine.compute_one("technical.rsi_14", ctx).value, 50.0, places=2)

    def test_relative_volume_matches_the_documented_ratio(self):
        series = candles(30, 100.0, 0.0, volume=10_000_000)
        series[-1].volume = 35_000_000
        ctx = context(candles=series)
        self.assertAlmostEqual(self.engine.compute_one("liquidity.relative_volume_20d", ctx).value, 3.5, places=3)

    def test_zero_volatility_on_a_flat_series(self):
        ctx = context(candles=candles(30, 100.0, 0.0))
        self.assertAlmostEqual(self.engine.compute_one("volatility.realized_20d", ctx).value, 0.0, places=8)

    def test_drawdown_is_zero_at_a_new_high(self):
        ctx = context(candles=candles(60, 100.0, 1.0))
        self.assertAlmostEqual(self.engine.compute_one("market.drawdown_60d", ctx).value, 0.0, places=6)

    def test_drawdown_is_negative_after_a_decline(self):
        series = candles(60, 100.0, 1.0)
        series[-1].price = 50.0
        ctx = context(candles=series)
        self.assertLess(self.engine.compute_one("market.drawdown_60d", ctx).value, 0)


class TestEventAndNewsFeatures(unittest.TestCase):
    def setUp(self):
        self.registry = build_default_registry()
        self.engine = FeatureEngine(self.registry)

    def test_event_count_of_zero_is_semantic_not_missing(self):
        """Zero events genuinely IS zero — this is the one case where 0 is correct."""
        value = self.engine.compute_one("event.count_7d", context(events=[]))
        self.assertEqual(value.value, 0)

    def test_novelty_is_1_for_a_first_occurrence(self):
        ctx = context(events=[], metadata={"event_type": "acquisition"})
        self.assertAlmostEqual(self.engine.compute_one("event.novelty_365d", ctx).value, 1.0, places=6)

    def test_novelty_decays_as_the_event_type_becomes_routine(self):
        repeated = [Event(CUTOFF - timedelta(days=30 * i), "earnings") for i in range(1, 5)]
        ctx = context(events=repeated, metadata={"event_type": "earnings"})
        self.assertLess(self.engine.compute_one("event.novelty_365d", ctx).value, 0.3)

    def test_source_diversity_is_1_when_every_article_is_from_a_different_outlet(self):
        articles = [Article(CUTOFF - timedelta(days=1), f"Source {i}") for i in range(5)]
        self.assertAlmostEqual(self.engine.compute_one("news.source_diversity_7d",
                                                        context(articles=articles)).value, 1.0, places=6)

    def test_source_diversity_is_low_when_one_story_is_echoed(self):
        articles = [Article(CUTOFF - timedelta(days=1), "Reuters") for _ in range(10)]
        self.assertAlmostEqual(self.engine.compute_one("news.source_diversity_7d",
                                                        context(articles=articles)).value, 0.1, places=6)

    def test_sentiment_dispersion_distinguishes_mixed_from_neutral(self):
        mixed = [Article(CUTOFF - timedelta(hours=1), "A", 1.0), Article(CUTOFF - timedelta(hours=2), "B", -1.0)]
        neutral = [Article(CUTOFF - timedelta(hours=1), "A", 0.0), Article(CUTOFF - timedelta(hours=2), "B", 0.0)]

        mixed_mean = self.engine.compute_one("sentiment.mean_7d", context(articles=mixed)).value
        mixed_dispersion = self.engine.compute_one("sentiment.dispersion_7d", context(articles=mixed)).value
        self.engine.invalidate()
        neutral_dispersion = self.engine.compute_one("sentiment.dispersion_7d", context(articles=neutral)).value

        self.assertAlmostEqual(mixed_mean, 0.0, places=6)      # identical mean...
        self.assertGreater(mixed_dispersion, neutral_dispersion)   # ...but clearly different dispersion


class TestPeerFeatures(unittest.TestCase):
    def setUp(self):
        self.registry = build_default_registry()
        self.engine = FeatureEngine(self.registry)

    def test_peer_relative_return_subtracts_the_peer_median(self):
        own = candles(10, 100.0, 0.0)
        for i, price in enumerate([100, 100, 100, 100, 100, 110]):
            own[-6 + i].price = float(price)
        flat_peer = candles(10, 50.0, 0.0)
        ctx = context(candles=own, peer_candles={"amd": flat_peer, "intc": flat_peer})

        value = self.engine.compute_one("peer.relative_return_5d", ctx)
        self.assertAlmostEqual(value.value, 0.10, places=4)   # own +10%, peers flat

    def test_peer_features_return_none_without_peers(self):
        self.assertIsNone(self.engine.compute_one("peer.relative_return_5d", context()).value)

    def test_peer_candles_are_also_cutoff_bounded(self):
        peer = candles(10, 50.0, 0.0) + [Candle(CUTOFF + timedelta(days=1), 999.0)]
        ctx = context(peer_candles={"amd": peer})
        self.assertTrue(all(c.timestamp <= CUTOFF for c in ctx.known_peer_candles("amd")))


class TestTransformations(unittest.TestCase):
    """Spec §28, §30, §32."""

    def test_zscore_needs_at_least_two_history_points(self):
        self.assertIsNone(zscore_point_in_time(0.02, [0.01]))

    def test_zscore_returns_none_on_zero_variance(self):
        self.assertIsNone(zscore_point_in_time(0.02, [0.01, 0.01, 0.01]))

    def test_percentile_rank_is_bounded(self):
        value = percentile_rank_point_in_time(0.05, [0.01, 0.02, 0.03, 0.04])
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 1.0)

    def test_winsorize_clips_rather_than_deletes(self):
        history = [0.01] * 98 + [0.5, -0.5]
        clipped = winsorize(0.9, history, 1.0, 99.0)
        self.assertIsNotNone(clipped)        # the extreme observation SURVIVES
        self.assertLess(clipped, 0.9)         # but its leverage is limited

    def test_winsorize_leaves_ordinary_values_untouched(self):
        history = [0.01, 0.02, 0.03, 0.04, 0.05]
        self.assertAlmostEqual(winsorize(0.03, history), 0.03, places=6)

    def test_cross_sectional_rank_handles_a_tiny_universe(self):
        self.assertIsNone(cross_sectional_rank(0.02, {"a": 0.01}, "a"))


class TestRegistryAndVersioning(unittest.TestCase):
    """Spec §33, §34, §35."""

    def setUp(self):
        self.registry = FeatureRegistry()

    def _definition(self, feature_id="x", version="v1", formula="a+b", dependencies=None):
        return FeatureDefinition(
            feature_id=feature_id, name=feature_id, namespace=FeatureNamespace.MARKET,
            version=version, formula=formula, dependencies=dependencies or [],
            compute=lambda ctx: 1.0)

    def test_editing_an_active_features_formula_is_refused(self):
        self.registry.register(self._definition(formula="a+b"))
        with self.assertRaises(ValueError) as caught:
            self.registry.register(self._definition(formula="a*b"))
        self.assertIn("Create a new version", str(caught.exception))

    def test_a_new_version_can_coexist_after_deprecating_the_old_one(self):
        self.registry.register(self._definition("x", "v1", "a+b"))
        self.registry.deprecate("x")
        self.registry.register(self._definition("x", "v2", "a*b"))
        self.assertEqual(self.registry.get("x").version, "v2")

    def test_circular_dependencies_are_caught_at_registration(self):
        self.registry.register(self._definition("a", dependencies=["b"]))
        with self.assertRaises(CircularDependencyError):
            self.registry.register(self._definition("b", dependencies=["a"]))

    def test_resolution_order_places_dependencies_first(self):
        self.registry.register(self._definition("base"))
        self.registry.register(self._definition("derived", dependencies=["base"]))
        order = self.registry.resolution_order(["derived"])
        self.assertLess(order.index("base"), order.index("derived"))

    def test_downstream_lookup_finds_dependents(self):
        self.registry.register(self._definition("base"))
        self.registry.register(self._definition("derived", dependencies=["base"]))
        self.assertEqual(self.registry.downstream_of("base"), ["derived"])

    def test_lineage_is_queryable_and_complete(self):
        registry = build_default_registry()
        lineage = registry.lineage("market.return_5d")
        self.assertIn("close[t]", lineage["formula"])
        self.assertEqual(lineage["namespace"], "market")
        self.assertIn("downstream", lineage)

    def test_qualified_id_pins_the_version(self):
        definition = self._definition("x", "v2")
        self.assertIn("@v2", definition.qualified_id)

    def test_feature_set_fingerprint_changes_with_membership(self):
        a = FeatureSet(feature_set_id="s", name="S", feature_ids=["f1"], created_at=CUTOFF)
        b = FeatureSet(feature_set_id="s", name="S", feature_ids=["f1", "f2"], created_at=CUTOFF)
        self.assertNotEqual(a.fingerprint(), b.fingerprint())


class TestComputationEngine(unittest.TestCase):
    """Spec §36, §47, §48."""

    def setUp(self):
        self.registry = build_default_registry()
        self.engine = FeatureEngine(self.registry)

    def test_feature_set_computes_every_member(self):
        feature_set = build_default_feature_sets(CUTOFF)["market_baseline_v1"]
        results = self.engine.compute_set(feature_set, context())
        self.assertEqual(len(results), len(feature_set.feature_ids))

    def test_cache_avoids_recomputation(self):
        ctx = context()
        self.engine.compute_one("market.return_5d", ctx)
        hits_before = self.engine.cache_hits
        self.engine.compute_one("market.return_5d", ctx)
        self.assertEqual(self.engine.cache_hits, hits_before + 1)

    def test_cache_key_includes_the_feature_version(self):
        """A v2 must never be served a value computed by v1."""
        registry = FeatureRegistry()
        registry.register(FeatureDefinition(
            feature_id="f", name="f", namespace=FeatureNamespace.MARKET, version="v1",
            formula="1", compute=lambda ctx: 1.0))
        engine = FeatureEngine(registry)
        ctx = context()
        self.assertEqual(engine.compute_one("f", ctx).value, 1.0)

        registry.deprecate("f")
        registry.register(FeatureDefinition(
            feature_id="f", name="f", namespace=FeatureNamespace.MARKET, version="v2",
            formula="2", compute=lambda ctx: 2.0))
        self.assertEqual(engine.compute_one("f", ctx).value, 2.0)   # NOT the cached 1.0

    def test_invalidating_a_feature_also_clears_its_downstream(self):
        registry = FeatureRegistry()
        registry.register(FeatureDefinition(feature_id="base", name="base",
                                             namespace=FeatureNamespace.MARKET, formula="1",
                                             compute=lambda ctx: 1.0))
        registry.register(FeatureDefinition(feature_id="derived", name="derived",
                                             namespace=FeatureNamespace.MARKET, formula="base*2",
                                             dependencies=["base"], compute=lambda ctx: 2.0))
        engine = FeatureEngine(registry)
        ctx = context()
        engine.compute_one("derived", ctx)
        self.assertGreaterEqual(engine.invalidate("base"), 1)

    def test_a_failing_feature_does_not_break_the_batch(self):
        registry = build_default_registry()
        registry.register(FeatureDefinition(
            feature_id="market.broken", name="broken", namespace=FeatureNamespace.MARKET,
            formula="boom", compute=lambda ctx: 1 / 0))
        engine = FeatureEngine(registry)
        feature_set = FeatureSet(feature_set_id="s", name="S", version="v1",
                                  feature_ids=["market.broken", "market.return_5d"], created_at=CUTOFF)
        results = engine.compute_set(feature_set, context())
        self.assertIsNone(results["market.broken"].value)
        self.assertIsNotNone(results["market.return_5d"].value)
        self.assertEqual(engine.computation_failures, 1)

    def test_batch_computation_over_many_observations(self):
        feature_set = build_default_feature_sets(CUTOFF)["market_baseline_v1"]
        contexts = [FeatureContext(cutoff=CUTOFF - timedelta(days=i), instrument_id="nvda",
                                    candles=candles(60, 100.0, 1.0, end=CUTOFF - timedelta(days=i)))
                     for i in range(5)]
        results = self.engine.compute_batch(feature_set, contexts)
        self.assertEqual(len(results), 5)

    def test_cache_hit_ratio_is_reported(self):
        ctx = context()
        self.engine.compute_one("market.return_5d", ctx)
        self.engine.compute_one("market.return_5d", ctx)
        self.assertIsNotNone(self.engine.cache_hit_ratio())

    def test_reproducibility_same_inputs_same_values(self):
        """Spec §61 — identical inputs must produce identical values."""
        first = FeatureEngine(build_default_registry()).compute_one("market.return_5d", context()).value
        second = FeatureEngine(build_default_registry()).compute_one("market.return_5d", context()).value
        self.assertEqual(first, second)


class TestFeatureQualityDiagnostics(unittest.TestCase):
    """Spec §39, §40, §43."""

    def setUp(self):
        self.engine = FeatureEngine(build_default_registry())

    def test_constant_feature_is_flagged_not_deleted(self):
        quality = self.engine.assess_quality("f", [1.0] * 20)
        self.assertTrue(quality.is_constant)
        self.assertIn("constant", " ".join(quality.problems()))

    def test_high_missingness_is_flagged(self):
        quality = self.engine.assess_quality("f", [None] * 15 + [1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertGreater(quality.missingness, 0.5)
        self.assertIn("missingness", " ".join(quality.problems()))

    def test_coverage_is_the_complement_of_missingness(self):
        quality = self.engine.assess_quality("f", [1.0, None, 2.0, None])
        self.assertAlmostEqual(quality.coverage, 0.5, places=4)

    def test_outliers_are_counted_never_removed(self):
        values = [0.01] * 100 + [50.0]
        quality = self.engine.assess_quality("f", values)
        self.assertGreaterEqual(quality.outlier_count, 1)
        self.assertEqual(quality.observation_count, 101)   # nothing was dropped

    def test_healthy_feature_reports_no_problems(self):
        quality = self.engine.assess_quality("f", [0.01, 0.02, 0.03, -0.01, 0.04] * 5)
        self.assertEqual(quality.problems(), [])

    def test_no_predictive_power_field_exists(self):
        """Spec §41/§42: this phase measures HEALTH, never predictive contribution."""
        fields = set(FeatureQuality.__dataclass_fields__.keys())
        for forbidden in ("importance", "predictive_power", "correlation_with_label", "contribution"):
            self.assertNotIn(forbidden, fields)

    def test_stability_report_detects_a_distribution_shift(self):
        early = [(CUTOFF - timedelta(days=1000 + i), 0.01) for i in range(20)]
        late = [(CUTOFF - timedelta(days=i), 5.0) for i in range(20)]
        report = self.engine.assess_stability("f", early + late, [
            ("early", CUTOFF - timedelta(days=1100), CUTOFF - timedelta(days=900)),
            ("late", CUTOFF - timedelta(days=100), CUTOFF),
        ])
        self.assertEqual(len(report.windows), 2)
        self.assertTrue(report.has_distribution_shift())

    def test_stable_feature_shows_no_shift(self):
        samples = [(CUTOFF - timedelta(days=i), 0.01 + (i % 3) * 0.001) for i in range(100)]
        report = self.engine.assess_stability("f", samples, [
            ("early", CUTOFF - timedelta(days=100), CUTOFF - timedelta(days=50)),
            ("late", CUTOFF - timedelta(days=49), CUTOFF),
        ])
        self.assertFalse(report.has_distribution_shift())


class TestCatalogIntegrity(unittest.TestCase):
    def setUp(self):
        self.registry = build_default_registry()

    def test_every_feature_has_a_documented_formula_and_source(self):
        for definition in self.registry.all():
            self.assertTrue(definition.formula, f"{definition.feature_id} has no formula")
            self.assertTrue(definition.source, f"{definition.feature_id} has no source")

    def test_every_feature_has_a_description(self):
        for definition in self.registry.all():
            self.assertTrue(definition.description, f"{definition.feature_id} has no description")

    def test_all_declared_dependencies_exist(self):
        known = {d.feature_id for d in self.registry.all()}
        for definition in self.registry.all():
            for dependency in definition.dependencies:
                self.assertIn(dependency, known, f"{definition.feature_id} depends on unknown {dependency}")

    def test_catalog_covers_the_required_namespaces(self):
        namespaces = {d.namespace for d in self.registry.all()}
        for required in (FeatureNamespace.MARKET, FeatureNamespace.VOLATILITY,
                          FeatureNamespace.LIQUIDITY, FeatureNamespace.TECHNICAL,
                          FeatureNamespace.EVENT, FeatureNamespace.NEWS,
                          FeatureNamespace.SENTIMENT, FeatureNamespace.PEER):
            self.assertIn(required, namespaces)

    def test_catalog_is_bounded_not_exploded(self):
        """Spec §5/§8 warn against feature proliferation — a sanity ceiling."""
        self.assertLess(len(self.registry.all()), 60)

    def test_default_feature_sets_reference_only_real_features(self):
        known = {d.feature_id for d in self.registry.all()}
        for feature_set in build_default_feature_sets(CUTOFF).values():
            for feature_id in feature_set.feature_ids:
                self.assertIn(feature_id, known, f"{feature_set.name} references unknown {feature_id}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
