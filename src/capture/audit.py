"""
src/capture/audit.py
----------------------------
The first-session acceptance audit (Phase 25.9H). READ-ONLY.

Every function here takes a connection and only SELECTs. The CLI
(`scripts/capture_report.py --acceptance`) opens the store `mode=ro`, so
the audit cannot create a table, repair a row or leave a trace.

REAL MEANS THE TRANSPORT WAS REAL
-------------------------------------
A session is REAL only if every process that ticked in it recorded
`transport = ClientPortalTransport`. A mock venue records its own class
name, and a process from before 25.9H recorded nothing (UNKNOWN). Only
REAL sessions count toward real evidence or research maturity; a store
holding any MOCK session fails the audit, because fixture data inside
the capture store would make every figure in it suspect.

VERDICTS (section 98), from frozen rules -- never tuned to a day:

    FULL SESSION VERIFIED          REAL, finalized, graded GOOD, observed
                                   from within 5 min of the open to within
                                   5 min of the close, and no check FAILED
    PARTIAL REAL SESSION VERIFIED  REAL, finalized, GOOD or PARTIAL, no
                                   check FAILED
    REAL DATA CAPTURED BUT SESSION INVALID
                                   REAL bars exist, but the grade is
                                   DEGRADED/FAILED or a check FAILED
    NO REAL SESSION CAPTURED       nothing REAL with archived bars
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.capture.quality import MINUTE, _iso, _parse, session_coverage
from src.capture.schema import CAPTURE_WRITABLE
from src.marketdata.intraday import LIVE_CAPTURE_SOURCE

REAL_TRANSPORT = "ClientPortalTransport"
FEATURE_EVERY_MINUTES = 5
BOUNDARY_TOLERANCE = timedelta(minutes=5)
LARGE_GAP_MINUTES = 15
EXECUTION_TABLES = ("execution_orders", "execution_fills", "order_intents",
                    "execution_events", "trained_models", "model_promotions",
                    "predictions", "signals", "risk_decisions")

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _tables(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f'PRAGMA table_info("{table}")'))


def session_evidence(conn: sqlite3.Connection, session_id: str) -> str:
    """REAL / MOCK / UNKNOWN / NONE, from the processes that ticked it."""
    if not _has_column(conn, "capture_instances", "transport"):
        return "UNKNOWN"
    kinds = [r[0] for r in conn.execute(
        "SELECT DISTINCT i.transport FROM capture_ticks t JOIN capture_instances i "
        "ON i.instance_id = t.instance_id WHERE t.session_id = ?", (session_id,))]
    if not kinds:
        return "NONE"
    if any(k not in (None, REAL_TRANSPORT) for k in kinds):
        return "MOCK"
    if any(k is None for k in kinds):
        return "UNKNOWN"
    return "REAL"


def real_sessions(conn: sqlite3.Connection) -> List[str]:
    return [sid for (sid,) in conn.execute(
        "SELECT session_id FROM capture_sessions ORDER BY session_date")
            if session_evidence(conn, sid) == "REAL"]


# ======================================================================
# Pieces of the record
# ======================================================================

def gaps(conn: sqlite3.Connection, session_id: str) -> List[Dict[str, Any]]:
    """Every run of session minutes with no archived member bar, with a cause."""
    opens, closes = [_parse(v) for v in conn.execute(
        "SELECT opens_at, closes_at FROM capture_sessions WHERE session_id = ?",
        (session_id,)).fetchone()]
    members = [r[0] for r in conn.execute(
        "SELECT instrument_id FROM capture_session_members WHERE session_id = ? "
        "AND mapping_status = 'RESOLVED'", (session_id,))]
    present = set()
    if members:
        marks = ",".join("?" * len(members))
        present = {_iso(_parse(r[0])) for r in conn.execute(
            "SELECT DISTINCT timestamp FROM price_candle_cache WHERE interval = '1m' "
            "AND timestamp >= ? AND timestamp < ? AND instrument_id IN (" + marks + ")",
            [_iso(opens), _iso(closes)] + members)}
    events = [(_parse(a), k) for a, k in conn.execute(
        "SELECT at, kind FROM capture_events WHERE at >= ? AND at <= ?",
        (_iso(opens - timedelta(hours=1)), _iso(closes + MINUTE)))]
    host = [(_parse(json.loads(d).get("expected")), _parse(json.loads(d).get("woke")))
            for (d,) in conn.execute(
                "SELECT detail FROM capture_events WHERE session_id = ? AND "
                "kind = 'HOST_SUSPEND_GAP'", (session_id,))]
    out, start = [], None
    minutes = int((closes - opens).total_seconds() // 60)
    for i in range(minutes + 1):
        moment = opens + i * MINUTE
        missing = i < minutes and _iso(moment) not in present
        if missing and start is None:
            start = moment
        elif not missing and start is not None:
            end = moment
            ticks = conn.execute(
                "SELECT COUNT(*), SUM(health = 'failed') FROM capture_ticks WHERE "
                "session_id = ? AND tick_at >= ? AND tick_at < ?",
                (session_id, _iso(start + MINUTE), _iso(end + MINUTE))).fetchone()
            window = [k for at, k in events if start - MINUTE <= at < end + MINUTE]
            # Auth state AT the gap's start: a wait announced once at the
            # pre-open still holds at the open.
            auth_before = [k for at, k in events if at < start + MINUTE and k in
                           ("WAITING_FOR_AUTH", "AUTH_LOST", "AUTHENTICATED")]
            if any(a and b and a < end and b > start for a, b in host):
                cause = "HOST SLEEP"
            elif "AUTH_LOST" in window or (auth_before and auth_before[-1] != "AUTHENTICATED"):
                cause = "AUTH"
            elif "STARTED" in window or "LEASE_TAKEOVER" in window:
                cause = "PROCESS RESTART"
            elif ticks[0] and ticks[1] == ticks[0]:
                cause = "QUOTE FAILURE"
            elif ticks[0] == 0 and start == opens:
                cause = "NOT STARTED"
            else:
                cause = "UNKNOWN"
            out.append({"start": _iso(start), "end": _iso(end),
                        "minutes": int((end - start).total_seconds() // 60),
                        "instruments_affected": len(members), "cause": cause,
                        "recoverable": False})
            start = None
    return out


def feature_coverage(conn: sqlite3.Connection, session_id: str,
                     every_minutes: int = FEATURE_EVERY_MINUTES) -> Dict[str, Any]:
    opens, closes = [_parse(v) for v in conn.execute(
        "SELECT opens_at, closes_at FROM capture_sessions WHERE session_id = ?",
        (session_id,)).fetchone()]
    # The runner computes at most once per `every_minutes` from its first
    # tick, so cutoffs are not aligned to the open. Judge by 5-minute
    # windows instead: every window that had a tick after the open should
    # hold one computed cutoff.
    step = timedelta(minutes=every_minutes)

    def window(moment: datetime) -> int:
        return int((moment - opens) // step)

    boundaries = [_parse(r[0]).replace(second=0, microsecond=0) for r in conn.execute(
        "SELECT tick_at FROM capture_ticks WHERE session_id = ?", (session_id,))]
    # Eligible only once the session has a closed bar to compute from: a
    # cutoff before the first archived minute has nothing to describe.
    first_bar = conn.execute(
        "SELECT MIN(p.timestamp) FROM price_candle_cache p JOIN "
        "capture_session_members m ON m.instrument_id = p.instrument_id AND "
        "m.session_id = ? WHERE p.interval = '1m' AND p.timestamp >= ? AND "
        "p.timestamp < ?", (session_id, _iso(opens), _iso(closes))).fetchone()[0]
    first_eligible = (_parse(first_bar) + MINUTE) if first_bar else None
    # A window counts only if it STARTS once data exists: the runner spends
    # its cadence slot on an empty cutoff (e.g. the first tick after a late
    # login), so the window holding the first bar may legitimately have none.
    expected = sorted({window(b) for b in boundaries
                       if first_eligible and opens < b <= closes
                       and opens + window(b) * step >= first_eligible})
    computed = {_iso(_parse(r[0])) for r in conn.execute(
        "SELECT DISTINCT cutoff FROM intraday_feature_values WHERE session_id = ?",
        (session_id,))}
    computed_windows = {window(_parse(c)) for c in computed}
    failed = set()
    for (detail,) in conn.execute(
            "SELECT detail FROM capture_events WHERE session_id = ? AND "
            "kind = 'FEATURE_FAILED'", (session_id,)):
        try:
            failed.add(_iso(_parse(json.loads(detail)["cutoff"])))
        except (ValueError, KeyError, TypeError):
            pass
    missing = [_iso(opens + w * step) for w in expected if w not in computed_windows]
    values = conn.execute(
        "SELECT COUNT(*), COUNT(value), SUM(value IS NOT NULL AND "
        "(value != value OR value > 1e300 OR value < -1e300)) "
        "FROM intraday_feature_values WHERE session_id = ?", (session_id,)).fetchone()
    versions = [r[0] for r in conn.execute(
        "SELECT DISTINCT feature_version FROM intraday_feature_values "
        "WHERE session_id = ?", (session_id,))]
    return {"expected_cutoffs": len(expected), "computed_cutoffs": len(computed),
            "failed_cutoffs": len(failed),
            "recomputed_cutoffs": len(failed & computed),
            "missing_cutoffs": missing,
            "coverage": round(len(set(expected) & computed_windows) / len(expected), 4)
            if expected else None,
            "values": values[0], "non_null_values": values[1],
            "non_finite_values": values[2] or 0, "feature_versions": versions}


def archive_integrity(conn: sqlite3.Connection, session_id: str) -> Dict[str, Any]:
    opens, closes = [_iso(_parse(v)) for v in conn.execute(
        "SELECT opens_at, closes_at FROM capture_sessions WHERE session_id = ?",
        (session_id,)).fetchone()]
    research_dupes = conn.execute(
        "SELECT COUNT(*) FROM (SELECT instrument_id, datetime(timestamp) m, COUNT(*) c "
        "FROM price_candle_cache WHERE interval = '1m' AND datetime(timestamp) >= "
        "datetime(?) AND datetime(timestamp) < datetime(?) GROUP BY 1, 2 HAVING c > 1)",
        (opens, closes)).fetchone()[0]
    operational_dupes = conn.execute(
        "SELECT COUNT(*) FROM (SELECT instrument_id, datetime(bar_start) m, COUNT(*) c "
        "FROM market_data_bars WHERE datetime(bar_start) >= datetime(?) AND "
        "datetime(bar_start) < datetime(?) GROUP BY 1, 2 HAVING c > 1)",
        (opens, closes)).fetchone()[0]
    not_archivable = conn.execute(
        "SELECT COUNT(*) FROM price_candle_cache p JOIN market_data_bars b ON "
        "b.instrument_id = p.instrument_id AND b.bar_start = p.timestamp WHERE "
        "p.interval = '1m' AND p.timestamp >= ? AND p.timestamp < ? AND "
        "(b.is_gap = 1 OR b.is_complete = 0)", (opens, closes)).fetchone()[0]
    unarchived = conn.execute(
        "SELECT COUNT(*) FROM market_data_bars b JOIN capture_session_members m ON "
        "m.instrument_id = b.instrument_id AND m.session_id = ? WHERE "
        "b.bar_start >= ? AND b.bar_start < ? AND b.is_gap = 0 AND b.is_complete = 1 "
        "AND NOT EXISTS (SELECT 1 FROM price_candle_cache p WHERE p.interval = '1m' "
        "AND p.instrument_id = b.instrument_id AND p.timestamp = b.bar_start)",
        (session_id, opens, closes)).fetchone()[0]
    sources = dict(conn.execute(
        "SELECT source, COUNT(*) FROM price_candle_cache WHERE interval = '1m' AND "
        "timestamp >= ? AND timestamp < ? GROUP BY source", (opens, closes)).fetchall())
    unlogged = conn.execute(
        "SELECT COUNT(*) FROM price_candle_cache p WHERE p.interval = '1m' AND "
        "p.timestamp >= ? AND p.timestamp < ? AND p.source = ? AND NOT EXISTS ("
        "SELECT 1 FROM capture_archive_log a WHERE a.instrument_id = p.instrument_id "
        "AND a.bar_start = p.timestamp)", (opens, closes, LIVE_CAPTURE_SOURCE)).fetchone()[0]
    close_mismatch = conn.execute(
        "SELECT COUNT(*) FROM price_candle_cache p JOIN market_data_bars b ON "
        "b.instrument_id = p.instrument_id AND b.bar_start = p.timestamp WHERE "
        "p.interval = '1m' AND p.timestamp >= ? AND p.timestamp < ? AND "
        "(ABS(p.close - b.close) > 1e-9 OR ABS(p.open - b.open) > 1e-9 OR "
        "ABS(p.high - b.high) > 1e-9 OR ABS(p.low - b.low) > 1e-9)",
        (opens, closes)).fetchone()[0]
    return {"research_duplicates": research_dupes,
            "operational_duplicates": operational_dupes,
            "gap_or_incomplete_archived": not_archivable,
            "complete_bars_not_archived": unarchived,
            "sources": sources, "archived_without_provenance": unlogged,
            "ohlc_mismatches": close_mismatch}


def mapping_identity(conn: sqlite3.Connection, session_id: str) -> List[Dict[str, Any]]:
    """Every expected member with its persisted contract, and any mismatch."""
    out = []
    for instrument_id, ticker, status, conid, detail in conn.execute(
            "SELECT instrument_id, ticker, mapping_status, conid, detail FROM "
            "capture_session_members WHERE session_id = ? ORDER BY instrument_id",
            (session_id,)):
        row = conn.execute(
            "SELECT broker_symbol, venue, asset_class, currency, broker_payload_json "
            "FROM broker_instrument_mapping WHERE canonical_instrument_id = ?",
            (instrument_id,)).fetchone()
        entry = {"instrument_id": instrument_id, "ticker": ticker, "status": status,
                 "conid": conid, "symbol": None, "sec_type": None, "currency": None,
                 "exchange": None, "problems": []}
        if row:
            payload = {}
            try:
                payload = json.loads(row[4] or "{}")
            except ValueError:
                pass
            entry.update(symbol=row[0], currency=row[3],
                         exchange=payload.get("primary_exchange") or payload.get(
                             "listing_exchange") or row[1],
                         sec_type=payload.get("sec_type") or row[2])
            if status == "RESOLVED":
                if (row[0] or "").upper() != ticker.upper():
                    entry["problems"].append(f"symbol {row[0]} != ticker {ticker}")
                if (row[3] or "").upper() != "USD":
                    entry["problems"].append(f"currency {row[3]}")
                if entry["sec_type"] and str(entry["sec_type"]).upper() not in (
                        "STK", "STOCK", "ETF"):
                    entry["problems"].append(f"security type {entry['sec_type']}")
        elif status == "RESOLVED":
            entry["problems"].append("RESOLVED but no persisted mapping")
        out.append(entry)
    return out


def quote_evidence(conn: sqlite3.Connection, session_id: str) -> Dict[str, Any]:
    if not _has_column(conn, "capture_ticks", "realtime"):
        return {"available": False}
    t = conn.execute(
        "SELECT COUNT(*), SUM(realtime), SUM(delayed), SUM(unknown_availability), "
        "SUM(unavailable), MAX(venue_spread_seconds), AVG(venue_spread_seconds), "
        "AVG(venue_lag_seconds), MAX(venue_lag_seconds), MIN(venue_lag_seconds), "
        "MAX(requests_last_minute), AVG(duration_seconds), MAX(duration_seconds), "
        "SUM(duration_seconds > 60) FROM capture_ticks WHERE session_id = ?",
        (session_id,)).fetchone()
    samples = conn.execute(
        "SELECT COUNT(*), SUM(last IS NULL), SUM(bid IS NULL), SUM(ask IS NULL), "
        "SUM(broker_at IS NULL), SUM(bid > ask), SUM(broker_at > received_at) "
        "FROM capture_quote_samples WHERE session_id = ?", (session_id,)).fetchone()
    first = conn.execute(
        "SELECT tick_at, tradeable, requested FROM capture_ticks WHERE session_id = ? "
        "ORDER BY tick_at LIMIT 1", (session_id,)).fetchone()
    warmed = conn.execute(
        "SELECT COUNT(*) FROM capture_events WHERE session_id = ? AND kind = 'WARMED_UP'",
        (session_id,)).fetchone()[0]
    return {"available": True, "ticks": t[0], "realtime": t[1], "delayed": t[2],
            "unknown": t[3], "unavailable": t[4], "max_venue_spread_s": t[5],
            "avg_venue_spread_s": round(t[6], 3) if t[6] is not None else None,
            "avg_venue_lag_s": round(t[7], 3) if t[7] is not None else None,
            "max_venue_lag_s": t[8], "min_venue_lag_s": t[9],
            "max_requests_last_minute": t[10],
            "avg_tick_seconds": round(t[11], 3) if t[11] is not None else None,
            "max_tick_seconds": t[12], "tick_overruns": t[13] or 0,
            "samples": samples[0], "sample_missing_last": samples[1],
            "sample_missing_bid": samples[2], "sample_missing_ask": samples[3],
            "sample_missing_venue_time": samples[4], "sample_crossed": samples[5],
            "sample_future_venue_time": samples[6],
            "warm_up": bool(warmed),
            "first_tick": {"at": first[0], "tradeable": first[1],
                           "requested": first[2]} if first else None}


def write_scope(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Tables holding rows that capture is not allowed to write."""
    unexpected = {}
    for table in _tables(conn):
        if table in CAPTURE_WRITABLE:
            continue
        count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        if count:
            unexpected[table] = count
    execution = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                 for t in EXECUTION_TABLES if t in _tables(conn)}
    return {"unexpected_tables_with_rows": unexpected, "execution_domain_rows": execution}


# ======================================================================
# The acceptance record
# ======================================================================

def acceptance(conn: sqlite3.Connection, session_id: Optional[str] = None,
               budget_per_minute: int = 50) -> Dict[str, Any]:
    sessions = [r[0] for r in conn.execute(
        "SELECT session_id FROM capture_sessions ORDER BY session_date")]
    evidence = {sid: session_evidence(conn, sid) for sid in sessions}
    real = [s for s in sessions if evidence[s] == "REAL"]
    report: Dict[str, Any] = {"sessions_in_store": evidence,
                              "real_sessions": real, "checks": []}
    checks = report["checks"]

    def check(name: str, status: str, detail: Any) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    mock = [s for s, e in evidence.items() if e == "MOCK"]
    check("no mock session in the capture store", FAIL if mock else PASS, mock)
    scope = write_scope(conn)
    bad_scope = scope["unexpected_tables_with_rows"] or any(
        scope["execution_domain_rows"].values())
    check("write scope", FAIL if bad_scope else PASS, scope)
    attempts = conn.execute(
        "SELECT COALESCE(MAX(broker_write_attempts), 0) FROM capture_instances"
    ).fetchone()[0] + conn.execute(
        "SELECT COUNT(*) FROM capture_events WHERE kind = 'BROKER_WRITE_REFUSED'"
    ).fetchone()[0]
    check("broker write attempts = 0", FAIL if attempts else PASS, attempts)
    report["broker_write_attempts"] = attempts

    target = session_id or (real[-1] if real else (sessions[-1] if sessions else None))
    if target is None:
        report["verdict"] = "NO REAL SESSION CAPTURED"
        report["session"] = None
        return report
    ev = evidence.get(target, "NONE")
    row = conn.execute(
        "SELECT session_date, session_type, opens_at, closes_at, status, quality "
        "FROM capture_sessions WHERE session_id = ?", (target,)).fetchone()
    cov = session_coverage(conn, target)
    instances = [r[0] for r in conn.execute(
        "SELECT DISTINCT instance_id FROM capture_ticks WHERE session_id = ?", (target,))]
    kinds = dict(conn.execute(
        "SELECT kind, COUNT(*) FROM capture_events WHERE at >= ? AND at <= ? "
        "GROUP BY kind", (_iso(_parse(row[2]) - timedelta(minutes=30)),
                          _iso(_parse(row[3]) + timedelta(minutes=15)))).fetchall())
    features = feature_coverage(conn, target)
    archive = archive_integrity(conn, target)
    mapping = mapping_identity(conn, target)
    quotes = quote_evidence(conn, target)
    session_gaps = gaps(conn, target)
    expected_instrument_minutes = cov["expected_minutes"] * cov["members"]
    resolved_instrument_minutes = cov["expected_minutes"] * cov["resolved"]
    record = {
        "session_id": target, "evidence": ev, "session_date": row[0],
        "session_type": row[1], "opens_at": row[2], "closes_at": row[3],
        "status": row[4], "quality": row[5],
        "start_time": cov["observed_from"], "end_time": cov["observed_until"],
        "process_instances": len(instances),
        "runner_takeovers": kinds.get("LEASE_TAKEOVER", 0),
        "auth_waits": kinds.get("WAITING_FOR_AUTH", 0),
        "auth_outages": kinds.get("AUTH_LOST", 0),
        "reauthentications": max(0, kinds.get("AUTHENTICATED", 0) - 1),
        "expected_universe": cov["members"], "resolved_universe": cov["resolved"],
        "unresolved": cov["unresolved"],
        "quote_cycles": cov["quote_cycles"],
        "expected_minutes": cov["expected_minutes"],
        "captured_minutes": cov["window_minutes"],
        "coverage_of_session_minutes": round(cov["window_minutes"] / cov["expected_minutes"], 4)
        if cov["expected_minutes"] else None,
        "expected_instrument_minutes": expected_instrument_minutes,
        "resolved_instrument_minutes": resolved_instrument_minutes,
        "observed_instrument_minutes": cov["bars_archived"],
        "coverage_of_resolved_instrument_minutes":
            round(cov["bars_archived"] / resolved_instrument_minutes, 4)
            if resolved_instrument_minutes else None,
        "largest_gap_minutes": cov["largest_gap_minutes"],
        "archived_bars": cov["bars_archived"], "feature_rows": cov["feature_rows"],
        "cross_sectional_minutes_80pct": cov["cross_sectional_minutes"],
        "minutes_ge": {k: cov[f"minutes_ge_{k}"] for k in (1, 2, 3, 5)},
        "median_simultaneous": cov["median_simultaneous"],
        "max_simultaneous": cov["max_simultaneous"],
        "dispersion_1m_coverage": cov["dispersion_1m_coverage"],
        "reconnects": cov["reconnects"], "host_suspend_gaps": cov["host_suspend_gaps"],
        "archive_failures": cov["archive_failures"],
        "feature_failures": cov["feature_failures"],
        "broker_write_attempts": attempts,
    }
    report.update(session=record, features=features, archive=archive,
                  mapping=mapping, quotes=quotes, gaps=session_gaps)

    check("session evidence is REAL", PASS if ev == "REAL" else FAIL, ev)
    check("session finalized", PASS if row[4] == "finalized" else WARN, row[4])
    bad_archive = (archive["research_duplicates"] or archive["operational_duplicates"]
                   or archive["gap_or_incomplete_archived"]
                   or archive["complete_bars_not_archived"]
                   or archive["archived_without_provenance"] or archive["ohlc_mismatches"]
                   or set(archive["sources"]) - {LIVE_CAPTURE_SOURCE})
    check("archive integrity", FAIL if bad_archive else PASS, archive)
    check("features at every due cutoff",
          FAIL if features["missing_cutoffs"] or features["non_finite_values"] else PASS,
          {k: features[k] for k in ("expected_cutoffs", "computed_cutoffs",
                                    "failed_cutoffs", "missing_cutoffs",
                                    "non_finite_values")})
    wrong = [m for m in mapping if m["problems"]]
    check("contract identity", FAIL if wrong else PASS, wrong)
    check("universe fully mapped", WARN if cov["unresolved"] else PASS, cov["unresolved"])
    check("no large gap", WARN if cov["largest_gap_minutes"] > LARGE_GAP_MINUTES else PASS,
          cov["largest_gap_minutes"])
    if quotes.get("available"):
        future = quotes["sample_future_venue_time"] or 0
        check("no venue timestamp in the future", FAIL if future else PASS, future)
        check("realtime data", WARN if (quotes["delayed"] or quotes["unknown"]) else PASS,
              {k: quotes[k] for k in ("realtime", "delayed", "unknown", "unavailable")})
        over = (quotes["max_requests_last_minute"] or 0) > budget_per_minute
        check("request budget", FAIL if over else PASS, quotes["max_requests_last_minute"])
        check("no tick overran 60 s", WARN if quotes["tick_overruns"] else PASS,
              quotes["tick_overruns"])

    failed = any(c["status"] == FAIL for c in checks)
    full = False
    if ev == "REAL" and row[4] == "finalized" and cov["observed_from"]:
        started = _parse(cov["observed_from"]) - _parse(row[2]) <= BOUNDARY_TOLERANCE
        ended = _parse(row[3]) - _parse(cov["observed_until"]) <= BOUNDARY_TOLERANCE
        full = started and ended and row[5] == "GOOD"
    if ev != "REAL" or not cov["bars_archived"]:
        verdict = "NO REAL SESSION CAPTURED"
    elif row[4] != "finalized":
        # Not one of the four verdicts on purpose: a session still being
        # captured has no verdict yet.
        verdict = "NO VERDICT YET: SESSION NOT FINALIZED"
    elif failed or row[5] in ("DEGRADED", "FAILED"):
        verdict = "REAL DATA CAPTURED BUT SESSION INVALID"
    elif full:
        verdict = "FULL SESSION VERIFIED"
    elif row[4] == "finalized" and row[5] in ("GOOD", "PARTIAL"):
        verdict = "PARTIAL REAL SESSION VERIFIED"
    else:
        verdict = "REAL DATA CAPTURED BUT SESSION INVALID"
    report["verdict"] = verdict
    return report
