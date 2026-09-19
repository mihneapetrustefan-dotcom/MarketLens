#!/usr/bin/env python3
"""
scripts/capture_report.py
-----------------------------------------------------------
Phase 25.9G — what has been captured, session by session.

    python scripts/capture_report.py [--db ...] [--sessions 20] [--json]

READ-ONLY. Sessions with their quality and cross-sectional coverage,
the universe's contract mapping, and data maturity counted in sessions
and calendar months -- never in rows. Milestones are planning targets:
reaching one is not evidence that any model is justified.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.capture import quality  # noqa: E402

DEFAULT_DB = os.path.join(ROOT, "data", "capture", "intraday_capture.db")


def build(conn: sqlite3.Connection, limit: int) -> dict:
    conn.row_factory = sqlite3.Row
    sessions = []
    for row in conn.execute(
            "SELECT * FROM capture_sessions ORDER BY session_date DESC LIMIT ?",
            (limit,)):
        summary = json.loads(row["summary_json"] or "{}")
        sessions.append({
            "session_id": row["session_id"], "date": row["session_date"],
            "type": row["session_type"], "status": row["status"],
            "quality": row["quality"], "universe": row["universe_version"],
            "expected_minutes": summary.get("expected_minutes"),
            "cross_sectional_minutes": summary.get("cross_sectional_minutes"),
            "bars_archived": summary.get("bars_archived"),
            "resolved": summary.get("resolved"), "members": summary.get("members"),
            "host_suspend_gaps": summary.get("host_suspend_gaps"),
            "processes": summary.get("processes"),
            "captured_instruments": summary.get("captured_instruments"),
            "captured_minutes": summary.get("window_minutes"),
            "largest_gap_minutes": summary.get("largest_gap_minutes"),
            "minutes_ge_1": summary.get("minutes_ge_1"),
            "minutes_ge_2": summary.get("minutes_ge_2"),
            "minutes_ge_3": summary.get("minutes_ge_3"),
            "minutes_ge_5": summary.get("minutes_ge_5"),
            "median_simultaneous": summary.get("median_simultaneous"),
            "max_simultaneous": summary.get("max_simultaneous"),
            "dispersion_1m_coverage": summary.get("dispersion_1m_coverage"),
            "feature_rows": summary.get("feature_rows"),
            "errors": summary.get("errors"),
            "reconnects": summary.get("reconnects"),
            "order_write_attempts": summary.get("order_write_attempts")})
    mappings = [dict(r) for r in conn.execute(
        "SELECT instrument_id, status, attempts, next_retry_at, detail "
        "FROM capture_mappings ORDER BY status, instrument_id")]
    features = conn.execute(
        "SELECT feature_version, COUNT(*), COUNT(DISTINCT cutoff) "
        "FROM intraday_feature_values GROUP BY feature_version").fetchall()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sessions": sessions,
        "mappings": mappings,
        "features": [{"version": f[0], "values": f[1], "cutoffs": f[2]}
                     for f in features],
        "maturity": quality.maturity(conn, datetime.now(timezone.utc).date()),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--trace", nargs=2, metavar=("INSTRUMENT", "BAR_START"),
                        help="provenance of one archived minute")
    args = parser.parse_args(argv)
    if not os.path.exists(args.db):
        print("No capture store at %s: capture has never run." % args.db)
        return 2
    uri = "file:%s?mode=ro" % os.path.abspath(args.db).replace("\\", "/")
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        if args.trace:
            print(json.dumps(quality.trace(conn, *args.trace), indent=2, default=str))
            return 0
        report = build(conn, args.sessions)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0

    print("CAPTURED SESSIONS (newest first)")
    print("  %-11s %-11s %-9s %7s %9s %9s %7s %6s %4s %4s %4s %4s %7s" % (
        "date", "quality", "instr", "minutes", "x-sect80", "bars", "maxgap",
        ">=1", ">=2", ">=3", ">=5", "med", "disp%"))
    for s in report["sessions"]:
        disp = s["dispersion_1m_coverage"]
        print("  %-11s %-11s %4s/%-4s %3s/%-3s %9s %9s %7s %6s %4s %4s %4s %4s %7s" % (
            s["date"], s["quality"] or s["status"], s["captured_instruments"],
            s["members"], s["captured_minutes"], s["expected_minutes"],
            s["cross_sectional_minutes"], s["bars_archived"],
            s["largest_gap_minutes"], s["minutes_ge_1"], s["minutes_ge_2"],
            s["minutes_ge_3"], s["minutes_ge_5"], s["median_simultaneous"],
            "-" if disp is None else "%.1f" % (100 * disp)))
    print("\nCONTRACT MAPPING")
    for m in report["mappings"]:
        if m["status"] != "RESOLVED":
            print("  %-26s %-11s attempts %s  %s" % (
                m["instrument_id"], m["status"], m["attempts"], m["detail"][:80]))
    resolved = sum(1 for m in report["mappings"] if m["status"] == "RESOLVED")
    print("  %d resolved of %d" % (resolved, len(report["mappings"])))
    m = report["maturity"]
    print("\nDATA MATURITY (sessions, not rows)")
    print("  first session %s | last session %s | span %s day(s)"
          % (m["first_session"], m["last_session"], m["calendar_span_days"]))
    print("  finalized %d: full %d, partial %d, degraded %d, failed %d, "
          "early-close %d" % (m["finalized_sessions"], m["full_sessions"],
                              m["partial_sessions"], m["by_quality"]["DEGRADED"],
                              m["failed_sessions"], m["early_close_sessions"]))
    print("  band %s; %d qualifying session(s) across %d month(s); ceiling %s (%s)"
          % (m["band"], m["qualifying_sessions"], m["calendar_months"],
             m["calendar_ceiling"], m["ceiling_reason"]))
    if m["next_milestone"]:
        print("  next milestone %d: %d more qualifying session(s), no earlier than %s"
              % (m["next_milestone"], m["sessions_to_next_milestone"],
                 m["earliest_date_for_next_milestone"]))
    print("  (%s)" % m["milestones_are"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
