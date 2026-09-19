#!/usr/bin/env python3
"""
scripts/capture_status.py
-----------------------------------------------------------
Phase 25.9G — is capture running, and is what it captures usable?

    python scripts/capture_status.py [--json]

READ-ONLY: the store is opened `mode=ro`, and nothing is contacted --
not IBKR, not the network. It reports what the runner and supervisor
last wrote, judged against the current clock.

EXIT CODES (for a scheduled check or a notification rule)
    0  OK              capturing, waiting correctly for the market, or idle
    1  ATTENTION       waiting for a human to log in to IBKR, the last
                       session was DEGRADED/FAILED, or members are unmapped
    2  NOT RUNNING     no heartbeat within the expected interval
    3  MANUAL          the supervisor stopped and needs a human
    4  SAFETY          a venue write was attempted and refused
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.capture import quality  # noqa: E402

CAPTURE_DIR = os.path.join(ROOT, "data", "capture")
DEFAULT_DB = os.path.join(CAPTURE_DIR, "intraday_capture.db")
STATE_FILE = os.path.join(CAPTURE_DIR, "supervisor.json")
STOP_FILE = os.path.join(CAPTURE_DIR, "STOP")

#: The runner heartbeats at least every idle poll (300s); twice that
#: plus slack before it counts as gone.
HEARTBEAT_STALE_SECONDS = 720.0

#: Below this, status asks for attention (measured: ~21 MB per session
#: for the 31-member v1 universe, so 2 GB is roughly three months).
DISK_FREE_FLOOR = 2 * 1024 ** 3

OK, ATTENTION, NOT_RUNNING, MANUAL, SAFETY = 0, 1, 2, 3, 4
VERDICT = {OK: "OK", ATTENTION: "ATTENTION", NOT_RUNNING: "NOT RUNNING",
           MANUAL: "MANUAL ATTENTION", SAFETY: "SAFETY"}


def _age(raw, now):
    if not raw:
        return None
    return round((now - datetime.fromisoformat(raw)).total_seconds(), 1)


def collect(db_path: str, state_path: str, now: datetime) -> dict:
    report = {"evaluated_at": now.isoformat(), "store": db_path,
              "stop_file": os.path.exists(STOP_FILE), "reasons": []}
    try:
        with open(state_path, encoding="utf-8") as handle:
            report["supervisor"] = json.load(handle)
        report["supervisor"]["age_seconds"] = _age(
            report["supervisor"].get("updated_at"), now)
    except (OSError, ValueError):
        report["supervisor"] = None

    if not os.path.exists(db_path):
        report["reasons"].append("capture store does not exist yet")
        report["instance"] = None
        return report
    import shutil
    report["store_bytes"] = sum(os.path.getsize(db_path + suffix)
                                for suffix in ("", "-wal", "-shm")
                                if os.path.exists(db_path + suffix))
    report["disk_free_bytes"] = shutil.disk_usage(os.path.dirname(
        os.path.abspath(db_path))).free
    uri = "file:%s?mode=ro" % os.path.abspath(db_path).replace("\\", "/")
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM capture_instances ORDER BY heartbeat_at DESC LIMIT 1"
        ).fetchone()
        report["instance"] = dict(row) if row else None
        if row:
            report["instance"]["heartbeat_age_seconds"] = _age(row["heartbeat_at"], now)
        lease = conn.execute(
            "SELECT owner, expires_at, released_at FROM session_runner_leases "
            "WHERE scope = 'capture:ibkr'").fetchone()
        report["lease"] = dict(lease) if lease else None
        sessions = conn.execute(
            "SELECT session_id, status, quality, summary_json FROM capture_sessions "
            "ORDER BY session_date DESC LIMIT 2").fetchall()
        report["sessions"] = []
        for s in sessions:
            summary = json.loads(s["summary_json"] or "{}")
            ticks = conn.execute(
                "SELECT COUNT(*), MAX(tick_at) FROM capture_ticks WHERE session_id = ?",
                (s["session_id"],)).fetchone()
            report["sessions"].append({
                "session_id": s["session_id"], "status": s["status"],
                "quality": s["quality"], "ticks": ticks[0], "last_tick": ticks[1],
                "cross_sectional_minutes": summary.get("cross_sectional_minutes"),
                "expected_minutes": summary.get("expected_minutes")})
        report["mappings"] = {r[0]: r[1] for r in conn.execute(
            "SELECT status, COUNT(*) FROM capture_mappings GROUP BY status")}
        tick = conn.execute(
            "SELECT session_id, tick_at, requested, tradeable, health "
            "FROM capture_ticks ORDER BY tick_at DESC LIMIT 1").fetchone()
        report["last_tick"] = dict(tick) if tick else None
        if report["sessions"] and report["sessions"][0]["status"] != "finalized":
            live = report["sessions"][0]
            from src.capture.quality import session_coverage
            cov = session_coverage(conn, live["session_id"])
            live["cross_sectional_minutes"] = cov["cross_sectional_minutes"]
            live["captured_minutes"] = cov["window_minutes"]
            live["captured_instruments"] = cov["captured_instruments"]
            live["minutes_ge_3"] = cov["minutes_ge_3"]
        report["maturity"] = quality.maturity(conn, now.date())
        report["archived_minutes"] = conn.execute(
            "SELECT COUNT(*) FROM price_candle_cache WHERE interval = '1m'").fetchone()[0]
    finally:
        conn.close()
    return report


def condition(report: dict) -> str:
    """
    Section 97: name the state rather than calling everything FAILED.

        HEALTHY_CAPTURE   active, every resolved member tradeable last tick
        DEGRADED_CAPTURE  active, some members not tradeable last tick
        PARTIAL_CAPTURE   active, but part of the universe is unmapped
        WAITING_FOR_AUTH  a human must log in to the gateway
        MARKET_CLOSED     idle, waiting for the market, or after the close
        SYSTEM_ERROR      not running, stopped for a human, or unsafe
    """
    inst = report.get("instance") or {}
    state = inst.get("state")
    if report.get("verdict_code") == NOT_RUNNING and (
            report.get("stop_file")
            or (report.get("supervisor") or {}).get("state") == "STOPPED"):
        return "STOPPED_BY_OPERATOR"
    if report.get("verdict_code") in (NOT_RUNNING, MANUAL, SAFETY):
        return "SYSTEM_ERROR"
    if state == "WAITING_FOR_AUTH":
        return "WAITING_FOR_AUTH"
    if state != "ACTIVE_SESSION":
        return "MARKET_CLOSED"
    unmapped = sum(v for k, v in (report.get("mappings") or {}).items()
                   if k != "RESOLVED")
    if unmapped:
        return "PARTIAL_CAPTURE"
    last = (report.get("last_tick") or {}).get("health")
    return "HEALTHY_CAPTURE" if last == "healthy" else "DEGRADED_CAPTURE"


def verdict(report: dict) -> int:
    reasons = report["reasons"]
    sup = report.get("supervisor") or {}
    inst = report.get("instance") or {}
    if inst.get("broker_write_attempts"):
        reasons.append("venue write attempts recorded: %s" % inst["broker_write_attempts"])
        return SAFETY
    if sup.get("state") == "MANUAL_ATTENTION":
        reasons.append("supervisor: %s" % sup.get("reason"))
        return MANUAL
    if report.get("stop_file"):
        reasons.append("STOP file present: capture is stopped on purpose")
        return NOT_RUNNING
    age = inst.get("heartbeat_age_seconds")
    if not inst or age is None or age > HEARTBEAT_STALE_SECONDS or \
            inst.get("state") == "STOPPED":
        reasons.append("no live capture heartbeat (last %s s ago, state %s)"
                       % (age, inst.get("state")))
        return NOT_RUNNING
    code = OK
    if inst.get("state") == "WAITING_FOR_AUTH":
        reasons.append("IBKR gateway is not logged in: log in to the Client "
                       "Portal Gateway in a browser; capture resumes by itself")
        code = ATTENTION
    finalized = [s for s in report.get("sessions", []) if s["status"] == "finalized"]
    if finalized and finalized[0]["quality"] in ("DEGRADED", "FAILED"):
        reasons.append("last session %s was %s" % (finalized[0]["session_id"],
                                                    finalized[0]["quality"]))
        code = ATTENTION
    unmapped = sum(v for k, v in (report.get("mappings") or {}).items()
                   if k != "RESOLVED")
    if unmapped:
        reasons.append("%d universe member(s) not mapped" % unmapped)
        code = ATTENTION
    free = report.get("disk_free_bytes")
    if free is not None and free < DISK_FREE_FLOOR:
        # ~21 MB per 30-instrument session: 2 GB is about three months.
        reasons.append("only %.1f GB free on the capture disk" % (free / 1e9))
        code = ATTENTION
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--state-file", default=STATE_FILE)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    report = collect(args.db, args.state_file, now)
    code = verdict(report)
    report["verdict"] = VERDICT[code]
    report["verdict_code"] = code
    report["exit_code"] = code
    report["capture_condition"] = condition(report)
    from datetime import timedelta
    from src.marketdata.calendar import NEW_YORK, USEquityCalendar
    calendar = USEquityCalendar()
    report["market"] = calendar.status(now).value
    day = now.astimezone(NEW_YORK).date()
    for _ in range(15):
        window = calendar.session(day)
        if window.is_trading_day and window.closes_at > now:
            report["next_session_opens"] = window.opens_at.isoformat()
            break
        day += timedelta(days=1)
    # Section 67: data readiness and order authority are different facts.
    # Capture is structurally unable to order, so the second is constant.
    report["data_capture_ready"] = code in (OK, ATTENTION) and \
        report["capture_condition"] != "WAITING_FOR_AUTH"
    report["order_authorized"] = False
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return code

    inst = report.get("instance") or {}
    sup = report.get("supervisor") or {}
    print("CAPTURE STATUS: %s (exit %d) -- %s" % (VERDICT[code], code,
                                                  report["capture_condition"]))
    print("  market     : %s, next session opens %s (IBKR auth is checked from "
          "20 min before) | DATA_CAPTURE_READY=%s | ORDER_AUTHORIZED=NO"
          % (report["market"], report.get("next_session_opens"),
             "YES" if report["data_capture_ready"] else "NO"))
    lease = report.get("lease") or {}
    print("  lease      : %s (expires %s, released %s)"
          % (lease.get("owner"), lease.get("expires_at"), lease.get("released_at")))
    print("  instance   : %s pid %s started %s"
          % (inst.get("instance_id"), inst.get("pid"), inst.get("started_at")))
    print("  supervisor : %s (pid %s, restarts %s, updated %ss ago)"
          % (sup.get("state", "none"), sup.get("pid"), sup.get("restarts"),
             sup.get("age_seconds")))
    print("  runner     : %s, auth %s, heartbeat %ss ago"
          % (inst.get("state", "none"), inst.get("auth_state"),
             inst.get("heartbeat_age_seconds")))
    print("  last quote : %s | bar %s | archive %s | features %s"
          % (inst.get("last_quote_at"), inst.get("last_bar_at"),
             inst.get("last_archive_at"), inst.get("last_feature_at")))
    for s in report.get("sessions", []):
        print("  session    : %s %s %s, %s ticks, %s/%s cross-sectional minutes"
              % (s["session_id"], s["status"], s["quality"] or "-", s["ticks"],
                 s["cross_sectional_minutes"], s["expected_minutes"]))
    if report.get("mappings"):
        print("  mappings   : %s" % report["mappings"])
    if report.get("store_bytes") is not None:
        print("  storage    : store %.1f MB, %.1f GB free"
              % (report["store_bytes"] / 1e6, report["disk_free_bytes"] / 1e9))
    print("  last error : %s" % (inst.get("last_error") or "-"))
    print("  broker writes attempted: %s" % (inst.get("broker_write_attempts") or 0))
    m = report.get("maturity")
    if m:
        print("  maturity   : %s -- %d qualifying session(s) over %d month(s); "
              "next milestone %s" % (m["band"], m["qualifying_sessions"],
                                     m["calendar_months"], m["next_milestone"]))
    for reason in report["reasons"]:
        print("  ! %s" % reason)
    return code


if __name__ == "__main__":
    sys.exit(main())
