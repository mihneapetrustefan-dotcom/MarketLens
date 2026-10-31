"""
src/domain/market_data_models.py
-------------------------------------------
Operational market data (Phase 25.7).

WHAT THIS IS, AND WHAT IT IS NOT
------------------------------------
This module describes CURRENT market state: what an instrument is
trading at right now, how old that observation is, and whether it can
be trusted enough to act on.

It is deliberately separate from `price_candle_cache`, which is the
reproducible RESEARCH record. The two answer different questions and
must never be confused:

    price_candle_cache      "what happened, reproducibly"
    market_data_state       "what is happening, right now"

A research bar is immutable and point-in-time; an operational quote is
replaced every cycle and is worthless the moment it goes stale. Writing
live observations into the research cache would make an event study
computed today differ from the same study recomputed tomorrow, which
is the one property the research system exists to guarantee.

WHY THE VOCABULARY IS BORROWED, NOT REINVENTED
--------------------------------------------------
`DataFreshness`, `FreshnessPolicy` and `HealthState` already exist in
`paper_models`, and Phase 13 already reasoned carefully about them.
Phase 25.7 reuses them rather than introducing a second, competing set
of words for the same ideas. What this module adds is only what did
not exist: a quote that came from a live venue rather than a stored
bar, the bars built from those quotes, and the health of the service
that acquires them.

`MarketDataStatus` in `paper_models` carries the comment "True when the
price came from a stored bar rather than a live quote. Every price in
this system currently does." This module is what makes that sentence
stop being true.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from src.domain.paper_models import DataFreshness, FreshnessPolicy, HealthState


def _require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc)


class MarketDataAvailability(str, Enum):
    """
    What market data this account actually has (spec §18).

    Modelled explicitly because an IBKR account does NOT automatically
    carry every subscription, and a delayed quote presented as live is
    the kind of error that only shows up in the fill price.

    DEFINED HERE rather than in the IBKR adapter because it is a
    property of market data itself, not of one broker's wire format.
    `adapters/ibkr/gateway.py` re-exports this name so every existing
    import keeps working.
    """
    AVAILABLE = "available"
    DELAYED = "delayed"
    RESTRICTED = "restricted"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"

    @property
    def is_tradeable(self) -> bool:
        """
        Only genuinely live data backs an order.

        DELAYED is excluded deliberately. A delayed quote is fine for a
        dashboard and wrong for a limit price, and the difference is
        invisible in the number itself.
        """
        return self is MarketDataAvailability.AVAILABLE


class OperationalSource(str, Enum):
    """
    Where a current price came from.

    There is deliberately NO member for the historical cache. A
    research bar is not a current price, and the absence of a name for
    that combination is what stops it being recorded as one. Research
    consumers that want a cached bar read `price_candle_cache`
    directly, where its age is obvious.
    """
    IBKR_SNAPSHOT = "ibkr_snapshot"


#: Freshness for INTRADAY OPERATIONAL data.
#:
#: Phase 13's defaults (fresh < 15 min, aging < 1 day) are correct for
#: a paper system running on daily bars and far too loose for acting on
#: a price now. A quote two minutes old is not a current price for a
#: system polling every minute.
#:
#: Same `FreshnessPolicy` class, so there remains ONE definition of
#: what fresh means; only the numbers differ, which is exactly the
#: variation that class was built to express.
OPERATIONAL_FRESHNESS = FreshnessPolicy(
    asset_class="operational_intraday",
    fresh_seconds=120.0,        # two polling cycles at the 60s default
    aging_seconds=300.0,        # five minutes: usable, visibly degraded
    stale_seconds=900.0,        # beyond this it is not market state
)


@dataclass
class OperationalQuote:
    """
    One current observation of one instrument, from a live venue.

    THREE TIMESTAMPS, KEPT APART
        broker_at    when the venue says the quote happened
        received_at  when this process saw it
        evaluated_at when freshness was judged

    Collapsing any pair of them makes stale data look current, which is
    the failure this whole module exists to prevent.
    """
    instrument_id: str
    conid: str = ""
    last: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    volume: Optional[float] = None
    availability: MarketDataAvailability = MarketDataAvailability.UNKNOWN
    broker_at: Optional[datetime] = None
    received_at: Optional[datetime] = None
    evaluated_at: Optional[datetime] = None
    source: OperationalSource = OperationalSource.IBKR_SNAPSHOT
    session_id: str = ""
    #: Set when the quote could not be used, naming why. Never a
    #: silent None.
    note: str = ""

    def __post_init__(self):
        for name in ("broker_at", "received_at", "evaluated_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))

    @property
    def reference_time(self) -> Optional[datetime]:
        """
        The moment the price describes.

        `broker_at` when the venue told us, otherwise `received_at`.
        Never invented: if neither exists the age is unknown, and
        unknown age is not freshness.
        """
        return self.broker_at or self.received_at

    def age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        reference = self.reference_time
        moment = now or self.evaluated_at
        if reference is None or moment is None:
            return None
        return (_require_utc(moment, "now") - reference).total_seconds()

    def freshness(self, now: Optional[datetime] = None,
                  policy: FreshnessPolicy = OPERATIONAL_FRESHNESS
                  ) -> DataFreshness:
        """
        Classify this quote.

        Order matters. An unusable availability outranks age: a DELAYED
        quote that arrived one second ago is recent and still must not
        back an order, so it is never reported FRESH.
        """
        if self.availability is MarketDataAvailability.UNAVAILABLE:
            return DataFreshness.UNAVAILABLE
        if self.availability in (MarketDataAvailability.RESTRICTED,
                                 MarketDataAvailability.UNKNOWN):
            return DataFreshness.UNAVAILABLE
        if self.reference_price is None:
            #: A quote with no price is not a quote.
            return DataFreshness.INVALID
        age = self.age_seconds(now)
        if age is None:
            return DataFreshness.UNAVAILABLE
        if age < 0:
            #: The venue cannot have produced this after we received
            #: it. Clock skew or a malformed payload; either way it is
            #: not something to trade on.
            return DataFreshness.INVALID
        classified = policy.classify(age)
        if self.availability is MarketDataAvailability.DELAYED:
            #: Delayed data is capped: never better than AGING, so it
            #: can inform a dashboard and can never read as live.
            if classified is DataFreshness.FRESH:
                return DataFreshness.AGING
        return classified

    @property
    def reference_price(self) -> Optional[float]:
        """Mid where both sides exist, otherwise last. Never invented."""
        if self.mid is not None:
            return self.mid
        return self.last

    def is_tradeable(self, now: Optional[datetime] = None,
                     policy: FreshnessPolicy = OPERATIONAL_FRESHNESS) -> bool:
        """
        Fresh enough AND live enough to back an order.

        Both conditions, deliberately. `DataFreshness.is_tradeable`
        allows AGING, which is right for a daily paper system; here a
        DELAYED quote is forced to AGING, so freshness alone would let
        delayed data through. Availability is checked separately.
        """
        return (self.availability.is_tradeable
                and self.freshness(now, policy).is_tradeable)

    def to_row(self) -> Dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "conid": self.conid,
            "last": self.last,
            "bid": self.bid,
            "ask": self.ask,
            "mid": self.mid,
            "volume": self.volume,
            "availability": self.availability.value,
            "broker_at": self.broker_at.isoformat() if self.broker_at else None,
            "received_at": (self.received_at.isoformat()
                            if self.received_at else None),
            "source": self.source.value,
            "session_id": self.session_id,
            "note": self.note,
        }


@dataclass
class MinuteBar:
    """
    One completed minute, built from operational quotes.

    `is_complete` is the whole point. A minute still in progress is not
    a bar, and handing a partial minute to a strategy that expects a
    closed one silently changes what the strategy is reacting to.
    """
    instrument_id: str
    bar_start: datetime
    bar_end: datetime
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    observation_count: int = 0
    is_complete: bool = False
    source: OperationalSource = OperationalSource.IBKR_SNAPSHOT
    session_id: str = ""
    #: Minutes for which no observation arrived at all. Recorded, never
    #: interpolated -- an invented bar is indistinguishable from a real
    #: one once it is stored.
    is_gap: bool = False

    def __post_init__(self):
        self.bar_start = _require_utc(self.bar_start, "bar_start")
        self.bar_end = _require_utc(self.bar_end, "bar_end")

    @property
    def minute_key(self) -> str:
        return self.bar_start.strftime("%Y-%m-%dT%H:%M")


@dataclass
class UniverseEntry:
    """One instrument the service is allowed to poll."""
    instrument_id: str
    conid: str
    broker_symbol: str = ""
    asset_class: str = "stock"
    venue: str = ""
    currency: str = "USD"
    tradable: bool = True
    #: Why this instrument is NOT in the active universe, when it is
    #: excluded. Named rather than silently dropped.
    excluded_reason: str = ""

    @property
    def is_active(self) -> bool:
        return bool(self.conid) and self.tradable and not self.excluded_reason


@dataclass
class MarketDataCycle:
    """One acquisition cycle, for diagnosis and capacity accounting."""
    cycle_id: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    session_id: str = ""
    requested: int = 0
    received: int = 0
    tradeable: int = 0
    stale: int = 0
    unavailable: int = 0
    invalid: int = 0
    broker_requests: int = 0
    bars_written: int = 0
    gaps_recorded: int = 0
    health: HealthState = HealthState.HEALTHY
    notes: List[str] = field(default_factory=list)

    def __post_init__(self):
        self.started_at = _require_utc(self.started_at, "started_at")
        self.finished_at = _require_utc(self.finished_at, "finished_at")

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def overran(self, interval_seconds: float) -> bool:
        """
        A cycle that cannot finish inside its own interval.

        Reported rather than absorbed: overlapping polling is how one
        slow cycle becomes an uncontrolled request storm.
        """
        duration = self.duration_seconds
        return duration is not None and duration > interval_seconds


@dataclass
class MarketDataHealthReport:
    """
    Whether the service can currently answer "what is this worth".

    `overall` is the WORST reading, never an average -- one blind
    instrument is not cancelled out by nine healthy ones.
    """
    evaluated_at: datetime
    connected: bool = False
    session_open: bool = False
    universe_size: int = 0
    fresh: int = 0
    aging: int = 0
    stale: int = 0
    unavailable: int = 0
    invalid: int = 0
    delayed_instruments: List[str] = field(default_factory=list)
    missing_instruments: List[str] = field(default_factory=list)
    budget_used: int = 0
    budget_limit: int = 0
    last_cycle_at: Optional[datetime] = None
    last_error: str = ""
    reasons: List[str] = field(default_factory=list)

    def __post_init__(self):
        self.evaluated_at = _require_utc(self.evaluated_at, "evaluated_at")
        self.last_cycle_at = _require_utc(self.last_cycle_at, "last_cycle_at")

    @property
    def budget_headroom(self) -> Optional[int]:
        if not self.budget_limit:
            return None
        return self.budget_limit - self.budget_used

    @property
    def overall(self) -> HealthState:
        if not self.connected:
            return HealthState.FAILED
        if not self.session_open:
            #: Closed is not broken. There is simply nothing to
            #: observe, and no order should be created either way.
            return HealthState.PAUSED
        if self.universe_size and self.fresh == 0:
            return HealthState.FAILED
        if self.missing_instruments or self.stale or self.invalid:
            return HealthState.DEGRADED
        if self.delayed_instruments:
            return HealthState.DEGRADED
        return HealthState.HEALTHY

    @property
    def usable_for_trading(self) -> bool:
        return self.overall.allows_new_orders and self.fresh > 0

    def summary(self) -> Dict[str, Any]:
        return {
            "overall": self.overall.value,
            "connected": self.connected,
            "session_open": self.session_open,
            "universe": self.universe_size,
            "fresh": self.fresh,
            "aging": self.aging,
            "stale": self.stale,
            "unavailable": self.unavailable,
            "invalid": self.invalid,
            "delayed": len(self.delayed_instruments),
            "missing": len(self.missing_instruments),
            "budget": f"{self.budget_used}/{self.budget_limit}",
            "usable_for_trading": self.usable_for_trading,
        }
