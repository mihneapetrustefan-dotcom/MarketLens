"""
src/paper/freshness.py
---------------------------
How old is the data, really? (Phase 13, spec §8, §9, §55)

THE MOST IMPORTANT MODULE IN THIS PHASE
-------------------------------------------
Paper trading's whole claim to usefulness is that it behaves like the
live system would. That claim collapses if the data is not what a live
system would have received — and in this repository it currently is
not: the newest cached bar is days old, and there is no streaming feed
of any kind.

The honest response is not to pretend, and not to refuse to run. It is
to MEASURE the gap on every tick and let it gate trading. A session
running on four-day-old bars is a legitimate thing to run; a session
that displays four-day-old bars as live is not.

WHY THRESHOLDS ARE PER ASSET CLASS
--------------------------------------
Spec §9 forbids arbitrary universal thresholds, and the reason is
visible in this project's own data: equities produce one bar per
trading day, so a six-hour-old equity bar is completely normal, while a
six-hour-old crypto price means something stopped. One threshold across
both would either cry stale constantly or never fire.

NOTHING HERE FETCHES
------------------------
This module classifies data the caller already holds. It performs no
network I/O and knows nothing about providers, which is what lets the
same freshness rules apply to a cached bar, a live quote, or a replayed
observation without the module changing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence

from src.backtest.calendar import MarketCalendar
from src.domain.paper_models import (
    DEFAULT_FRESHNESS_POLICIES, DataFreshness, FreshnessPolicy, MarketDataStatus,
)
from src.paper.clock import require_utc


@dataclass
class FreshnessReport:
    """
    The data state behind one tick.

    `worst` drives the trading gate, because a portfolio priced with one
    stale instrument is a stale portfolio — averaging freshness across
    instruments would let a single dead feed hide behind healthy ones.
    """
    at: datetime
    statuses: Dict[str, MarketDataStatus] = field(default_factory=dict)

    def __post_init__(self):
        self.at = require_utc(self.at, "at")

    _SEVERITY = [DataFreshness.FRESH, DataFreshness.AGING, DataFreshness.STALE,
                 DataFreshness.INVALID, DataFreshness.UNAVAILABLE]

    @property
    def worst(self) -> DataFreshness:
        if not self.statuses:
            return DataFreshness.UNAVAILABLE
        worst = DataFreshness.FRESH
        for status in self.statuses.values():
            if self._SEVERITY.index(status.freshness) > self._SEVERITY.index(worst):
                worst = status.freshness
        return worst

    @property
    def tradeable_instruments(self) -> List[str]:
        return sorted(i for i, s in self.statuses.items() if s.is_tradeable)

    @property
    def blocked_instruments(self) -> List[str]:
        return sorted(i for i, s in self.statuses.items() if not s.is_tradeable)

    def status_for(self, instrument_id: str) -> Optional[MarketDataStatus]:
        return self.statuses.get(instrument_id)

    def is_tradeable(self, instrument_id: str) -> bool:
        status = self.statuses.get(instrument_id)
        return status is not None and status.is_tradeable

    def prices(self) -> Dict[str, Optional[float]]:
        """Every known price, tradeable or not — valuation may use stale marks."""
        return {i: s.price for i, s in self.statuses.items()}

    def tradeable_prices(self) -> Dict[str, float]:
        """Only prices fresh enough to back a new order."""
        return {i: s.price for i, s in self.statuses.items()
                if s.is_tradeable and s.price is not None}

    def summary(self) -> Dict[str, object]:
        counts: Dict[str, int] = {}
        for status in self.statuses.values():
            counts[status.freshness.value] = counts.get(status.freshness.value, 0) + 1
        oldest = None
        for status in self.statuses.values():
            age = status.age_seconds
            if age is not None and (oldest is None or age > oldest):
                oldest = age
        return {
            "at": self.at.isoformat(),
            "instruments": len(self.statuses),
            "worst": self.worst.value,
            "counts": counts,
            "oldest_age_seconds": oldest,
            "tradeable": len(self.tradeable_instruments),
            "blocked": len(self.blocked_instruments),
        }


class FreshnessMonitor:
    """
    Classifies the market data available at a moment.

    Reads prices through the Phase 12 calendar rather than issuing its
    own SQL: the calendar already enforces "no bar after the anchor",
    and duplicating that read would create a second place where a
    look-ahead bug could appear.
    """

    def __init__(self, calendar: MarketCalendar,
                 policies: Optional[Dict[str, FreshnessPolicy]] = None,
                 asset_class_by_instrument: Optional[Dict[str, Optional[str]]] = None):
        self.calendar = calendar
        self.policies = dict(policies or DEFAULT_FRESHNESS_POLICIES)
        self.asset_class_by_instrument = dict(asset_class_by_instrument or {})

    def policy_for(self, instrument_id: str) -> FreshnessPolicy:
        asset_class = self.asset_class_by_instrument.get(instrument_id)
        if asset_class and asset_class in self.policies:
            return self.policies[asset_class]
        return self.policies.get("default", DEFAULT_FRESHNESS_POLICIES["default"])

    def evaluate(self, instrument_ids: Sequence[str], now: datetime,
                 received_at: Optional[datetime] = None) -> FreshnessReport:
        """
        Classify every instrument's most recent observation at `now`.

        `received_at` defaults to `now`: this system reads from a cache
        it already holds, so receipt and evaluation are the same
        instant. A future streaming feed would pass the real receipt
        time and the difference would become measurable without this
        signature changing.
        """
        require_utc(now, "now")
        report = FreshnessReport(at=now)
        received = received_at or now

        for instrument_id in instrument_ids:
            bar = self.calendar.bar_at_or_before(instrument_id, now)
            asset_class = self.asset_class_by_instrument.get(instrument_id)
            policy = self.policy_for(instrument_id)

            if bar is None or bar.close is None:
                report.statuses[instrument_id] = MarketDataStatus(
                    instrument_id=instrument_id, asset_class=asset_class,
                    price=None, observed_at=None, received_at=received,
                    evaluated_at=now, freshness=DataFreshness.UNAVAILABLE,
                    source="price_candle_cache", is_cached=True)
                continue

            age = (now - bar.timestamp).total_seconds()
            report.statuses[instrument_id] = MarketDataStatus(
                instrument_id=instrument_id, asset_class=asset_class,
                price=bar.close, observed_at=bar.timestamp,
                received_at=received, evaluated_at=now,
                freshness=policy.classify(age),
                source="price_candle_cache", is_cached=True)

        return report

    def describe(self) -> Dict[str, object]:
        return {
            "policies": {
                name: {
                    "fresh_seconds": policy.fresh_seconds,
                    "aging_seconds": policy.aging_seconds,
                    "stale_seconds": policy.stale_seconds,
                } for name, policy in self.policies.items()
            },
            "source": "price_candle_cache (stored bars, not a live feed)",
            "limitation": (
                "this system has no streaming market feed; every price is a "
                "cached bar, so freshness measures how old the cache is"),
        }
