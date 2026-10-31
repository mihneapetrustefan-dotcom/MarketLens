"""
src/trading/pricing.py
-----------------------------
Which prices the trading loop may decide on (Phase 25.9E).

THE DEFECT
--------------
Phase 25.7 built an operational price layer and a structural rule --
`operational_price()` cannot reach the research cache -- and nothing on
the order path used it. `TradingLoop` built a `PortfolioService`, whose
`PriceRepository` reads `price_candle_cache`, and every price that fed
eligibility, sizing, valuation, risk, targets and the order's reference
price came from there, accepted up to FIVE DAYS old. The session runner
computed operational prices only to print them.

On the production snapshot of 2026-09-16 the newest daily close was
eleven days old. Had a model qualified, a paper order would have been
sized and risk-checked on a price from the previous week.

THE FIX
-----------
`OperationalPriceRepository` is a `PriceRepository` whose point-in-time
price comes from `market_data_state` and nothing else: only quotes that
are tradeable -- fresh, live, available -- at the moment the decision
is made. Everything else is absent, never substituted.

History is a different question. Volatility and return series are
legitimately research data (a one-year daily series is not something a
60-second poller can produce), so those calls are delegated to the
research repository unchanged.

`resolve_price_source` makes the choice structural: against a real
venue the source is OPERATIONAL whatever the configuration says. Only
the deterministic mock may use the research cache, which is how the
existing replay-style tests keep a price at all.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.marketdata.prices import operational_price
from src.portfolio.valuation import PricePoint, PriceRepository

OPERATIONAL = "operational"
RESEARCH = "research"

#: The one transport allowed to decide on research prices.
MOCK_TRANSPORT = "mock"


def resolve_price_source(transport_name: str, configured: str) -> str:
    """
    OPERATIONAL for every real venue, whatever was configured.

    A configuration value cannot move a real session onto cached
    closes: that is the confusion Phase 25.7 named as forbidden, and a
    flag is exactly how it would come back.
    """
    if (transport_name or "") != MOCK_TRANSPORT:
        return OPERATIONAL
    return OPERATIONAL if configured == OPERATIONAL else RESEARCH


class OperationalPriceRepository(PriceRepository):
    """
    Current prices from the operational layer; history from research.

    `evaluated_at` is the wall-clock moment of the decision. Freshness
    is judged at that moment, and a quote stamped after it is refused as
    future information -- the point-in-time rule for a live decision,
    whose information cutoff is when it is made.
    """

    def __init__(self, conn: sqlite3.Connection, evaluated_at: datetime,
                 history: Optional[PriceRepository] = None):
        super().__init__(conn)
        self.evaluated_at = evaluated_at.astimezone(timezone.utc)
        self.history = history or PriceRepository(conn)
        #: Seconds, not days: operational freshness is enforced by the
        #: quote policy (stale after 900s). The inherited day limit is
        #: kept only so the valuator's own check never loosens it.
        self.max_price_age_days = self.history.max_price_age_days
        self.refused: Dict[str, str] = {}

    def prices_as_of(self, instrument_ids: Sequence[str],
                     as_of: datetime) -> Dict[str, PricePoint]:
        found: Dict[str, PricePoint] = {}
        for instrument_id in sorted({i for i in instrument_ids if i}):
            quote = operational_price(self.conn, instrument_id,
                                      self.evaluated_at)
            if not quote.is_tradeable or quote.price is None or quote.price <= 0:
                self.refused[instrument_id] = (
                    f"{quote.freshness.value}/{quote.availability}"
                    + (f": {quote.note}" if quote.note else ""))
                continue
            stamp = quote.as_of
            if stamp is None:
                self.refused[instrument_id] = "quote carries no timestamp"
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if stamp > self.evaluated_at:
                self.refused[instrument_id] = (
                    "quote is stamped after the decision moment")
                continue
            found[instrument_id] = PricePoint(
                instrument_id=instrument_id, timestamp=stamp,
                price=float(quote.price))
        return found

    # History is research data by nature; delegated, never faked.
    def close_series_batch(self, *args, **kwargs):
        return self.history.close_series_batch(*args, **kwargs)

    def return_series_batch(self, *args, **kwargs):
        return self.history.return_series_batch(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.history, name)


def freshest_operational_age_days(conn: sqlite3.Connection,
                                  now: datetime) -> Optional[float]:
    """
    Age of the freshest TRADEABLE operational quote, or None.

    The loop's market-data health reading in operational mode. A stale
    or delayed quote does not count, so a disconnected feed ages into a
    blocked cycle rather than resting on the last good number.
    """
    try:
        rows: List[str] = [r[0] for r in conn.execute(
            "SELECT instrument_id FROM market_data_state")]
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return None
        raise
    best: Optional[float] = None
    for instrument_id in rows:
        quote = operational_price(conn, instrument_id, now)
        if not quote.is_tradeable or quote.age_seconds is None:
            continue
        age = max(0.0, quote.age_seconds) / 86400.0
        best = age if best is None else min(best, age)
    return best
