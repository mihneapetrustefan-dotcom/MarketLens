"""
src/execution/instruments.py
---------------------------------
Canonical instrument to broker symbol (Phase 14, spec §15).

WHY THIS IS ITS OWN LAYER
-----------------------------
The core system knows an instrument by `instrument_id`. A venue knows
it as `AAPL`, or `AAPL.US`, or `EURUSD.a`, or a contract object with an
exchange and a currency attached. If any of those spellings leaks
upward, every consumer of an instrument has to learn every venue's
dialect, and adding the second broker becomes a rewrite rather than an
adapter.

So the mapping is a table, not a function. Venue-specific facts — tick
size, lot size, quantity increment, contract multiplier, whether it is
tradable at all — belong to the (instrument, broker) pair rather than
to the instrument, because the same security is fractional at one
venue and whole-lot at another.

WHAT THE REGISTRY REFUSES TO GUESS
--------------------------------------
There is no fallback that turns an unmapped instrument into a symbol by
string manipulation. `AAPL` happening to be a valid symbol at some
broker is not evidence that it is the right symbol at this one, and a
guess that is right most of the time is the worst kind: it works until
it silently trades the wrong contract. An unmapped instrument produces
`NO_INSTRUMENT_MAPPING` and no order.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.domain.broker_models import (
    BrokerInstrumentMapping, ExecutionRejectCode,
)


def _loads(raw) -> dict:
    """Parse a stored payload, falling back rather than failing a load."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass
class MappingResolution:
    """A lookup outcome that carries its own failure reason."""
    mapping: Optional[BrokerInstrumentMapping] = None
    code: Optional[ExecutionRejectCode] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.mapping is not None and self.code is None


class InstrumentRegistry:
    """
    Holds instrument mappings for every registered broker.

    Loads from the database when one is supplied and works entirely in
    memory otherwise, so an adapter or a test can register mappings
    without persistence.
    """

    def __init__(self, conn: Optional[sqlite3.Connection] = None):
        self.conn = conn
        #: (broker_id, canonical_instrument_id) -> mapping
        self._by_pair: Dict[Tuple[str, str], BrokerInstrumentMapping] = {}
        #: (broker_id, broker_symbol) -> canonical id, for inbound events
        self._by_symbol: Dict[Tuple[str, str], str] = {}

    # ---------------- registration ----------------

    def register(self, mapping: BrokerInstrumentMapping) -> BrokerInstrumentMapping:
        key = (mapping.broker_id, mapping.canonical_instrument_id)
        self._by_pair[key] = mapping
        self._by_symbol[(mapping.broker_id, mapping.broker_symbol)] = \
            mapping.canonical_instrument_id
        return mapping

    def register_all(self, mappings: Iterable[BrokerInstrumentMapping]) -> int:
        count = 0
        for mapping in mappings:
            self.register(mapping)
            count += 1
        return count

    def load(self, broker_id: Optional[str] = None) -> int:
        """Load persisted mappings. Returns how many were read."""
        if self.conn is None:
            return 0
        exists = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='broker_instrument_mapping'").fetchone()
        if not exists:
            return 0

        sql = """
            SELECT canonical_instrument_id, broker_id, broker_symbol, venue,
                   asset_class, currency, tick_size, lot_size, minimum_quantity,
                   quantity_increment, price_precision, contract_multiplier,
                   timezone_name, trading_hours, tradable, broker_payload_json
            FROM broker_instrument_mapping
        """
        params: Tuple[Any, ...] = ()
        if broker_id:
            sql += " WHERE broker_id = ?"
            params = (broker_id,)

        count = 0
        for row in self.conn.execute(sql, params):
            self.register(BrokerInstrumentMapping(
                canonical_instrument_id=row[0], broker_id=row[1],
                broker_symbol=row[2], venue=row[3] or "",
                asset_class=row[4] or "stock", currency=row[5] or "USD",
                tick_size=row[6], lot_size=row[7], minimum_quantity=row[8],
                quantity_increment=row[9], price_precision=row[10],
                contract_multiplier=row[11] if row[11] is not None else 1.0,
                timezone_name=row[12] or "UTC", trading_hours=row[13] or "",
                tradable=bool(row[14]),
                # The venue's own identifier scheme lives here — IBKR
                # keeps its conid in it. Losing it on reload would mean
                # re-resolving every contract on every run, and a
                # re-resolution can reach a DIFFERENT answer after a
                # listing change. That is a moment for a human, not a
                # silent remap.
                broker_payload=_loads(row[15])))
            count += 1
        return count

    # ---------------- lookup ----------------

    def resolve(self, broker_id: str,
                instrument_id: str) -> MappingResolution:
        """
        Canonical id to venue symbol.

        Returns a resolution rather than raising, because an unmapped
        instrument is an ordinary operational condition — a new name in
        the universe that nobody has mapped yet — and the caller needs
        the reason code to record, not a stack trace.
        """
        mapping = self._by_pair.get((broker_id, instrument_id))
        if mapping is None:
            return MappingResolution(
                code=ExecutionRejectCode.NO_INSTRUMENT_MAPPING,
                detail=f"{instrument_id} is not mapped for broker {broker_id}")
        if not mapping.tradable:
            return MappingResolution(
                mapping=mapping,
                code=ExecutionRejectCode.INSTRUMENT_NOT_TRADABLE,
                detail=f"{mapping.broker_symbol} is marked not tradable")
        return MappingResolution(mapping=mapping)

    def canonical_for(self, broker_id: str,
                      broker_symbol: str) -> Optional[str]:
        """
        Venue symbol back to canonical id, for inbound broker events.

        The reverse direction matters as much as the forward one: a
        fill arrives naming the venue's symbol, and it has to be
        attributed to the right instrument before it can touch a
        position.
        """
        return self._by_symbol.get((broker_id, broker_symbol))

    def get(self, broker_id: str,
            instrument_id: str) -> Optional[BrokerInstrumentMapping]:
        return self._by_pair.get((broker_id, instrument_id))

    def for_broker(self, broker_id: str) -> List[BrokerInstrumentMapping]:
        return [m for (b, _), m in self._by_pair.items() if b == broker_id]

    def brokers_for(self, instrument_id: str) -> List[str]:
        """Which venues can trade this instrument. The basis of routing."""
        return sorted(b for (b, i) in self._by_pair if i == instrument_id)

    def __len__(self) -> int:
        return len(self._by_pair)


def default_equity_mapping(instrument_id: str, broker_id: str,
                           ticker: str, **overrides) -> BrokerInstrumentMapping:
    """
    A plain whole-share equity mapping.

    Used by the paper adapter, whose "venue" is this project's own
    cached bars — so the canonical ticker IS the symbol, and saying so
    explicitly is more honest than leaving the mapping implicit and
    letting `resolve` fall back to the id.
    """
    defaults: Dict[str, Any] = dict(
        canonical_instrument_id=instrument_id, broker_id=broker_id,
        broker_symbol=ticker, asset_class="stock", currency="USD",
        minimum_quantity=1.0, quantity_increment=1.0, price_precision=2,
        contract_multiplier=1.0, timezone_name="UTC",
        trading_hours="daily session, derived from cached bars",
        tradable=True)
    defaults.update(overrides)
    return BrokerInstrumentMapping(**defaults)
