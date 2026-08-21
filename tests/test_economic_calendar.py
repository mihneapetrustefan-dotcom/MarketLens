"""
test_economic_calendar.py
-----------------------------
Unit tests for Economic Calendar v1 (FOMC meeting dates).
"""

import unittest
from datetime import date

from economic_calendar import EconomicCalendar, MEETING_DATES


FIXTURE_MEETINGS = [
    {"start": date(2026, 1, 10), "end": date(2026, 1, 11), "statement_date": date(2026, 1, 11)},
    {"start": date(2026, 3, 10), "end": date(2026, 3, 11), "statement_date": date(2026, 3, 11)},
    {"start": date(2026, 5, 10), "end": date(2026, 5, 11), "statement_date": date(2026, 5, 11)},
    {"start": date(2026, 7, 10), "end": date(2026, 7, 11), "statement_date": date(2026, 7, 11)},
]


class TestUpcomingMeetings(unittest.TestCase):
    def setUp(self):
        self.calendar = EconomicCalendar(meeting_dates=FIXTURE_MEETINGS)

    def test_past_meetings_excluded(self):
        results = self.calendar.get_upcoming_fomc_meetings(today=date(2026, 2, 1))
        starts = [r["start"] for r in results]
        self.assertNotIn(date(2026, 1, 10), starts)

    def test_meeting_in_progress_today_still_included(self):
        # today falls WITHIN the 2-day meeting window (end date >= today)
        results = self.calendar.get_upcoming_fomc_meetings(today=date(2026, 3, 11))
        starts = [r["start"] for r in results]
        self.assertIn(date(2026, 3, 10), starts)

    def test_results_ordered_soonest_first(self):
        results = self.calendar.get_upcoming_fomc_meetings(today=date(2026, 1, 1))
        starts = [r["start"] for r in results]
        self.assertEqual(starts, sorted(starts))

    def test_limit_respected(self):
        results = self.calendar.get_upcoming_fomc_meetings(today=date(2026, 1, 1), limit=2)
        self.assertEqual(len(results), 2)

    def test_days_until_computed_correctly(self):
        results = self.calendar.get_upcoming_fomc_meetings(today=date(2026, 1, 5))
        self.assertEqual(results[0]["days_until"], 5)

    def test_no_meetings_left_returns_empty_list(self):
        results = self.calendar.get_upcoming_fomc_meetings(today=date(2030, 1, 1))
        self.assertEqual(results, [])

    def test_default_today_does_not_crash(self):
        # Just confirms calling without an explicit `today` works — the
        # real MEETING_DATES list will eventually be all in the past,
        # this should still return an empty list gracefully, not raise.
        results = EconomicCalendar().get_upcoming_fomc_meetings()
        self.assertIsInstance(results, list)


class TestRealMeetingDatesIntegrity(unittest.TestCase):
    """Sanity checks on the real, hardcoded MEETING_DATES list itself."""

    def test_no_duplicate_start_dates(self):
        starts = [m["start"] for m in MEETING_DATES]
        self.assertEqual(len(starts), len(set(starts)))

    def test_every_entry_has_required_fields(self):
        for m in MEETING_DATES:
            self.assertIn("start", m)
            self.assertIn("end", m)
            self.assertIn("statement_date", m)
            self.assertLessEqual(m["start"], m["end"])

    def test_entries_are_chronologically_non_decreasing_as_listed(self):
        starts = [m["start"] for m in MEETING_DATES]
        self.assertEqual(starts, sorted(starts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
