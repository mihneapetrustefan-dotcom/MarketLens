"""
src/capture/features.py
-------------------------------
Persisted intraday features, computed by the ONE canonical builder.

Nothing here defines a feature. Values come from
`IntradayDatasetBuilder.observation` -- the 25.9F registry, its version,
its point-in-time rules -- and are written to `intraday_feature_values`
keyed by (instrument, cutoff, feature, feature_version).

BOUNDED LOADING, WITHOUT CHANGING A VALUE
---------------------------------------------
`required_history` (25.9F) measured what a feature needs: the whole
contiguous run containing the cutoff, plus the previous session's last
close. Runs break at the overnight gap, so both lie inside "the previous
trading day onwards". Loading from that day's open reproduces the full-
history value exactly (asserted in tests) while reading two days of rows
instead of the whole corpus, every five minutes.

LIVE WRITES NEVER OVERWRITE; RECOMPUTATION DOES
-----------------------------------------------------
A live value is `INSERT OR IGNORE`: the value that existed at the moment
is the one kept. `recompute_session` is the repair path for a feature
bug (§41): it deletes and rebuilds one session's values from the
archived bars -- the data is re-derived, never re-collected.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Sequence

from src.features.intraday import INTRADAY_FEATURE_VERSION
from src.marketdata.calendar import NEW_YORK, USEquityCalendar
from src.marketdata.intraday import load_research_bars, session_governed
from src.research.intraday_dataset import DEFAULT_BENCHMARK, IntradayDatasetBuilder


def history_start(cutoff: datetime, calendar: USEquityCalendar) -> datetime:
    """The open of the trading day before the cutoff's session day."""
    day = cutoff.astimezone(NEW_YORK).date()
    for _ in range(15):
        day -= timedelta(days=1)
        window = calendar.session(day)
        if window.is_trading_day:
            return window.opens_at
    return cutoff - timedelta(days=15)


def bounded_builder(conn: sqlite3.Connection, instrument_ids: Sequence[str],
                    cutoff: datetime,
                    calendar: Optional[USEquityCalendar] = None
                    ) -> IntradayDatasetBuilder:
    calendar = calendar or USEquityCalendar()
    builder = IntradayDatasetBuilder(conn, calendar=calendar)
    start = history_start(cutoff, calendar)
    for instrument_id in set(instrument_ids) | {DEFAULT_BENCHMARK}:
        builder.seed_bars(instrument_id, load_research_bars(
            conn, instrument_id, start=start, end=cutoff, calendar=calendar,
            governs_session=session_governed(conn, instrument_id)))
    return builder


def compute_and_persist(conn: sqlite3.Connection, instrument_ids: Sequence[str],
                        cutoff: datetime, session_id: str, now: datetime,
                        calendar: Optional[USEquityCalendar] = None,
                        replace: bool = False) -> Dict[str, int]:
    """Features for every instrument at one closed-bar cutoff."""
    ids = sorted(set(instrument_ids))
    peers = [i for i in ids if i != DEFAULT_BENCHMARK]
    builder = bounded_builder(conn, ids, cutoff, calendar)
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    counts = {"computed": 0, "partial": 0, "no_data": 0, "values": 0}
    for instrument_id in ids:
        row = builder.observation(instrument_id, cutoff, peers=peers, now=now)
        if row is None:
            counts["no_data"] += 1
            continue
        missing = sum(1 for v in row.features.values() if v is None)
        counts["partial" if missing else "computed"] += 1
        for feature_id, value in sorted(row.features.items()):
            cursor = conn.execute(
                verb + " INTO intraday_feature_values (instrument_id, cutoff, "
                "feature_id, feature_version, value, session_id, computed_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (instrument_id, cutoff.astimezone(timezone.utc).isoformat(),
                 feature_id, INTRADAY_FEATURE_VERSION, value, session_id,
                 now.astimezone(timezone.utc).isoformat()))
            counts["values"] += max(0, cursor.rowcount or 0)
    conn.commit()
    return counts


def recompute_session(conn: sqlite3.Connection, session_id: str,
                      now: datetime) -> Dict[str, int]:
    """Rebuild one session's persisted features from its archived bars."""
    cutoffs = {r[0] for r in conn.execute(
        "SELECT DISTINCT cutoff FROM intraday_feature_values "
        "WHERE session_id = ?", (session_id,))}
    # Cutoffs whose live computation failed are repaired too (section 99).
    import json
    for (detail,) in conn.execute(
            "SELECT detail FROM capture_events WHERE session_id = ? "
            "AND kind = 'FEATURE_FAILED'", (session_id,)):
        try:
            cutoffs.add(json.loads(detail)["cutoff"])
        except (ValueError, KeyError, TypeError):
            pass
    cutoffs = sorted(cutoffs)
    members = [r[0] for r in conn.execute(
        "SELECT instrument_id FROM capture_session_members "
        "WHERE session_id = ? AND mapping_status = 'RESOLVED'", (session_id,))]
    conn.execute("DELETE FROM intraday_feature_values WHERE session_id = ? "
                 "AND feature_version = ?", (session_id, INTRADAY_FEATURE_VERSION))
    totals = {"cutoffs": len(cutoffs), "values": 0}
    for raw in cutoffs:
        result = compute_and_persist(conn, members, datetime.fromisoformat(raw),
                                     session_id, now, replace=True)
        totals["values"] += result["values"]
    return totals
