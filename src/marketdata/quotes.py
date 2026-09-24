"""
src/marketdata/quotes.py
-------------------------------------------
Acquiring current quotes from IBKR (Phase 25.7, §6, §38, §39).

BATCHED, BECAUSE THE ENDPOINT IS
------------------------------------
`transport.market_snapshot(conids, fields)` already takes a SEQUENCE of
contract ids. The whole active universe is therefore ONE broker
request per cycle, not one per instrument. That is the difference
between 9 requests a minute and 1, against a budget of 50 -- and it is
the existing adapter's own capability, not a new endpoint invented for
this phase.

COLD CONTRACTS ARE A REAL BEHAVIOUR, NOT AN ERROR
-----------------------------------------------------
Measured live on 2026-09-11: IBKR's first snapshot for a conid it has
not subscribed to returns no fields. The request OPENS the
subscription and the data arrives on a later call. Retrying inside one
invocation does not hurry it -- a cold conid stayed empty across four
attempts over six seconds, then answered in 0.4s from the next
process.

So a cold instrument is reported UNAVAILABLE for this cycle and
becomes available on a subsequent one. It is never filled in from the
historical cache: a five-day-old research close presented as a current
price is precisely the failure this phase exists to prevent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.market_data_models import (
    MarketDataAvailability, OperationalQuote, OperationalSource, UniverseEntry,
)
from src.execution.adapters.ibkr.errors import IBKRError
from src.execution.adapters.ibkr.mapper import SNAPSHOT_FIELDS, quote_from_ibkr


def _availability_of(payload: Dict[str, Any]) -> MarketDataAvailability:
    """
    Read IBKR's own live/delayed marker.

    Field 6509 is a status string whose characters describe the
    subscription. 'D' anywhere in it means delayed; 'R' means realtime.
    Absent or unreadable is UNKNOWN, never assumed live -- assuming
    live is how a delayed price ends up backing a limit order.
    """
    marker = payload.get("6509")
    if not isinstance(marker, str) or not marker:
        return MarketDataAvailability.UNKNOWN
    upper = marker.upper()
    if "D" in upper:
        return MarketDataAvailability.DELAYED
    if "R" in upper:
        return MarketDataAvailability.AVAILABLE
    if "Z" in upper or "Y" in upper:
        # Frozen / halted feeds: present but not live.
        return MarketDataAvailability.DELAYED
    return MarketDataAvailability.UNKNOWN


def acquire(transport, entries: Sequence[UniverseEntry],
            now: datetime, session_id: str = "",
            fields: Sequence[str] = SNAPSHOT_FIELDS
            ) -> Tuple[List[OperationalQuote], int, str]:
    """
    One batched acquisition for the active universe.

    Returns (quotes, broker_requests, error). Every requested
    instrument gets exactly one quote back, including the ones the
    venue said nothing about -- those carry
    `availability=UNAVAILABLE` and a note, so a caller counting quotes
    can never mistake a missing instrument for a present one.

    Transport failures are returned, not raised: one bad cycle must
    degrade the service, not crash the process that is also managing
    bars and session state.
    """
    active = [e for e in entries if e.is_active]
    if not active:
        return [], 0, ""

    by_conid = {e.conid: e for e in active}
    try:
        payloads = transport.market_snapshot(list(by_conid), list(fields))
        requests = 1
        error = ""
    except IBKRError as failure:
        # The whole universe is blind this cycle. Reported per
        # instrument so downstream sees UNAVAILABLE rather than an
        # absence it has to interpret.
        return ([_blind(e, now, session_id, failure.message) for e in active],
                1, failure.message)
    except Exception as failure:                          # noqa: BLE001
        return ([_blind(e, now, session_id, str(failure)) for e in active],
                1, str(failure))

    seen: Dict[str, OperationalQuote] = {}
    for payload in payloads or []:
        conid = str(payload.get("conid") or "")
        entry = by_conid.get(conid)
        if entry is None:
            continue
        canonical = quote_from_ibkr(payload, received_at=now)
        availability = _availability_of(payload)
        quote = OperationalQuote(
            instrument_id=entry.instrument_id,
            conid=conid,
            last=canonical.get("last"),
            bid=canonical.get("bid"),
            ask=canonical.get("ask"),
            mid=canonical.get("mid"),
            volume=canonical.get("volume"),
            availability=availability,
            broker_at=canonical.get("broker_at"),
            received_at=now,
            evaluated_at=now,
            source=OperationalSource.IBKR_SNAPSHOT,
            session_id=session_id,
        )
        if quote.reference_price is None:
            # The documented cold-contract case: the subscription just
            # opened and carries no fields yet.
            quote.availability = MarketDataAvailability.UNAVAILABLE
            quote.note = ("no price fields returned; contract subscription "
                          "is cold, retry next cycle")
        seen[entry.instrument_id] = quote

    quotes = [seen.get(e.instrument_id)
              or _blind(e, now, session_id,
                        "instrument not present in the snapshot response")
              for e in active]
    return quotes, requests, error


def _blind(entry: UniverseEntry, now: datetime, session_id: str,
           note: str) -> OperationalQuote:
    """An instrument we asked about and learned nothing about."""
    return OperationalQuote(
        instrument_id=entry.instrument_id,
        conid=entry.conid,
        availability=MarketDataAvailability.UNAVAILABLE,
        received_at=now,
        evaluated_at=now,
        source=OperationalSource.IBKR_SNAPSHOT,
        session_id=session_id,
        note=note,
    )
