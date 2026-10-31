"""
src/marketdata/calendar.py
-------------------------------------------
The US equity exchange session calendar (Phase 25.9E).

WHY THIS EXISTS
-------------------
Before this phase the project had two answers to "is the market open":

  - the Phase 12 calendar, which says OPEN for any DATE that has a cached
    daily bar -- all 24 hours of it, pre-market and after-hours included,
    and never for a date the cache has not yet fetched;
  - the venue, which says OPEN whenever IBKR serves a fresh, available
    snapshot. IBKR serves those outside regular hours.

Neither knows a holiday or an early close. The session runner could
therefore start on Thanksgiving if a quote arrived, keep ticking at
20:00 New York time, and treat the day after Thanksgiving as closing at
16:00. Phase 25.8's own report listed this as a known limitation.

WHAT IT IS
--------------
Rules, not a data feed: regular hours 09:30-16:00 America/New_York; the
ten NYSE full-day holidays with their observance rules; the 13:00 early
closes. Computed for any year, so it does not go stale on 1 January.

WHAT IT IS NOT
------------------
It does not know unscheduled closures (a national day of mourning, a
market-wide halt) -- the venue remains the second opinion for those,
and can only make the verdict MORE closed, never open a closed day.
Pre-market and after-hours are reported as such, not as OPEN: nothing
in this project trades extended hours.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from src.domain.broker_models import MarketStatus

NEW_YORK = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
PRE_MARKET_START = time(4, 0)
AFTER_HOURS_END = time(20, 0)

#: Asset classes this calendar governs. Crypto trades continuously and
#: is deliberately not covered.
US_EQUITY_CLASSES = ("stock", "etf", "equity")


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    following = date(year + (month == 12), (month % 12) + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> Optional[date]:
    """Saturday -> Friday, Sunday -> Monday."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def holidays(year: int) -> dict:
    """NYSE full-day closures for `year`, name by date."""
    found = {}
    new_year = date(year, 1, 1)
    # NYSE does not close the preceding Friday when 1 January is a
    # Saturday (Rule 7.2); a Sunday moves to Monday.
    if new_year.weekday() == 6:
        found[new_year + timedelta(days=1)] = "New Year's Day (observed)"
    elif new_year.weekday() < 5:
        found[new_year] = "New Year's Day"
    found[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    found[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    found[_easter(year) - timedelta(days=2)] = "Good Friday"
    found[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        found[_observed(date(year, 6, 19))] = "Juneteenth"
    found[_observed(date(year, 7, 4))] = "Independence Day"
    found[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    found[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    found[_observed(date(year, 12, 25))] = "Christmas Day"
    return found


def early_closes(year: int) -> dict:
    """13:00 closes: 3 July, the day after Thanksgiving, Christmas Eve."""
    found = {}
    closed = holidays(year)
    july3 = date(year, 7, 3)
    if july3.weekday() < 5 and july3 not in closed:
        found[july3] = "Independence Day eve"
    black_friday = _nth_weekday(year, 11, 3, 4) + timedelta(days=1)
    found[black_friday] = "day after Thanksgiving"
    christmas_eve = date(year, 12, 24)
    if christmas_eve.weekday() < 5 and christmas_eve not in closed:
        found[christmas_eve] = "Christmas Eve"
    return found


@dataclass(frozen=True)
class SessionWindow:
    day: date
    opens_at: Optional[datetime]
    closes_at: Optional[datetime]
    reason: str = ""

    @property
    def is_trading_day(self) -> bool:
        return self.opens_at is not None


class USEquityCalendar:
    """Session state for US-listed equities, in UTC, from exchange rules."""

    def session(self, day: date) -> SessionWindow:
        if day.weekday() >= 5:
            return SessionWindow(day, None, None, "weekend")
        closed = holidays(day.year).get(day)
        if closed:
            return SessionWindow(day, None, None, f"holiday: {closed}")
        close = REGULAR_CLOSE
        reason = "regular session"
        early = early_closes(day.year).get(day)
        if early:
            close, reason = EARLY_CLOSE, f"early close: {early}"
        opens = datetime.combine(day, REGULAR_OPEN, NEW_YORK)
        closes = datetime.combine(day, close, NEW_YORK)
        return SessionWindow(day, opens.astimezone(timezone.utc),
                             closes.astimezone(timezone.utc), reason)

    def status(self, now: datetime) -> MarketStatus:
        local = now.astimezone(NEW_YORK)
        window = self.session(local.date())
        if not window.is_trading_day:
            if window.reason.startswith("holiday"):
                return MarketStatus.HOLIDAY
            return MarketStatus.CLOSED
        moment = now.astimezone(timezone.utc)
        if window.opens_at <= moment < window.closes_at:
            return MarketStatus.OPEN
        if moment < window.opens_at:
            pre = datetime.combine(local.date(), PRE_MARKET_START, NEW_YORK)
            return (MarketStatus.PRE_MARKET if moment >= pre.astimezone(timezone.utc)
                    else MarketStatus.CLOSED)
        after = datetime.combine(local.date(), AFTER_HOURS_END, NEW_YORK)
        return (MarketStatus.AFTER_HOURS if moment < after.astimezone(timezone.utc)
                else MarketStatus.CLOSED)

    def next_close(self, now: datetime) -> Optional[datetime]:
        window = self.session(now.astimezone(NEW_YORK).date())
        return window.closes_at

    @staticmethod
    def governs(asset_class: str, currency: str) -> bool:
        return ((asset_class or "").lower() in US_EQUITY_CLASSES
                and (currency or "USD").upper() == "USD")
