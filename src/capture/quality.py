"""
src/capture/quality.py
------------------------------
Session quality, cross-sectional coverage, data maturity, safe pruning.

WHAT A SESSION IS WORTH (§30-§34)
-------------------------------------
Rows are not the unit. A model is qualified on independent market days,
and a day is only useful when the instruments were observed TOGETHER --
a cross-sectional feature at 10:41 needs the peers' 10:41 bars, not
their 14:02 ones. So a session is judged on:

    expected_minutes     regular-hours minutes the calendar defines
    resolved_fraction    members with a contract / members expected
    window_fraction      minutes with ANY member bar / expected
    cross_sectional      minutes where >= 80% of the resolved members
                         have a real (archived, non-gap) bar

and classified, with the rule written here rather than tuned later:

    FAILED    nothing usable: no resolved member, no bar, or fewer than
              10% of minutes cross-sectional AND fewer than 10% observed
    GOOD      >= 90% of minutes cross-sectional and >= 90% of members
              resolved
    PARTIAL   the session was only partly observed (late start, early
              stop, host sleep: window < 90%) but what WAS observed is
              >= 90% cross-sectional
    DEGRADED  anything else -- observed, but the cross-section was thin

MATURITY IS COUNTED IN SESSIONS (§35-§37)
----------------------------------------------
Qualifying sessions are GOOD and PARTIAL. The verdict reuses
`scripts/audit_intraday_data.calendar_ceiling` unchanged -- one rule for
"how much intraday history is enough", not two. The milestones
20/40/60/90/120 are planning targets: reaching one says the calendar
allows the next question, never that a model is justified.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.marketdata.calendar import USEquityCalendar

MINUTE = timedelta(minutes=1)

CROSS_SECTIONAL_SHARE = 0.80
GOOD_CROSS_SECTIONAL = 0.90
GOOD_RESOLVED = 0.90
PARTIAL_WINDOW = 0.90
FAILED_FLOOR = 0.10

QUALIFYING = ("GOOD", "PARTIAL")
MILESTONES = (20, 40, 60, 90, 120)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    stamp = datetime.fromisoformat(raw)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def classify(expected: int, resolved: int, members: int, window_minutes: int,
             cross_sectional: int, bars: int) -> str:
    if expected <= 0 or resolved == 0 or bars == 0:
        return "FAILED"
    cs = cross_sectional / expected
    window = window_minutes / expected
    if cs < FAILED_FLOOR and window < FAILED_FLOOR:
        return "FAILED"
    if cs >= GOOD_CROSS_SECTIONAL and resolved / max(1, members) >= GOOD_RESOLVED:
        return "GOOD"
    if window < PARTIAL_WINDOW and window_minutes and \
            cross_sectional / window_minutes >= GOOD_CROSS_SECTIONAL:
        return "PARTIAL"
    return "DEGRADED"


def session_coverage(conn: sqlite3.Connection, session_id: str) -> Dict[str, Any]:
    """Measure one session from what is actually in the research archive."""
    session = conn.execute(
        "SELECT opens_at, closes_at, session_type, session_date FROM "
        "capture_sessions WHERE session_id = ?", (session_id,)).fetchone()
    if session is None:
        raise KeyError(session_id)
    opens, closes = _parse(session[0]), _parse(session[1])
    members = conn.execute(
        "SELECT instrument_id, mapping_status FROM capture_session_members "
        "WHERE session_id = ?", (session_id,)).fetchall()
    resolved = sorted(i for i, s in members if s == "RESOLVED")

    expected = int((closes - opens).total_seconds() // 60)
    per_minute: Dict[str, int] = {}
    per_member: Dict[str, int] = {i: 0 for i, _ in members}
    if resolved:
        marks = ",".join("?" * len(resolved))
        rows = conn.execute(
            "SELECT instrument_id, timestamp FROM price_candle_cache "
            "WHERE interval = '1m' AND timestamp >= ? AND timestamp < ? "
            "AND instrument_id IN (" + marks + ")",
            [_iso(opens), _iso(closes)] + resolved).fetchall()
        for instrument_id, stamp in rows:
            key = _iso(_parse(stamp))
            per_minute[key] = per_minute.get(key, 0) + 1
            per_member[instrument_id] = per_member.get(instrument_id, 0) + 1
    threshold = max(1, math.ceil(CROSS_SECTIONAL_SHARE * len(resolved)))
    cross = sum(1 for n in per_minute.values() if n >= threshold)
    bars = sum(per_minute.values())

    gaps_rows = conn.execute(
        "SELECT detail FROM capture_events WHERE session_id = ? "
        "AND kind = 'HOST_SUSPEND_GAP'", (session_id,)).fetchall()
    gap_seconds = 0.0
    for (detail,) in gaps_rows:
        try:
            gap_seconds += float(json.loads(detail).get("seconds", 0))
        except (ValueError, TypeError, AttributeError):
            pass
    ticks = conn.execute(
        "SELECT MIN(tick_at), MAX(tick_at), COUNT(*) FROM capture_ticks "
        "WHERE session_id = ?", (session_id,)).fetchone()
    restarts = conn.execute(
        "SELECT COUNT(DISTINCT instance_id) FROM capture_ticks WHERE session_id = ?",
        (session_id,)).fetchone()[0]

    quality = classify(expected, len(resolved), len(members), len(per_minute),
                       cross, bars)

    # Simultaneity (sections 42, 95, 124): how many instruments share each
    # research minute. A minute absent from per_minute had none.
    counts = [per_minute.get(_iso(opens + i * MINUTE), 0) for i in range(expected)]
    ordered = sorted(counts)
    median = (ordered[len(ordered) // 2] if len(ordered) % 2 else
              (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2
              ) if ordered else 0
    gaps, run, largest, largest_at = 0, 0, 0, None
    for i, n in enumerate(counts + [1]):
        if n == 0:
            run += 1
            continue
        if run:
            gaps += 1
            if run > largest:
                largest, largest_at = run, _iso(opens + (i - run) * MINUTE)
        run = 0

    def at_least(k: int) -> int:
        return sum(1 for n in counts if n >= k)

    dispersion = conn.execute(
        "SELECT COUNT(value), COUNT(*) FROM intraday_feature_values "
        "WHERE session_id = ? AND feature_id LIKE '%dispersion_1m'",
        (session_id,)).fetchone()
    feature_rows = conn.execute(
        "SELECT COUNT(*) FROM intraday_feature_values WHERE session_id = ?",
        (session_id,)).fetchone()[0]
    cycles = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(stale),0), COALESCE(SUM(invalid),0), "
        "COALESCE(SUM(unavailable),0), COALESCE(SUM(tradeable),0) "
        "FROM market_data_cycles WHERE started_at >= ? AND started_at <= ?",
        (_iso(opens), _iso(closes + MINUTE))).fetchone()
    kinds = dict(conn.execute(
        "SELECT kind, COUNT(*) FROM capture_events WHERE session_id = ? "
        "GROUP BY kind", (session_id,)).fetchall())
    session_events = conn.execute(
        "SELECT kind, COUNT(*) FROM capture_events WHERE at >= ? AND at <= ? "
        "GROUP BY kind", (_iso(opens), _iso(closes))).fetchall()
    during = dict(session_events)

    return {
        "session_id": session_id,
        "session_date": session[3],
        "session_type": session[2],
        "expected_minutes": expected,
        "members": len(members),
        "resolved": len(resolved),
        "unresolved": sorted(i for i, s in members if s != "RESOLVED"),
        "bars_archived": bars,
        "window_minutes": len(per_minute),
        "cross_sectional_minutes": cross,
        "cross_sectional_threshold": threshold,
        "cross_sectional_fraction": round(cross / expected, 4) if expected else 0.0,
        "member_coverage": {i: (round(n / expected, 4) if expected else 0.0)
                            for i, n in sorted(per_member.items())},
        "ticks": ticks[2],
        "observed_from": ticks[0],
        "observed_until": ticks[1],
        "processes": restarts,
        "host_suspend_gaps": len(gaps_rows),
        "host_suspend_seconds": round(gap_seconds, 1),
        "captured_instruments": sum(1 for n in per_member.values() if n > 0),
        "minutes_ge_1": at_least(1),
        "minutes_ge_2": at_least(2),
        "minutes_ge_3": at_least(3),
        "minutes_ge_5": at_least(5),
        "minutes_ge_3_fraction": round(at_least(3) / expected, 4) if expected else 0.0,
        "median_simultaneous": median,
        "max_simultaneous": max(counts) if counts else 0,
        "gap_count": gaps,
        "largest_gap_minutes": largest,
        "largest_gap_starts": largest_at,
        "feature_rows": feature_rows,
        "dispersion_1m_coverage": (round(dispersion[0] / dispersion[1], 4)
                                   if dispersion[1] else None),
        "quote_cycles": cycles[0],
        "stale_observations": cycles[1],
        "invalid_observations": cycles[2],
        "unavailable_observations": cycles[3],
        "delayed_observations": "not recorded per cycle by MarketDataService",
        "archive_failures": kinds.get("ARCHIVE_FAILED", 0),
        "feature_failures": kinds.get("FEATURE_FAILED", 0),
        "errors": (during.get("STEP_ERROR", 0) + kinds.get("ARCHIVE_FAILED", 0)
                   + kinds.get("FEATURE_FAILED", 0)),
        "reconnects": during.get("AUTH_LOST", 0),
        "auth_waits": during.get("WAITING_FOR_AUTH", 0),
        "order_write_attempts": during.get("BROKER_WRITE_REFUSED", 0),
        "runner_minutes": ticks[2],
        "quality": quality,
    }


def finalize_session(conn: sqlite3.Connection, session_id: str,
                     now: datetime) -> Dict[str, Any]:
    """Measure and seal a session. Idempotent: a sealed one is re-read."""
    row = conn.execute(
        "SELECT status, summary_json FROM capture_sessions WHERE session_id = ?",
        (session_id,)).fetchone()
    if row is not None and row[0] == "finalized":
        return json.loads(row[1] or "{}")
    summary = session_coverage(conn, session_id)
    conn.execute(
        "UPDATE capture_sessions SET status = 'finalized', quality = ?, "
        "observed_from = ?, observed_until = ?, finalized_at = ?, "
        "summary_json = ? WHERE session_id = ?",
        (summary["quality"], summary["observed_from"], summary["observed_until"],
         _iso(now), json.dumps(summary, sort_keys=True), session_id))
    conn.commit()
    return summary


# ======================================================================
# Maturity
# ======================================================================

def _trading_days_after(start: date, count: int,
                        calendar: USEquityCalendar) -> Optional[date]:
    day, seen = start, 0
    for _ in range(count * 2 + 30):
        day += timedelta(days=1)
        if calendar.session(day).is_trading_day:
            seen += 1
            if seen >= count:
                return day
    return None


def maturity(conn: sqlite3.Connection, today: date,
             calendar: Optional[USEquityCalendar] = None) -> Dict[str, Any]:
    """Where the captured corpus stands, in sessions and calendar months."""
    import os
    import sys
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from scripts.audit_intraday_data import calendar_ceiling

    calendar = calendar or USEquityCalendar()
    rows = conn.execute(
        "SELECT session_date, quality, session_type FROM capture_sessions "
        "WHERE status = 'finalized' ORDER BY session_date").fetchall()
    by_quality = {q: 0 for q in ("GOOD", "PARTIAL", "DEGRADED", "FAILED")}
    qualifying_dates: List[str] = []
    early_close = sum(1 for r in rows if r[2] == "early_close")
    for session_date, quality, _type in rows:
        by_quality[quality] = by_quality.get(quality, 0) + 1
        if quality in QUALIFYING:
            qualifying_dates.append(session_date)
    sessions = len(qualifying_dates)
    months = len({d[:7] for d in qualifying_dates})
    ceiling, why = calendar_ceiling(sessions, months)
    # The phase vocabulary, in order: INSUFFICIENT < MARGINAL < IMPROVING <
    # READY. IMPROVING is MARGINAL that has passed the 90-session planning
    # milestone on the way to the READY ceiling; it is never granted by rows,
    # and READY comes only from calendar_ceiling itself.
    if ceiling == "READY":
        band = "READY"
    elif ceiling == "MARGINAL":
        band = "IMPROVING" if sessions >= 90 else "MARGINAL"
    else:
        band = "INSUFFICIENT"
    upcoming = [m for m in MILESTONES if m > sessions]
    next_milestone = upcoming[0] if upcoming else None
    projected = (_trading_days_after(today, next_milestone - sessions, calendar)
                 if next_milestone else None)
    span_days = ((date.fromisoformat(qualifying_dates[-1])
                  - date.fromisoformat(qualifying_dates[0])).days + 1
                 if qualifying_dates else 0)
    return {
        "finalized_sessions": len(rows),
        "by_quality": by_quality,
        "full_sessions": by_quality["GOOD"],
        "partial_sessions": by_quality["PARTIAL"],
        "failed_sessions": by_quality["FAILED"],
        "early_close_sessions": early_close,
        "calendar_span_days": span_days,
        "qualifying_sessions": sessions,
        "calendar_months": months,
        "first_session": qualifying_dates[0] if qualifying_dates else None,
        "last_session": qualifying_dates[-1] if qualifying_dates else None,
        "calendar_ceiling": ceiling,
        "ceiling_reason": why,
        "band": band,
        "next_milestone": next_milestone,
        "sessions_to_next_milestone": (next_milestone - sessions
                                       if next_milestone else 0),
        "earliest_date_for_next_milestone": projected.isoformat() if projected else None,
        "milestones_are": "planning targets if every future session qualifies; "
                          "not evidence that any model is justified",
    }


# ======================================================================
# Pruning that cannot lose data
# ======================================================================

def prune_archived(conn: sqlite3.Connection, now: datetime,
                   retention_days: int = 30) -> Dict[str, int]:
    """
    Apply the operational retention WITHOUT the 25.7 hazard.

    `market_data_schema.prune` deletes every bar older than thirty days.
    Run by a capture process that had failed to archive for a month, it
    would delete the only copy. Here an old bar is removed only when it
    can never matter to research (a gap marker or an incomplete minute)
    or when its research copy provably exists.
    """
    cutoff = _iso(now - timedelta(days=retention_days))
    cursor = conn.execute("""
        DELETE FROM market_data_bars
         WHERE bar_start < ?
           AND (is_gap = 1 OR is_complete = 0 OR EXISTS (
                SELECT 1 FROM price_candle_cache p
                 WHERE p.instrument_id = market_data_bars.instrument_id
                   AND p.interval = '1m'
                   AND p.timestamp = market_data_bars.bar_start))
    """, (cutoff,))
    bars = max(0, cursor.rowcount or 0)
    kept = conn.execute(
        "SELECT COUNT(*) FROM market_data_bars WHERE bar_start < ?",
        (cutoff,)).fetchone()[0]
    cursor = conn.execute(
        "DELETE FROM market_data_cycles WHERE started_at < ?", (cutoff,))
    cycles = max(0, cursor.rowcount or 0)
    conn.commit()
    return {"bars": bars, "cycles": cycles, "old_unarchived_kept": kept}


# ======================================================================
# Provenance (section 50)
# ======================================================================

def trace(conn: sqlite3.Connection, instrument_id: str, bar_start: str) -> Dict[str, Any]:
    """
    One research minute, traced back to the contract and forward to features.

        contract (session membership) -> snapshot tick (request/receipt)
        -> operational bar -> archive record -> research bar -> features
    """
    stamp = _iso(_parse(bar_start))
    research = conn.execute(
        "SELECT timestamp, open, high, low, close, volume, source, fetched_at "
        "FROM price_candle_cache WHERE instrument_id = ? AND interval = '1m' "
        "AND timestamp = ?", (instrument_id, stamp)).fetchone()
    archive = conn.execute(
        "SELECT session_id, instance_id, archive_version, archived_at "
        "FROM capture_archive_log WHERE instrument_id = ? AND bar_start = ?",
        (instrument_id, stamp)).fetchone()
    operational = conn.execute(
        "SELECT bar_start, bar_end, open, high, low, close, observation_count, "
        "is_complete, is_gap, source, session_id, created_at "
        "FROM market_data_bars WHERE instrument_id = ? AND bar_start = ?",
        (instrument_id, stamp)).fetchone()
    session_id = archive[0] if archive else None
    member = conn.execute(
        "SELECT ticker, mapping_status, conid FROM capture_session_members "
        "WHERE session_id = ? AND instrument_id = ?",
        (session_id, instrument_id)).fetchone() if session_id else None
    end = _parse(stamp) + MINUTE
    ticks = conn.execute(
        "SELECT tick_at, instance_id, requested_at, received_at, tradeable, "
        "requested FROM capture_ticks WHERE session_id = ? AND tick_at >= ? "
        "AND tick_at < ? ORDER BY tick_at", (session_id, stamp, _iso(end + MINUTE))
    ).fetchall() if session_id else []
    features = conn.execute(
        "SELECT cutoff, COUNT(*), feature_version FROM intraday_feature_values "
        "WHERE instrument_id = ? AND cutoff >= ? GROUP BY cutoff, feature_version "
        "ORDER BY cutoff LIMIT 1", (instrument_id, _iso(end))).fetchone()
    return {
        "instrument_id": instrument_id,
        "bar_start": stamp,
        "contract": ({"ticker": member[0], "mapping": member[1], "conid": member[2]}
                     if member else None),
        "snapshot_ticks": [{"tick_at": t[0], "instance": t[1], "requested_at": t[2],
                            "received_at": t[3], "tradeable": t[4],
                            "requested": t[5]} for t in ticks],
        "operational_bar": (dict(zip(("bar_start", "bar_end", "open", "high", "low",
                                      "close", "observations", "complete", "gap",
                                      "source", "session", "written_at"),
                                     operational)) if operational else None),
        "archive_record": (dict(zip(("session_id", "instance_id", "archive_version",
                                     "archived_at"), archive)) if archive else None),
        "research_bar": (dict(zip(("timestamp", "open", "high", "low", "close",
                                   "volume", "source", "fetched_at"), research))
                         if research else None),
        "first_features_using_it": ({"cutoff": features[0], "values": features[1],
                                     "feature_version": features[2]}
                                    if features else None),
    }
