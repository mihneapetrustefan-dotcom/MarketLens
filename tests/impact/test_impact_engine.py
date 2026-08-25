"""
tests/impact/test_impact_engine.py
---------------------------------------
Tests for Market Impact Intelligence.

The look-ahead tests are ADVERSARIAL by design (spec §39): they
construct data where future information WOULD change the answer, then
assert the engine does not use it.
"""

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.impact.engine import EventStudyEngine, Candle, MAGNITUDE_LARGE
from src.impact import calculations as calc
from src.domain.impact_models import (
    ReturnMethod, BenchmarkModel, DataQualityLevel, DataQualityIssue,
    ImpactDirection, ImpactMagnitude, ImpactBreadth, ReactionDistribution,
    EventWindow, WindowKind, WindowUnit, RegimeTrend,
)

EVENT_AT = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)


def series(start: datetime, count: int, start_price: float, step: float,
           volume: float = 1_000_000, minutes: int = 1440, adjusted: bool = True):
    """A regular candle series. `step` is the per-candle price increment."""
    candles = []
    price = start_price
    for i in range(count):
        ts = start + timedelta(minutes=minutes * i)
        candles.append(Candle(ts, open_=price, high=price * 1.01, low=price * 0.99,
                               close=price, volume=volume,
                               adjusted_close=price if adjusted else None))
        price += step
    return candles


def flat_history(count=40, price=100.0, volume=1_000_000):
    """Quiet pre-event history ending exactly at the event moment."""
    start = EVENT_AT - timedelta(days=count)
    return series(start, count, price, 0.0, volume=volume)


class TestReturnCalculations(unittest.TestCase):
    """Spec §7 — one consistent definition, applied everywhere."""

    def test_simple_return(self):
        self.assertAlmostEqual(calc.compute_return(100, 110, ReturnMethod.SIMPLE), 0.10, places=6)

    def test_negative_return(self):
        self.assertAlmostEqual(calc.compute_return(100, 90, ReturnMethod.SIMPLE), -0.10, places=6)

    def test_log_return_differs_from_simple(self):
        simple = calc.compute_return(100, 110, ReturnMethod.SIMPLE)
        log = calc.compute_return(100, 110, ReturnMethod.LOG)
        self.assertNotAlmostEqual(simple, log, places=4)

    def test_zero_or_negative_start_price_returns_none_not_infinity(self):
        self.assertIsNone(calc.compute_return(0, 110))
        self.assertIsNone(calc.compute_return(-5, 110))

    def test_missing_price_returns_none(self):
        self.assertIsNone(calc.compute_return(None, 110))
        self.assertIsNone(calc.compute_return(100, None))

    def test_abnormal_return_is_asset_minus_expected(self):
        self.assertAlmostEqual(calc.compute_abnormal_return(0.04, 0.035), 0.005, places=6)

    def test_abnormal_return_none_when_benchmark_missing(self):
        self.assertIsNone(calc.compute_abnormal_return(0.04, None))

    def test_log_returns_sum_while_simple_returns_compound(self):
        simple = calc.cumulative_return([0.1, 0.1], ReturnMethod.SIMPLE)
        log = calc.cumulative_return([0.1, 0.1], ReturnMethod.LOG)
        self.assertAlmostEqual(simple, 0.21, places=6)   # compounded
        self.assertAlmostEqual(log, 0.20, places=6)      # summed

    def test_volatility_needs_at_least_two_observations(self):
        self.assertIsNone(calc.realized_volatility([0.01]))
        self.assertIsNotNone(calc.realized_volatility([0.01, -0.02, 0.03]))

    def test_volume_metrics_are_normalized(self):
        self.assertAlmostEqual(calc.relative_volume(35_000_000, 10_000_000), 3.5, places=4)
        self.assertIsNone(calc.volume_zscore(35_000_000, 10_000_000, 0))   # no dispersion

    def test_percentile_interpolates(self):
        self.assertAlmostEqual(calc.percentile([1, 2, 3, 4], 50), 2.5, places=6)

    def test_t_statistic_returns_statistic_only_never_a_verdict(self):
        value = calc.t_statistic([0.02, 0.03, 0.01, 0.025])
        self.assertIsInstance(value, float)


class TestLookAheadProtection(unittest.TestCase):
    """Spec §31, §39 — the most important tests in this phase."""

    def setUp(self):
        self.engine = EventStudyEngine()

    def test_pre_event_baseline_ignores_a_dramatic_future_price(self):
        """
        A huge post-event spike must not contaminate the pre-event
        baseline. If it did, the abnormal return would be computed
        against a price nobody could have seen.
        """
        history = flat_history(40, 100.0)
        # Two post-event sessions: measuring a +1-session return needs a
        # session AT +1, so a single future candle is genuinely not enough.
        future_spike = series(EVENT_AT + timedelta(days=1), 2, 500.0, 0.0, volume=99_000_000)
        study = self.engine.build_study(
            "e1", "nvda", history + future_spike, publication_time=EVENT_AT)

        d1 = study.returns.get("d1")
        self.assertIsNotNone(d1)
        # The BEFORE price must be the pre-event level, never the spike.
        self.assertAlmostEqual(float(d1.price_before), 100.0, places=4)

    def test_insufficient_pre_event_history_is_rejected_not_estimated(self):
        short = flat_history(5, 100.0)
        study = self.engine.build_study("e1", "nvda", short, publication_time=EVENT_AT)
        self.assertEqual(study.quality.level, DataQualityLevel.UNUSABLE)
        self.assertIn(DataQualityIssue.INSUFFICIENT_HISTORY, study.quality.issues)

    def test_comparables_are_strictly_historical(self):
        history = flat_history(40)
        target = self.engine.build_study("target", "nvda", history, publication_time=EVENT_AT)

        past = self.engine.build_study(
            "past", "amd", series(EVENT_AT - timedelta(days=100), 40, 100.0, 0.0),
            publication_time=EVENT_AT - timedelta(days=60))
        future = self.engine.build_study(
            "future", "intc", series(EVENT_AT + timedelta(days=10), 40, 100.0, 0.0),
            publication_time=EVENT_AT + timedelta(days=50))

        types = {"target": "earnings", "past": "earnings", "future": "earnings"}
        comparables = self.engine.find_comparables(target, [past, future], types)
        ids = {c.event_id for c in comparables}
        self.assertIn("past", ids)
        self.assertNotIn("future", ids, "a future event must never be selected as a historical comparable")

    def test_market_regime_uses_only_pre_event_data(self):
        rising_then_crashing = (series(EVENT_AT - timedelta(days=60), 60, 100.0, 1.0)
                                 + series(EVENT_AT + timedelta(days=1), 20, 500.0, -20.0))
        regime = self.engine.compute_market_regime(rising_then_crashing, EVENT_AT, "spy")
        # The post-event crash must not turn the pre-event uptrend into a downtrend.
        self.assertEqual(regime.trend, RegimeTrend.UPTREND)

    def test_distribution_excludes_unusable_studies(self):
        good = self.engine.build_study("g", "a", flat_history(40), publication_time=EVENT_AT)
        bad = self.engine.build_study("b", "b", flat_history(3), publication_time=EVENT_AT)
        distribution = self.engine.reaction_distribution([good, bad])
        self.assertLessEqual(distribution.sample_size, 1)


class TestEventTiming(unittest.TestCase):
    """Spec §3, §5 — publication time is not event time, and uncertainty is recorded."""

    def setUp(self):
        self.engine = EventStudyEngine()

    def test_event_and_publication_times_are_kept_distinct(self):
        event_at = EVENT_AT - timedelta(minutes=12)
        study = self.engine.build_study("e1", "nvda", flat_history(40),
                                         event_time=event_at, publication_time=EVENT_AT)
        self.assertEqual(study.event_time, event_at)
        self.assertEqual(study.publication_time, EVENT_AT)
        self.assertNotEqual(study.event_time, study.publication_time)

    def test_uncertain_visibility_is_flagged_not_assumed_precise(self):
        study = self.engine.build_study("e1", "nvda", flat_history(40),
                                         event_time=EVENT_AT - timedelta(minutes=12),
                                         publication_time=EVENT_AT)
        self.assertIn(DataQualityIssue.UNCERTAIN_EVENT_TIME, study.quality.issues)
        self.assertNotEqual(study.market_visibility_earliest, study.market_visibility_latest)

    def test_anchor_is_the_latest_plausible_visibility(self):
        """Conservative: never credit the system with knowing earlier than it might have."""
        study = self.engine.build_study("e1", "nvda", flat_history(40),
                                         event_time=EVENT_AT - timedelta(minutes=30),
                                         publication_time=EVENT_AT)
        self.assertEqual(study.market_visibility_latest, EVENT_AT)

    def test_no_timestamps_at_all_is_rejected(self):
        study = self.engine.build_study("e1", "nvda", flat_history(40))
        self.assertEqual(study.quality.level, DataQualityLevel.UNUSABLE)

    def test_publication_before_event_time_is_flagged_as_inconsistent(self):
        study = self.engine.build_study("e1", "nvda", flat_history(40),
                                         event_time=EVENT_AT,
                                         publication_time=EVENT_AT - timedelta(hours=2))
        self.assertIn(DataQualityIssue.BAD_TIMESTAMP, study.quality.issues)


class TestMarketReactionMeasurement(unittest.TestCase):
    def setUp(self):
        self.engine = EventStudyEngine()

    def test_positive_reaction_is_measured(self):
        history = flat_history(40, 100.0)
        after = series(EVENT_AT + timedelta(days=1), 5, 110.0, 0.0)
        study = self.engine.build_study("e1", "nvda", history + after, publication_time=EVENT_AT)
        self.assertGreater(study.returns["d1"].raw_return, 0)

    def test_negative_reaction_is_measured(self):
        history = flat_history(40, 100.0)
        after = series(EVENT_AT + timedelta(days=1), 5, 90.0, 0.0)
        study = self.engine.build_study("e1", "nvda", history + after, publication_time=EVENT_AT)
        self.assertLess(study.returns["d1"].raw_return, 0)

    def test_no_reaction_yields_near_zero_return(self):
        history = flat_history(40, 100.0)
        after = series(EVENT_AT + timedelta(days=1), 5, 100.0, 0.0)
        study = self.engine.build_study("e1", "nvda", history + after, publication_time=EVENT_AT)
        self.assertAlmostEqual(study.returns["d1"].raw_return, 0.0, places=4)

    def test_abnormal_return_strips_out_a_market_wide_move(self):
        """Spec §8's example: +4% asset against +3.5% market is a small abnormal move, not a big one."""
        history = flat_history(40, 100.0)
        after = series(EVENT_AT + timedelta(days=1), 3, 104.0, 0.0)
        benchmark = flat_history(40, 50.0) + series(EVENT_AT + timedelta(days=1), 3, 51.75, 0.0)

        study = self.engine.build_study("e1", "nvda", history + after, publication_time=EVENT_AT,
                                         benchmark_id="spy", benchmark_candles=benchmark)
        d1 = study.returns["d1"]
        self.assertAlmostEqual(d1.raw_return, 0.04, places=3)
        self.assertAlmostEqual(d1.benchmark_return, 0.035, places=3)
        self.assertAlmostEqual(d1.abnormal_return, 0.005, places=3)
        self.assertLess(abs(d1.abnormal_return), abs(d1.raw_return))

    def test_missing_benchmark_is_flagged_not_silently_skipped(self):
        study = self.engine.build_study("e1", "nvda", flat_history(40) + series(EVENT_AT + timedelta(days=1), 3, 110.0, 0.0),
                                         publication_time=EVENT_AT, benchmark_id="spy", benchmark_candles=[])
        self.assertIsNone(study.returns["d1"].abnormal_return)

    def test_high_volume_reaction_is_detected(self):
        history = flat_history(40, 100.0, volume=10_000_000)
        spike = [Candle(EVENT_AT + timedelta(days=1), open_=100.0, close=100.0,
                         volume=35_000_000, adjusted_close=100.0)]
        study = self.engine.build_study("e1", "nvda", history + spike, publication_time=EVENT_AT)
        volume = next(iter(study.volume.values()))
        self.assertGreater(volume.relative_volume, 3.0)

    def test_volatility_spike_is_measured(self):
        history = flat_history(40, 100.0)
        volatile = []
        price, ts = 100.0, EVENT_AT + timedelta(days=1)
        for i in range(10):
            price *= 1.08 if i % 2 == 0 else 0.93
            volatile.append(Candle(ts + timedelta(days=i), open_=price, close=price,
                                    volume=1_000_000, adjusted_close=price))
        study = self.engine.build_study("e1", "nvda", history + volatile, publication_time=EVENT_AT)
        volatility = study.volatility.get("overall")
        self.assertIsNotNone(volatility.post_volatility)
        self.assertGreater(volatility.post_volatility, volatility.pre_volatility or 0)


class TestDataQuality(unittest.TestCase):
    """Spec §26, §27 — incomplete data must never produce a silent result."""

    def setUp(self):
        self.engine = EventStudyEngine()

    def test_unadjusted_prices_are_flagged(self):
        history = series(EVENT_AT - timedelta(days=40), 40, 100.0, 0.0, adjusted=False)
        study = self.engine.build_study("e1", "nvda", history, publication_time=EVENT_AT)
        self.assertIn(DataQualityIssue.UNADJUSTED_CORPORATE_ACTION, study.quality.issues)

    def test_trading_halt_is_flagged(self):
        history = flat_history(40)
        halted = Candle(EVENT_AT + timedelta(days=1), close=100.0, adjusted_close=100.0,
                         volume=0, is_halted=True)
        study = self.engine.build_study("e1", "nvda", history + [halted], publication_time=EVENT_AT)
        self.assertIn(DataQualityIssue.TRADING_HALT, study.quality.issues)

    def test_unusable_quality_blocks_attribution(self):
        study = self.engine.build_study("e1", "nvda", flat_history(3), publication_time=EVENT_AT)
        self.assertFalse(study.attribution_permitted)

    def test_completeness_is_reported(self):
        study = self.engine.build_study("e1", "nvda", flat_history(40), publication_time=EVENT_AT)
        self.assertIsNotNone(study.quality.completeness)

    def test_adjusted_close_is_preferred_over_raw_close(self):
        candle = Candle(EVENT_AT, close=200.0, adjusted_close=100.0)
        self.assertEqual(candle.price, 100.0)
        self.assertTrue(candle.uses_adjusted)


class TestConfoundingEvents(unittest.TestCase):
    """Spec §18 — a confounder suspends attribution, not measurement."""

    def setUp(self):
        self.engine = EventStudyEngine()

    def test_overlapping_macro_event_is_detected(self):
        study = self.engine.build_study("e1", "nvda", flat_history(40), publication_time=EVENT_AT)
        found = self.engine.detect_confounders(study, [
            {"event_id": "fomc", "description": "Fed surprise rate decision",
             "kind": "macro", "occurred_at": EVENT_AT + timedelta(hours=1), "severity": "high"},
        ])
        self.assertEqual(len(found), 1)
        self.assertTrue(study.has_confounders)

    def test_confounder_blocks_attribution_but_keeps_the_measurement(self):
        history = flat_history(40, 100.0)
        after = series(EVENT_AT + timedelta(days=1), 3, 106.0, 0.0)
        study = self.engine.build_study("e1", "nvda", history + after, publication_time=EVENT_AT)
        self.engine.detect_confounders(study, [
            {"event_id": "fomc", "kind": "macro", "occurred_at": EVENT_AT, "severity": "high"}])

        self.assertFalse(study.attribution_permitted)
        # The 6% move is still MEASURED — only its attribution is barred.
        self.assertIsNotNone(study.returns["d1"].raw_return)

    def test_distant_event_is_not_a_confounder(self):
        study = self.engine.build_study("e1", "nvda", flat_history(40), publication_time=EVENT_AT)
        found = self.engine.detect_confounders(study, [
            {"event_id": "old", "kind": "macro", "occurred_at": EVENT_AT - timedelta(days=10)}])
        self.assertEqual(found, [])

    def test_confounder_lowers_measurement_confidence(self):
        history = flat_history(40, 100.0)
        after = series(EVENT_AT + timedelta(days=1), 3, 106.0, 0.0)
        clean = self.engine.build_study("e1", "nvda", history + after, publication_time=EVENT_AT)
        confounded = self.engine.build_study("e2", "nvda", history + after, publication_time=EVENT_AT)
        self.engine.detect_confounders(confounded, [
            {"event_id": "fomc", "kind": "macro", "occurred_at": EVENT_AT}])

        self.assertGreater(self.engine.classify_dimensions(clean).measurement_confidence,
                            self.engine.classify_dimensions(confounded).measurement_confidence)


class TestImpactDimensionsAndScore(unittest.TestCase):
    """Spec §19, §20, §32 — multi-dimensional, descriptive, never predictive."""

    def setUp(self):
        self.engine = EventStudyEngine()

    def _study_with_move(self, pct):
        history = flat_history(40, 100.0)
        after = series(EVENT_AT + timedelta(days=1), 6, 100.0 * (1 + pct), 0.0)
        return self.engine.build_study("e1", "nvda", history + after, publication_time=EVENT_AT)

    def test_direction_positive(self):
        dimensions = self.engine.classify_dimensions(self._study_with_move(0.05))
        self.assertEqual(dimensions.direction, ImpactDirection.POSITIVE)

    def test_direction_negative(self):
        dimensions = self.engine.classify_dimensions(self._study_with_move(-0.05))
        self.assertEqual(dimensions.direction, ImpactDirection.NEGATIVE)

    def test_direction_neutral_for_a_tiny_move(self):
        dimensions = self.engine.classify_dimensions(self._study_with_move(0.001))
        self.assertEqual(dimensions.direction, ImpactDirection.NEUTRAL)

    def test_magnitude_bands(self):
        self.assertEqual(self.engine.classify_dimensions(self._study_with_move(0.08)).magnitude,
                          ImpactMagnitude.LARGE)
        self.assertEqual(self.engine.classify_dimensions(self._study_with_move(0.002)).magnitude,
                          ImpactMagnitude.SMALL)

    def test_impact_score_is_bounded_and_descriptive(self):
        study = self._study_with_move(0.10)
        dimensions = self.engine.classify_dimensions(study)
        score = self.engine.compute_impact_score(study, dimensions)
        self.assertIsNotNone(score)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_impact_score_does_not_use_sentiment(self):
        """
        Spec §20: the score must NOT be sentiment x price move.

        Inspects the EXECUTABLE BODY only — the docstring legitimately
        mentions sentiment in order to state that it is excluded, and a
        naive grep over the whole function flags its own disclaimer.
        """
        import inspect
        source = inspect.getsource(self.engine.compute_impact_score)
        body = source.split('"""')[2] if source.count('"""') >= 2 else source
        for forbidden in ("sentiment", "recommendation", "signal", "forecast", "predict"):
            self.assertNotIn(forbidden, body.lower())

    def test_no_predictive_field_exists_anywhere_on_the_profile(self):
        from src.domain.impact_models import ImpactProfile
        fields = set(ImpactProfile.__dataclass_fields__.keys())
        for forbidden in ("prediction", "forecast", "signal", "recommendation", "expected_move", "target_price"):
            self.assertNotIn(forbidden, fields)

    def test_event_confidence_is_carried_separately_from_impact(self):
        study = self._study_with_move(0.05)
        profile = self.engine.build_profile("e1", [study], event_confidence=0.96)
        self.assertEqual(profile.event_confidence, 0.96)
        self.assertIsNotNone(profile.impact_score)
        self.assertNotEqual(profile.event_confidence, profile.impact_score)


class TestBreadthAndIndirectReaction(unittest.TestCase):
    """Spec §6 — direct vs indirect, and sector breadth."""

    def setUp(self):
        self.engine = EventStudyEngine()

    def test_sector_breadth_when_peers_also_moved(self):
        history = flat_history(40, 100.0)
        after = series(EVENT_AT + timedelta(days=1), 3, 106.0, 0.0)
        benchmark = flat_history(40, 50.0) + series(EVENT_AT + timedelta(days=1), 3, 50.0, 0.0)

        direct = self.engine.build_study("e1", "nvda", history + after, publication_time=EVENT_AT,
                                          benchmark_id="spy", benchmark_candles=benchmark, is_direct=True)
        peer = self.engine.build_study("e1", "amd", history + after, publication_time=EVENT_AT,
                                        benchmark_id="spy", benchmark_candles=benchmark, is_direct=False)

        profile = self.engine.build_profile("e1", [direct, peer])
        self.assertEqual(profile.dimensions.breadth, ImpactBreadth.SECTOR)

    def test_company_breadth_when_peers_did_not_move(self):
        history = flat_history(40, 100.0)
        moved = series(EVENT_AT + timedelta(days=1), 3, 106.0, 0.0)
        unmoved = series(EVENT_AT + timedelta(days=1), 3, 100.0, 0.0)
        benchmark = flat_history(40, 50.0) + series(EVENT_AT + timedelta(days=1), 3, 50.0, 0.0)

        direct = self.engine.build_study("e1", "nvda", history + moved, publication_time=EVENT_AT,
                                          benchmark_id="spy", benchmark_candles=benchmark, is_direct=True)
        peer = self.engine.build_study("e1", "amd", history + unmoved, publication_time=EVENT_AT,
                                        benchmark_id="spy", benchmark_candles=benchmark, is_direct=False)

        profile = self.engine.build_profile("e1", [direct, peer])
        self.assertEqual(profile.dimensions.breadth, ImpactBreadth.COMPANY)


class TestDistributions(unittest.TestCase):
    """Spec §21, §24 — descriptive only, small samples flagged."""

    def setUp(self):
        self.engine = EventStudyEngine()

    def _studies(self, moves):
        studies = []
        for i, move in enumerate(moves):
            history = flat_history(40, 100.0)
            after = series(EVENT_AT + timedelta(days=1), 3, 100.0 * (1 + move), 0.0)
            studies.append(self.engine.build_study(f"e{i}", "nvda", history + after,
                                                    publication_time=EVENT_AT))
        return studies

    def test_small_sample_is_flagged(self):
        distribution = self.engine.reaction_distribution(self._studies([0.02, 0.03, -0.01]))
        self.assertTrue(distribution.small_sample)
        self.assertEqual(distribution.sample_size, 3)

    def test_large_sample_is_not_flagged(self):
        distribution = self.engine.reaction_distribution(self._studies([0.01] * 35))
        self.assertFalse(distribution.small_sample)

    def test_percentiles_and_probabilities_are_computed(self):
        distribution = self.engine.reaction_distribution(self._studies([0.05, 0.02, -0.03, 0.01, 0.04]))
        self.assertIsNotNone(distribution.median)
        self.assertIsNotNone(distribution.p25)
        self.assertIn("+1%", distribution.probability_above)

    def test_empty_sample_returns_an_empty_distribution_not_an_error(self):
        distribution = self.engine.reaction_distribution([])
        self.assertEqual(distribution.sample_size, 0)
        self.assertIsNone(distribution.median)


class TestWindows(unittest.TestCase):
    def test_custom_windows_are_supported(self):
        custom = [EventWindow("d30", WindowKind.POST_EVENT, WindowUnit.TRADING_DAYS, 0, 30)]
        engine = EventStudyEngine(windows=custom)
        self.assertEqual(len(engine.windows), 1)

    def test_window_rejects_inverted_offsets(self):
        with self.assertRaises(ValueError):
            EventWindow("bad", WindowKind.POST_EVENT, WindowUnit.MINUTES, 10, 5)

    def test_minute_window_bounds_are_wall_clock(self):
        engine = EventStudyEngine()
        window = EventWindow("m30", WindowKind.POST_EVENT, WindowUnit.MINUTES, 0, 30)
        start, end = engine.window_bounds(EVENT_AT, window)
        self.assertEqual(end - start, timedelta(minutes=30))

    def test_trading_day_window_skips_non_sessions(self):
        """A weekend must not silently consume trading days."""
        engine = EventStudyEngine()
        sessions = [EVENT_AT + timedelta(days=d) for d in (0, 1, 4, 5, 6)]   # gap = weekend
        window = EventWindow("d3", WindowKind.POST_EVENT, WindowUnit.TRADING_DAYS, 0, 3)
        start, end = engine.window_bounds(EVENT_AT, window, sessions)
        self.assertEqual(end, EVENT_AT + timedelta(days=5))   # 3rd SESSION, not 3rd calendar day


if __name__ == "__main__":
    unittest.main(verbosity=2)
