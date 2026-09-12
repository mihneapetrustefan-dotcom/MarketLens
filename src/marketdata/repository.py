"""
src/marketdata/repository.py
-------------------------------------------
Persistence for operational market data (Phase 25.7).

`market_data_state` is UPSERTED: one row per instrument, replaced in
place, no history. `market_data_bars` is INSERT OR IGNORE on
(instrument, bar_start) so a replayed cycle cannot rewrite a minute
that was already published.

Nothing in this module touches `price_candle_cache`. That is asserted
by a test, not just stated here.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from src.domain.market_data_models import (
    MarketDataCycle, MinuteBar, OperationalQuote,
)
from src.domain.paper_models import DataFreshness


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


class MarketDataRepository:
    """Reads and writes the operational market-data tables."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------------- current state ----------------

    def upsert_quotes(self, quotes: Sequence[OperationalQuote],
                      now: datetime) -> int:
        """
        Replace the current state for each instrument.

        The freshness stored is the one judged AT WRITE TIME. It is a
        snapshot of a verdict, not a substitute for re-judging: a
        reader that cares whether the price is still fresh must
        re-evaluate against its own clock, which is why the three
        timestamps are stored alongside it.
        """
        written = 0
        for quote in quotes:
            freshness = quote.freshness(now)
            self.conn.execute("""
                INSERT INTO market_data_state
                  (instrument_id, conid, last, bid, ask, mid, volume,
                   availability, freshness, broker_at, received_at,
                   evaluated_at, source, session_id, note, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(instrument_id) DO UPDATE SET
                  conid=excluded.conid, last=excluded.last, bid=excluded.bid,
                  ask=excluded.ask, mid=excluded.mid, volume=excluded.volume,
                  availability=excluded.availability,
                  freshness=excluded.freshness, broker_at=excluded.broker_at,
                  received_at=excluded.received_at,
                  evaluated_at=excluded.evaluated_at, source=excluded.source,
                  session_id=excluded.session_id, note=excluded.note,
                  updated_at=excluded.updated_at
            """, (quote.instrument_id, quote.conid, quote.last, quote.bid,
                  quote.ask, quote.mid, quote.volume,
                  quote.availability.value, freshness.value,
                  _iso(quote.broker_at), _iso(quote.received_at),
                  _iso(quote.evaluated_at), quote.source.value,
                  quote.session_id, quote.note, _iso(now)))
            written += 1
        self.conn.commit()
        return written

    def latest(self, instrument_id: str) -> Optional[Dict[str, object]]:
        row = self.conn.execute(
            "SELECT * FROM market_data_state WHERE instrument_id = ?",
            (instrument_id,)).fetchone()
        if row is None:
            return None
        columns = [d[0] for d in self.conn.execute(
            "SELECT * FROM market_data_state LIMIT 0").description]
        return dict(zip(columns, row))

    def all_latest(self) -> List[Dict[str, object]]:
        columns = [d[0] for d in self.conn.execute(
            "SELECT * FROM market_data_state LIMIT 0").description]
        return [dict(zip(columns, row)) for row in self.conn.execute(
            "SELECT * FROM market_data_state ORDER BY instrument_id")]

    # ---------------- bars ----------------

    def write_bars(self, bars: Sequence[MinuteBar], now: datetime) -> int:
        """
        Persist completed bars.

        INSERT OR IGNORE, so re-running a cycle is harmless and an
        already-published minute is never rewritten by a later
        arrival.
        """
        written = 0
        for bar in bars:
            cursor = self.conn.execute("""
                INSERT OR IGNORE INTO market_data_bars
                  (instrument_id, bar_start, bar_end, open, high, low, close,
                   volume, observation_count, is_complete, is_gap, source,
                   session_id, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (bar.instrument_id, _iso(bar.bar_start), _iso(bar.bar_end),
                  bar.open, bar.high, bar.low, bar.close, bar.volume,
                  bar.observation_count, int(bar.is_complete), int(bar.is_gap),
                  bar.source.value, bar.session_id, _iso(now)))
            written += cursor.rowcount if cursor.rowcount > 0 else 0
        self.conn.commit()
        return written

    def bars_for(self, instrument_id: str, limit: int = 50,
                 complete_only: bool = True) -> List[Dict[str, object]]:
        sql = ("SELECT * FROM market_data_bars WHERE instrument_id = ? "
               + ("AND is_complete = 1 " if complete_only else "")
               + "ORDER BY bar_start DESC LIMIT ?")
        columns = [d[0] for d in self.conn.execute(
            "SELECT * FROM market_data_bars LIMIT 0").description]
        return [dict(zip(columns, row))
                for row in self.conn.execute(sql, (instrument_id, limit))]

    # ---------------- cycles ----------------

    def record_cycle(self, cycle: MarketDataCycle) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO market_data_cycles
              (cycle_id, session_id, started_at, finished_at,
               duration_seconds, requested, received, tradeable, stale,
               unavailable, invalid, broker_requests, bars_written,
               gaps_recorded, health, notes_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (cycle.cycle_id, cycle.session_id, _iso(cycle.started_at),
              _iso(cycle.finished_at), cycle.duration_seconds,
              cycle.requested, cycle.received, cycle.tradeable, cycle.stale,
              cycle.unavailable, cycle.invalid, cycle.broker_requests,
              cycle.bars_written, cycle.gaps_recorded, cycle.health.value,
              json.dumps(cycle.notes)))
        self.conn.commit()

    def recent_cycles(self, limit: int = 20) -> List[Dict[str, object]]:
        columns = [d[0] for d in self.conn.execute(
            "SELECT * FROM market_data_cycles LIMIT 0").description]
        return [dict(zip(columns, row)) for row in self.conn.execute(
            "SELECT * FROM market_data_cycles ORDER BY started_at DESC LIMIT ?",
            (limit,))]

    def last_cycle_at(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT MAX(started_at) FROM market_data_cycles").fetchone()
        return row[0] if row else None
