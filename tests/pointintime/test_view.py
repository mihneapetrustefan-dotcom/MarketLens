"""
tests/pointintime/test_view.py
-----------------------------------
Tests for the point-in-time barrier.

These are deliberately ADVERSARIAL: most of them are attempts to leak
future information, and they pass only if the leak is BLOCKED. A test
suite that merely confirms the happy path would not prove anything
here — the whole value of this module is what it refuses to do.
"""

import sys
import os
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.pointintime.view import (
    PointInTimeView, LookAheadViolation, TimeUncertainty,
    build_view, market_visibility_time,
)

T0 = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)


@dataclass
class Price:
    """A minimal timestamped record, standing in for any real observation."""
    observed_at: Optional[datetime]
    close: float


def prices():
    """Three prices before the anchor, three after — the classic leakage setup."""
    return [
        Price(T0 - timedelta(hours=2), 100.0),
        Price(T0 - timedelta(hours=1), 101.0),
        Price(T0, 102.0),                          # exactly at the anchor: knowable
        Price(T0 + timedelta(hours=1), 108.0),     # future
        Price(T0 + timedelta(hours=6), 115.0),     # future
        Price(T0 + timedelta(days=1), 120.0),      # future
    ]


def view(as_of=T0):
    return build_view(as_of, prices(), lambda p: p.observed_at, label="prices")


class TestInformationSetIsBounded(unittest.TestCase):
    def test_only_records_at_or_before_the_anchor_are_visible(self):
        known = view().known()
        self.assertEqual(len(known), 3)
        self.assertTrue(all(p.observed_at <= T0 for p in known))

    def test_a_record_exactly_at_the_anchor_is_knowable(self):
        self.assertIn(102.0, [p.close for p in view().known()])

    def test_future_records_are_invisible_to_the_information_set(self):
        closes = [p.close for p in view().known()]
        for future_close in (108.0, 115.0, 120.0):
            self.assertNotIn(future_close, closes)

    def test_most_recent_never_returns_a_future_record(self):
        self.assertEqual(view().most_recent().close, 102.0)

    def test_undated_records_are_excluded_conservatively(self):
        """An undated record cannot be PROVEN to have been available, so it must not be assumed available."""
        records = prices() + [Price(None, 999.0)]
        v = build_view(T0, records, lambda p: p.observed_at)
        self.assertNotIn(999.0, [p.close for p in v.known()])

    def test_lookback_window_bounds_both_ends(self):
        recent = view().known_within(timedelta(hours=1))
        self.assertEqual({p.close for p in recent}, {101.0, 102.0})

    def test_counts_report_what_the_anchor_hides(self):
        v = view()
        self.assertEqual(v.count_known(), 3)
        self.assertEqual(v.excluded_count(), 3)


class TestAdversarialLeakageAttempts(unittest.TestCase):
    """Every test here is an attempt to read the future. All must FAIL to do so."""

    def test_explicitly_asking_for_a_future_timestamp_raises(self):
        with self.assertRaises(LookAheadViolation):
            view().assert_knowable(T0 + timedelta(minutes=1), "closing price")

    def test_the_classic_leak_using_the_days_closing_price_is_blocked(self):
        """
        Spec §39's canonical example: an event at 10:00 must not be
        analysed using the day's final close.
        """
        v = view()
        day_close = Price(T0 + timedelta(hours=6), 115.0)
        with self.assertRaises(LookAheadViolation):
            v.get_knowable(day_close)

    def test_a_later_confirmation_cannot_enter_the_information_set(self):
        v = view()
        later_confirmation = T0 + timedelta(hours=5)
        with self.assertRaises(LookAheadViolation):
            v.assert_knowable(later_confirmation, "event confirmation")

    def test_moving_the_anchor_forward_is_refused(self):
        """Widening an already-constrained information set must be explicit, never incidental."""
        with self.assertRaises(LookAheadViolation):
            view().rewind_to(T0 + timedelta(hours=1))

    def test_error_message_names_both_timestamps_for_debuggability(self):
        try:
            view().assert_knowable(T0 + timedelta(hours=2), "some value")
            self.fail("expected LookAheadViolation")
        except LookAheadViolation as exc:
            message = str(exc)
            self.assertIn("2026-08-20", message)
            self.assertIn("some value", message)

    def test_a_knowable_timestamp_passes_the_guard(self):
        view().assert_knowable(T0 - timedelta(minutes=1), "prior price")   # must not raise

    def test_none_timestamp_passes_the_guard_without_claiming_knowledge(self):
        view().assert_knowable(None, "missing")   # must not raise

    def test_naive_timestamps_are_rejected_outright(self):
        with self.assertRaises(ValueError):
            view().assert_knowable(datetime(2026, 8, 20, 9, 0), "naive")


class TestOutcomesAreSeparateAndDeliberate(unittest.TestCase):
    """Future data IS legitimate for measuring what happened — via a differently-named accessor."""

    def test_outcomes_return_only_records_after_the_anchor(self):
        outcomes = view().outcome_after()
        self.assertEqual(len(outcomes), 3)
        self.assertTrue(all(p.observed_at > T0 for p in outcomes))

    def test_outcome_horizon_bounds_the_measurement_window(self):
        within_two_hours = view().outcome_after(timedelta(hours=2))
        self.assertEqual([p.close for p in within_two_hours], [108.0])

    def test_information_set_and_outcomes_never_overlap(self):
        v = view()
        known_ids = {id(p) for p in v.known()}
        outcome_ids = {id(p) for p in v.outcome_after()}
        self.assertEqual(known_ids & outcome_ids, set())

    def test_outcome_at_or_after_a_specific_later_moment(self):
        later = view().outcome_at_or_after(T0 + timedelta(hours=6))
        self.assertEqual({p.close for p in later}, {115.0, 120.0})


class TestRewindingBackwards(unittest.TestCase):
    def test_rewinding_narrows_the_information_set(self):
        earlier = view().rewind_to(T0 - timedelta(hours=1))
        self.assertEqual(earlier.count_known(), 2)

    def test_rewound_view_hides_what_the_original_could_see(self):
        earlier = view().rewind_to(T0 - timedelta(hours=2))
        self.assertNotIn(102.0, [p.close for p in earlier.known()])

    def test_describe_reports_the_anchor_and_visibility(self):
        described = view().describe()
        self.assertEqual(described["known_at_anchor"], 3)
        self.assertEqual(described["hidden_by_anchor"], 3)


class TestTimeUncertainty(unittest.TestCase):
    """Imprecise moments must be expressed as ranges, never as invented precision."""

    def test_precise_moment_has_zero_uncertainty(self):
        precise = TimeUncertainty.precise(T0)
        self.assertTrue(precise.is_precise)
        self.assertEqual(precise.uncertainty_seconds, 0.0)

    def test_range_reports_its_width(self):
        span = TimeUncertainty.between(T0, T0 + timedelta(minutes=12))
        self.assertFalse(span.is_precise)
        self.assertEqual(span.uncertainty_seconds, 720.0)

    def test_midpoint_is_computed(self):
        span = TimeUncertainty.between(T0, T0 + timedelta(minutes=10))
        self.assertEqual(span.midpoint, T0 + timedelta(minutes=5))

    def test_inverted_range_is_rejected(self):
        with self.assertRaises(ValueError):
            TimeUncertainty(earliest=T0, latest=T0 - timedelta(hours=1))

    def test_naive_timestamps_rejected(self):
        with self.assertRaises(ValueError):
            TimeUncertainty.precise(datetime(2026, 8, 20, 10, 0))


class TestMarketVisibilityTime(unittest.TestCase):
    """The timestamp that actually matters for an event study — and that the system does not currently record."""

    def test_event_before_publication_yields_the_span_between_them(self):
        event = T0
        published = T0 + timedelta(minutes=12)
        visibility = market_visibility_time(event, published)
        self.assertEqual(visibility.earliest, event)
        self.assertEqual(visibility.latest, published)
        self.assertFalse(visibility.is_precise)

    def test_publication_only_is_precise_at_publication(self):
        visibility = market_visibility_time(None, T0)
        self.assertTrue(visibility.is_precise)
        self.assertEqual(visibility.latest, T0)

    def test_event_only_with_ingestion_yields_a_range(self):
        visibility = market_visibility_time(T0, None, ingestion_time=T0 + timedelta(hours=1))
        self.assertEqual(visibility.earliest, T0)
        self.assertEqual(visibility.latest, T0 + timedelta(hours=1))

    def test_event_only_without_ingestion_falls_back_to_the_event_time(self):
        visibility = market_visibility_time(T0, None)
        self.assertTrue(visibility.is_precise)

    def test_no_timestamps_returns_none_rather_than_inventing_one(self):
        self.assertIsNone(market_visibility_time(None, None))

    def test_publication_before_event_is_flagged_as_inconsistent_not_silently_resolved(self):
        visibility = market_visibility_time(T0, T0 - timedelta(hours=1))
        self.assertIn("INCONSISTENT", visibility.basis)

    def test_visibility_range_is_usable_as_a_conservative_anchor(self):
        """
        The intended use: anchor the information set at the LATEST
        plausible visibility (never claim we knew earlier than we might
        have), while measuring outcomes from the EARLIEST.
        """
        visibility = market_visibility_time(T0, T0 + timedelta(minutes=12))
        conservative = build_view(visibility.latest, prices(), lambda p: p.observed_at)
        self.assertEqual(conservative.count_known(), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
