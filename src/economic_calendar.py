"""
economic_calendar.py
------------------------
Economic Calendar module for MarketLens.

RESPONSIBILITY:
Surface a small, HONESTLY-SCOPED list of known upcoming macro events —
not a full commercial economic calendar (CPI/jobs report/earnings
dates shift monthly and require a real, usually paid, calendar API
this project does not have), but the single most market-moving,
PRECISELY KNOWN recurring event: FOMC (Federal Reserve) interest rate
meetings, which the Fed itself announces up to 2 years in advance.

WHY THIS SCOPE, NOT MORE: a calendar entry that turns out to be wrong
(a fabricated or estimated date presented as certain) is worse than no
calendar at all — it's the kind of unverifiable claim this project
avoids everywhere else (see the "facts, no verdict" policy on the
Date de piață table). FOMC dates are the one category of "future
event" that can be stated with real, official, verifiable certainty.

MAINTENANCE NOTE: MEETING_DATES below is a MANUALLY MAINTAINED,
HARDCODED list — it does not update itself and does not call any API.
Verified directly against the Federal Reserve's own published calendar
(federalreserve.gov) as of August 2026. The Fed publishes its
"tentative" schedule for the following 1-2 years each year, typically
around May/September — this list should be refreshed against
https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
periodically (at least once a year) to stay current, and especially
once the entries here start running out.
"""

from datetime import date
from typing import List, Dict, Any, Optional

# Each entry: the meeting's start and end date (2-day meetings), plus
# the exact date its policy statement is released. Source: Federal
# Reserve Board press releases and federalreserve.gov calendar pages,
# verified August 2026.
MEETING_DATES: List[Dict[str, Any]] = [
    {"start": date(2026, 1, 27), "end": date(2026, 1, 28), "statement_date": date(2026, 1, 28)},
    {"start": date(2026, 3, 17), "end": date(2026, 3, 18), "statement_date": date(2026, 3, 18)},
    {"start": date(2026, 4, 28), "end": date(2026, 4, 29), "statement_date": date(2026, 4, 29)},
    {"start": date(2026, 6, 16), "end": date(2026, 6, 17), "statement_date": date(2026, 6, 17)},
    {"start": date(2026, 7, 28), "end": date(2026, 7, 29), "statement_date": date(2026, 7, 29)},
    {"start": date(2026, 9, 15), "end": date(2026, 9, 16), "statement_date": date(2026, 9, 16)},
    {"start": date(2026, 10, 27), "end": date(2026, 10, 28), "statement_date": date(2026, 10, 28)},
    {"start": date(2026, 12, 8), "end": date(2026, 12, 9), "statement_date": date(2026, 12, 9)},
    # 2027 — announced by the Fed on 2025-09-05, "tentative" per the Fed's own wording.
    {"start": date(2027, 1, 26), "end": date(2027, 1, 27), "statement_date": date(2027, 1, 27)},
    {"start": date(2027, 3, 16), "end": date(2027, 3, 17), "statement_date": date(2027, 3, 17)},
    {"start": date(2027, 4, 27), "end": date(2027, 4, 28), "statement_date": date(2027, 4, 28)},
    {"start": date(2027, 6, 8), "end": date(2027, 6, 9), "statement_date": date(2027, 6, 9)},
    {"start": date(2027, 7, 27), "end": date(2027, 7, 28), "statement_date": date(2027, 7, 28)},
    {"start": date(2027, 9, 14), "end": date(2027, 9, 15), "statement_date": date(2027, 9, 15)},
    {"start": date(2027, 10, 26), "end": date(2027, 10, 27), "statement_date": date(2027, 10, 27)},
    {"start": date(2027, 12, 7), "end": date(2027, 12, 8), "statement_date": date(2027, 12, 8)},
]


class EconomicCalendar:
    """Provides upcoming known macro events (currently: FOMC meetings only)."""

    def __init__(self, meeting_dates: Optional[List[Dict[str, Any]]] = None):
        """
        Args:
            meeting_dates: overrides MEETING_DATES — mainly for testing
                with a controlled fixture instead of real dates that
                will eventually all be in the past.
        """
        self.meeting_dates = meeting_dates if meeting_dates is not None else MEETING_DATES

    def get_upcoming_fomc_meetings(self, today: Optional[date] = None, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Return the next `limit` FOMC meetings on or after `today`,
        chronologically. Past meetings are never returned.

        Args:
            today: reference date; defaults to the real current date.
            limit: maximum number of upcoming meetings to return.

        Returns:
            A list of {"start", "end", "statement_date",
            "days_until"} dicts, ordered soonest first. Once the
            hardcoded MEETING_DATES list runs out (no more future
            entries), returns fewer than `limit` — or an empty list —
            rather than fabricating a date; see the module docstring's
            MAINTENANCE NOTE to refresh it.
        """
        today = today or date.today()
        upcoming = [m for m in self.meeting_dates if m["end"] >= today]
        upcoming.sort(key=lambda m: m["start"])

        results = []
        for meeting in upcoming[:limit]:
            results.append({
                **meeting,
                "days_until": (meeting["start"] - today).days,
            })
        return results
