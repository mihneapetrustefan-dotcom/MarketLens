"""
src/adaptive/exchange_map.py
------------------------------------------
Which billing venue a request belongs to (Phase 25.9I-C, spec sections 11, 27, 28).

THE QUESTION THIS ANSWERS, AND WHY IT IS NOT OBVIOUS
--------------------------------------------------------
A capped, per-exchange bill can only be tracked if every request can be
attributed to an exchange. Neither universe file carries one:

    config/capture_universe_v1.json   instrument_id, ticker, sector_id,
                                      asset_class, sec_type, currency, role
    config/parent_universe_v1.json    the same, plus liquidity fields

and the research database's `exchanges` table holds three rows, of
which the one every US instrument points at is literally named
"US & International (unspecified)". That is a research bucket, not a
billing venue, and using it would put NASDAQ, NYSE and ARCA in one
counter.

THE ONLY CANONICAL SOURCE IS THE BROKER CONTRACT
----------------------------------------------------
`broker_instrument_mapping.broker_payload_json.primary_exchange`, written
by the IBKR contract resolution that capture already performs. That is
the LISTING exchange, which is what a market-data bill is keyed by.

NOT `exchange`, which reads SMART for all 31 instruments: SMART is
IBKR's order router, not a venue, and it has no market-data price list.
Billing a request to SMART would produce one bucket that silently
aggregated every venue -- exactly what spec section 11 forbids.

UNKNOWN FAILS CLOSED
------------------------
Measured 2026-09-29: 109 of the 130 parent-universe members have no
resolved broker contract, so their billing venue is unknown. They are
reported UNKNOWN, and `BillableRequestPlan.is_billable` is False for
them, so a plan containing one cannot be approved without an explicit
conservative fallback the caller has to ask for by name.

This module reads. It never resolves a contract, because resolving one
costs a broker request, and a module whose job is to price requests
must not quietly make them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Tuple

#: IBKR's smart router. Present in `exchange` for every mapped
#: instrument and never a billing venue.
ROUTING_PSEUDO_EXCHANGES = frozenset({"SMART", "SMARTUS", "IBKRATS", "OVERNIGHT"})

#: What `exchange_of` returns when the venue is not known. A sentinel
#: rather than None so it can key a dict and appear in a report without
#: a caller having to special-case it into looking like zero.
UNKNOWN_EXCHANGE = "UNKNOWN"


@dataclass(frozen=True)
class InstrumentVenue:
    """One instrument's billing venue and where that claim came from."""
    instrument_id: str
    exchange: str = UNKNOWN_EXCHANGE
    conid: str = ""
    source: str = "unresolved"

    @property
    def is_known(self) -> bool:
        return (self.exchange != UNKNOWN_EXCHANGE
                and self.exchange not in ROUTING_PSEUDO_EXCHANGES)


def normalize_exchange(raw: Optional[str]) -> str:
    """
    Upper-cased venue, with routers and blanks collapsed to UNKNOWN.

    A router name is treated as unknown rather than passed through: it
    is a real string that would look like a real answer downstream.
    """
    text = str(raw or "").strip().upper()
    if not text or text in ROUTING_PSEUDO_EXCHANGES:
        return UNKNOWN_EXCHANGE
    return text


class ExchangeMap:
    """
    instrument_id -> billing venue, read once from canonical metadata.

    Holds no connection after construction, so it cannot re-read the
    database at an awkward moment and cannot be handed to something that
    would write through it.
    """

    def __init__(self, venues: Mapping[str, InstrumentVenue]):
        self._venues: Dict[str, InstrumentVenue] = dict(venues)

    # ---------------- construction ----------------

    @classmethod
    def from_connection(cls, conn: sqlite3.Connection) -> "ExchangeMap":
        """
        Build from `broker_instrument_mapping`.

        Prefers the payload's `primary_exchange` (the listing venue) and
        falls back to the `venue` column, which the mapper fills from the
        same source. Never reads `exchange`; see the module docstring.
        """
        venues: Dict[str, InstrumentVenue] = {}
        try:
            rows = conn.execute(
                "SELECT canonical_instrument_id, venue, broker_payload_json "
                "FROM broker_instrument_mapping").fetchall()
        except sqlite3.DatabaseError:
            return cls({})
        for instrument_id, venue, payload_json in rows:
            payload: Dict[str, object] = {}
            if payload_json:
                try:
                    loaded = json.loads(payload_json)
                    payload = loaded if isinstance(loaded, dict) else {}
                except (TypeError, ValueError):
                    payload = {}
            primary = normalize_exchange(payload.get("primary_exchange"))
            source = "broker_payload.primary_exchange"
            if primary == UNKNOWN_EXCHANGE:
                primary = normalize_exchange(venue)
                source = "broker_instrument_mapping.venue"
            if primary == UNKNOWN_EXCHANGE:
                source = "unresolved"
            venues[str(instrument_id)] = InstrumentVenue(
                instrument_id=str(instrument_id), exchange=primary,
                conid=str(payload.get("conid") or ""), source=source)
        return cls(venues)

    # ---------------- lookup ----------------

    def exchange_of(self, instrument_id: str) -> str:
        venue = self._venues.get(str(instrument_id))
        return venue.exchange if venue else UNKNOWN_EXCHANGE

    def venue_of(self, instrument_id: str) -> InstrumentVenue:
        return self._venues.get(str(instrument_id)) or InstrumentVenue(
            instrument_id=str(instrument_id))

    def __contains__(self, instrument_id: object) -> bool:
        return str(instrument_id) in self._venues

    def __len__(self) -> int:
        return len(self._venues)

    def distribution(self, instrument_ids: Iterable[str]) -> Dict[str, int]:
        """
        exchange -> count over the given instruments.

        UNKNOWN is a key like any other and is never dropped: a report
        that omitted it would understate what cannot be priced.
        """
        counts: Dict[str, int] = {}
        for instrument_id in instrument_ids:
            key = self.exchange_of(instrument_id)
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    def exchanges_for(self, instrument_ids: Iterable[str]) -> Tuple[str, ...]:
        """The distinct KNOWN venues a set of instruments would bill to."""
        return tuple(sorted({self.exchange_of(i) for i in instrument_ids}
                            - {UNKNOWN_EXCHANGE}))

    def unknown_instruments(self, instrument_ids: Iterable[str]) -> Tuple[str, ...]:
        return tuple(sorted(str(i) for i in instrument_ids
                            if self.exchange_of(i) == UNKNOWN_EXCHANGE))

    def describe(self, instrument_ids: Optional[Iterable[str]] = None
                 ) -> Dict[str, object]:
        ids = (list(instrument_ids) if instrument_ids is not None
               else sorted(self._venues))
        unknown = self.unknown_instruments(ids)
        return {
            "instruments": len(ids),
            "distribution": self.distribution(ids),
            "known_exchanges": list(self.exchanges_for(ids)),
            "unknown_count": len(unknown),
            "unknown_instruments": list(unknown[:20]),
            "unknown_truncated": max(0, len(unknown) - 20),
        }
