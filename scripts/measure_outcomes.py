#!/usr/bin/env python3
"""
scripts/measure_outcomes.py
---------------------------------
Measure what happened after every signal and prediction (Phase 19).

WHERE THIS BELONGS IN THE PIPELINE (§57)
--------------------------------------------
LAST, and it must stay last:

    ... compute features -> train -> predict -> generate signals
        -> [ measure outcomes ]

Outcomes are the only stage allowed to read prices dated after an
information cutoff, because "what happened next" is the question. Put
it anywhere earlier and a feature computed after it could see the
answer. The ordering is the leakage control, and
`tests/outcomes/test_leakage.py` asserts that the workflow keeps it.

It writes to `outcome_measurements` and `outcome_aggregates` and to
nothing else. No model is retrained, promoted or modified by anything
in this file (§51).

WHAT IT MEASURES AND WHAT IT REFUSES TO
-------------------------------------------
Measured: forward return (simple and log), MFE, MAE, time to each,
direction versus claim, over every horizon in the ladder.

Refused: profit, P&L, position value. There are no orders and no
positions in this database — the Phase 14 execution tables do not exist
in production — and a "return" that quietly assumed equal position
sizing would be a portfolio result wearing a signal's clothes.

    python scripts/measure_outcomes.py --apply
    python scripts/measure_outcomes.py --apply --predictions
    python scripts/measure_outcomes.py --horizons 1d,5d,20d --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.data_access.outcome_schema import initialize_outcome_schema
from src.domain.outcome_models import (
    DEFAULT_HORIZONS, OUTCOME_METHOD_VERSION, SubjectKind,
)
from src.outcomes.analytics import (
    MIN_SAMPLE, build_cohorts, cohort_warning, decay_curve, load_measurements,
    save_aggregates,
)
from src.outcomes.pipeline import run, save

DEFAULT_DB = os.path.join("data", "marketlens.db")


def line(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 68 - len(title)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--horizons", default=",".join(DEFAULT_HORIZONS),
                        help="Comma-separated, e.g. '1h,1d,5d'. Units: "
                             "m, h, d. A daily horizon counts TRADING bars.")
    parser.add_argument("--predictions", action="store_true",
                        help="Also measure prediction outcomes. A prediction "
                             "is a number; a signal is a claim. They are "
                             "measured separately and never pooled.")
    parser.add_argument("--signals-only", action="store_true",
                        help="Measure signals and skip predictions (default).")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--since", default=None, metavar="ISO_DATE",
                        help="Only subjects with a cutoff at or after this.")
    parser.add_argument("--rescore", action="store_true",
                        help="Re-measure rows that already hold a settled "
                             "result. PENDING rows are always revisited; this "
                             "flag additionally rewrites AVAILABLE ones, which "
                             "is a data correction and not routine.")
    parser.add_argument("--method-version", default=OUTCOME_METHOD_VERSION,
                        help="Bump to record a methodology change as NEW rows "
                             "beside the old ones rather than over them.")
    parser.add_argument("--no-aggregates", action="store_true",
                        help="Skip the aggregation pass.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Without this it is a dry run.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"No database at {args.db}.")
        return 1

    kinds = [SubjectKind.SIGNAL]
    if args.predictions and not args.signals_only:
        kinds.append(SubjectKind.PREDICTION)

    conn = sqlite3.connect(args.db)
    initialize_outcome_schema(conn)

    print("=" * 72)
    print("MarketLens - outcome intelligence")
    print("Measures what happened AFTER a signal. Never modifies a model,")
    print("a prediction, a feature or a signal.")
    if not args.apply:
        print("MODE: DRY RUN - nothing is written")
    print("=" * 72)

    started = time.time()
    outcomes, report = run(
        conn,
        horizons=[h.strip() for h in args.horizons.split(",") if h.strip()],
        subject_kinds=tuple(kinds), limit=args.limit, since=args.since,
        rescore=args.rescore, method_version=args.method_version)
    elapsed = time.time() - started

    line("SUBJECTS")
    print(f"  measured as               {', '.join(k.value for k in kinds)}")
    print(f"  subjects considered       {report.subjects:,}")
    print(f"  horizons                  {', '.join(report.horizons)}")
    print(f"  methodology version       {report.method_version}")
    print(f"  price data as of          "
          f"{report.data_as_of.isoformat()[:19] if report.data_as_of else 'unknown'}")

    line("MEASUREMENTS")
    total = report.measured + report.pending + report.insufficient + report.invalid
    print(f"  available                 {report.measured:,}")
    print(f"  pending (window open)     {report.pending:,}")
    print(f"  insufficient data         {report.insufficient:,}")
    print(f"  invalid (flagged)         {report.invalid:,}")
    print(f"  already settled, skipped  {report.skipped_existing:,}")
    print(f"  no claim to score         {report.skipped_no_direction:,}")
    print(f"  total rows                {total:,}   ({elapsed:.1f}s)")

    if report.insufficient:
        print()
        print("  INSUFFICIENT_DATA is never a zero return and never a miss.")
        print("  It means the price data cannot answer the question, which is")
        print("  a different fact from the signal having been wrong.")

    if not args.apply:
        line("DRY RUN")
        print(f"  {total:,} measurement(s) would be written.")
        print("  Add --apply to write them.")
        conn.close()
        return 0

    written = save(conn, outcomes)
    line("WRITTEN")
    print(f"  {written:,} measurement(s)")
    print("  Identity is (subject_kind, subject_id, horizon, method_version),")
    print("  so a second run over the same subjects replaces rather than adds.")

    if not args.no_aggregates:
        started = time.time()
        rows = load_measurements(conn, args.method_version)
        cohorts = build_cohorts(rows, args.method_version)
        count = save_aggregates(conn, cohorts, args.method_version)
        line("AGGREGATES")
        print(f"  {count:,} cohort(s) over {len(rows):,} measurement(s)"
              f"   ({time.time() - started:.1f}s)")
        usable = sum(1 for c in cohorts if not c.small_sample)
        print(f"  {usable:,} cohort(s) reach the {MIN_SAMPLE}-observation "
              f"threshold; the rest are descriptive only")

        curve = decay_curve(conn, method_version=args.method_version)
        if curve:
            line("SIGNAL DECAY (overall)")
            print("  %-6s %7s %10s %10s %9s %9s %9s"
                  % ("horizon", "n", "mean", "median", "dir.acc", "MFE", "MAE"))
            for point in curve:
                def fmt(value, places=4):
                    return f"{value:+.{places}f}" if value is not None else "     —"
                print("  %-6s %7d %10s %10s %9s %9s %9s%s" % (
                    point["horizon"], point["sample_size"],
                    fmt(point["mean_return"]), fmt(point["median_return"]),
                    fmt(point["directional_accuracy"], 3),
                    fmt(point["mean_mfe"]), fmt(point["mean_mae"]),
                    "  small" if point["small_sample"] else ""))

        print()
        for chunk in _wrap(cohort_warning(count), 70):
            print(f"  {chunk}")

    conn.close()
    return 0


def _wrap(text: str, width: int):
    words, line_out = text.split(), ""
    for word in words:
        if len(line_out) + len(word) + 1 > width:
            yield line_out
            line_out = word
        else:
            line_out = f"{line_out} {word}".strip()
    if line_out:
        yield line_out


if __name__ == "__main__":
    raise SystemExit(main())
