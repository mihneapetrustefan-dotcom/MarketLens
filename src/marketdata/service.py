"""
src/marketdata/service.py
-------------------------------------------
The market-data service (Phase 25.7).

ONE RESPONSIBILITY
----------------------
Acquire current market prices from IBKR, validate them, normalise
them, update current state, and produce intraday bars.

It does NOT generate trades, call execution, touch risk, or write to
the research cache. Those boundaries are asserted by tests, because a
market-data component that can place an order is no longer a
market-data component.

WHY POLLING AND NOT A WEBSOCKET
-----------------------------------
Checked against the repository rather than assumed:

  - there is no persistent runtime anywhere in this project. Every
    entry point is a batch job under GitHub Actions cron, and
    `docs/API_AUDIT.md` records the deliberate absence of any server,
    worker or queue. A websocket needs a process to hold it.
  - `transport.market_snapshot` already accepts a LIST of conids, so
    the entire active universe costs ONE request per cycle.
  - no current strategy reads below a 5-minute horizon.

So 60-second bounded snapshot polling covers the requirement at about
1/50th of the request budget. A websocket would add a daemon, a
reconnect state machine and an ordering problem to buy resolution
nothing currently consumes. If a sub-minute strategy ever appears,
this module is the seam to change and the rest of the pipeline does
not move.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from src.data_access.market_data_schema import initialize_market_data_schema
from src.domain.broker_models import MarketStatus
from src.domain.market_data_models import (
    MarketDataCycle, MarketDataHealthReport, MinuteBar, OperationalQuote,
    UniverseEntry,
)
from src.domain.paper_models import DataFreshness, HealthState
from src.marketdata import quotes as quote_acquisition
from src.marketdata import universe as universe_module
from src.marketdata.bars import MinuteBarBuilder
from src.marketdata.repository import MarketDataRepository

#: One cycle a minute. Explicit configuration, not a constant buried in
#: a loop -- §15 requires the frequency to be inspectable and sized
#: against the budget before use.
DEFAULT_INTERVAL_SECONDS = 60.0

#: A cycle claim older than this is assumed abandoned. Matches the
#: trading loop's own reclaim discipline rather than inventing a second
#: convention.
DEFAULT_CLAIM_TIMEOUT_SECONDS = 300.0


def session_id_for(now: datetime) -> str:
    """
    One session per UTC calendar day.

    Deliberately coarse. A finer session identity needs a real venue
    calendar with holidays and early closes, which this project does
    not yet have -- see `MarketSessionView` below for exactly what is
    and is not known.
    """
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def cycle_id_for(session_id: str, started_at: datetime) -> str:
    """Deterministic, so a retried cycle replaces rather than duplicates."""
    raw = f"{session_id}|{started_at.astimezone(timezone.utc):%Y-%m-%dT%H:%M}"
    return f"mdc-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


class MarketSessionView:
    """
    What this project actually knows about trading sessions.

    HONEST ABOUT ITS LIMITS. `MarketStatus` declares PRE_MARKET,
    AFTER_HOURS and HOLIDAY, but the gateway can only distinguish a
    session day from a non-session day plus a live venue quote. So
    those members are never GUESSED here: the view reports OPEN,
    CLOSED or UNKNOWN and says which source decided.

    Building a second market calendar was explicitly out of scope
    (§16), so this delegates to `IBKRGateway.market_status`, which
    already consults the Phase 12 calendar first and the venue second.
    """

    def __init__(self, gateway):
        self.gateway = gateway

    def status(self, instrument_id: str, now: datetime) -> MarketStatus:
        try:
            return self.gateway.market_status(instrument_id, now)
        except Exception:                                 # noqa: BLE001
            return MarketStatus.UNKNOWN

    def any_open(self, entries: Sequence[UniverseEntry],
                 now: datetime) -> bool:
        """
        True when at least one instrument is in session.

        Any, not all: a universe spanning venues does not open at one
        moment, and blocking the whole service because one instrument
        is shut would blind the ones that are trading.
        """
        for entry in entries:
            if self.status(entry.instrument_id, now) is MarketStatus.OPEN:
                return True
        return False


class MarketDataService:
    """
    Acquires and maintains current market state.

    Stateless between invocations except for the bar builder, which is
    passed in when a caller wants bars to span cycles. A single
    `run_cycle` is safe to call from a batch job.
    """

    def __init__(self, conn: sqlite3.Connection, gateway,
                 interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
                 builder: Optional[MinuteBarBuilder] = None,
                 broker_id: str = "ibkr"):
        self.conn = conn
        self.gateway = gateway
        self.interval_seconds = float(interval_seconds)
        self.broker_id = broker_id
        self.repository = MarketDataRepository(conn)
        self.session = MarketSessionView(gateway)
        self.builder = builder
        self.last_error = ""

    # ---------------- capacity ----------------

    def capacity(self, entries: Sequence[UniverseEntry]) -> Dict[str, object]:
        """
        Whether this universe fits the budget at this interval.

        Answered before any request is sent. One batched snapshot per
        cycle means requests_per_cycle is 1 regardless of universe
        size, which is the entire reason batching matters here.
        """
        budget = getattr(self.gateway.config, "max_requests_per_minute", 0)
        return universe_module.capacity_report(
            entry_count=len(universe_module.active(entries)),
            requests_per_cycle=1,
            interval_seconds=self.interval_seconds,
            budget_per_minute=int(budget or 0))

    # ---------------- one cycle ----------------

    def run_cycle(self, now: Optional[datetime] = None,
                  limit: Optional[int] = None,
                  instruments: Optional[Sequence[str]] = None,
                  write: bool = True) -> MarketDataCycle:
        """
        Acquire once for the active universe.

        Never raises for ordinary market-data failure. A disconnected
        broker, a cold contract or a malformed payload all degrade the
        cycle and are recorded; the caller sees a cycle with a health
        state, not an exception.
        """
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        session_id = session_id_for(now)
        started = time.monotonic()
        cycle = MarketDataCycle(
            cycle_id=cycle_id_for(session_id, now),
            started_at=now, session_id=session_id)

        initialize_market_data_schema(self.conn)
        entries = universe_module.resolve_universe(
            self.conn, broker_id=self.broker_id, limit=limit,
            instruments=instruments)
        active = universe_module.active(entries)
        blocked = universe_module.excluded(entries)
        cycle.requested = len(active)
        for instrument_id, reason in sorted(blocked.items()):
            cycle.notes.append(f"excluded {instrument_id}: {reason}")

        if not active:
            cycle.health = HealthState.FAILED
            cycle.notes.append(
                "no instrument has a resolved IBKR contract; nothing to poll")
            return self._finish(cycle, started, write)

        fit = self.capacity(entries)
        if not fit["fits"]:
            # A sizing problem is reported and the cycle refuses. The
            # alternative -- raising the broker limit -- is forbidden.
            cycle.health = HealthState.FAILED
            cycle.notes.append(
                f"universe of {fit['instruments']} needs "
                f"{fit['requests_per_minute']} req/min against a budget of "
                f"{fit['budget_per_minute']}; refusing rather than exceeding it")
            return self._finish(cycle, started, write)

        if not self.session.any_open(active, now):
            # Closed is not broken. Nothing is acquired and the state
            # from the previous session is left exactly as it was,
            # carrying its own timestamps so its age is obvious.
            cycle.health = HealthState.PAUSED
            cycle.notes.append("no instrument is in session; nothing acquired")
            return self._finish(cycle, started, write)

        acquired, requests, error = quote_acquisition.acquire(
            self.gateway.transport, active, now, session_id=session_id)
        cycle.broker_requests = requests
        if error:
            self.last_error = error
            cycle.notes.append(f"acquisition error: {error}")

        cycle.received = sum(1 for q in acquired
                             if q.reference_price is not None)
        for quote in acquired:
            freshness = quote.freshness(now)
            if quote.is_tradeable(now):
                cycle.tradeable += 1
            elif freshness is DataFreshness.STALE:
                cycle.stale += 1
            elif freshness is DataFreshness.INVALID:
                cycle.invalid += 1
            elif freshness is DataFreshness.UNAVAILABLE:
                cycle.unavailable += 1

        bars: List[MinuteBar] = []
        if self.builder is not None:
            for quote in acquired:
                if quote.reference_price is not None:
                    bars.extend(self.builder.observe(quote, now))
            bars.extend(self.builder.flush(now))
            cycle.gaps_recorded = sum(1 for b in bars if b.is_gap)

        if write:
            self.repository.upsert_quotes(acquired, now)
            if bars:
                cycle.bars_written = self.repository.write_bars(bars, now)

        if cycle.tradeable == 0:
            cycle.health = HealthState.FAILED
            cycle.notes.append("no instrument produced a tradeable quote")
        elif cycle.tradeable < cycle.requested:
            cycle.health = HealthState.DEGRADED
            cycle.notes.append(
                f"{cycle.requested - cycle.tradeable} of {cycle.requested} "
                f"instrument(s) are not tradeable this cycle")

        return self._finish(cycle, started, write)

    def _finish(self, cycle: MarketDataCycle, started: float,
                write: bool) -> MarketDataCycle:
        cycle.finished_at = datetime.now(timezone.utc)
        if cycle.overran(self.interval_seconds):
            # Detected and said, per §30. A cycle that cannot finish
            # inside its interval is how overlapping polling starts.
            cycle.notes.append(
                f"cycle took {cycle.duration_seconds:.1f}s against a "
                f"{self.interval_seconds:.0f}s interval")
            if cycle.health is HealthState.HEALTHY:
                cycle.health = HealthState.DEGRADED
        if write:
            self.repository.record_cycle(cycle)
        return cycle

    # ---------------- health ----------------

    def health(self, now: Optional[datetime] = None,
               limit: Optional[int] = None) -> MarketDataHealthReport:
        """Whether current state can currently be trusted."""
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        entries = universe_module.resolve_universe(
            self.conn, broker_id=self.broker_id, limit=limit)
        active = universe_module.active(entries)

        connected = False
        try:
            connected = bool(self.gateway.connection_state().value == "connected")
        except Exception:                                 # noqa: BLE001
            connected = False

        report = MarketDataHealthReport(
            evaluated_at=now,
            connected=connected,
            session_open=self.session.any_open(active, now) if active else False,
            universe_size=len(active),
            budget_limit=int(getattr(self.gateway.config,
                                     "max_requests_per_minute", 0) or 0),
            last_error=self.last_error,
        )

        stored = {row["instrument_id"]: row
                  for row in self.repository.all_latest()}
        for entry in active:
            row = stored.get(entry.instrument_id)
            if row is None:
                report.missing_instruments.append(entry.instrument_id)
                continue
            # Re-judged against the CURRENT clock. The stored verdict
            # was true when written and says nothing about now.
            quote = _quote_from_row(row, now)
            freshness = quote.freshness(now)
            if quote.availability.value == "delayed":
                report.delayed_instruments.append(entry.instrument_id)
            if freshness is DataFreshness.FRESH:
                report.fresh += 1
            elif freshness is DataFreshness.AGING:
                report.aging += 1
            elif freshness is DataFreshness.STALE:
                report.stale += 1
            elif freshness is DataFreshness.INVALID:
                report.invalid += 1
            else:
                report.unavailable += 1

        last = self.repository.last_cycle_at()
        if last:
            try:
                report.last_cycle_at = datetime.fromisoformat(last)
            except ValueError:
                pass
        for instrument_id in report.missing_instruments:
            report.reasons.append(
                f"{instrument_id} has no stored market state")
        return report


def _quote_from_row(row: Dict[str, object],
                    now: datetime) -> OperationalQuote:
    """Rebuild a quote from stored state so freshness can be re-judged."""
    from src.domain.market_data_models import (
        MarketDataAvailability, OperationalSource,
    )

    def moment(key: str) -> Optional[datetime]:
        raw = row.get(key)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            return None

    try:
        availability = MarketDataAvailability(str(row.get("availability")))
    except ValueError:
        availability = MarketDataAvailability.UNKNOWN
    return OperationalQuote(
        instrument_id=str(row.get("instrument_id")),
        conid=str(row.get("conid") or ""),
        last=row.get("last"), bid=row.get("bid"), ask=row.get("ask"),
        mid=row.get("mid"), volume=row.get("volume"),
        availability=availability,
        broker_at=moment("broker_at"), received_at=moment("received_at"),
        evaluated_at=now,
        source=OperationalSource.IBKR_SNAPSHOT,
        session_id=str(row.get("session_id") or ""),
        note=str(row.get("note") or ""),
    )
