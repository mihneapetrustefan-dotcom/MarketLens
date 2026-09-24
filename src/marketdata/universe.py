"""
src/marketdata/universe.py
-------------------------------------------
Which instruments the market-data service is allowed to poll
(Phase 25.7, spec §5).

WHY AN EXPLICIT UNIVERSE
----------------------------
Polling everything in the database is not an option. There are 389
instruments across three asset classes, only some carry a resolved
IBKR contract, and the broker request budget is 50 requests per
minute. An unbounded universe would either exhaust the budget or
quietly drop instruments depending on dictionary order.

So the universe is DETERMINISTIC and INSPECTABLE: the same database
yields the same ordered list, every excluded instrument names its
reason, and nothing is silently substituted. An instrument that cannot
be resolved safely is blocked, not replaced by a neighbour.
"""

from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional, Sequence

from src.domain.market_data_models import UniverseEntry

#: Instruments this project holds but IBKR cannot trade through the
#: Client Portal contract search used here. Recorded rather than
#: attempted: a failed resolution every cycle is wasted budget.
UNSUPPORTED_ASSET_CLASSES = ("bvb",)


def _conid_of(payload_json: str) -> str:
    """The IBKR contract id stored on a broker mapping, if resolved."""
    import json
    try:
        payload = json.loads(payload_json or "{}")
    except (ValueError, TypeError):
        return ""
    for key in ("conid", "conidex", "contract_id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def resolve_universe(conn: sqlite3.Connection,
                     broker_id: str = "ibkr",
                     limit: Optional[int] = None,
                     instruments: Optional[Sequence[str]] = None
                     ) -> List[UniverseEntry]:
    """
    The ordered, deterministic operational universe.

    Ordered by instrument_id so two runs against the same database
    poll the same instruments in the same order. `limit` truncates
    AFTER ordering, so it is reproducible rather than arbitrary.

    Every row that cannot be polled is still returned, carrying
    `excluded_reason`. The caller can therefore report "9 active, 3
    blocked because X" instead of silently seeing 9.
    """
    try:
        rows = conn.execute("""
            SELECT canonical_instrument_id, broker_symbol, venue,
                   asset_class, currency, tradable, broker_payload_json
            FROM broker_instrument_mapping
            WHERE broker_id = ?
            ORDER BY canonical_instrument_id ASC
        """, (broker_id,)).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return []
        raise

    wanted = set(instruments) if instruments else None
    entries: List[UniverseEntry] = []
    for (instrument_id, symbol, venue, asset_class, currency,
         tradable, payload_json) in rows:
        if wanted is not None and instrument_id not in wanted:
            continue
        conid = _conid_of(payload_json)
        reason = ""
        if (asset_class or "").lower() in UNSUPPORTED_ASSET_CLASSES:
            reason = f"asset class {asset_class!r} is not supported at {broker_id}"
        elif not conid:
            reason = "no resolved IBKR contract (run --resolve first)"
        elif not tradable:
            reason = "mapping is marked not tradable"
        entries.append(UniverseEntry(
            instrument_id=instrument_id,
            conid=conid,
            broker_symbol=symbol or "",
            asset_class=asset_class or "stock",
            venue=venue or "",
            currency=currency or "USD",
            tradable=bool(tradable),
            excluded_reason=reason,
        ))

    if limit is not None and limit >= 0:
        active = [e for e in entries if e.is_active][:limit]
        keep = {e.instrument_id for e in active}
        # Excluded entries are preserved so the report stays honest
        # about what was skipped and why.
        entries = [e for e in entries
                   if e.instrument_id in keep or not e.is_active]
    return entries


def active(entries: Sequence[UniverseEntry]) -> List[UniverseEntry]:
    return [e for e in entries if e.is_active]


def excluded(entries: Sequence[UniverseEntry]) -> Dict[str, str]:
    return {e.instrument_id: e.excluded_reason
            for e in entries if not e.is_active}


def capacity_report(entry_count: int, requests_per_cycle: int,
                    interval_seconds: float,
                    budget_per_minute: int) -> Dict[str, object]:
    """
    Can this universe be serviced at this interval within the budget?

    Answered arithmetically BEFORE any request is sent, because the
    failure mode otherwise is a cycle that exhausts the budget halfway
    through and leaves half the universe blind without saying so.

    `fits` False is a sizing problem to report, never a reason to
    quietly raise the broker limit.
    """
    cycles_per_minute = (60.0 / interval_seconds) if interval_seconds > 0 else 0.0
    per_minute = requests_per_cycle * cycles_per_minute
    return {
        "instruments": entry_count,
        "requests_per_cycle": requests_per_cycle,
        "cycles_per_minute": round(cycles_per_minute, 3),
        "requests_per_minute": round(per_minute, 2),
        "budget_per_minute": budget_per_minute,
        "fits": per_minute <= budget_per_minute,
        "headroom": round(budget_per_minute - per_minute, 2),
    }
