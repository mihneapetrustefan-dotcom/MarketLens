"""
tests/outcomes/test_measurement.py
------------------------------------------
The arithmetic of "what happened next".

These cover §53 items 1-14 and 18-19: forward return, log return, LONG,
SHORT, NEUTRAL, HIT, MISS, insufficient data, pending horizon, MFE,
MAE, time to threshold, multiple horizons, market calendar, missing
candles.

WHY SO MANY TESTS ABOUT ABSENCE
-----------------------------------
The dangerous failure in an outcome layer is not a wrong number. It is
a MISSING number quietly rendered as a real one: a zero return where no
price existed, a MISS where the data could not answer, a flat move
scored as a loss. Each of those biases every aggregate built on top,
and none of them looks wrong in a table.

So roughly half of what follows checks that the code declines to
answer, and says why.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.outcome_models import (
    DEFAULT_HORIZONS, NEUTRAL_BAND, DirectionResult, OutcomeStatus,
    OutcomeWindow, ReferencePriceRule, SubjectKind, classify_direction,
    excursions, log_return, parse_horizons, realized_direction, simple_return,
    time_to_threshold,
)
from src.outcomes.measurement import (
    IMPLAUSIBLE_RETURN, INTERVAL_FOR_UNIT, measure, reference_price_for,
    window_bars,
)

CUTOFF = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)


def bar(offset_days=0, offset_minutes=0, close=100.0, high=None, low=None,
        adjusted=None):
    return {
        "timestamp": CUTOFF + timedelta(days=offset_days, minutes=offset_minutes),
        "open": close, "high": high if high is not None else close,
        "low": low if low is not None else close, "close": close,
        "adjusted_close": adjusted, "volume": 1000.0,
    }


def daily(closes, highs=None, lows=None, start=0):
    out = []
    for index, close in enumerate(closes):
        out.append(bar(offset_days=start + index, close=close,
                       high=highs[index] if highs else close,
                       low=lows[index] if lows else close))
    return out


# ======================================================================
# §53.1, §53.2 — returns
# ======================================================================

class TestForwardReturn(unittest.TestCase):

    def test_a_simple_return_is_end_over_reference_minus_one(self):
        self.assertAlmostEqual(simple_return(100.0, 105.0), 0.05, places=9)
        self.assertAlmostEqual(simple_return(100.0, 95.0), -0.05, places=9)

    def test_a_log_return_is_the_natural_log_of_the_ratio(self):
        import math
        self.assertAlmostEqual(log_return(100.0, 105.0),
                               math.log(1.05), places=9)

    def test_both_forms_are_stored_because_they_answer_different_questions(self):
        """
        Log returns add across time, simple returns add across a
        portfolio. Keeping only one guarantees somebody eventually uses
        it for the other job.
        """
        self.assertNotAlmostEqual(simple_return(100.0, 150.0),
                                  log_return(100.0, 150.0), places=3)

    def test_a_missing_price_yields_none_not_zero(self):
        """The single most damaging possible default (§24)."""
        self.assertIsNone(simple_return(None, 105.0))
        self.assertIsNone(simple_return(100.0, None))
        self.assertIsNone(log_return(None, None))

    def test_a_non_positive_price_yields_none_rather_than_a_number(self):
        """
        A zero or negative price is not a cheap stock, it is bad data,
        and dividing by it produces something that looks like a return.
        """
        self.assertIsNone(simple_return(0.0, 105.0))
        self.assertIsNone(simple_return(-10.0, 105.0))
        self.assertIsNone(log_return(100.0, 0.0))


# ======================================================================
# §53.3-7, §9, §10 — direction
# ======================================================================

class TestDirectionalOutcome(unittest.TestCase):

    def test_a_long_that_rose_is_a_hit(self):
        self.assertEqual(classify_direction("long", 0.02), DirectionResult.HIT)

    def test_a_long_that_fell_is_a_miss(self):
        self.assertEqual(classify_direction("long", -0.02), DirectionResult.MISS)

    def test_a_short_that_fell_is_a_hit(self):
        self.assertEqual(classify_direction("short", -0.02), DirectionResult.HIT)

    def test_a_short_that_rose_is_a_miss(self):
        self.assertEqual(classify_direction("short", 0.02), DirectionResult.MISS)

    def test_a_directional_claim_inside_the_band_is_neutral_not_a_miss(self):
        """
        §9 asks for neutral semantics to be documented, and this is the
        load-bearing half: the market did not move enough to say the
        claim was wrong. Recording a MISS would punish a signal for an
        absence of evidence, and since most moves are small it would
        drag every hit rate toward zero.
        """
        self.assertEqual(classify_direction("long", 0.0001),
                         DirectionResult.NEUTRAL)
        self.assertEqual(classify_direction("short", -0.0001),
                         DirectionResult.NEUTRAL)

    def test_a_neutral_claim_is_a_hit_when_nothing_happened(self):
        """A neutral signal is an active claim that nothing much will happen."""
        self.assertEqual(classify_direction("neutral", 0.0001),
                         DirectionResult.HIT)

    def test_a_neutral_claim_broken_in_either_direction_is_a_miss(self):
        """Direction is irrelevant to a neutral call. Up is as wrong as down."""
        self.assertEqual(classify_direction("neutral", 0.05), DirectionResult.MISS)
        self.assertEqual(classify_direction("neutral", -0.05), DirectionResult.MISS)

    def test_an_abstention_is_never_scored(self):
        """
        Counting abstentions as hits or misses would make abstaining a
        strategy for improving one's record.
        """
        for claim in ("no_signal", "none", ""):
            self.assertEqual(classify_direction(claim, 0.05),
                             DirectionResult.INSUFFICIENT_DATA)

    def test_a_missing_return_is_insufficient_data_never_a_miss(self):
        """
        §10 exactly: do not let NULL silently become MISS. An unmeasured
        signal counted as a failure understates every hit rate, and the
        bias grows with however many instruments lack price coverage.
        """
        self.assertEqual(classify_direction("long", None),
                         DirectionResult.INSUFFICIENT_DATA)

    def test_the_realized_direction_reports_flat_as_neutral(self):
        self.assertEqual(realized_direction(0.02), "long")
        self.assertEqual(realized_direction(-0.02), "short")
        self.assertEqual(realized_direction(0.0), "neutral")
        self.assertIsNone(realized_direction(None))

    def test_the_band_is_wider_than_floating_point_noise(self):
        """
        Without a dead band a 0.0001% drift decides a HIT, and hit rate
        becomes a measure of arithmetic noise rather than of the market.
        """
        self.assertGreater(NEUTRAL_BAND, 1e-6)


# ======================================================================
# §53.10, §53.11 — excursions
# ======================================================================

class TestExcursions(unittest.TestCase):

    def test_a_long_takes_mfe_from_the_high_and_mae_from_the_low(self):
        found = excursions("long", 100.0, [102.0, 108.0, 104.0],
                           [99.0, 96.0, 101.0])
        self.assertAlmostEqual(found["mfe"], 0.08, places=9)
        self.assertAlmostEqual(found["mae"], -0.04, places=9)

    def test_a_short_takes_mfe_from_the_low_and_mae_from_the_high(self):
        found = excursions("short", 100.0, [102.0, 108.0, 104.0],
                           [99.0, 96.0, 101.0])
        self.assertAlmostEqual(found["mfe"], 0.04, places=9)
        self.assertAlmostEqual(found["mae"], -0.08, places=9)

    def test_favourable_is_positive_for_both_directions(self):
        """
        The sign convention that lets longs and shorts pool into one
        distribution. Without it, averaging MFE across a mixed book
        cancels the two halves against each other.
        """
        long_side = excursions("long", 100.0, [110.0], [100.0])
        short_side = excursions("short", 100.0, [100.0], [90.0])
        self.assertGreater(long_side["mfe"], 0)
        self.assertGreater(short_side["mfe"], 0)
        self.assertAlmostEqual(long_side["mfe"], short_side["mfe"], places=9)

    def test_adverse_is_negative_for_both_directions(self):
        self.assertLess(excursions("long", 100.0, [100.0], [90.0])["mae"], 0)
        self.assertLess(excursions("short", 100.0, [110.0], [100.0])["mae"], 0)

    def test_the_index_of_each_extreme_comes_back_for_timing(self):
        found = excursions("long", 100.0, [101.0, 109.0, 103.0],
                           [99.0, 98.0, 90.0])
        self.assertEqual(found["mfe_index"], 1)
        self.assertEqual(found["mae_index"], 2)

    def test_mismatched_or_empty_bars_yield_none_rather_than_a_guess(self):
        self.assertIsNone(excursions("long", 100.0, [], [])["mfe"])
        self.assertIsNone(excursions("long", 100.0, [101.0], [])["mfe"])

    def test_an_unknown_direction_computes_nothing(self):
        """MFE has no meaning without a side to be favourable to."""
        self.assertIsNone(excursions("sideways", 100.0, [110.0], [90.0])["mfe"])


# ======================================================================
# §53.12 — time to threshold
# ======================================================================

class TestTimeToThreshold(unittest.TestCase):

    def test_it_reports_seconds_to_the_first_bar_that_reached_the_move(self):
        bars = [bar(0, close=100.0, high=100.5),
                bar(1, close=101.0, high=101.0),
                bar(2, close=103.0, high=103.0)]
        seconds = time_to_threshold(100.0, "long", bars, 0.02)
        self.assertAlmostEqual(seconds, 2 * 86400.0, places=6)

    def test_it_uses_the_favourable_extreme_not_the_close(self):
        """
        Answers "when was this first reachable", not "when did it close
        there" — which is the question a stop or a target actually asks.
        """
        bars = [bar(0, close=100.0, high=100.0),
                bar(1, close=100.1, high=105.0)]
        self.assertAlmostEqual(time_to_threshold(100.0, "long", bars, 0.04),
                               86400.0, places=6)

    def test_a_threshold_never_reached_returns_none_not_the_window_length(self):
        """
        Returning the window length would silently claim it was reached
        at the end, which is exactly backwards.
        """
        bars = [bar(0, close=100.0, high=100.2), bar(1, close=100.3, high=100.4)]
        self.assertIsNone(time_to_threshold(100.0, "long", bars, 0.10))

    def test_a_short_measures_downward_moves(self):
        bars = [bar(0, close=100.0, low=100.0), bar(1, close=97.0, low=97.0)]
        self.assertAlmostEqual(time_to_threshold(100.0, "short", bars, 0.02),
                               86400.0, places=6)


# ======================================================================
# §6, §53.14, §53.18 — windows and the market calendar
# ======================================================================

class TestOutcomeWindows(unittest.TestCase):

    def test_the_default_ladder_spans_intraday_to_multi_day(self):
        """
        §14 asks whether predictive power decays with time. A ladder
        that only measured days could not see an edge that dies inside
        an hour.
        """
        windows = parse_horizons(DEFAULT_HORIZONS)
        self.assertTrue(any(w.is_intraday for w in windows))
        self.assertTrue(any(not w.is_intraday for w in windows))

    def test_horizons_carry_value_and_unit_separately(self):
        window = OutcomeWindow.parse("15m")
        self.assertEqual(window.horizon_value, 15.0)
        self.assertEqual(window.horizon_unit, "m")

    def test_horizons_sort_numerically_not_alphabetically(self):
        """
        '10d' sorts before '5d' as text. A decay curve built on string
        order would be wrong in a way that looks entirely plausible.
        """
        windows = sorted(parse_horizons(("10d", "5d", "1h")),
                         key=lambda w: w.sort_key)
        self.assertEqual([w.key for w in windows], ["1h", "5d", "10d"])

    def test_a_bad_horizon_is_refused_rather_than_defaulted(self):
        for bad in ("", "5", "5x", "abc", "-1d", "0d"):
            with self.assertRaises(ValueError):
                OutcomeWindow.parse(bad)

    def test_a_fractional_daily_horizon_is_refused(self):
        """A daily horizon counts trading bars, and half a bar is nothing."""
        with self.assertRaises(ValueError):
            OutcomeWindow.parse("1.5d")

    def test_duplicate_horizons_collapse_but_order_is_kept(self):
        windows = parse_horizons(("1d", "5d", "1d"))
        self.assertEqual([w.key for w in windows], ["1d", "5d"])


class TestTheMarketCalendar(unittest.TestCase):
    """
    §34: 1 calendar day is not 1 trading day.

    The rule here is to count BARS. Daily candles only exist for
    sessions the market actually held, so counting them respects
    weekends and holidays exactly — without this project carrying a
    holiday table per venue that would itself go stale and be wrong in a
    quieter way.
    """

    def test_a_daily_horizon_takes_the_reference_bar_plus_n_sessions(self):
        bars = daily([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
        window = OutcomeWindow.parse("3d")
        inside = window_bars(bars, window, bars[0]["timestamp"])
        self.assertEqual(len(inside), 4, "reference bar plus three sessions")

    def test_a_weekend_gap_does_not_consume_the_horizon(self):
        """
        Bars dated Friday then Monday are two consecutive SESSIONS. A
        calendar-day rule would burn the horizon on days the market was
        shut and measure a shorter move than was asked for.
        """
        friday = [bar(0, close=100.0)]
        monday_onwards = [bar(3, close=103.0), bar(4, close=104.0)]
        bars = friday + monday_onwards
        inside = window_bars(bars, OutcomeWindow.parse("2d"), bars[0]["timestamp"])
        self.assertEqual(len(inside), 3)
        self.assertAlmostEqual(inside[-1]["close"], 104.0)

    def test_an_intraday_horizon_is_wall_clock_within_available_bars(self):
        bars = [bar(0, offset_minutes=m, close=100.0 + m) for m in range(0, 40, 5)]
        inside = window_bars(bars, OutcomeWindow.parse("15m"), bars[0]["timestamp"])
        self.assertEqual([b["timestamp"] for b in inside][-1],
                         CUTOFF + timedelta(minutes=15))

    def test_a_daily_horizon_uses_daily_bars_and_intraday_uses_minute_bars(self):
        """
        Measuring a 15-minute horizon on daily candles would return the
        day's close and call it a 15-minute move, and nothing on the
        stored row would reveal it.
        """
        self.assertEqual(INTERVAL_FOR_UNIT["d"], "1d")
        self.assertEqual(INTERVAL_FOR_UNIT["m"], "1m")
        self.assertEqual(INTERVAL_FOR_UNIT["h"], "1m")


# ======================================================================
# §8 — the reference price
# ======================================================================

class TestReferencePrice(unittest.TestCase):

    def test_it_is_the_first_close_at_or_after_the_cutoff(self):
        bars = [bar(-1, close=90.0), bar(0, close=100.0), bar(1, close=110.0)]
        price, moment = reference_price_for(bars, CUTOFF)
        self.assertAlmostEqual(price, 100.0)
        self.assertEqual(moment, CUTOFF)

    def test_a_bar_before_the_cutoff_is_never_used(self):
        """
        The close BEFORE the cutoff would credit the signal with a move
        that had already happened by the time it spoke — the easiest
        way in the world to manufacture an edge that does not exist.
        """
        bars = [bar(-5, close=50.0), bar(2, close=100.0)]
        price, moment = reference_price_for(bars, CUTOFF)
        self.assertAlmostEqual(price, 100.0)
        self.assertGreater(moment, CUTOFF)

    def test_no_bar_at_or_after_the_cutoff_yields_nothing(self):
        price, moment = reference_price_for([bar(-1, close=90.0)], CUTOFF)
        self.assertIsNone(price)
        self.assertIsNone(moment)

    def test_the_rule_is_recorded_on_the_measurement(self):
        """A return without a stated starting point is not reproducible."""
        outcome = measure(
            SubjectKind.SIGNAL, "sig-1", OutcomeWindow.parse("1d"),
            cutoff=CUTOFF, direction="long", bars=daily([100.0, 105.0]),
            data_as_of=CUTOFF + timedelta(days=30), interval="1d")
        self.assertEqual(outcome.reference_rule,
                         ReferencePriceRule.FIRST_CLOSE_AT_OR_AFTER_CUTOFF)


# ======================================================================
# End-to-end measurement, including everything it refuses
# ======================================================================

class TestMeasure(unittest.TestCase):

    def run_measure(self, **kwargs):
        defaults = dict(
            subject_kind=SubjectKind.SIGNAL, subject_id="sig-1",
            window=OutcomeWindow.parse("1d"), cutoff=CUTOFF, direction="long",
            bars=daily([100.0, 105.0]),
            data_as_of=CUTOFF + timedelta(days=60), interval="1d")
        defaults.update(kwargs)
        window = defaults.pop("window")
        kind = defaults.pop("subject_kind")
        subject_id = defaults.pop("subject_id")
        return measure(kind, subject_id, window, **defaults)

    def test_a_complete_window_is_available_with_every_number_filled(self):
        outcome = self.run_measure()
        self.assertEqual(outcome.status, OutcomeStatus.AVAILABLE)
        self.assertAlmostEqual(outcome.simple_return, 0.05, places=9)
        self.assertEqual(outcome.direction_result, DirectionResult.HIT)
        self.assertAlmostEqual(outcome.reference_price, 100.0)
        self.assertAlmostEqual(outcome.end_price, 105.0)
        self.assertEqual(outcome.bars_observed, 2)

    def test_the_expected_return_becomes_a_signed_and_absolute_error(self):
        outcome = self.run_measure(expected_return=0.02)
        self.assertAlmostEqual(outcome.error, 0.03, places=9)
        self.assertAlmostEqual(outcome.absolute_error, 0.03, places=9)

    def test_an_open_window_is_pending_not_insufficient(self):
        """
        §33. A signal issued an hour ago has no 10-day outcome YET, and
        that is a different fact from one whose instrument stopped
        trading. Come back later versus stop waiting.
        """
        outcome = self.run_measure(
            window=OutcomeWindow.parse("10d"), bars=daily([100.0, 101.0]),
            data_as_of=CUTOFF + timedelta(days=1))
        self.assertEqual(outcome.status, OutcomeStatus.PENDING)
        self.assertIsNone(outcome.simple_return)

    def test_a_closed_window_that_never_filled_is_insufficient_data(self):
        outcome = self.run_measure(
            window=OutcomeWindow.parse("15m"), interval="1m",
            bars=[bar(0, offset_minutes=0, close=100.0)],
            data_as_of=CUTOFF + timedelta(days=365))
        self.assertEqual(outcome.status, OutcomeStatus.INSUFFICIENT_DATA)
        self.assertIsNone(outcome.simple_return)

    def test_no_coverage_at_all_is_insufficient_data_with_a_reason(self):
        outcome = self.run_measure(bars=[])
        self.assertEqual(outcome.status, OutcomeStatus.INSUFFICIENT_DATA)
        self.assertTrue(any("no price coverage" in note or "no candle" in note
                            for note in outcome.notes))

    def test_an_unmeasurable_subject_still_produces_a_row(self):
        """
        A dropped subject is invisible; an INSUFFICIENT_DATA row is a
        question somebody can answer.
        """
        outcome = self.run_measure(bars=[])
        self.assertEqual(outcome.subject_id, "sig-1")
        self.assertEqual(outcome.horizon.key, "1d")

    def test_an_implausible_return_is_flagged_invalid_not_clamped(self):
        """
        §25 and §56. A 900% move over one day is a split the adjusted
        series missed. Clamping it to something plausible would hide the
        defect AND keep the wrong sign.
        """
        outcome = self.run_measure(bars=daily([10.0, 10.0 * (IMPLAUSIBLE_RETURN + 2)]))
        self.assertEqual(outcome.status, OutcomeStatus.INVALID)
        self.assertTrue(any("implausible" in note for note in outcome.notes))

    def test_an_adjusted_close_is_preferred_over_the_raw_close(self):
        """
        Measuring across a split on unadjusted prices manufactures a 50%
        loss that never happened (§25).
        """
        bars = [bar(0, close=100.0, adjusted=50.0),
                bar(1, close=52.0, adjusted=52.0)]
        outcome = self.run_measure(bars=bars)
        self.assertAlmostEqual(outcome.reference_price, 50.0, places=6)
        self.assertGreater(outcome.simple_return, 0)

    def test_mfe_and_mae_bracket_the_realized_return(self):
        outcome = self.run_measure(
            bars=daily([100.0, 103.0], highs=[100.0, 106.0], lows=[98.0, 102.0]))
        self.assertGreaterEqual(outcome.mfe, outcome.simple_return)
        self.assertLessEqual(outcome.mae, outcome.simple_return)

    def test_the_time_to_each_excursion_is_recorded(self):
        outcome = self.run_measure(
            window=OutcomeWindow.parse("3d"),
            bars=daily([100.0, 103.0, 101.0, 102.0],
                       highs=[100.0, 108.0, 101.0, 102.0],
                       lows=[100.0, 103.0, 95.0, 102.0]))
        self.assertAlmostEqual(outcome.time_to_mfe_seconds, 86400.0, places=6)
        self.assertAlmostEqual(outcome.time_to_mae_seconds, 2 * 86400.0, places=6)

    def test_missing_highs_suppress_mfe_rather_than_understating_it(self):
        """
        Computing MFE over the subset of bars that happen to have highs
        would understate the excursion without saying so.
        """
        bars = daily([100.0, 105.0])
        bars[1]["high"] = None
        outcome = self.run_measure(bars=bars)
        self.assertIsNone(outcome.mfe)
        self.assertEqual(outcome.status, OutcomeStatus.AVAILABLE,
                         "a missing high must not invalidate the return")

    def test_the_measurement_carries_its_own_provenance(self):
        """§26: which data, which interval, how many bars, as of when."""
        outcome = self.run_measure(data_source="price_candle_cache")
        self.assertEqual(outcome.data_source, "price_candle_cache")
        self.assertEqual(outcome.data_interval, "1d")
        self.assertGreater(outcome.bars_observed, 0)
        self.assertIsNotNone(outcome.data_as_of)

    def test_context_is_copied_onto_the_measurement_for_slicing(self):
        outcome = self.run_measure(
            instrument_id="us_and_intl-aapl", trained_model_id="tm-1",
            model_status="evaluated", market_regime="bull", confidence=0.3,
            strength=0.7)
        self.assertEqual(outcome.instrument_id, "us_and_intl-aapl")
        self.assertEqual(outcome.trained_model_id, "tm-1")
        self.assertEqual(outcome.model_status, "evaluated")
        self.assertEqual(outcome.market_regime, "bull")

    def test_the_identity_is_the_idempotency_key(self):
        outcome = self.run_measure()
        self.assertEqual(outcome.identity,
                         ("signal", "sig-1", "1d", outcome.method_version))


if __name__ == "__main__":
    unittest.main()
